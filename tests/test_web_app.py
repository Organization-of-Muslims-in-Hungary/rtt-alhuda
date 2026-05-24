"""Application wiring and WebSocket handler tests."""

from rtt_alhuda.web_app import create_app


def test_create_app_registers_stream_route() -> None:
    app = create_app()
    get_paths = {
        r.resource.canonical
        for r in app.router.routes()
        if getattr(r, "method", None) == "GET"
    }
    assert "/stream" in get_paths
    assert "/stream/text" in get_paths
    assert "/stream/tts/{lang}" in get_paths
    assert "/api/lan-ipv4" in get_paths


def test_create_app_has_no_webrtc_routes() -> None:
    app = create_app()
    all_paths = {
        r.resource.canonical
        for r in app.router.routes()
    }
    assert "/webrtc/input" not in all_paths
    assert "/webrtc/tts" not in all_paths
    assert "/webrtc-test.html" not in all_paths

