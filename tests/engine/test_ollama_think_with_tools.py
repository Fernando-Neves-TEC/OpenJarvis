"""Tests for the Ollama ``think_with_tools`` opt-in.

Some models (gemma4:e4b) only emit tool calls with thinking on, while the
adapter disables thinking by default. The opt-in enables it for requests that
carry tools; the reasoning Ollama returns in ``message.thinking`` must never
surface as content.
"""

from __future__ import annotations

import json

import httpx
import pytest

from openjarvis.core.config import JarvisConfig
from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message, Role
from openjarvis.engine._discovery import _make_engine
from openjarvis.engine.ollama import OllamaEngine

_TOOLS = [{"type": "function", "function": {"name": "memory_manage"}}]
_REASONING = "SECRET-REASONING: the user wants me to save this"


def _sync_engine(handler, *, think_with_tools: bool) -> OllamaEngine:
    engine = OllamaEngine(
        host="http://testhost:11434", think_with_tools=think_with_tools
    )
    engine._client = httpx.Client(
        base_url="http://testhost:11434", transport=httpx.MockTransport(handler)
    )
    return engine


def _recording_handler(payloads: list[dict], message: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"message": message, "model": "m"})

    return handler


def _generate(engine: OllamaEngine, **kwargs):
    return engine.generate(
        [Message(role=Role.USER, content="Grave isto")], model="m", **kwargs
    )


class TestGenerateThinkPayload:
    def test_default_keeps_think_off_with_tools(self) -> None:
        payloads: list[dict] = []
        engine = _sync_engine(
            _recording_handler(payloads, {"content": "ok"}), think_with_tools=False
        )
        _generate(engine, tools=_TOOLS)
        assert payloads[0]["think"] is False

    def test_opt_in_enables_think_only_with_tools(self) -> None:
        payloads: list[dict] = []
        engine = _sync_engine(
            _recording_handler(payloads, {"content": "ok"}), think_with_tools=True
        )
        _generate(engine, tools=_TOOLS)
        _generate(engine)
        assert payloads[0]["think"] is True
        assert payloads[1]["think"] is False

    def test_explicit_think_kwarg_wins(self) -> None:
        payloads: list[dict] = []
        engine = _sync_engine(
            _recording_handler(payloads, {"content": "ok"}), think_with_tools=True
        )
        _generate(engine, tools=_TOOLS, think=False)
        assert payloads[0]["think"] is False

    def test_reasoning_never_reaches_content(self) -> None:
        payloads: list[dict] = []
        message = {
            "content": "Guardei na memória.",
            "thinking": _REASONING,
            "tool_calls": [
                {
                    "function": {
                        "name": "memory_manage",
                        "arguments": {"action": "add", "entry": "Fifine"},
                    }
                }
            ],
        }
        engine = _sync_engine(
            _recording_handler(payloads, message), think_with_tools=True
        )
        result = _generate(engine, tools=_TOOLS)
        assert result["content"] == "Guardei na memória."
        assert _REASONING not in json.dumps(result)
        assert result["tool_calls"][0]["name"] == "memory_manage"

    def test_400_on_think_retries_without_think_keeping_tools(self) -> None:
        payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            payloads.append(payload)
            if payload.get("think"):
                return httpx.Response(400, text="model does not support thinking")
            return httpx.Response(200, json={"message": {"content": "ok"}})

        engine = _sync_engine(handler, think_with_tools=True)
        result = _generate(engine, tools=_TOOLS)
        assert [(p["think"], "tools" in p) for p in payloads] == [
            (True, True),
            (False, True),
        ]
        assert result["content"] == "ok"


class TestStreamFullThink:
    @pytest.mark.asyncio
    async def test_reasoning_chunks_are_not_streamed(self) -> None:
        payloads: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payloads.append(json.loads(request.content))
            lines = [
                {"message": {"content": "", "thinking": _REASONING}},
                {"message": {"content": "Guardei."}},
                {"message": {"content": ""}, "done": True},
            ]
            return httpx.Response(200, text="\n".join(json.dumps(x) for x in lines))

        engine = OllamaEngine(host="http://localhost:11434", think_with_tools=True)
        engine._async_transport = httpx.MockTransport(handler)
        chunks = [
            c
            async for c in engine.stream_full(
                [Message(role=Role.USER, content="Grave isto")],
                model="m",
                tools=_TOOLS,
            )
        ]
        assert payloads[0]["think"] is True
        streamed = "".join(c.content or "" for c in chunks)
        assert streamed == "Guardei."
        assert "SECRET-REASONING" not in repr(chunks)

    @pytest.mark.asyncio
    async def test_400_on_think_retries_without_think_keeping_tools(self) -> None:
        calls: list[tuple[bool, bool]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            calls.append((payload.get("think"), "tools" in payload))
            if payload.get("think"):
                return httpx.Response(400, text="model does not support thinking")
            body = json.dumps({"message": {"content": "ok"}, "done": True}) + "\n"
            return httpx.Response(200, text=body)

        engine = OllamaEngine(host="http://localhost:11434", think_with_tools=True)
        engine._async_transport = httpx.MockTransport(handler)
        chunks = [
            c
            async for c in engine.stream_full(
                [Message(role=Role.USER, content="Hi")], model="m", tools=_TOOLS
            )
        ]
        assert calls == [(True, True), (False, True)]
        assert any(c.content == "ok" for c in chunks)


def test_make_engine_forwards_config() -> None:
    EngineRegistry.register_value("ollama", OllamaEngine)
    cfg = JarvisConfig()
    cfg.engine.ollama.host = "http://example:11434"
    cfg.engine.ollama.think_with_tools = True
    engine = _make_engine("ollama", cfg)
    assert isinstance(engine, OllamaEngine)
    assert engine._host == "http://example:11434"
    assert engine._think_with_tools is True
