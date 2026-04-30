"""Tests for OpenRouter TTS client."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from rtt_alhuda.tts_openrouter import synthesize_speech_bytes


@pytest.mark.asyncio
async def test_synthesize_speech_bytes_returns_octets(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    fake_body = b"%fake-audio%"
    resp = MagicMock()
    resp.status = 200
    resp.headers = {"X-Generation-Id": "gen-1"}
    resp.read = AsyncMock(return_value=fake_body)

    post_cm = MagicMock()
    post_cm.__aenter__ = AsyncMock(return_value=resp)
    post_cm.__aexit__ = AsyncMock(return_value=None)

    http = MagicMock()
    http.post = MagicMock(return_value=post_cm)

    audio, gen_id = await synthesize_speech_bytes(
        http, text="Hello", language="en", voice="alloy"
    )
    assert audio == fake_body
    assert gen_id == "gen-1"
    http.post.assert_called_once()


@pytest.mark.asyncio
async def test_synthesize_empty_text_returns_empty(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    http = MagicMock()
    audio, gen_id = await synthesize_speech_bytes(http, text="   ", language="en")
    assert audio == b""
    assert gen_id is None
    http.post.assert_not_called()


@pytest.mark.asyncio
async def test_synthesize_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    http = MagicMock()
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        await synthesize_speech_bytes(http, text="Hi", language="en")
