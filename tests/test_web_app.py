"""Application wiring tests."""

from rtt_alhuda.web_app import create_app


def test_create_app_registers_webrtc_post_routes() -> None:
    app = create_app()
    post_paths = {
        r.resource.canonical
        for r in app.router.routes()
        if getattr(r, "method", None) == "POST"
    }
    assert "/webrtc/input" in post_paths
    assert "/webrtc/tts" in post_paths


def test_create_app_registers_webrtc_test_page() -> None:
    app = create_app()
    get_paths = {
        r.resource.canonical
        for r in app.router.routes()
        if getattr(r, "method", None) == "GET"
    }
    assert "/webrtc-test.html" in get_paths
