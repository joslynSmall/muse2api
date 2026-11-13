"""CHATSESS 端点接线：真实 ASGI 请求 + Header，替换网页生成；不连 Muse 账号。

Run: python tests/test_chat_session_endpoints.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
THREAD_A = "424a43ce-c2e9-4a30-82de-d35311f33b5c"
THREAD_B = "819e0425-77d4-433f-b457-c3fb91bfdc79"


async def asgi(app, path, body, headers):
    raw = json.dumps(body).encode()
    hdr = {"authorization": "Bearer test-only", "content-type": "application/json",
           "content-length": str(len(raw)), **{k.lower(): v for k, v in headers.items()}}
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": b"", "root_path": "",
             "headers": [(k.encode(), v.encode()) for k, v in hdr.items()],
             "server": ("test", 80), "client": ("test", 1)}
    sent = False
    out = []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        out.append(message)

    await app(scope, receive, send)
    status = next(m["status"] for m in out if m["type"] == "http.response.start")
    return status, b"".join(m.get("body", b"") for m in out).decode()


with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_TOOL_PROTOCOL="1")
    spec = importlib.util.spec_from_file_location("chat_session_endpoints_target", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    for label in ("a", "b"):
        app.store.add_account({"hatch_sess": label, "hatch_vml": label,
                               "hatch_native_auth_device": label}, label)
    calls = []
    next_thread = [THREAD_A]

    def fake(cookies, prompt, expires, timeout, account_id=None, **kw):
        calls.append({"prompt": prompt, "account": account_id, "thread": kw.get("thread_id"),
                      "pin": kw.get("pin_account", False)})
        if "FAIL" in prompt:
            raise app.MuseGenerationError("fixture failure")
        on_thread = kw.get("on_thread")
        if on_thread:
            on_thread(None if "NOTHREAD" in prompt else kw.get("thread_id") or next_thread[0])
        if kw.get("on_final_text"):
            kw["on_final_text"]("好的")
        yield "好的"

    session = {"X-Claude-Code-Session-Id": "claude-session-endpoints"}

    def chat(messages, headers=session, stream=True):
        return asyncio.run(asgi(app.app, "/v1/chat/completions",
                                {"model": "muse-spark", "stream": stream, "messages": messages},
                                headers))

    def messages_api(messages, headers=session):
        return asyncio.run(asgi(app.app, "/v1/messages",
                                {"model": "muse-spark", "max_tokens": 64, "stream": True,
                                 "messages": messages}, headers))

    with patch.object(app, "safe_chat_stream", side_effect=fake), \
         patch.object(app, "_pace_text", side_effect=lambda t, *a, **k: iter([t] if t else [])):
        # CHATSESS-B01：无 Header 保持无状态。
        status, _ = chat([{"role": "user", "content": "stateless"}], headers={})
        assert status == 200 and calls[-1]["thread"] is None and not calls[-1]["pin"]
        assert app.CHAT_SESSIONS.sessions == {}

        # CHATSESS-B02：首轮新建并绑定。
        h1 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "first-q"}]
        status, body = chat(h1)
        assert status == 200 and "[DONE]" in body
        assert calls[-1]["thread"] is None and calls[-1]["pin"]
        account = calls[-1]["account"]
        row = app.CHAT_SESSIONS.get(app.CHAT_SESSIONS.key("claude-session-endpoints"))
        assert row["thread_id"] == THREAD_A and row["account_id"] == account

        # CHATSESS-B03：续轮回到同一 thread、同一账号，只发新输入。
        h2 = h1 + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "second-q"}]
        status, _ = chat(h2)
        assert status == 200
        assert calls[-1]["thread"] == THREAD_A and calls[-1]["account"] == account
        assert "second-q" in calls[-1]["prompt"] and "first-q" not in calls[-1]["prompt"]
        assert "sys" not in calls[-1]["prompt"], "续轮不重发系统提示"

        # CHATSESS-B07：失败不推进，下轮仍发送这条输入到同一 thread。
        h3 = h2 + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "FAIL-q"}]
        status, body = chat(h3)
        assert "error" in body
        h3b = h2 + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "retry-q"}]
        status, _ = chat(h3b)
        assert calls[-1]["thread"] == THREAD_A and "retry-q" in calls[-1]["prompt"]

        # CHATSESS-B04：压缩后历史改写 → 同账号新开 thread 并发送完整历史。
        next_thread[0] = THREAD_B
        compacted = [{"role": "system", "content": "sys"},
                     {"role": "user", "content": "summary-of-earlier"},
                     {"role": "user", "content": "after-compact"}]
        status, _ = chat(compacted)
        assert calls[-1]["thread"] is None and calls[-1]["account"] == account
        assert "summary-of-earlier" in calls[-1]["prompt"]
        row = app.CHAT_SESSIONS.get(app.CHAT_SESSIONS.key("claude-session-endpoints"))
        assert row["thread_id"] == THREAD_B

        # CHATSESS-B05：绑定账号不可用时报错，不切账号。
        before = len(calls)
        app.store.update_account(account, enabled=False)
        status, body = chat(compacted + [{"role": "assistant", "content": "好的"},
                                         {"role": "user", "content": "x"}])
        assert status == 409 and len(calls) == before
        app.store.update_account(account, enabled=True)

        # CHATSESS-B08：同一会话正在运行时拒绝并发。
        key = app.CHAT_SESSIONS.key("claude-session-endpoints")
        held = app.CHAT_SESSIONS.try_begin(key, ttl=60)
        assert held
        status, _ = chat(compacted + [{"role": "assistant", "content": "好的"},
                                      {"role": "user", "content": "y"}])
        assert status == 409
        app.CHAT_SESSIONS.end(key, held)

        # CHATSESS-B09：后台输入建议请求走无状态，不碰绑定 thread、不改指纹。
        before_row = app.CHAT_SESSIONS.get(key)
        side = compacted + [{"role": "assistant", "content": "好的"},
                            {"role": "user", "content": "[SUGGESTION MODE: Suggest what the user might type]"}]
        status, _ = chat(side)
        assert status == 200 and calls[-1]["thread"] is None and not calls[-1]["pin"]
        assert app.CHAT_SESSIONS.get(key) == before_row
        main = compacted + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "real-next"}]
        status, _ = chat(main)
        assert calls[-1]["thread"] == THREAD_B, "附带请求之后主对话仍续接原 thread"

        # CHATSESS-B10：绑定会话生成前失败必须报错，不能是空的成功回复。
        fail_turn = main + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "FAIL-again"}]
        before_row = app.CHAT_SESSIONS.get(key)
        status, body = chat(fail_turn)
        assert '"error"' in body and "[DONE]" not in body
        status, body = chat(fail_turn, stream=False)
        assert status == 502
        status, body = messages_api(fail_turn)
        assert "event: error" in body and "message_stop" not in body
        assert app.CHAT_SESSIONS.get(key) == before_row

        # CHATSESS-B11：生成成功但拿不到 thread ID → 报错，不发成功结束事件，不写绑定。
        lost = main + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "NOTHREAD-q"}]
        status, body = chat(lost)
        assert '"error"' in body and "[DONE]" not in body
        status, body = messages_api(lost)
        assert "event: error" in body and "message_stop" not in body
        assert app.CHAT_SESSIONS.get(key) == before_row
        assert app.CHAT_SESSIONS.try_begin(key, ttl=60), "失败后会话占用已释放"
        app.CHAT_SESSIONS._active.clear()

        # /v1/messages 共用同一逻辑；子代理与主会话隔离。
        next_thread[0] = THREAD_A
        agent = {**session, "X-Claude-Code-Session-Id": "claude-session-msg",
                 "X-Claude-Code-Agent-Id": "sub-1"}
        m1 = [{"role": "user", "content": "msg-first"}]
        status, body = messages_api(m1, agent)
        assert status == 200 and "message_stop" in body
        m2 = m1 + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "msg-second"}]
        status, _ = messages_api(m2, agent)
        assert calls[-1]["thread"] == THREAD_A and "msg-first" not in calls[-1]["prompt"]
        assert app.CHAT_SESSIONS.get(app.CHAT_SESSIONS.key("claude-session-msg")) is None

        # CHATSESS-B13：续轮工具清单不变 → 只发简短提醒；清单变化或重绑 → 发完整清单。
        def msg_tools(messages, tools, sid):
            return asyncio.run(asgi(app.app, "/v1/messages",
                                    {"model": "muse-spark", "max_tokens": 64, "stream": True,
                                     "messages": messages, "tools": tools},
                                    {"X-Claude-Code-Session-Id": sid}))
        bash = {"name": "Bash", "description": "LONG-BASH-DOC",
                "input_schema": {"type": "object", "properties": {"command": {"type": "string"}},
                                 "required": ["command"]}}
        read = {"name": "Read", "description": "LONG-READ-DOC",
                "input_schema": {"type": "object", "properties": {"file_path": {"type": "string"}}}}
        next_thread[0] = THREAD_A
        t1 = [{"role": "user", "content": "tools-first"}]
        msg_tools(t1, [bash], "claude-session-tools")
        assert "LONG-BASH-DOC" in calls[-1]["prompt"], "首轮发完整清单"
        t2 = t1 + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "tools-2"}]
        msg_tools(t2, [bash], "claude-session-tools")
        p = calls[-1]["prompt"]
        assert calls[-1]["thread"] == THREAD_A and "LONG-BASH-DOC" not in p
        assert "沿用前文" in p and "Bash(command*)" in p, p
        t3 = t2 + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "tools-3"}]
        msg_tools(t3, [bash, read], "claude-session-tools")
        assert "LONG-READ-DOC" in calls[-1]["prompt"], "清单变化时重发完整清单"
        t4 = t3 + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "tools-4"}]
        msg_tools(t4, [bash, read], "claude-session-tools")
        assert "LONG-READ-DOC" not in calls[-1]["prompt"], "新清单发过后续轮用提醒"
        # 失败的轮次不推进记录：下一轮仍按上次成功发送的清单判断。
        t5 = t4 + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "FAIL-tools"}]
        msg_tools(t5, [bash], "claude-session-tools")
        t5b = t4 + [{"role": "assistant", "content": "好的"}, {"role": "user", "content": "tools-5"}]
        msg_tools(t5b, [bash, read], "claude-session-tools")
        assert "LONG-READ-DOC" not in calls[-1]["prompt"]
        # 压缩重绑新开 thread：新 thread 没见过清单，必须完整发送。
        next_thread[0] = THREAD_B
        msg_tools([{"role": "user", "content": "compacted-summary"},
                   {"role": "user", "content": "after"}], [bash, read], "claude-session-tools")
        assert calls[-1]["thread"] is None and "LONG-READ-DOC" in calls[-1]["prompt"]

    # CHATSESS-B16：模型调用客户端没有的工具 → 同一 thread 追发纠正，拿到真实工具调用。
    replies = []
    seen = []

    def scripted(cookies, prompt, expires, timeout, account_id=None, **kw):
        seen.append({"prompt": prompt, "thread": kw.get("thread_id")})
        text = replies.pop(0)
        if kw.get("on_thread"):
            kw["on_thread"](kw.get("thread_id") or THREAD_A)
        if kw.get("on_final_text"):
            kw["on_final_text"](text)
        yield text

    bash = {"type": "function", "function": {
        "name": "Bash", "description": "run", "parameters": {
            "type": "object", "properties": {"command": {"type": "string"}},
            "required": ["command"]}}}
    search = {"type": "function", "function": {"name": "ToolSearch", "parameters": {
        "type": "object", "properties": {"query": {"type": "string"}}}}}
    bogus = ('我用这边的浏览器。\n\n```tool_call\n{"name": "browser.browse", '
             '"arguments": {"task": "x"}}\n```')
    good = '```tool_call\n{"name": "Bash", "arguments": {"command": "ls"}}\n```'

    def tool_chat(sid, stateless=False):
        body = {"model": "muse-spark", "stream": True, "tools": [bash, search],
                "messages": [{"role": "user", "content": "open the site"}]}
        hdr = {} if stateless else {"X-Claude-Code-Session-Id": sid}
        return asyncio.run(asgi(app.app, "/v1/chat/completions", body, hdr))

    with patch.object(app, "safe_chat_stream", side_effect=scripted), \
         patch.object(app, "_pace_text", side_effect=lambda t, *a, **k: iter([t] if t else [])):
        replies[:] = [bogus, good]
        status, body = tool_chat("claude-session-unknown-tool")
        assert status == 200 and '"finish_reason": "tool_calls"' in body, body
        assert '"name": "Bash"' in body, body
        # 执行不了的调用不能作为调用或原始 JSON 到达客户端（说明文字里提到名字可以）。
        assert '\\"name\\": \\"browser.browse' not in body and '"name": "browser.browse"' not in body
        assert len(seen) == 2 and seen[1]["thread"] == THREAD_A, seen
        assert "browser.browse" in seen[1]["prompt"] and "ToolSearch" in seen[1]["prompt"]
        # 无会话（无法在同一 thread 纠正）：不重试，也不把执行不了的调用当正文发出。
        seen.clear()
        replies[:] = [bogus]
        status, body = tool_chat("", stateless=True)
        assert len(seen) == 1 and '"finish_reason": "stop"' in body
        assert '"tool_calls"' not in body and '{\\"name\\": \\"browser.browse' not in body, body
        assert "已丢弃" in body, "无法纠正时给出可见说明（CHATMSG-B12）"

        # CHATMSG-B10：只缺末尾括号的 JSON 自动补齐，不重试。
        seen.clear()
        replies[:] = ['好。\n\n```tool_call\n{"name": "Bash", "arguments": {"command": "ls"}\n```']
        status, body = tool_chat("claude-session-repair")
        assert len(seen) == 1 and '"finish_reason": "tool_calls"' in body, body
        # CHATMSG-B11/B03：参数不在 schema（stdin）→ 同一 thread 纠正后拿到合法调用。
        seen.clear()
        replies[:] = ['```tool_call\n{"name": "Bash", "arguments": {"command": "python3 -", '
                      '"stdin": "print(1)"}}\n```', good]
        status, body = tool_chat("claude-session-badargs")
        assert len(seen) == 2 and "stdin" in seen[1]["prompt"] and "Write" in seen[1]["prompt"]
        assert '"finish_reason": "tool_calls"' in body and "stdin" not in body, body
        # CHATMSG-B12：JSON 坏到无法修复 → 纠正。
        seen.clear()
        replies[:] = ['```tool_call\n{"name": "Bash", "arguments": {"command": "echo "x}\n```', good]
        status, body = tool_chat("claude-session-broken")
        assert len(seen) == 2 and "JSON" in seen[1]["prompt"], seen
        assert '"finish_reason": "tool_calls"' in body
        # 纠正后仍然错误：只纠正一次，给出可见说明，不进入循环。
        seen.clear()
        replies[:] = [bogus, bogus]
        status, body = tool_chat("claude-session-twice")
        assert len(seen) == 2 and "已丢弃" in body and '"finish_reason": "stop"' in body

        # CHATMSG-B15：回复停在 JSON 字符串中间（被截断）→ 纠正两次，提示要求拆短重发。
        cut = 'tool_call\n{"name": "Bash", "arguments": {"command": "ls flowmap.js'
        seen.clear()
        replies[:] = [cut, cut, good]
        status, body = tool_chat("claude-session-truncated")
        assert len(seen) == 3 and all("截断" in s["prompt"] for s in seen[1:]), seen
        assert '"finish_reason": "tool_calls"' in body and '"name": "Bash"' in body, body
        # 两次都断在同一个地方：给出可见说明，不再追发第三次。
        seen.clear()
        replies[:] = [cut, cut, cut]
        status, body = tool_chat("claude-session-truncated-again")
        assert len(seen) == 3 and "被截断" in body and '"finish_reason": "stop"' in body, body

        # CHATMSG-B13：没调用工具却声称已执行 → 同一 thread 纠正，拿到真实调用。
        seen.clear()
        replies[:] = ['已生成并执行，cat fmt.txt 的真实内容是：1 4 9', good]
        status, body = tool_chat("claude-session-fabricated")
        assert len(seen) == 2 and "没有发出任何工具调用" in seen[1]["prompt"], seen
        assert '"finish_reason": "tool_calls"' in body and '"name": "Bash"' in body, body
        assert "没有实际执行" in body, "已流出的编造正文后要有可见说明"
        # 带着工具结果的续轮，据此总结"已修改…"不算编造，不重试。
        sid = "claude-session-summary"
        replies[:] = [good]
        tool_chat(sid)
        seen.clear()
        replies[:] = ["已修改 nginx.conf 并重载服务。"]
        follow = {"model": "muse-spark", "stream": True, "tools": [bash, search], "messages": [
            {"role": "user", "content": "open the site"},
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "Bash", "arguments": "{\"command\": \"ls\"}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"}]}
        status, body = asyncio.run(asgi(app.app, "/v1/chat/completions", follow,
                                        {"X-Claude-Code-Session-Id": sid}))
        assert len(seen) == 1 and "已修改 nginx.conf" in body and '"finish_reason": "stop"' in body, body

    print("PASS: CHATSESS endpoints bind, continue, rebind, fail closed, isolate agents, "
          "B16 correction, CHATMSG-B10..B13 repair/validate/correct/fabrication")
