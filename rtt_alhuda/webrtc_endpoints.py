"""WebRTC signaling: server mic and TTS outbound audio (v1 single-session)."""

from collections.abc import Callable

from aiohttp import web
from aiortc.mediastreams import MediaStreamTrack
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription

from rtt_alhuda.config import WEBRTC_STUN_URLS
from rtt_alhuda.models import ClientState
from rtt_alhuda.webrtc_tracks import MicPcmTrack, TtsAudioTrack


def _build_rtc_configuration() -> RTCConfiguration:
    urls = [u.strip() for u in WEBRTC_STUN_URLS.split(",") if u.strip()]
    if not urls:
        urls = ["stun:stun.l.google.com:19302"]
    return RTCConfiguration(iceServers=[RTCIceServer(urls=urls)])


def _active_client(app: web.Application) -> ClientState | None:
    client = app.get("last_ws_client")
    return client if isinstance(client, ClientState) else None


async def _webrtc_offer(
    request: web.Request,
    *,
    track_factory: Callable[[], MediaStreamTrack],
    app_key: str,
) -> web.StreamResponse:
    app = request.app
    client = _active_client(app)
    if client is None or not client.recording:
        return web.json_response({"error": "recording not started"}, status=400)

    try:
        body = await request.json()
        sdp = body["sdp"]
        typ = body["type"]
    except (ValueError, KeyError, TypeError) as exc:
        return web.json_response({"error": f"invalid JSON body: {exc}"}, status=400)

    offer = RTCSessionDescription(sdp=sdp, type=typ)

    old_pc = app.get(app_key)
    if old_pc is not None:
        await old_pc.close()

    pc = RTCPeerConnection(configuration=_build_rtc_configuration())
    app[app_key] = pc

    track = track_factory()
    pc.addTrack(track)

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.json_response(
        {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    )


async def webrtc_input_offer(request: web.Request) -> web.StreamResponse:
    """SDP offer/answer for one outbound track: live server microphone (16 kHz tap)."""

    client = _active_client(request.app)
    if client is None or client.media_mic_queue is None:
        return web.json_response(
            {"error": "media queues not ready — start recording from the WebSocket first"},
            status=400,
        )

    return await _webrtc_offer(
        request,
        track_factory=lambda: MicPcmTrack(client.media_mic_queue),
        app_key="webrtc_input_pc",
    )


async def webrtc_tts_offer(request: web.Request) -> web.StreamResponse:
    """SDP offer/answer for one outbound track: TTS audio from OpenRouter."""

    client = _active_client(request.app)
    if client is None or client.media_tts_queue is None:
        return web.json_response(
            {"error": "media queues not ready — start recording from the WebSocket first"},
            status=400,
        )

    return await _webrtc_offer(
        request,
        track_factory=lambda: TtsAudioTrack(client.media_tts_queue),
        app_key="webrtc_tts_pc",
    )


async def on_cleanup_webrtc(app: web.Application) -> None:
    """Close peer connections on app shutdown."""

    for key in ("webrtc_input_pc", "webrtc_tts_pc"):
        pc = app.get(key)
        if pc is not None:
            await pc.close()
            app[key] = None


def register_webrtc_routes(app: web.Application) -> None:
    app.router.add_post("/webrtc/input", webrtc_input_offer)
    app.router.add_post("/webrtc/tts", webrtc_tts_offer)
    app.on_cleanup.append(on_cleanup_webrtc)
