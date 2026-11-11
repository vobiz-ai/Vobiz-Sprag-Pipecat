"""A Pipecat voice agent that answers Vobiz calls with Sprag's models.

Rhythm transcribes the caller, Symphony writes the reply, and Chorus speaks it.
Vobiz requests /answer when a call connects, and the returned XML opens a
bidirectional audio stream to /ws/<token>.
"""

import os
import secrets

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.workers.runner import WorkerRunner
from pipecat_sprag.llm import SpragLLMService
from pipecat_sprag.stt import SpragSTTService
from pipecat_sprag.tts import SpragTTSService
from pipecat_vobiz import VobizFrameSerializer, parse_vobiz_start

load_dotenv(override=True)

app = FastAPI()

# Vobiz can't send auth headers when it opens the stream, so the URL carries a
# secret that only this server's answer XML reveals.
STREAM_TOKEN = os.getenv("STREAM_TOKEN") or secrets.token_urlsafe(32)

# The Stream format Vobiz sends: audio/x-mulaw at 8000 Hz, or audio/x-l16 at 8000 or 16000 Hz.
STREAM_CONTENT_TYPE = os.getenv("VOBIZ_CONTENT_TYPE", "audio/x-mulaw;rate=8000")

SYSTEM_INSTRUCTION = (
    "You are a helpful assistant on a phone call. Your replies are spoken aloud, "
    "so keep them to one or two short sentences, with no lists or formatting."
)


@app.api_route("/answer", methods=["GET", "POST"])
async def answer(request: Request) -> Response:
    """Tell Vobiz to stream the call's audio to this server."""
    host = request.headers["host"]
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Stream bidirectional="true" keepCallAlive="true" contentType="{STREAM_CONTENT_TYPE}">wss://{host}/ws/{STREAM_TOKEN}</Stream>
</Response>"""
    return Response(content=xml, media_type="application/xml")


@app.websocket("/ws/{token}")
async def media_stream(websocket: WebSocket, token: str) -> None:
    """Run one agent for the duration of one call."""
    if not secrets.compare_digest(token, STREAM_TOKEN):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    try:
        start = await parse_vobiz_start(websocket)
    except WebSocketDisconnect:
        return
    except ValueError as e:
        logger.error(e)
        await websocket.close(code=1002)
        return

    logger.info(f"Call {start['call_id']}: {start['encoding']} at {start['sample_rate']} Hz")
    await run_bot(websocket, start)


async def run_bot(websocket: WebSocket, start: dict) -> None:
    """Connect a Sprag pipeline to the Vobiz stream."""
    sample_rate = start["sample_rate"] or 8000
    serializer = VobizFrameSerializer(
        stream_id=start["stream_id"],
        call_id=start["call_id"],
        auth_id=os.environ["VOBIZ_AUTH_ID"],
        auth_token=os.environ["VOBIZ_AUTH_TOKEN"],
        params=VobizFrameSerializer.InputParams(
            encoding=start["encoding"] or "audio/x-mulaw",
            vobiz_sample_rate=sample_rate,
        ),
    )
    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            serializer=serializer,
        ),
    )

    stt = SpragSTTService()
    llm = SpragLLMService(settings=SpragLLMService.Settings(system_instruction=SYSTEM_INSTRUCTION))
    tts = SpragTTSService(settings=SpragTTSService.Settings(voice=os.getenv("SPRAG_VOICE", "wade")))

    context = LLMContext()
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer()),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        # Chorus speaks at 24 kHz. The serializer converts it to the call's format.
        params=PipelineParams(
            audio_in_sample_rate=sample_rate,
            audio_out_sample_rate=24000,
            enable_metrics=True,
        ),
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        context.add_message({"role": "developer", "content": "Greet the caller in one sentence."})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        await runner.cancel()

    await runner.run()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8765")))
