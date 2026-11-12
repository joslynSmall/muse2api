"""把网页模型输出解析成 OpenAI 风格 tool_calls 的独立协议模块。

只接受四种明确的调用形态：
1. ``<tool_call>JSON</tool_call>`` 标签；
2. ```` ```tool_call `` 代码块；
3. ``tool_call`` 行内标记，标记后紧跟 JSON；
4. 整段文本就是一个 JSON 对象/数组。

普通说明文字里的 JSON（包括 ```` ```json `` 代码块）不会被当成调用。
所有解析都要求标记后紧跟 JSON，不做跨标记扫描。没有工具白名单时
fail closed，返回原文而不是猜测调用。

模块只依赖标准库，便于从 app.py 独立复用和测试。
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

__all__ = ["build_tools_prompt", "build_tools_reminder", "parse_tool_calls", "tool_choice_value"]


_TOOL_PROTOCOL_HEAD = """【客户端工具说明】
用户的文件、项目和命令行都在用户自己的电脑上；你的沙箱、虚拟机、内置终端和联网搜索都碰不到那里。
凡是读写文件、执行命令、查资料、操作浏览器，都只能用下面的客户端工具。需要时输出 tool_call 代码块，然后结束回复，等客户端把结果发回来：
```tool_call
{"name": "工具名", "arguments": {参数}}
```
- 每个代码块只放一个 JSON 对象；要多个工具就写多个代码块。
- name 必须是下面清单里的名字；arguments 必须是合法 JSON 对象。
- 不要用 json 代码块表示调用，也不要编造执行结果、退出码或主机名——命令还没跑过。
- 不需要工具时，直接用自然语言回答。

可用工具（参数为 JSON Schema）：
"""

_TOOL_REMINDER_HEAD = """【客户端工具说明（沿用前文）】
工具清单和参数与前文相同。用户电脑上的操作只能通过客户端工具完成；需要时只输出 ```tool_call 代码块（块内一个 JSON：{"name": "工具名", "arguments": {参数}}），然后结束回复等待结果。不要编造执行结果；不需要工具时直接回答。
"""

# 只有工具清单里有 ToolSearch 时才追加：客户端把 MCP 等工具设为延迟加载，
# 名字只出现在系统提示里，必须先用 ToolSearch 加载才能调用。
_DEFERRED_TOOLS_NOTE = """
【延迟加载的工具】
上文系统提示里列出的其他工具（例如 mcp__ 开头的 MCP 工具）也可以用，只是还不在上面的清单里。
要用它们时，先调用 ToolSearch，参数 {"query": "select:工具全名", "max_results": 5}，然后结束回复；
客户端加载后，下一轮清单里就会出现这个工具，再正常调用它。不要因为清单里暂时没有就说它不可用。
"""

_TAG_OPEN_RE = re.compile(r"<tool_call\s*>", re.I)
_TAG_CLOSE_RE = re.compile(r"</tool_call\s*>", re.I)
_FENCE_OPEN_RE = re.compile(
    r"```[ \t]*tool_call(?![A-Za-z0-9_])[ \t]*(?:\r?\n)?", re.I)
_FENCE_CLOSE_RE = re.compile(r"```")
_INLINE_RE = re.compile(r"(?<![A-Za-z0-9_])tool_call(?![A-Za-z0-9_])", re.I)
_BARE_LABEL_RE = re.compile(r"^[ \t]*tool_call[ \t]*$", re.I | re.M)

_INVALID = object()


def tool_choice_value(tool_choice) -> str:
    """归一化 tool_choice。

    返回 ``"none"``、``"required"``、指定工具名，或空串（模型自行决定）。
    同时接受 OpenAI 的 ``none``/``required``/function 选择和 Anthropic 的
    ``none``/``auto``/``any``/``tool`` 选择。是否真正满足 required 或指定
    工具，由上层根据解析结果判断。
    """
    if isinstance(tool_choice, str):
        value = tool_choice.strip().lower()
        if value in ("none", "required"):
            return value
        if value in ("auto", "any"):
            return "" if value == "auto" else "required"
        return ""

    if not isinstance(tool_choice, dict):
        return ""

    kind = tool_choice.get("type")
    kind = kind.strip().lower() if isinstance(kind, str) else ""
    if kind == "none":
        return "none"
    if kind in ("required", "any"):
        return "required"
    if kind == "auto":
        return ""
    if kind in ("tool", "function"):
        name = tool_choice.get("name")
        if not isinstance(name, str) or not name.strip():
            nested = tool_choice.get("function")
            if isinstance(nested, dict):
                name = nested.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else ""

    nested = tool_choice.get("function")
    if isinstance(nested, dict):
        name = nested.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    name = tool_choice.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return ""


# 兼容 app.py 里的私有命名，便于逐段迁移时直接替换调用。
_tool_choice_value = tool_choice_value


def _read_schema(source: dict):
    """读取 parameters/input_schema；返回 None（未提供）或 _INVALID（格式错误）。"""
    for key in ("parameters", "input_schema"):
        if key not in source:
            continue
        value = source.get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            return value
        return _INVALID
    return None


def _normalize_tool(tool):
    """把一个工具定义归一成 {name, description, parameters}。

    兼容 Chat Completions、Responses 和 Anthropic 三种声明形态。
    内置工具（type 如 bash_20250124）和没有 JSON Schema 的 freeform custom
    工具不会进入白名单，避免把无法执行的能力伪装成 function。
    """
    if not isinstance(tool, dict):
        return None

    outer_type = tool.get("type")
    fn = tool.get("function")
    if isinstance(fn, dict):
        if outer_type not in (None, "function"):
            return None
        source = fn
    else:
        if outer_type not in (None, "function", "custom"):
            return None
        source = tool

    name = source.get("name")
    if not isinstance(name, str) or not name.strip():
        if isinstance(fn, str):
            name = fn
    if not isinstance(name, str) or not name.strip():
        return None

    schema = _read_schema(source)
    if schema is _INVALID:
        return None
    if schema is None:
        if outer_type == "custom":
            return None
        schema = {}

    description = source.get("description")
    return {
        "name": name.strip(),
        "description": description if isinstance(description, str) else "",
        "parameters": schema,
    }


def _normalize_tools(tools) -> list[dict]:
    """统一规范化工具列表；build_tools_prompt 和解析白名单共用此函数。"""
    if tools is None:
        return []
    try:
        entries = list(tools)
    except TypeError:
        return []

    normalized: dict[str, dict] = {}
    for tool in entries:
        item = _normalize_tool(tool)
        if item is not None:
            normalized.setdefault(item["name"], item)
    return list(normalized.values())


def _declared_tool_names(tools) -> set[str] | None:
    """兼容 app.py 的私有命名；无可用工具时返回 None。"""
    names = {tool["name"] for tool in _normalize_tools(tools)}
    return names or None


def _strip_schema_noise(value):
    """去掉对模型无用的元信息（$schema）；其余约束原样保留。"""
    if isinstance(value, dict):
        return {k: _strip_schema_noise(v) for k, v in value.items() if k != "$schema"}
    if isinstance(value, list):
        return [_strip_schema_noise(v) for v in value]
    return value


def _format_schema(schema: dict) -> str:
    """单行紧凑 JSON：保留嵌套结构、enum、items 等全部约束，缩进只占篇幅不增信息。"""
    if not schema:
        return "（无参数）"
    return json.dumps(_strip_schema_noise(schema), ensure_ascii=False, separators=(",", ":"))


def _choice_note(pick: str) -> str:
    if pick == "required":
        return "这一轮必须至少调用一个工具，不要直接回答。\n"
    if pick:
        return "这一轮必须调用工具 %s。\n" % pick
    return ""


def tools_signature(tools) -> str:
    """工具清单指纹：同一会话里清单不变时，续轮只需发简短提醒。"""
    normalized = _normalize_tools(tools)
    if not normalized:
        return ""
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_tools_prompt(tools, tool_choice=None) -> str:
    """把工具定义翻译成提示词片段；无可用工具时返回空串。"""
    pick = tool_choice_value(tool_choice)
    if pick == "none":
        return ""

    normalized = _normalize_tools(tools)
    if not normalized:
        return ""

    items = []
    for index, tool in enumerate(normalized, 1):
        desc = (" — " + tool["description"]) if tool["description"] else ""
        items.append("%d. %s%s\n   参数：%s" % (
            index, tool["name"], desc, _format_schema(tool["parameters"])))

    deferred = _DEFERRED_TOOLS_NOTE if any(t["name"] == "ToolSearch" for t in normalized) else ""
    return _TOOL_PROTOCOL_HEAD + _choice_note(pick) + "\n".join(items) + "\n" + deferred


def build_tools_reminder(tools, tool_choice=None) -> str:
    """续轮且工具清单与本 thread 上次发送的完全相同时使用的简短提醒。

    完整说明和参数定义已在同一 thread 的前文里；这里只重申调用格式和可用工具名，
    避免每轮重发几万字的清单。
    """
    pick = tool_choice_value(tool_choice)
    if pick == "none":
        return ""
    normalized = _normalize_tools(tools)
    if not normalized:
        return ""
    lines = []
    for tool in normalized:
        schema = tool["parameters"] if isinstance(tool["parameters"], dict) else {}
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        required = set(schema.get("required") or [])
        # 参数名（* 为必填）留作提示：长对话里模型最容易记错的正是参数名。
        args = ", ".join(k + ("*" if k in required else "") for k in props)
        lines.append("- %s(%s)" % (tool["name"], args))
    return (_TOOL_REMINDER_HEAD + _choice_note(pick)
            + "可用工具（* 为必填参数，完整定义见前文）：\n" + "\n".join(lines) + "\n")


def _canonical_arguments(value):
    """把 arguments 校验为 JSON object，并按排序键序列化用于去重。"""
    if value is None:
        value = {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return None
    if not isinstance(value, dict):
        return None
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return None


def _call_from_item(item, names: set[str]):
    if not isinstance(item, dict):
        return None

    fn = item.get("function")
    name = item.get("tool")
    if not isinstance(name, str) or not name.strip():
        name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        if isinstance(fn, str):
            name = fn
    if not isinstance(name, str) or not name.strip():
        if isinstance(fn, dict):
            name = fn.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    name = name.strip()
    if name not in names:
        return None

    args = item.get("arguments")
    if args is None and isinstance(fn, dict):
        args = fn.get("arguments")
    if args is None:
        args = item.get("parameters")
    if args is None:
        args = item.get("input")
    if args is None:
        args = item.get("args")

    canonical = _canonical_arguments(args)
    if canonical is None:
        return None
    return {
        "id": "call_" + uuid.uuid4().hex[:20],
        "type": "function",
        "function": {"name": name, "arguments": canonical},
    }


def _as_tool_calls(obj, names: set[str] | None = None):
    """把 JSON 值转成 tool_calls；任一调用不合法就整体拒绝。"""
    if not names:
        return None
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None

    out = []
    for item in raw:
        call = _call_from_item(item, names)
        if call is None:
            return None
        out.append(call)
    return out or None


def _decode_at(text: str, pos: int, names: set[str],
               decoder: json.JSONDecoder):
    """从 pos 起跳过空白后必须紧跟 JSON；返回 (calls, JSON 结束位置)。"""
    size = len(text)
    while pos < size and text[pos].isspace():
        pos += 1
    if pos >= size or text[pos] not in "{[":
        return None, None
    try:
        obj, end = decoder.raw_decode(text, pos)
    except json.JSONDecodeError:
        return None, None
    found = _as_tool_calls(obj, names)
    return (found, end) if found else (None, None)


def _scan_delimited(text: str, open_re, close_re, names: set[str],
                    decoder: json.JSONDecoder, priority: int,
                    candidates: list) -> None:
    """扫描标签/代码块：JSON 后找闭合标记，且不跨过下一个开启标记。"""
    for match in open_re.finditer(text):
        found, json_end = _decode_at(text, match.end(), names, decoder)
        if not found:
            continue
        close = close_re.search(text, json_end)
        if not close:
            continue
        next_open = open_re.search(text, json_end)
        # 代码块的闭合 ``` 可能正是下一个开启标记的三反引号；这种未闭合
        # 情况必须拒绝，不能把下一个标记的起始当成本块的结束。
        if next_open and next_open.start() < close.end():
            continue
        candidates.append((match.start(), close.end(), priority, found))


def _json_start(text: str, pos: int):
    """行内标记后必须紧跟（允许空白和冒号）JSON，不能跨标记扫描。"""
    size = len(text)
    while pos < size and text[pos].isspace():
        pos += 1
    if pos < size and text[pos] in (":", "："):
        pos += 1
        while pos < size and text[pos].isspace():
            pos += 1
    if pos < size and text[pos] in "{[":
        return pos
    return None


def _whole_text_call(text: str, names: set[str], decoder: json.JSONDecoder):
    """整段文本恰好是一个 JSON 值时接受；纯 JSON 但不是调用则不再扫描内部。"""
    start = len(text) - len(text.lstrip())
    if start >= len(text) or text[start] not in "{[":
        return None
    try:
        obj, end = decoder.raw_decode(text, start)
    except json.JSONDecodeError:
        return None
    if text[end:].strip():
        return None
    found = _as_tool_calls(obj, names)
    return (found, "") if found else (None, text)


def _add_calls(calls: list[dict], found: list[dict]) -> None:
    seen = {(call["function"]["name"], call["function"]["arguments"])
            for call in calls}
    for call in found:
        key = (call["function"]["name"], call["function"]["arguments"])
        if key not in seen:
            calls.append(call)
            seen.add(key)


def _strip_spans(text: str, spans: list[tuple[int, int]]) -> str:
    parts = []
    cursor = 0
    for start, end in sorted(set(spans)):
        if start > cursor:
            parts.append(text[cursor:start])
        cursor = max(cursor, end)
    parts.append(text[cursor:])

    rest = "\n".join(parts)
    rest = _BARE_LABEL_RE.sub("", rest)
    rest = re.sub(r"\n{3,}", "\n\n", rest)
    return rest.strip()


class ToolStreamSplitter:
    """透传普通文本，发现工具标记后暂存余下内容供最终解析。"""

    _MARKERS = ("```tool_call", "<tool_call", "tool_call")

    def __init__(self):
        self.pending = ""
        self.held = False

    def feed(self, chunk: str) -> str:
        if self.held:
            self.pending += chunk
            return ""

        self.pending += chunk
        positions = [self.pending.lower().find(marker)
                     for marker in self._MARKERS]
        positions = [pos for pos in positions if pos >= 0]
        if positions:
            pos = min(positions)
            ready = self.pending[:pos]
            self.pending = self.pending[pos:]
            self.held = True
            return ready

        # Retain only a suffix that could be the beginning of a marker split
        # across chunks; ordinary text is forwarded without a fixed-size delay.
        lower = self.pending.lower()
        keep = 0
        for marker in self._MARKERS:
            limit = min(len(marker) - 1, len(lower))
            for size in range(limit, 0, -1):
                if lower.endswith(marker[:size]):
                    keep = max(keep, size)
                    break
        ready = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else ""
        return ready

    def finish(self, tools=None) -> tuple[str, list[dict] | None]:
        if not self.held:
            remainder, self.pending = self.pending, ""
            return remainder, None
        calls, rest = parse_tool_calls(self.pending, tools)
        return rest, calls


__all__.append("ToolStreamSplitter")


def parse_tool_calls(text: str, tools=None) -> tuple[list[dict] | None, str]:
    """从网页文本提取工具调用，返回 ``(calls, 去掉调用片段后的正文)``。

    ``tools`` 同时决定白名单：没有工具或没有任何受支持的 function 工具时
    fail closed，返回 ``(None, text)``。标签、代码块和行内标记按出现顺序
    合并；重叠的候选只保留最先出现的那个，避免同一段 JSON 被重复执行。
    """
    if not isinstance(text, str) or not text:
        return None, text

    names = {tool["name"] for tool in _normalize_tools(tools)}
    if not names:
        return None, text

    decoder = json.JSONDecoder()

    # 纯 JSON 优先处理：如果整段就是一个 JSON 值，就不再到字符串内部找标记。
    whole = _whole_text_call(text, names, decoder)
    if whole is not None:
        return whole

    candidates = []
    opening_spans = [(match.start(), match.end())
                     for match in _TAG_OPEN_RE.finditer(text)]
    opening_spans.extend((match.start(), match.end())
                         for match in _FENCE_OPEN_RE.finditer(text))
    _scan_delimited(text, _TAG_OPEN_RE, _TAG_CLOSE_RE, names, decoder, 0,
                    candidates)
    _scan_delimited(text, _FENCE_OPEN_RE, _FENCE_CLOSE_RE, names, decoder, 1,
                    candidates)
    for match in _INLINE_RE.finditer(text):
        # 开启标记里的 tool_call 是标签/代码块的一部分，不能降级成行内标记。
        if any(start <= match.start() < end
               for start, end in opening_spans):
            continue
        start = _json_start(text, match.end())
        if start is None:
            continue
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        found = _as_tool_calls(obj, names)
        if found:
            candidates.append((match.start(), end, 2, found))

    if not candidates:
        return None, text

    candidates.sort(key=lambda item: (item[0], item[2]))
    calls: list[dict] = []
    spans: list[tuple[int, int]] = []
    occupied: list[tuple[int, int]] = []
    for start, end, _priority, found in candidates:
        if any(start < used_end and end > used_start
               for used_start, used_end in occupied):
            continue
        occupied.append((start, end))
        spans.append((start, end))
        _add_calls(calls, found)

    if not calls:
        return None, text
    return calls, _strip_spans(text, spans)
