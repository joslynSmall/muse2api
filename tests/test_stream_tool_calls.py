"""Streaming chat must turn a tool call that follows prose into tool_calls."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 实测回复形态：先一句说明，再一行 tool_call 标签，最后是 JSON
REPLY = ('先查现状，确认缺哪些字段再改。\n\ntool_call\n'
         '{"name": "Bash", "arguments": {"command": "ls -l \\"/tmp/a b\\""}}')
TOOLS = [{"type": "function", "function": {
    "name": "Bash", "description": "run", "parameters": {
        "type": "object", "properties": {"command": {"type": "string"}},
        "required": ["command"]}}}]


def stream(app, reply, chunks=7):
    """把 reply 切成若干片模拟上游流，返回 (正文, tool_calls, finish_reason)。"""
    step = max(1, len(reply) // chunks)
    pieces = [reply[i:i + step] for i in range(0, len(reply), step)]
    req = app.ChatRequest(model="muse-spark", stream=True, tools=TOOLS,
                          messages=[{"role": "user", "content": "hi"}])

    async def collect():
        resp = await app.chat_completions(req)
        out = []
        async for part in resp.body_iterator:
            out.append(part if isinstance(part, str) else part.decode())
        return "".join(out)

    with patch.object(app, "safe_chat_stream", side_effect=lambda *a, **k: iter(pieces)):
        body = asyncio.run(collect())
    text, calls, finish = "", {}, None
    for line in body.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        event = json.loads(line[6:])
        assert "error" not in event, event
        for choice in event.get("choices", []):
            delta = choice.get("delta") or {}
            text += delta.get("content") or ""
            for call in delta.get("tool_calls") or []:
                slot = calls.setdefault(call["index"], {"name": "", "arguments": ""})
                fn = call.get("function") or {}
                slot["name"] += fn.get("name") or ""
                slot["arguments"] += fn.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
    return text, [calls[i] for i in sorted(calls)], finish


with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_TOOL_PROTOCOL="1")
    spec = importlib.util.spec_from_file_location("stream_tool_calls_candidate", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    app.store.add_account({"hatch_sess": "one", "hatch_vml": "one",
                           "hatch_native_auth_device": "one"}, "one")
    with patch.object(app, "_pace_text", side_effect=lambda text, *a, **k: iter([text] if text else [])):
        text, calls, finish = stream(app, REPLY)
        assert finish == "tool_calls", finish
        assert len(calls) == 1 and calls[0]["name"] == "Bash", calls
        assert json.loads(calls[0]["arguments"]) == {"command": 'ls -l "/tmp/a b"'}, calls
        assert text == "先查现状，确认缺哪些字段再改。", repr(text)

        # CHATMSG-B09：正文在上游生成过程中就发出，不等整段结束。
        # 上游在吐出工具调用前等待客户端先收到正文；若正文被整段缓冲，等待会超时。
        import threading
        client_saw_prose = threading.Event()
        waited = []

        def slow_upstream(*a, **k):
            yield "先查一下"
            yield "现状。\n\n"
            waited.append(client_saw_prose.wait(timeout=5))
            yield "tool_call\n"
            yield '{"name": "Bash", "arguments": {"command": "ls"}}'

        req = app.ChatRequest(model="muse-spark", stream=True, tools=TOOLS,
                              messages=[{"role": "user", "content": "hi"}])

        async def consume():
            resp = await app.chat_completions(req)
            body = []
            async for part in resp.body_iterator:
                part = part if isinstance(part, str) else part.decode()
                body.append(part)
                if '"content"' in part and "先查一下" in part:
                    client_saw_prose.set()
            return "".join(body)

        with patch.object(app, "safe_chat_stream", side_effect=slow_upstream):
            body = asyncio.run(consume())
        assert waited == [True], ("CHATMSG-B09 正文必须在上游结束前送达客户端", waited)
        assert '"finish_reason": "tool_calls"' in body or '"finish_reason":"tool_calls"' in body

        plain = "不需要工具，直接回答：上下文是 128k。"
        text, calls, finish = stream(app, plain)
        assert (text, calls, finish) == (plain, [], "stop"), (text, calls, finish)
print("PASS: CHATMSG-B09 streamed prose + tool call becomes tool_calls; prose streams before end")
