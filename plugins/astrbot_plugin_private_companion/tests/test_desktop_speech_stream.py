"""Desktop speech negotiation and forwarding against a synthetic MiniMax endpoint."""

import asyncio
import base64
import io
import json
from types import SimpleNamespace
import wave

from aiohttp import web
import pytest

from astrbot_host import desktop_api


PCM = b"\x00\x20" * 640


def wav_audio():
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams((1, 2, desktop_api.PCM_RATE, 0, "NONE", "not compressed"))
        wav.writeframes(PCM)
    return out.getvalue()


def provider(url):
    return SimpleNamespace(
        provider_config={"id": "companion-minimax", "type": "minimax_tts_api"},
        request_headers={}, headers={"Authorization": "Bearer synthetic"}, concat_base_url=url,
        _build_tts_stream_body=lambda text: json.dumps({
            "model": "speech-2.8-turbo", "text": text, "stream": True, "audio_setting": {"format": "wav"},
            "voice_setting": {"voice_id": "demo", "speed": 1.0},
        }),
    )


def sse(status, audio=b"", code=0):
    body = {"base_resp": {"status_code": code}, "data": {"status": status, "audio": audio.hex()}}
    return b"data: " + json.dumps(body).encode() + b"\r\n\r\n"


def desktop(monkeypatch, provider, *, enabled, accept):
    api = desktop_api.DesktopAPI(SimpleNamespace(
        config={desktop_api.DESKTOP_SESSION_FLAG: enabled},
        context=SimpleNamespace(get_all_tts_providers=lambda: [provider]),
    ))
    monkeypatch.setattr(api, "_authorize", lambda: None)
    monkeypatch.setattr(api, "_live", lambda: None)

    async def body():
        return b'{"text":"Demo sentence."}'

    monkeypatch.setattr(desktop_api, "request", SimpleNamespace(headers={"accept": accept}, body=body, query={}))
    return api


async def serve(handler):
    app = web.Application()
    app.router.add_post("/tts", handler)
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, provider(f"http://127.0.0.1:{runner.addresses[0][1]}/tts")


def test_flag_and_accept_keep_full_wav_response(monkeypatch):
    async def run():
        audio = wav_audio()
        payloads = []

        async def synthesize(request):
            payloads.append(await request.json())
            return web.json_response({"base_resp": {"status_code": 0}, "data": {"audio": audio.hex()}})

        runner, config = await serve(synthesize)
        try:
            expected = {"status": "ok", "data": {"id": "demo-id", "audio": base64.b64encode(audio).decode(),
                                                   "mouth_available": False}}
            monkeypatch.setattr(desktop_api, "uuid4", lambda: SimpleNamespace(hex="demo-id"))
            # Old client, new client with flag off, and the WAV fallback with flag on.
            for enabled, accept in ((False, "*/*"), (False, desktop_api.PCM_CONTENT_TYPE), (True, "*/*")):
                assert await desktop(monkeypatch, config, enabled=enabled, accept=accept).speech() == expected
            assert payloads == [{"model": "speech-2.8-turbo", "text": "Demo sentence.", "stream": False,
                                 "audio_setting": {"format": "wav"},
                                 "voice_setting": {"voice_id": "demo", "speed": 1.0}}] * 3
        finally:
            await runner.cleanup()

    asyncio.run(run())


@pytest.mark.parametrize("completion", ["success", "upstream_error", "truncated", "disconnect"])
def test_pcm_forwards_before_completion_and_requires_success(monkeypatch, completion):
    async def run():
        finish = asyncio.Event()
        disconnected = asyncio.Event()
        observed = {}

        async def synthesize(request):
            observed["payload"] = await request.json()
            observed["accept"] = request.headers["Accept"]
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            first = sse(1, PCM)
            await response.write(first[:17])
            await response.write(first[17:])
            try:
                await finish.wait()
            except asyncio.CancelledError:
                disconnected.set()
                raise
            if completion == "success":
                await response.write(sse(1, PCM) + sse(2, PCM * 2))  # never replay an aggregate
            elif completion == "upstream_error":
                await response.write(sse(2, code=1001))
            return response

        runner, config = await serve(synthesize)
        try:
            api = desktop(monkeypatch, config, enabled=True, accept=desktop_api.PCM_CONTENT_TYPE)
            response = await api.speech()
            assert response.headers["content-type"] == desktop_api.PCM_CONTENT_TYPE
            assert response.headers["x-accel-buffering"] == "no"
            stream = response.body_iterator
            frame = len(PCM).to_bytes(4, "big") + PCM
            assert await asyncio.wait_for(anext(stream), 2) == frame
            assert not finish.is_set()  # the desktop has audio even though synthesis isn't complete
            assert observed["accept"] == "text/event-stream"
            assert observed["payload"] == {
                "model": "speech-2.8-turbo", "text": "Demo sentence.", "stream": True,
                "stream_options": {"exclude_aggregated_audio": True},
                "audio_setting": {"format": "pcm", "sample_rate": desktop_api.PCM_RATE, "channel": 1},
                "voice_setting": {"voice_id": "demo", "speed": 1.0},
            }
            if completion == "disconnect":
                await stream.aclose()
                await asyncio.wait_for(disconnected.wait(), 2)
            else:
                finish.set()
                rest = [item async for item in stream]
                assert rest == ([frame, b"\0" * 4] if completion == "success" else [])
        finally:
            finish.set()
            await runner.cleanup()

    asyncio.run(run())


def test_stream_still_requires_desktop_authorization(monkeypatch):
    api = desktop(monkeypatch, None, enabled=True, accept=desktop_api.PCM_CONTENT_TYPE)

    def deny():
        raise PermissionError

    monkeypatch.setattr(api, "_authorize", deny)
    assert asyncio.run(api.speech())["status"] == "error"
