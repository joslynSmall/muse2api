"""Responses and Anthropic Messages must round-trip tool calls and results."""
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

REPLY = ('先看主机名。\n\ntool_call\n'
         '{"name": "Read", "arguments": {"file_path": "/etc/hostname"}}')
ANTHROPIC_TOOLS = [{"name": "Read", "description": "read a file", "input_schema": {
    "type": "object", "properties": {"file_path": {"type": "string"}},
    "required": ["file_path"]}},
    {"type": "bash_20250124", "name": "bash"}]
RESPONSE_TOOLS = [{"type": "function", "name": "Read", "description": "read a file",
                   "parameters": {"type": "object",
                                  "properties": {"file_path": {"type": "string"}},
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


def fake_stream(reply):
    step = max(1, len(reply) // 5)
    return lambda *a, **k: iter(reply[i:i + step] for i in range(0, len(reply), step))


with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only", MUSE2API_TOOL_PROTOCOL="1")
    spec = importlib.util.spec_from_file_location("protocol_tool_calls_candidate", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    app.store.add_account({"hatch_sess": "one", "hatch_vml": "one",
                           "hatch_native_auth_device": "one"}, "one")
    seen = []

    def capture(cookies, prompt, *a, **k):
        seen.append(prompt)
        reply = "主机名是 probe-host。" if "probe-host" in prompt else REPLY
        callback = k.get("on_final_text")
        if callback:
            callback(reply)
        return fake_stream(reply)()

    with patch.object(app, "safe_chat_stream", side_effect=capture), \
         patch.object(app, "_pace_text", side_effect=lambda text, *a, **k: iter([text] if text else [])):
        # Anthropic：工具定义、流式 tool_use、tool_result 回传
        first = asyncio.run(collect(asyncio.run(app.messages_api(app.MessagesRequest(
            model="muse-spark", stream=True, tools=ANTHROPIC_TOOLS,
            system="你是编程助手", messages=[{"role": "user", "content": "看主机名"}])))))
        parsed = events(first)
        assert parsed[0][0] == "message_start" and parsed[-1][0] == "message_stop"
        assert [name for name, _ in parsed].count("content_block_start") == 2
        assert [name for name, _ in parsed].count("content_block_stop") == 2
        text_deltas = [data["delta"]["text"] for name, data in parsed
                       if name == "content_block_delta" and
                       data.get("delta", {}).get("type") == "text_delta"]
        assert "".join(text_deltas).strip() == "先看主机名。"
        text_start = next(i for i, (name, data) in enumerate(parsed)
                          if name == "content_block_start" and
                          data["content_block"]["type"] == "text")
        tool_start = next(i for i, (name, data) in enumerate(parsed)
                          if name == "content_block_start" and
                          data["content_block"]["type"] == "tool_use")
        assert text_start < tool_start
        tool = parsed[tool_start][1]["content_block"]
        assert tool["type"] == "tool_use" and tool["name"] == "Read" and tool["input"] == {}
        args_event = next(data for name, data in parsed
                          if name == "content_block_delta" and
                          data.get("delta", {}).get("type") == "input_json_delta")
        assert json.loads(args_event["delta"]["partial_json"]) == {"file_path": "/etc/hostname"}
        message_delta = next(data for name, data in parsed if name == "message_delta")
        assert message_delta["delta"]["stop_reason"] == "tool_use"
        assert "bash" not in seen[0] and "file_path" in seen[0] and "你是编程助手" in seen[0]

        prior = [{"role": "user", "content": "看主机名"},
                 {"role": "assistant", "content": [
                     {"type": "text", "text": "先看主机名。"},
                     {"type": "tool_use", "id": tool["id"], "name": "Read",
                      "input": {"file_path": "/etc/hostname"}}]},
                 {"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": tool["id"],
                      "content": [{"type": "text", "text": "probe-host"},
                                  {"type": "image", "source": {"type": "base64"}}]}]}]
        normalized = app._messages_to_chat(app.MessagesRequest(
            model="muse-spark", tools=ANTHROPIC_TOOLS, messages=prior))
        result_msg = normalized[-1]
        assert result_msg.role == "tool" and result_msg.tool_call_id == tool["id"]
        assert result_msg.name == "Read" and "probe-host" in result_msg.content
        assert "[图片/截图结果]" in result_msg.content
        seen.clear()
        second = asyncio.run(app.messages_api(app.MessagesRequest(
            model="muse-spark", tools=ANTHROPIC_TOOLS, messages=prior)))
        assert second["stop_reason"] == "end_turn" and second["content"] == [
            {"type": "text", "text": "主机名是 probe-host。"}], second
        assert "call_id=" + tool["id"] in seen[0]
        assert seen[0].index("probe-host") > seen[0].index("call_id=" + tool["id"])
        assert "工具结果已经执行完成，以结果为准" in seen[0]

        # Responses：function 工具、流式 function_call、function_call_output 回传
        seen.clear()
        body = asyncio.run(collect(asyncio.run(app.responses_api(app.ResponsesRequest(
            model="muse-spark", stream=True, tools=RESPONSE_TOOLS,
            instructions="你是编程助手",
            input=[{"role": "user", "content": "看主机名"}])))))
        parsed = events(body)
        names = [name for name, _ in parsed]
        assert names[0] == "response.created" and names[-1] == "response.completed", names
        added = [data for name, data in parsed if name == "response.output_item.added"]
        assert [item["item"]["type"] for item in added] == ["message", "function_call"], added
        call = added[1]["item"]
        assert call["name"] == "Read" and call["arguments"] == "" and call["call_id"]
        done = [data for name, data in parsed
                if name == "response.function_call_arguments.done"][0]
        assert json.loads(done["arguments"]) == {"file_path": "/etc/hostname"}
        completed = parsed[-1][1]["response"]
        assert [item["type"] for item in completed["output"]] == ["message", "function_call"]

        again = asyncio.run(app.responses_api(app.ResponsesRequest(
            model="muse-spark", tools=RESPONSE_TOOLS, input=[
                {"role": "user", "content": "看主机名"},
                {"type": "function_call", "call_id": call["call_id"], "name": "Read",
                 "arguments": done["arguments"]},
                {"type": "function_call_output", "call_id": call["call_id"],
                 "output": "probe-host"}])))
        assert again["output"] == [{
            "type": "message", "id": again["output"][0]["id"], "role": "assistant",
            "status": "completed", "content": [
                {"type": "output_text", "text": "主机名是 probe-host。", "annotations": []}]}], again
        assert "probe-host" in seen[1]

        # 无工具的 Responses 流式仍逐块发出增量
        plain_reply = "这是普通文本增量。"
        with patch.object(app, "safe_chat_stream", side_effect=fake_stream(plain_reply)):
            live = asyncio.run(collect(asyncio.run(app.responses_api(app.ResponsesRequest(
                model="muse-spark", stream=True, input="直接回答")))))
        deltas = [data["delta"] for name, data in events(live)
                  if name == "response.output_text.delta"]
        assert len(deltas) > 1 and "".join(deltas) == plain_reply, deltas

        plain_anthropic = "这是一段由Muse DOM一次性返回的大文本，用于验证Anthropic消息增量分块。"
        with patch.object(app, "safe_chat_stream", side_effect=fake_stream(plain_anthropic)):
            live = asyncio.run(collect(asyncio.run(app.messages_api(app.MessagesRequest(
                model="muse-spark", stream=True,
                messages=[{"role": "user", "content": "直接回答"}])))))
        deltas = [data["delta"]["text"] for name, data in events(live)
                  if name == "content_block_delta"]
        assert len(deltas) > 1 and "".join(deltas) == plain_anthropic, deltas

        large = "这是Muse一次性返回的大段正文。" * 20
        with patch.object(app, "safe_chat_stream", side_effect=lambda *a, **k: iter([large])), \
             patch.object(app.time, "sleep") as sleep:
            live = asyncio.run(collect(asyncio.run(app.messages_api(app.MessagesRequest(
                model="muse-spark", stream=True,
                messages=[{"role": "user", "content": "直接回答"}])))))
        parsed = events(live)
        deltas = [data["delta"]["text"] for name, data in parsed
                  if name == "content_block_delta"]
        assert "".join(deltas) == large and max(map(len, deltas)) <= 4, deltas
        assert sum(call.args[0] for call in sleep.call_args_list) <= 2.01

        # 大块纯文本即使请求声明了 tools，也必须逐小片发送且不触发 tool_use。
        with patch.object(app, "safe_chat_stream", side_effect=lambda *a, **k: iter([large])), \
             patch.object(app.time, "sleep") as sleep:
            live = asyncio.run(collect(asyncio.run(app.messages_api(app.MessagesRequest(
                model="muse-spark", stream=True, tools=ANTHROPIC_TOOLS,
                messages=[{"role": "user", "content": "给我概览"}])))))
        parsed = events(live)
        deltas = [data["delta"]["text"] for name, data in parsed
                  if name == "content_block_delta" and
                  data.get("delta", {}).get("type") == "text_delta"]
        assert "".join(deltas) == large and max(map(len, deltas)) <= 4, deltas
        assert not any(data.get("content_block", {}).get("type") == "tool_use"
                       for name, data in parsed if name == "content_block_start")
        assert sum(call.args[0] for call in sleep.call_args_list) <= 2.01

        def chunked(reply):
            return lambda *a, **k: iter([reply[:8], reply[8:]])
        with patch.object(app, "safe_chat_stream", side_effect=chunked(REPLY)):
            live = asyncio.run(collect(asyncio.run(app.messages_api(app.MessagesRequest(
                model="muse-spark", stream=True, tools=ANTHROPIC_TOOLS,
                messages=[{"role": "user", "content": "看主机名"}])))))
        parsed = events(live)
        emitted_text = "".join(data["delta"]["text"] for name, data in parsed
                               if name == "content_block_delta" and
                               data.get("delta", {}).get("type") == "text_delta")
        assert emitted_text.strip() == "先看主机名。", repr(emitted_text)
        assert "file_path" not in emitted_text
        assert any(data.get("content_block", {}).get("type") == "tool_use"
                   for name, data in parsed if name == "content_block_start")

    # 开关关闭时，正文里的 JSON 必须原样返回，不能被当成工具调用
    app.CFG.tool_protocol = False
    with patch.object(app, "safe_chat_stream", side_effect=fake_stream(REPLY)):
        off = asyncio.run(app.messages_api(app.MessagesRequest(
            model="muse-spark", tools=ANTHROPIC_TOOLS,
            messages=[{"role": "user", "content": "看主机名"}])))
        assert off["stop_reason"] == "end_turn" and off["content"] == [
            {"type": "text", "text": REPLY}], off
        off = asyncio.run(app.responses_api(app.ResponsesRequest(
            model="muse-spark", tools=RESPONSE_TOOLS, input="看主机名")))
        assert [item["type"] for item in off["output"]] == ["message"], off
        assert off["output"][0]["content"][0]["text"] == REPLY

    # 网页端没有原生 reasoning 控制，不对外声明档位，也不把客户端 effort 注入 prompt。
    listed = {item["id"]: item for item in app.models()["data"]}
    assert listed["muse-spark"]["context_window"] == 160000
    assert "reasoning" not in listed["muse-spark"]
    assert "supported_reasoning_levels" not in listed["muse-spark"]
    seen.clear()
    with patch.object(app, "safe_chat_stream", side_effect=capture):
        asyncio.run(app.chat_completions(app.ChatRequest(
            model="claude-sonnet-4", reasoning_effort="xhigh",
            messages=[{"role": "user", "content": "看主机名"}])))
        asyncio.run(app.responses_api(app.ResponsesRequest(
            model="muse-spark", reasoning={"effort": "minimal"}, input="看主机名")))
        asyncio.run(app.messages_api(app.MessagesRequest(
            model="muse-spark", output_config={"effort": "MAX"},
            messages=[{"role": "user", "content": "看主机名"}])))
    assert len(seen) == 3
    assert all("思考深度" not in prompt for prompt in seen)

    # 沙箱回执是普通文本；不额外重放整轮，避免重复执行与延迟倍增。
    sandbox = "我已经在云端沙箱里跑了命令，主机名是 htch-runtime，退出码是 0。"
    prompts = []

    def sandbox_reply(cookies, prompt, *a, **k):
        prompts.append(prompt)
        return iter([sandbox])

    app.CFG.tool_protocol = True
    with patch.object(app, "safe_chat_stream", side_effect=sandbox_reply):
        result = asyncio.run(app.chat_completions(app.ChatRequest(
            model="muse-spark", tools=[{"type": "function", "function": {
                "name": "Bash", "parameters": {"type": "object"}}}],
            messages=[{"role": "user", "content": "跑个 echo"}])))
    assert len(prompts) == 1, len(prompts)
    assert result["choices"][0]["finish_reason"] == "stop", result
    assert result["choices"][0]["message"]["content"] == sandbox

    progress = "已切到热搜榜页面，正在读取榜单内容。"
    prompts.clear()
    with patch.object(app, "safe_chat_stream", side_effect=sandbox_reply):
        result = asyncio.run(app.chat_completions(app.ChatRequest(
            model="muse-spark", tools=[{"type": "function", "function": {
                "name": "Bash", "parameters": {"type": "object"}}}],
            messages=[{"role": "user", "content": "读热搜"}])))
    assert len(prompts) == 1 and result["choices"][0]["finish_reason"] == "stop"
    assert result["choices"][0]["message"]["content"] == sandbox

    # 说明文字里碰巧出现的 JSON 不是调用；不认识的工具名也不是
    quoted = '示例：{"name": "Bash", "arguments": {"command": "rm -rf /"}}，不要执行。'
    assert app.parse_tool_calls(quoted, [{"name": "Bash"}]) == (None, quoted)
    foreign = '```tool_call\n{"name": "Other", "arguments": {}}\n```'
    assert app.parse_tool_calls(foreign, [{"name": "Bash"}])[0] is None
    both = ('先看文件。\n```tool_call\n{"name": "Read", "arguments": '
            '{"file_path": "/etc/hostname"}}\n```\n再看目录。\n```tool_call\n'
            '{"name": "Bash", "arguments": {"command": "ls"}}\n```')
    found, rest = app.parse_tool_calls(both, [{"name": "Read"}, {"name": "Bash"}])
    assert [call["function"]["name"] for call in found] == ["Read", "Bash"], found
    assert rest == "先看文件。\n\n再看目录。", repr(rest)

    inline = ('已切到实时热搜页面，正在读取榜单内容。 tool_call\n'
              '{"name":"Read","arguments":{"file_path":"/tmp/a"}}\n'
              '已切到实时热搜页面，正在读取榜单内容。 tool_call\n'
              '{"name":"Read","arguments":{"file_path":"/tmp/a"}}\n')
    found, rest = app.parse_tool_calls(inline, [{"name": "Read"}])
    assert len(found) == 1 and found[0]["function"]["name"] == "Read", found
    assert "正在读取" in rest and "tool_call" not in rest, repr(rest)

    trailing = ('tool_call\n{"name":"Bash","arguments":{"command":"echo ok"}}\n'
                '工具调用已准备好。')
    found, rest = app.parse_tool_calls(trailing, [{"name": "Bash"}])
    assert len(found) == 1 and rest == "工具调用已准备好。", (found, rest)
print("PASS: responses and messages round-trip tool calls, results, and live text")
