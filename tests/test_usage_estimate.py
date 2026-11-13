"""CHATMSG-B14：Muse 网页不返回真实 token 用量，usage 按字符估算。"""
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

THREAD = "424a43ce-c2e9-4a30-82de-d35311f33b5c"
PLAIN_REPLY = "这是按字符估算的回复，用于检查 usage。"
TOOL_REPLY = ('先看主机名。\n\ntool_call\n'
              '{"name": "Read", "arguments": {"file_path": "/etc/hostname"}}')
TOOLS = [{"name": "Read", "description": "read a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}},
    "required": ["file_path"]}}]


def events(body):
    """按 SSE 的 event/data 成对取出事件。"""
    out, event = [], None
    for line in body.splitlines():
        if line.startswith("event: "):
            event = line[7:]
        elif line.startswith("data: ") and event:
            out.append((event, json.loads(line[6:])))
            event = None
    return out


async def collect(resp):
    out = []
    async for part in resp.body_iterator:
        out.append(part if isinstance(part, str) else part.decode())
    return "".join(out)


with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_TOOL_PROTOCOL="1")
    spec = importlib.util.spec_from_file_location("usage_estimate_candidate", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    app.store.add_account({"hatch_sess": "one", "hatch_vml": "one",
                           "hatch_native_auth_device": "one"}, "one")
    seen = []

    def fake_chat(cookies, prompt, *a, **k):
        seen.append(prompt)
        reply = TOOL_REPLY if k.get("expect_tools") else PLAIN_REPLY
        callback = k.get("on_final_text")
        if callback:
            callback(reply)
        return iter([reply])

    def prompt_estimate():
        return app.estimate_tokens(seen[-1])

    def full_estimate(req):
        """Messages 的 input 期望：客户端完整对话 + 完整工具清单。"""
        incoming = app._messages_to_chat(req)
        full = app._messages_prompt(incoming)
        note = (app.build_tools_prompt(req.tools, req.tool_choice)
                if app.CFG.tool_protocol else "")
        return app.estimate_tokens(full + ("\n\n" + note if note else ""))

    class StubRequest:
        """messages_api 只读 headers；续轮测试需要会话 Header。"""

        def __init__(self, session_id):
            self.headers = {"x-claude-code-session-id": session_id}

        async def is_disconnected(self):
            return False

    def fake_session(cookies, prompt, expires, timeout, account_id=None, **kw):
        seen.append(prompt)
        on_thread = kw.get("on_thread")
        if on_thread:
            on_thread(kw.get("thread_id") or THREAD)
        if kw.get("on_final_text"):
            kw["on_final_text"]("好的")
        yield "好的"

    with patch.object(app, "safe_chat_stream", side_effect=fake_chat), \
         patch.object(app, "_pace_text",
                      side_effect=lambda text, *a, **k: iter([text] if text else [])):
        # 纯函数：英文、中文、混合、空串，以及假名/谚文/全角标点范围。
        assert app.estimate_tokens("") == 0
        assert app.estimate_tokens("hello world") == 3
        assert app.estimate_tokens("你好世界") == 4
        assert app.estimate_tokens("hi你好") == 3
        assert app.estimate_tokens("こんにちは") == 5
        assert app.estimate_tokens("안녕") == 2
        assert app.estimate_tokens("，。！？") == 4

        # Anthropic Messages 非流式。
        req = app.MessagesRequest(
            model="muse-spark", messages=[{"role": "user", "content": "你好，介绍一下自己"}])
        msg = asyncio.run(app.messages_api(req))
        assert msg["usage"]["input_tokens"] == full_estimate(req) > 0, msg["usage"]
        assert msg["usage"]["output_tokens"] == app.estimate_tokens(PLAIN_REPLY) > 0

        # Anthropic Messages 流式：message_start 给输入，message_delta 给累计输出。
        req = app.MessagesRequest(
            model="muse-spark", stream=True,
            messages=[{"role": "user", "content": "介绍一下你自己"}])
        body = asyncio.run(collect(asyncio.run(app.messages_api(req))))
        parsed = events(body)
        start = next(data for name, data in parsed if name == "message_start")
        delta = next(data for name, data in parsed if name == "message_delta")
        deltas = "".join(
            data["delta"]["text"] for name, data in parsed
            if name == "content_block_delta"
            and data.get("delta", {}).get("type") == "text_delta")
        assert deltas == PLAIN_REPLY, repr(deltas)
        assert start["message"]["usage"]["input_tokens"] == full_estimate(req) > 0
        assert start["message"]["usage"]["output_tokens"] == 1
        assert delta["usage"]["input_tokens"] == start["message"]["usage"]["input_tokens"]
        assert delta["usage"]["output_tokens"] == app.estimate_tokens(deltas) > 0

        # 流式工具调用：输入按完整对话 + 完整工具清单；输出包含工具名和参数。
        req = app.MessagesRequest(
            model="muse-spark", stream=True, tools=TOOLS,
            messages=[{"role": "user", "content": "看主机名"}])
        body = asyncio.run(collect(asyncio.run(app.messages_api(req))))
        parsed = events(body)
        start = next(data for name, data in parsed if name == "message_start")
        delta = next(data for name, data in parsed if name == "message_delta")
        text = "".join(
            data["delta"]["text"] for name, data in parsed
            if name == "content_block_delta"
            and data.get("delta", {}).get("type") == "text_delta")
        arguments = next(
            data["delta"]["partial_json"] for name, data in parsed
            if name == "content_block_delta"
            and data.get("delta", {}).get("type") == "input_json_delta")
        assert text == "先看主机名。", repr(text)
        assert start["message"]["usage"]["input_tokens"] == full_estimate(req) > 0
        expected = app.estimate_tokens("\n".join([text, "Read", arguments]))
        assert delta["usage"]["output_tokens"] == expected
        assert delta["usage"]["output_tokens"] > app.estimate_tokens(text)

        # Chat Completions 非流式：total = prompt + completion。
        completion = asyncio.run(app.chat_completions(app.ChatRequest(
            model="muse-spark", messages=[{"role": "user", "content": "你好"}])))
        usage = completion["usage"]
        assert usage["prompt_tokens"] == prompt_estimate() > 0, usage
        assert usage["completion_tokens"] == app.estimate_tokens(PLAIN_REPLY) > 0
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

        # Chat Completions 流式 include_usage。
        resp = asyncio.run(app.chat_completions(app.ChatRequest(
            model="muse-spark", stream=True, stream_options={"include_usage": True},
            messages=[{"role": "user", "content": "你好"}])))
        body = asyncio.run(collect(resp))
        chunks = [json.loads(line[6:]) for line in body.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        usage_chunks = [chunk["usage"] for chunk in chunks if chunk.get("usage")]
        assert len(usage_chunks) == 1, usage_chunks
        usage = usage_chunks[0]
        assert usage["prompt_tokens"] == prompt_estimate() > 0, usage
        assert usage["completion_tokens"] == app.estimate_tokens(PLAIN_REPLY) > 0
        assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]

        # CHATMSG_B14：Messages 续轮实际只发增量，但 input 仍按客户端完整对话
        # （system + 全部 messages + 完整工具清单）估算，随上下文增长单调不减。
        sid = "usage-b14-continue"
        first_messages = [{"role": "user", "content": "first question: read README"}]
        first_req = app.MessagesRequest(
            model="muse-spark", stream=True, system="system prompt for usage",
            tools=TOOLS, messages=first_messages)
        with patch.object(app, "safe_chat_stream", side_effect=fake_session):
            body = asyncio.run(collect(asyncio.run(
                app.messages_api(first_req, StubRequest(sid)))))
            first_start = next(data for name, data in events(body)
                               if name == "message_start")
            first_input = first_start["message"]["usage"]["input_tokens"]
            assert first_input == full_estimate(first_req) > 0, first_input
            row = app.CHAT_SESSIONS.get(app.CHAT_SESSIONS.key(sid))
            assert row and row["thread_id"] == THREAD

            second_messages = first_messages + [
                {"role": "assistant", "content": "好的"},
                {"role": "user", "content": "second question: grep hello"}]
            second_req = app.MessagesRequest(
                model="muse-spark", stream=True, system="system prompt for usage",
                tools=TOOLS, messages=second_messages)
            body = asyncio.run(collect(asyncio.run(
                app.messages_api(second_req, StubRequest(sid)))))
            second_start = next(data for name, data in events(body)
                                if name == "message_start")
            second_input = second_start["message"]["usage"]["input_tokens"]
            actual = seen[-1]
            # 实际发给 Muse 的只是增量：不含 system、首轮问题与完整工具说明。
            assert "second question" in actual and "first question" not in actual, actual
            assert "system prompt for usage" not in actual, actual
            assert "read a file" not in actual, actual
            # input_tokens 是完整对话 + 完整工具清单的估算，大于实际增量。
            assert second_input == full_estimate(second_req), second_input
            assert second_input > app.estimate_tokens(actual), (second_input, actual)
            assert second_input > first_input, (first_input, second_input)
            complete = app._messages_prompt(app._messages_to_chat(second_req))
            complete += "\n\n" + app.build_tools_prompt(TOOLS)
            assert len(actual) < len(complete), (len(actual), len(complete))

print("PASS: CHATMSG_B14 usage estimates CJK/ASCII input, text + tool arguments, "
      "and continued-turn input from the full client conversation")
