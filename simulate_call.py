"""Test bot.py with a simulated Vobiz call, without a phone number or a tunnel.

It speaks Vobiz's bidirectional stream protocol: a start event, then real-time
media events in the chosen format. It records the agent's greeting, plays one
caller utterance, records the reply, and saves both as WAV files.

    python simulate_call.py
    python simulate_call.py --encoding audio/x-l16 --rate 16000
    python simulate_call.py --wav my-question.wav

The caller's words come from --wav, or from macOS `say` with --say. Both need ffmpeg.
"""

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave

try:
    import audioop  # Python 3.12 and earlier
except ImportError:  # Python 3.13+: pip install audioop-lts
    import audioop_lts as audioop  # type: ignore

import websockets
from dotenv import load_dotenv

# Read .env before the defaults below use PORT and STREAM_TOKEN.
load_dotenv()

FRAME_MS = 20


def to_pcm(path: str, rate: int) -> bytes:
    """Convert any audio file to 16-bit mono PCM at the given rate."""
    out = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", path, "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
        check=True,
        capture_output=True,
    )
    return out.stdout


def caller_audio(args) -> bytes:
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is required: https://ffmpeg.org/download.html")
    if args.wav:
        return to_pcm(args.wav, args.rate)
    if shutil.which("say") is None:
        sys.exit("`say` is macOS only. Record a question and pass it with --wav.")
    with tempfile.TemporaryDirectory() as d:
        aiff = os.path.join(d, "caller.aiff")
        subprocess.run(["say", "-o", aiff, args.say], check=True)
        return to_pcm(aiff, args.rate)


def encode(pcm: bytes, encoding: str) -> bytes:
    return audioop.lin2ulaw(pcm, 2) if encoding == "audio/x-mulaw" else pcm


def decode(payload: bytes, encoding: str) -> bytes:
    return audioop.ulaw2lin(payload, 2) if encoding == "audio/x-mulaw" else payload


def save(path: str, pcm: bytes, rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


async def main(args) -> int:
    token = args.token or os.getenv("STREAM_TOKEN")
    if not token:
        sys.exit("Set STREAM_TOKEN in .env (bot.py reads the same value), or pass --token.")

    rate, enc = args.rate, args.encoding
    frame_bytes = rate * FRAME_MS // 1000 * 2
    silence = bytes(frame_bytes)
    utterance = caller_audio(args)
    received: list[tuple[float, bytes]] = []

    async with websockets.connect(f"ws://127.0.0.1:{args.port}/ws/{token}", max_size=None) as ws:
        t0 = time.monotonic()
        await ws.send(json.dumps({
            "event": "start",
            "start": {
                "callId": "simulated-call",
                "streamId": "simulated-stream",
                "mediaFormat": {"encoding": enc, "sampleRate": rate},
            },
        }))

        async def receive():
            async for message in ws:
                event = json.loads(message)
                if event.get("event") == "playAudio":
                    payload = base64.b64decode(event["media"]["payload"])
                    received.append((time.monotonic() - t0, decode(payload, enc)))

        async def send(pcm: bytes):
            for i in range(0, len(pcm), frame_bytes):
                chunk = pcm[i:i + frame_bytes].ljust(frame_bytes, b"\0")
                media = {"payload": base64.b64encode(encode(chunk, enc)).decode()}
                await ws.send(json.dumps({"event": "media", "media": media}))
                await asyncio.sleep(FRAME_MS / 1000)

        receiver = asyncio.create_task(receive())
        # Silence while the greeting plays, then the caller speaks, then silence for the reply.
        await send(silence * int(args.greeting_wait * 1000 / FRAME_MS))
        speak_at = time.monotonic() - t0
        await send(utterance)
        spoke_until = time.monotonic() - t0
        await send(silence * int(args.reply_wait * 1000 / FRAME_MS))
        await ws.send(json.dumps({"event": "stop"}))
        receiver.cancel()

    greeting = [(t, a) for t, a in received if t < speak_at]
    reply = [(t, a) for t, a in received if t >= spoke_until]
    save("greeting.wav", b"".join(a for _, a in greeting), rate)
    save("reply.wav", b"".join(a for _, a in reply), rate)

    def seconds(chunks):
        return round(sum(len(a) for _, a in chunks) / (rate * 2), 2)

    print(f"Format: {enc} at {rate} Hz")
    if greeting:
        print(f"Greeting: started after {greeting[0][0]:.2f} s, {seconds(greeting)} s long -> greeting.wav")
    else:
        print("Greeting: none received")
    if reply:
        print(f"Reply: started {reply[0][0] - spoke_until:.2f} s after the caller stopped, {seconds(reply)} s long -> reply.wav")
    else:
        print("Reply: none received")
    return 0 if greeting and reply else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--encoding", default="audio/x-mulaw", choices=["audio/x-mulaw", "audio/x-l16"])
    p.add_argument("--rate", type=int, default=8000, choices=[8000, 16000])
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    p.add_argument("--token", help="The stream token. Defaults to STREAM_TOKEN from .env.")
    p.add_argument("--wav", help="An audio file with the caller's question.")
    p.add_argument("--say", default="Hi, can you tell me what the capital of France is?",
                   help="What the caller says, spoken with macOS `say` when --wav is not given.")
    p.add_argument("--greeting-wait", type=float, default=8.0, help="Seconds of silence before the caller speaks.")
    p.add_argument("--reply-wait", type=float, default=10.0, help="Seconds to wait for the reply.")
    sys.exit(asyncio.run(main(p.parse_args())))
