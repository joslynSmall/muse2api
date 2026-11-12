"""tool_protocol 独立解析器的回归测试。

运行：python tests/test_tool_protocol_parser.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tool_protocol as tp  # noqa: E402

TOOLS = [
    {"type": "function", "function": {
        "name": "Bash", "description": "run a command", "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"]}}},
    {"type": "function", "name": "Read", "description": "read a file",
     "parameters": {"type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"]}},
    {"name": "AnthropicTool", "description": "anthropic custom",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string"}}}},
]


def names(calls):
    return [call["function"]["name"] for call in calls]


def arguments(call):
    return json.loads(call["function"]["arguments"])


def test_tool_choice_value():
    assert tp.tool_choice_value(None) == ""
    assert tp.tool_choice_value("none") == "none"
    assert tp.tool_choice_value("required") == "required"
    assert tp.tool_choice_value("auto") == ""
    assert tp.tool_choice_value({"type": "none"}) == "none"
    assert tp.tool_choice_value({"type": "any"}) == "required"
    assert tp.tool_choice_value({"type": "auto"}) == ""
    assert tp.tool_choice_value({"type": "tool", "name": "Read"}) == "Read"
    assert tp.tool_choice_value(
        {"type": "function", "function": {"name": "Bash"}}) == "Bash"
    assert tp.tool_choice_value({"type": "function", "name": "Read"}) == "Read"
    assert tp.tool_choice_value({"name": "Read"}) == "Read"
    assert tp.tool_choice_value({"function": {"name": "Read"}}) == "Read"
    assert tp.tool_choice_value({"type": "tool"}) == ""
    assert tp.tool_choice_value("unknown") == ""


def test_build_tools_prompt_shapes_and_choice():
    prompt = tp.build_tools_prompt(TOOLS)
    assert prompt
    assert "Bash" in prompt and "Read" in prompt and "AnthropicTool" in prompt
    assert "file_path" in prompt and "run a command" in prompt

    assert tp.build_tools_prompt(None) == ""
    assert tp.build_tools_prompt([]) == ""
    assert tp.build_tools_prompt(TOOLS, "none") == ""
    assert tp.build_tools_prompt(TOOLS, {"type": "none"}) == ""

    required = tp.build_tools_prompt(TOOLS, "required")
    assert "这一轮必须至少调用一个工具" in required
    assert "这一轮必须至少调用一个工具" in tp.build_tools_prompt(
        TOOLS, {"type": "any"})
    assert "这一轮必须调用工具 Read" in tp.build_tools_prompt(
        TOOLS, {"type": "tool", "name": "Read"})
    assert "这一轮必须调用工具 Bash" in tp.build_tools_prompt(
        TOOLS, {"type": "function", "function": {"name": "Bash"}})

    auto = tp.build_tools_prompt(TOOLS, {"type": "auto"})
    assert "这一轮必须" not in auto and "Bash" in auto


def test_full_schema_is_preserved():
    nested = [{"name": "Configure", "parameters": {
        "type": "object",
        "properties": {
            "options": {
                "type": "object",
                "properties": {
                    "mode": {"type": "string", "enum": ["fast", "safe"]}},
                "required": ["mode"]},
            "tags": {"type": "array", "items": {"type": "string"}}}}}]
    prompt = tp.build_tools_prompt(nested)
    for token in ('"options"', '"mode"', '"enum"', '"fast"', '"safe"',
                  '"tags"', '"items"'):
        assert token in prompt, token


def test_unsupported_tools_are_not_functions():
    unsupported = [
        {"type": "bash_20250124", "name": "bash"},
        {"type": "custom", "name": "freeform", "description": "no schema"},
    ]
    assert tp.build_tools_prompt(unsupported) == ""
    tag = '<tool_call>{"name": "bash", "arguments": {}}</tool_call>'
    assert tp.parse_tool_calls(tag, unsupported) == (None, tag)
    tag = '<tool_call>{"name": "freeform", "arguments": {}}</tool_call>'
    assert tp.parse_tool_calls(tag, unsupported) == (None, tag)

    # 带 JSON Schema 的 custom 工具不是 freeform，可以正常参与协议。
    structured = [{"type": "custom", "name": "shell", "parameters": {
        "type": "object", "properties": {"cmd": {"type": "string"}}}}]
    assert "shell" in tp.build_tools_prompt(structured)
    found, _ = tp.parse_tool_calls(
        '<tool_call>{"name": "shell", "arguments": {"cmd": "ls"}}</tool_call>',
        structured)
    assert found and names(found) == ["shell"]


def test_fail_closed_without_tools():
    text = '<tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call>'
    assert tp.parse_tool_calls(text, None) == (None, text)
    assert tp.parse_tool_calls(text, []) == (None, text)
    assert tp.parse_tool_calls(text, [{}]) == (None, text)
    assert tp.parse_tool_calls(
        text, [{"type": "bash_20250124", "name": "bash"}]) == (None, text)


def test_stream_splitter_flushes_prose_and_holds_tool_call():
    splitter = tp.ToolStreamSplitter()
    prose = "自然语言回复。" * 10
    assert splitter.feed(prose) == prose
    assert splitter.feed("后续说明。 tool_") == "后续说明。 "
    assert splitter.feed('call\n{"name":"Read","arguments":{}}') == ""
    text, calls = splitter.finish([{"name": "Read"}])
    assert text.strip() == ""
    assert [call["function"]["name"] for call in calls] == ["Read"]


def test_explicit_call_forms():
    tag = ('<tool_call>{"name": "Read", "arguments": '
           '{"file_path": "/etc/hostname"}}</tool_call>')
    found, rest = tp.parse_tool_calls(tag, TOOLS)
    assert names(found) == ["Read"] and rest == ""
    assert arguments(found[0]) == {"file_path": "/etc/hostname"}

    fence = ('```tool_call\n{"name": "Bash", "arguments": '
             '{"command": "ls"}}\n```')
    found, rest = tp.parse_tool_calls(fence, TOOLS)
    assert names(found) == ["Bash"] and rest == ""

    inline = ('已切到实时热搜页面，正在读取榜单内容。 tool_call\n'
              '{"name":"Read","arguments":{"file_path":"/tmp/a"}}')
    found, rest = tp.parse_tool_calls(inline, TOOLS)
    assert names(found) == ["Read"]
    assert rest == "已切到实时热搜页面，正在读取榜单内容。"

    inline_colon = 'tool_call: {"name": "Read", "arguments": {"file_path": "/a"}}'
    found, rest = tp.parse_tool_calls(inline_colon, TOOLS)
    assert names(found) == ["Read"] and rest == ""

    whole = '{"name": "Read", "arguments": {"file_path": "/a"}}'
    found, rest = tp.parse_tool_calls(whole, TOOLS)
    assert names(found) == ["Read"] and rest == ""

    whole_array = ('[{"name": "Read", "arguments": {"file_path": "/a"}},'
                   '{"name": "Bash", "arguments": {"command": "ls"}}]')
    found, rest = tp.parse_tool_calls(whole_array, TOOLS)
    assert names(found) == ["Read", "Bash"] and rest == ""


def test_stream_compatible_prose_and_label():
    stream_tools = [{"type": "function", "function": {
        "name": "Bash", "description": "run", "parameters": {
            "type": "object", "properties": {"command": {"type": "string"}},
            "required": ["command"]}}}]
    reply = ('先查现状，确认缺哪些字段再改。\n\ntool_call\n'
             '{"name": "Bash", "arguments": {"command": "ls -l \\"/tmp/a b\\""}}')
    found, rest = tp.parse_tool_calls(reply, stream_tools)
    assert len(found) == 1 and names(found) == ["Bash"]
    assert arguments(found[0]) == {"command": 'ls -l "/tmp/a b"'}
    assert rest == "先查现状，确认缺哪些字段再改。"


def test_prose_json_is_not_executed():
    quoted = ('示例：{"name": "Bash", "arguments": '
              '{"command": "rm -rf /"}}，不要执行。')
    assert tp.parse_tool_calls(quoted, TOOLS) == (None, quoted)

    json_fence = ('说明：\n```json\n'
                  '{"name": "Bash", "arguments": {"command": "rm -rf /"}}\n'
                  '```\n不要执行。')
    assert tp.parse_tool_calls(json_fence, TOOLS) == (None, json_fence)

    plain_fence = '```\n{"name": "Bash", "arguments": {}}\n```'
    assert tp.parse_tool_calls(plain_fence, TOOLS) == (None, plain_fence)

    # 有真正的 tool_call 时，说明文字里的 json 代码块只保留在正文中。
    mixed = ('示例：\n```json\n'
             '{"name": "Bash", "arguments": {"command": "rm -rf /"}}\n'
             '```\n```tool_call\n'
             '{"name": "Read", "arguments": {"file_path": "/a"}}\n```')
    found, rest = tp.parse_tool_calls(mixed, TOOLS)
    assert names(found) == ["Read"]
    assert "```json" in rest and "rm -rf /" in rest
    assert "```tool_call" not in rest


def test_marker_must_be_followed_immediately_by_json():
    text = ('tool_call\n先说明一下\n'
            '{"name": "Bash", "arguments": {"command": "ls"}}')
    assert tp.parse_tool_calls(text, TOOLS) == (None, text)

    text = ('<tool_call>\n说明\n'
            '{"name": "Bash", "arguments": {"command": "ls"}}\n</tool_call>')
    assert tp.parse_tool_calls(text, TOOLS) == (None, text)

    text = ('```tool_call\n说明\n'
            '{"name": "Bash", "arguments": {"command": "ls"}}\n```')
    assert tp.parse_tool_calls(text, TOOLS) == (None, text)


def test_unknown_and_malformed_calls_are_rejected():
    foreign = '```tool_call\n{"name": "Other", "arguments": {}}\n```'
    assert tp.parse_tool_calls(foreign, TOOLS) == (None, foreign)

    builtin_name = '<tool_call>{"name": "bash", "arguments": {}}</tool_call>'
    assert tp.parse_tool_calls(builtin_name, TOOLS) == (None, builtin_name)

    malformed = '<tool_call>{"name": "Bash", "arguments": </tool_call>'
    assert tp.parse_tool_calls(malformed, TOOLS) == (None, malformed)

    # 一个数组里只要有一个非法调用，整批拒绝，避免部分执行。
    partial = ('<tool_call>[{"name": "Bash", "arguments": {"command": "ls"}},'
               '{"name": "Other", "arguments": {}}]</tool_call>')
    assert tp.parse_tool_calls(partial, TOOLS) == (None, partial)


def test_arguments_must_be_json_object():
    # 字符串形式的 arguments 需要解码并验证为 object。
    call = {"name": "Bash", "arguments": json.dumps({"command": "ls"})}
    text = "<tool_call>" + json.dumps(call) + "</tool_call>"
    found, _ = tp.parse_tool_calls(text, TOOLS)
    assert arguments(found[0]) == {"command": "ls"}

    # parameters/args/input 是兼容别名。
    for key in ("parameters", "args", "input"):
        text = ('<tool_call>{"name": "Bash", "%s": {"command": "ls"}}'
                '</tool_call>' % key)
        found, _ = tp.parse_tool_calls(text, TOOLS)
        assert arguments(found[0]) == {"command": "ls"}, key

    # 缺省或 null 参数按空对象处理。
    for payload in ({"name": "Bash"},
                    {"name": "Bash", "arguments": None}):
        text = "<tool_call>" + json.dumps(payload) + "</tool_call>"
        found, _ = tp.parse_tool_calls(text, TOOLS)
        assert arguments(found[0]) == {}

    for bad in ("not json", "[1, 2]", "123", "null", "true"):
        call = {"name": "Bash", "arguments": bad}
        text = "<tool_call>" + json.dumps(call) + "</tool_call>"
        assert tp.parse_tool_calls(text, TOOLS) == (None, text), bad

    for bad in ([1, 2], 123, True):
        call = {"name": "Bash", "arguments": bad}
        text = "<tool_call>" + json.dumps(call) + "</tool_call>"
        assert tp.parse_tool_calls(text, TOOLS) == (None, text), bad


def test_sorted_arguments_dedupe():
    duplicate = (
        '<tool_call>{"name": "Bash", "arguments": {"b": 2, "a": 1}}</tool_call>\n'
        '<tool_call>{"name": "Bash", "arguments": {"a": 1, "b": 2}}</tool_call>')
    found, rest = tp.parse_tool_calls(duplicate, TOOLS)
    assert len(found) == 1 and rest == ""
    assert found[0]["function"]["arguments"] == json.dumps(
        {"a": 1, "b": 2}, ensure_ascii=False, sort_keys=True)


def test_mixed_forms_keep_text_order_and_do_not_overlap():
    mixed = (
        '```tool_call\n{"name": "Bash", "arguments": {"command": "ls"}}\n```\n'
        '<tool_call>{"name": "Read", "arguments": {"file_path": "/a"}}'
        '</tool_call>\n'
        'tool_call\n{"name": "AnthropicTool", "arguments": {"path": "/b"}}')
    found, rest = tp.parse_tool_calls(mixed, TOOLS)
    assert names(found) == ["Bash", "Read", "AnthropicTool"], found
    assert rest == ""

    both = ('先看文件。\n```tool_call\n'
            '{"name": "Read", "arguments": {"file_path": "/a"}}\n```\n'
            '再看目录。\n```tool_call\n'
            '{"name": "Bash", "arguments": {"command": "ls"}}\n```')
    found, rest = tp.parse_tool_calls(both, TOOLS)
    assert names(found) == ["Read", "Bash"]
    assert rest == "先看文件。\n\n再看目录。"

    # 标签内部再出现行内标记时，标签整体优先，内部标记不重复执行。
    overlap = ('<tool_call>{"name": "Read", "arguments": {"file_path": "/a"}}\n'
               'tool_call\n{"name": "Bash", "arguments": {"command": "ls"}}'
               '</tool_call>')
    found, rest = tp.parse_tool_calls(overlap, TOOLS)
    assert names(found) == ["Read"], found
    assert rest == ""


def test_json_content_may_contain_marker_text():
    fence = ('```tool_call\n'
             '{"name": "Bash", "arguments": {"command": "echo ```"}}\n```')
    found, rest = tp.parse_tool_calls(fence, TOOLS)
    assert names(found) == ["Bash"] and rest == ""
    assert arguments(found[0]) == {"command": "echo ```"}

    tag = ('<tool_call>{"name": "Bash", "arguments": '
           '{"command": "echo </tool_call>"}}</tool_call>')
    found, rest = tp.parse_tool_calls(tag, TOOLS)
    assert names(found) == ["Bash"] and rest == ""
    assert arguments(found[0]) == {"command": "echo </tool_call>"}


def test_unclosed_marker_does_not_consume_next_marker():
    text = ('<tool_call>{"name": "Read", "arguments": {"file_path": "/a"}}\n'
            '<tool_call>{"name": "Bash", "arguments": {"command": "ls"}}'
            '</tool_call>')
    found, _ = tp.parse_tool_calls(text, TOOLS)
    assert names(found) == ["Bash"], found

    text = ('```tool_call\n'
            '{"name": "Read", "arguments": {"file_path": "/a"}}\n'
            '```tool_call\n'
            '{"name": "Bash", "arguments": {"command": "ls"}}\n```')
    found, _ = tp.parse_tool_calls(text, TOOLS)
    assert names(found) == ["Bash"], found


def test_trailing_text_and_duplicate_inline_markers():
    trailing = ('tool_call\n'
                '{"name":"Bash","arguments":{"command":"echo ok"}}\n'
                '工具调用已准备好。')
    found, rest = tp.parse_tool_calls(trailing, TOOLS)
    assert len(found) == 1 and rest == "工具调用已准备好。"

    inline = ('已切到实时热搜页面，正在读取榜单内容。 tool_call\n'
              '{"name":"Read","arguments":{"file_path":"/tmp/a"}}\n'
              '已切到实时热搜页面，正在读取榜单内容。 tool_call\n'
              '{"name":"Read","arguments":{"file_path":"/tmp/a"}}\n')
    found, rest = tp.parse_tool_calls(inline, TOOLS)
    assert len(found) == 1 and names(found) == ["Read"]
    assert "正在读取" in rest and "tool_call" not in rest


def main():
    tests = [value for key, value in sorted(globals().items())
             if key.startswith("test_") and callable(value)]
    for test in tests:
        test()
    print("PASS: %d tool protocol parser tests" % len(tests))


if __name__ == "__main__":
    main()
