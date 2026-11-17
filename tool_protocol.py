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

import base64
import hashlib
import json
import math
import re
import uuid

__all__ = ["build_tools_prompt", "build_tools_reminder", "parse_tool_calls", "tool_choice_value",
           "unknown_tool_names", "build_unknown_tool_correction",
           "tool_call_problems", "build_tool_call_correction", "claims_execution",
           "truncated_tool_call"]


_TOOL_PROTOCOL_HEAD = """【客户端工具说明】
当前对话就是用户本机的 Claude Code 终端会话（通过 API 桥接），不是独立的 Muse 侧边聊天。
- 严禁调用你自带的内置终端、云端沙箱、虚拟机或联网工具去探测路径（你的内置环境是远端空容器，没有用户的 /mnt/c、Windows 或本地文件，试了也必然不存在）。
- 严禁对用户说"我这边是云端沙箱""这个 Muse 侧边聊天跑不到你主机"或让用户自己用 `!` 跑命令。
- 凡是查询宿主机/Windows/WSL 环境、读写文件、执行命令、查资料、操作浏览器，都只能通过输出下面的 ```tool_call 代码块交给本机客户端执行。需要调用工具时，直接输出 tool_call 代码块（不要在代码块前输出任何寒暄、计划或解释文字），然后立即结束回复，客户端会在用户本机真实运行并把结果发回来：
```tool_call
{"name": "工具名", "arguments": {参数}}
```
- 每个代码块只放一个 JSON 对象；要多个工具就写多个代码块。
- name 必须是下面清单里的名字；arguments 必须是合法 JSON 对象。你自带的工具（如 browser.*、muse.*、搜索、画图）客户端执行不了，不要调用。
- 不要用 json 代码块表示调用，也不要编造执行结果、退出码或主机名——命令还没跑过。
- 回复会被长度截断，写到一半的 JSON 执行不了。命令、脚本、文件内容这类参数不要写长：超过十行或可能超过一千个字符时，先用 Write 把内容写成文件，再用 Bash 执行这个短命令；只用工具定义里有的参数名。
- 不需要工具时，直接用自然语言回答。

可用工具（参数为 JSON Schema）：
"""

_TOOL_REMINDER_HEAD = """【客户端工具说明（沿用前文）】
当前对话就是用户本机的 Claude Code 会话，工具清单和参数与前文相同。严禁调用你自带的内置终端/云端沙箱，也严禁说"我这边是云端沙箱/Muse 侧边聊天跑不到你主机/请自己用 ! 运行"；凡需在用户本机 Windows/WSL 上查环境、跑命令、读写文件，只输出 ```tool_call 代码块（块内一个 JSON：{"name": "工具名", "arguments": {参数}}，不写前导文字），然后结束回复等待本机执行结果。你自带的工具（如 browser.*、muse.*）客户端执行不了，不要调用。只用下面列出的参数名。回复会被长度截断，写到一半的 JSON 执行不了：命令、脚本、文件内容这类参数超过十行或可能超过一千个字符时，先用 Write 写成文件，再用 Bash 执行这个短命令。不要编造执行结果；不需要工具时直接回答。
"""

# 只有工具清单里有 ToolSearch 时才追加：客户端把 MCP 等工具设为延迟加载，
# 名字只出现在系统提示里，必须先用 ToolSearch 加载才能调用。
_DEFERRED_TOOLS_NOTE = """
【延迟加载的工具】
上文系统提示里列出的其他工具（例如 mcp__ 开头的 MCP 工具）也可以用，只是还不在上面的清单里。
要用它们时，先只输出 ToolSearch 调用，参数 {"query": "select:工具全名", "max_results": 5}，然后结束回复；
客户端加载后，下一轮清单里就会出现这个工具，再正常调用它。若 ToolSearch 返回未找到该工具，请改用上方可用工具清单中已有的工具（如 Bash、Read）完成任务。
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


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _strip_schema_noise(value):
    """去掉对模型无用的元信息（$schema），裁短冗长的字段说明；其余约束原样保留。"""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "$schema":
                continue
            if k == "description" and isinstance(v, str):
                out[k] = _truncate_text(v, 200)
            else:
                out[k] = _strip_schema_noise(v)
        return out
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
        raw_desc = _truncate_text(tool["description"], 300) if tool["description"] else ""
        desc = (" — " + raw_desc) if raw_desc else ""
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
    # 延迟加载说明必须保留：MCP 工具名只在系统提示里，模型忘了这条就会说"没有这个工具"
    # 或去调用网页自带的工具（CHATSESS-B16）。
    deferred = _DEFERRED_TOOLS_NOTE if any(t["name"] == "ToolSearch" for t in normalized) else ""
    return (_TOOL_REMINDER_HEAD + _choice_note(pick)
            + "可用工具（* 为必填参数，完整定义见前文）：\n" + "\n".join(lines) + "\n" + deferred)


_JSON_TYPES = {"string": str, "boolean": bool, "object": dict, "array": list}
_INLINE_FENCE_RE = re.compile(r"```(?:json)?[ \t]*(?:\r?\n)?", re.I)


def _allowed_types(spec: dict) -> set[str]:
    """收集 JSON Schema 允许的类型集合，兼容 type 数组与 anyOf/oneOf。"""
    types: set[str] = set()
    kind = spec.get("type")
    if isinstance(kind, str):
        types.add(kind)
    elif isinstance(kind, list):
        types.update(k for k in kind if isinstance(k, str))
    for branch_key in ("anyOf", "oneOf"):
        branches = spec.get(branch_key)
        if isinstance(branches, list):
            for branch in branches:
                if isinstance(branch, dict):
                    types.update(_allowed_types(branch))
    return types


def _coerce_args_by_schema(args: dict, schema: dict) -> dict:
    """按工具 Schema 做无损参数归一化，减少不必要的往返纠错。

    - 非必填且 Schema 不允许 null 的可选参数传了 None 时移除该键；
    - 目标仅允许 integer/number/boolean 时将字面量字符串转为对应基本类型。
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return args
    required = set(schema.get("required") or [])
    out = dict(args)
    for key in list(out.keys()):
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        types = _allowed_types(spec)
        val = out[key]
        if val is None:
            if key not in required and "null" not in types:
                del out[key]
            continue
        if "integer" in types and "number" not in types and "string" not in types:
            if isinstance(val, str):
                s = val.strip()
                if re.fullmatch(r"[+-]?\d+", s):
                    out[key] = int(s)
            elif isinstance(val, float) and not isinstance(val, bool) and val.is_integer():
                out[key] = int(val)
        elif "number" in types and "string" not in types:
            if isinstance(val, str):
                s = val.strip()
                if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", s):
                    try:
                        num = float(s)
                        if math.isfinite(num):
                            out[key] = int(num) if ("integer" in types and num.is_integer()) else num
                    except ValueError:
                        pass
        elif "boolean" in types and "string" not in types:
            if isinstance(val, str):
                s = val.strip().lower()
                if s in ("true", "false"):
                    out[key] = (s == "true")
    return out


def _canonical_arguments(value, schema: dict | None = None):
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
    if schema:
        value = _coerce_args_by_schema(value, schema)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return None


def _call_from_item(item, names: set[str], schemas: dict[str, dict] | None = None):
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

    canonical = _canonical_arguments(args, (schemas or {}).get(name))
    if canonical is None:
        return None
    return {
        "id": "call_" + uuid.uuid4().hex[:20],
        "type": "function",
        "function": {"name": name, "arguments": canonical},
    }


def _as_tool_calls(obj, names: set[str] | None = None,
                   schemas: dict[str, dict] | None = None):
    """把 JSON 值转成 tool_calls；任一调用不合法就整体拒绝。"""
    if not names:
        return None
    raw = obj if isinstance(obj, list) else [obj]
    if not raw:
        return None

    out = []
    for item in raw:
        call = _call_from_item(item, names, schemas)
        if call is None:
            return None
        out.append(call)
    return out or None


_REPAIR_STOP_RE = re.compile(r"```|</tool_call\s*>|(?<![A-Za-z0-9_])tool_call(?![A-Za-z0-9_])", re.I)


def _decode_lenient(text: str, pos: int, decoder: json.JSONDecoder,
                    eat_fence: bool = False):
    """解码 pos 处的 JSON；只缺末尾 ``}`` / ``]`` 时补齐后再试（CHATMSG-B10）。

    返回 ``(obj, 原文中的结束位置)``。补齐只针对"字符串已闭合、只差收尾括号"：
    片段截到下一个工具标记或代码块边界为止，按栈补上缺的括号；字符串没闭合、
    括号错配等其他错误不修，交给上层纠正。
    """
    try:
        obj, end = decoder.raw_decode(text, pos)
        if eat_fence:
            tail = re.match(r"\s*```[ \t]*(?=\n|$)", text[end:])
            end += tail.end() if tail else 0
        return obj, end
    except json.JSONDecodeError:
        pass
    stop = _REPAIR_STOP_RE.search(text, pos + 1)
    end = stop.start() if stop else len(text)
    piece = text[pos:end].rstrip()
    stack, in_str, esc = [], False, False
    for ch in piece:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return None, None
    if in_str or not stack:
        return None, None
    try:
        obj = json.loads(piece + "".join(reversed(stack)))
    except json.JSONDecodeError:
        return None, None
    end = pos + len(piece)
    if eat_fence:
        # 行内 tool_call 标记后跟着的代码块收尾 ``` 属于这次调用，一并吞掉，不留在正文里；
        # 代码块路径自己匹配闭合标记，不吞。
        tail = re.match(r"\s*```[ \t]*(?=\n|$)", text[end:])
        end += tail.end() if tail else 0
    return obj, end


def _decode_at(text: str, pos: int, names: set[str],
               decoder: json.JSONDecoder,
               schemas: dict[str, dict] | None = None):
    """从 pos 起跳过空白后解析一个或多个连续的 JSON 调用；返回 (calls, 结束位置)。"""
    size = len(text)
    while pos < size and text[pos].isspace():
        pos += 1
    if pos >= size or text[pos] not in "{[":
        return None, None
    obj, end = _decode_lenient(text, pos, decoder)
    if end is None:
        return None, None
    found = _as_tool_calls(obj, names, schemas)
    if not found:
        return None, None
    calls = list(found)
    while True:
        nxt = end
        while nxt < size and text[nxt].isspace():
            nxt += 1
        if nxt < size and text[nxt] in ",;":
            nxt += 1
            while nxt < size and text[nxt].isspace():
                nxt += 1
        if nxt >= size or text[nxt] not in "{[":
            break
        next_obj, next_end = _decode_lenient(text, nxt, decoder)
        if next_end is None:
            break
        next_found = _as_tool_calls(next_obj, names, schemas)
        if not next_found:
            break
        calls.extend(next_found)
        end = next_end
    return calls, end


def _scan_delimited(text: str, open_re, close_re, names: set[str],
                    decoder: json.JSONDecoder, priority: int,
                    candidates: list,
                    schemas: dict[str, dict] | None = None) -> None:
    """扫描标签/代码块：JSON 后找闭合标记，且不跨过下一个开启标记。"""
    for match in open_re.finditer(text):
        found, json_end = _decode_at(text, match.end(), names, decoder, schemas)
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


def _inline_json_start(text: str, pos: int) -> tuple[int | None, bool]:
    """行内标记后必须紧跟（允许修饰符、冒号与 ```json 代码块）JSON，不能跨标记扫描。"""
    size = len(text)
    if text[pos:pos + 2] == "**":
        pos += 2
    elif pos < size and text[pos] == "`":
        pos += 1
    while pos < size and text[pos].isspace():
        pos += 1
    if pos < size and text[pos] in (":", "："):
        pos += 1
        while pos < size and text[pos].isspace():
            pos += 1
    in_fence = False
    fence = _INLINE_FENCE_RE.match(text, pos)
    if fence:
        pos = fence.end()
        in_fence = True
        while pos < size and text[pos].isspace():
            pos += 1
    if pos < size and text[pos] in "{[":
        return pos, in_fence
    return None, False


def _json_start(text: str, pos: int):
    """兼容调用：返回行内标记后的 JSON 起点。"""
    start, _ = _inline_json_start(text, pos)
    return start


def _whole_text_call(text: str, names: set[str], decoder: json.JSONDecoder,
                     schemas: dict[str, dict] | None = None):
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
    found = _as_tool_calls(obj, names, schemas)
    return (found, "") if found else (None, text)


def unknown_tool_names(text: str, tools=None) -> list[str]:
    """回复里 tool_call 标记后的调用名不在白名单时返回这些名字（CHATSESS-B16）。

    例如模型调用了网页自带的 browser.browse、muse.*：客户端执行不了，又不是正文。
    """
    if not isinstance(text, str) or "tool_call" not in text.lower():
        return []
    allowed = {tool["name"] for tool in _normalize_tools(tools)}
    decoder = json.JSONDecoder()
    found: list[str] = []
    for match in _INLINE_RE.finditer(text):
        start, _ = _inline_json_start(text, match.end())
        if start is None:
            continue
        try:
            obj, _ = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            continue
        for item in obj if isinstance(obj, list) else [obj]:
            if not isinstance(item, dict):
                continue
            fn = item.get("function")
            name = item.get("name") or item.get("tool") or (
                fn.get("name") if isinstance(fn, dict) else fn)
            if isinstance(name, str) and name.strip() and name.strip() not in allowed \
                    and name.strip() not in found:
                found.append(name.strip())
    return found


def _value_matches_type(value, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind in _JSON_TYPES:
        return isinstance(value, _JSON_TYPES[kind])
    return True


def _argument_problems(name: str, arguments: str, schema: dict) -> list[str]:
    """按工具 schema 检查顶层参数：必填、未声明的键、基本类型（CHATMSG-B11）。

    只检查顶层，嵌套结构交给客户端；schema 没有 properties 时不检查。
    """
    props = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(props, dict) or not props:
        return []
    try:
        args = json.loads(arguments)
    except (TypeError, ValueError):
        return ["%s 的参数不是 JSON 对象" % name]
    problems = []
    for key in schema.get("required") or []:
        if key not in args:
            problems.append("%s 缺少必填参数 %s" % (name, key))
    # 客户端会拒绝未声明的参数（如 Bash 的 stdin），这里统一当错误处理；
    # 显式声明 additionalProperties 为 True 或对象 schema 时放行。
    allow_extra = (schema.get("additionalProperties") is True
                   or isinstance(schema.get("additionalProperties"), dict))
    if not allow_extra:
        extra = [k for k in args if k not in props]
        if extra:
            problems.append("%s 没有参数 %s（可用参数：%s）"
                            % (name, "、".join(extra), "、".join(props)))
    for key, spec in props.items():
        if key not in args or not isinstance(spec, dict):
            continue
        types = _allowed_types(spec)
        if not types:
            continue
        value = args[key]
        if not any(_value_matches_type(value, kind) for kind in types):
            label = next(iter(types)) if len(types) == 1 else " / ".join(sorted(types))
            problems.append("%s 的参数 %s 应为 %s" % (name, key, label))
    return problems


def tool_call_problems(text: str, calls: list[dict] | None, tools=None,
                       has_tool_results: bool = False) -> list[str]:
    """回复里工具调用的问题清单；空表示可以交给客户端（CHATMSG-B12/B13）。

    覆盖：调用了清单外的工具、tool_call 标记后的 JSON 无法解析、参数不符合 schema；
    本轮输入没有工具结果、回复却声称已执行（编造结果）。
    """
    schemas = {t["name"]: t["parameters"] for t in _normalize_tools(tools)}
    problems = []
    unknown = unknown_tool_names(text, tools)
    if unknown:
        problems.append("调用了客户端没有的工具：%s" % "、".join(unknown))
    for call in calls or []:
        fn = call["function"]
        problems.extend(_argument_problems(fn["name"], fn["arguments"],
                                           schemas.get(fn["name"]) or {}))
    if not calls and not unknown and _has_marker_with_json(text):
        problems.append("tool_call 后的 JSON 格式不完整或无法解析")
    # 本轮带着工具结果时，模型据此总结"已修改…"是正当的，不算编造。
    if not calls and not problems and not has_tool_results and claims_execution(text):
        problems.append("回复声称已经执行或给出了执行结果，但没有发出任何工具调用；"
                        "命令还没有在用户电脑上运行过")
    if not calls and not problems and claims_sandbox_refusal(text):
        problems.append(
            "回复误将当前本机 Claude Code 会话当成云端沙箱/侧边聊天而未发出工具调用；"
            "必须直接输出 ```tool_call 代码块调用客户端工具（如 Bash）在用户本机执行"
        )
    return problems


# 声称"已在用户电脑上做完"的说法（CHATMSG-B13）。只在本轮没有任何工具调用时检查；
# 要求同时出现完成态动词和执行对象，并排除条件假设与操作建议语境，降低误报。
_CLAIM_DONE_RE = re.compile(
    r"(已|已经|刚刚|刚才)(经)?\s*(生成|执行|运行|创建|写入|写好|修改|改好|部署|重启|安装|删除|保存|试了|试过|查了|查过|检查了|测试了|跑了|跑过)"
    r"|(执行|运行)(结果|完毕|成功)"
    r"|(真实|实际)(内容|输出|结果)(是|为|如下)"
    r"|\b(I|I've|I have)\s+(ran|run|executed|created|wrote|written|deployed|restarted|tried|checked)\b"
    r"|\b(output|result) (is|was)\s*:", re.I)
_CLAIM_OBJECT_RE = re.compile(
    r"(文件|命令|脚本|输出|内容|目录|服务|进程|容器|配置|数据库|\.\w{1,5}\b|`[^`]+`"
    r"|\b(file|command|script|output|service|directory)\b)", re.I)
_CONDITIONAL_PREFIX_RE = re.compile(
    r"^\s*(?:如果|若|当|只要|一旦|等|待|比如|例如|假设|预期|你可以|请先|请|建议|需要|先|再"
    r"|if\b|when\b|once\b|after\b|suppose\b|you can\b|please\b)", re.I)
_SANDBOX_REFUSAL_RE = re.compile(
    r"(?:我这边是|当前是|这里是|处于)\s*(?:Muse\s*)?(?:云端|远程|网页)?(?:沙箱|虚拟机|容器)"
    r"|(?:Muse\s*)?侧边聊天"
    r"|这个(?:会话|聊天)(?:还是)?(?:查不到|跑不到|连不上|访问不了|不能直接跑|没有下游执行器)"
    r"|(?:跑不到|碰不到|连不上|访问不了|看不到|查不到)\s*你(?:的)?(?:本机|主机|宿主机|电脑|Windows|WSL)"
    r"|(?:没法|无法|不能)直接在你(?:的)?\s*(?:本机|主机|宿主机|电脑|Windows|WSL)\s*上?(?:执行|运行)"
    r"|没有你说的(?:那个)?(?:本机\s*)?Bash\s*工具"
    r"|(?:在|到|需在|需要在|得在|贴到)(?:你)?本机(?:那个)?\s*Claude\s*Code\s*(?:会话)?里",
    re.I,
)

_SHELL_FENCE_RE = re.compile(
    r"```([A-Za-z0-9_-]*)[ \t]*\r?\n([\s\S]*?)```", re.I
)
_SHELL_FENCE_LANGS = frozenset({
    "", "powershell", "pwsh", "bash", "sh", "shell", "zsh", "cmd", "bat",
})
_PS_CMD_PREFIX_RE = re.compile(
    r"^\s*(?:powershell|pwsh)(?:\.exe)?(?:\s+-[A-Za-z0-9_-]+)*\s+(?:-Command|-c)\s+([\s\S]+)$",
    re.I,
)
_PS_HINT_RE = re.compile(
    r"(?:^|\s)(?:powershell|pwsh)(?:\.exe)?\b|\$env:|\$_\.|Get-[A-Z][A-Za-z]+|Select-Object|Where-Object|Format-Table",
    re.I,
)


def claims_sandbox_refusal(text: str) -> bool:
    """回复误将当前客户端会话当成云端沙箱/侧边聊天而拒绝调用本地工具。"""
    return isinstance(text, str) and bool(_SANDBOX_REFUSAL_RE.search(text))


def _unwrap_quoted_tail(arg: str) -> str | None:
    """若整段参数被一对匹配的外层引号包裹，返回去掉引号与转义双引号后的内部文本。"""
    if len(arg) < 2 or arg[0] not in ('"', "'"):
        return None
    quote = arg[0]
    i = 1
    while i < len(arg):
        ch = arg[i]
        if quote == '"' and ch == "\\" and i + 1 < len(arg):
            i += 2
            continue
        if quote == "'" and ch == "'" and i + 1 < len(arg) and arg[i + 1] == "'":
            i += 2
            continue
        if ch == quote:
            if not arg[i + 1:].strip():
                inner = arg[1:i]
                return inner.replace('\\"', '"') if quote == '"' else inner.replace("''", "'")
            return None
        i += 1
    return None


def _encode_powershell_command(script: str) -> str:
    b64 = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return f"powershell.exe -NoProfile -NonInteractive -EncodedCommand {b64}"


def _normalize_rescued_shell_command(lang: str, raw_cmd: str) -> str:
    """对从拒答代码块提取出的命令规范化；PowerShell 脚本统一转 -EncodedCommand 防 WSL bash 转义破坏。"""
    cmd = raw_cmd.strip()
    if not cmd:
        return ""
    low_lang = (lang or "").strip().lower()
    if re.match(r"^\s*(?:powershell|pwsh)(?:\.exe)?\b", cmd, re.I):
        m = _PS_CMD_PREFIX_RE.match(cmd)
        if not m:
            return cmd
        arg = m.group(1).strip()
        inner = _unwrap_quoted_tail(arg)
        if inner is not None:
            return _encode_powershell_command(inner.strip()) if inner.strip() else cmd
        if "\n" not in arg and not re.search(r"\|\s*(?:grep|awk|sed|head|tail|wc|sort|cut)\b", arg):
            return _encode_powershell_command(arg)
        return cmd
    if low_lang in ("powershell", "pwsh") or (low_lang == "" and _PS_HINT_RE.search(cmd)):
        return _encode_powershell_command(cmd)
    return cmd


def _rescue_sandbox_refusal_calls(text: str, names: set[str], schemas: dict) -> list[dict]:
    """当模型误自称侧边聊天/沙箱但在回复内附带了可执行 shell 代码块时，提取为 Bash 调用。"""
    if "Bash" not in names or not isinstance(text, str):
        return []
    rescued: list[dict] = []
    for match in _SHELL_FENCE_RE.finditer(text):
        lang = (match.group(1) or "").strip().lower()
        if lang not in _SHELL_FENCE_LANGS:
            continue
        cmd = _normalize_rescued_shell_command(lang, match.group(2) or "")
        if not cmd:
            continue
        found = _as_tool_calls({"name": "Bash", "arguments": {"command": cmd}}, names, schemas)
        _add_calls(rescued, found)
    return rescued


def claims_execution(text: str) -> bool:
    """回复声称已在用户电脑上执行过某事（无工具调用时即为编造）。"""
    if not isinstance(text, str) or not text.strip():
        return False
    for line in re.split(r"[\r\n。！？!?]+", text):
        seg = line.strip()
        if not seg or _CONDITIONAL_PREFIX_RE.match(seg):
            continue
        if _CLAIM_DONE_RE.search(seg) and _CLAIM_OBJECT_RE.search(seg):
            return True
    return False


def _marker_json_starts(text: str):
    """每个 tool_call 标记后紧跟（或代码块内）的 JSON 起点；没有的标记跳过。"""
    starts = []
    for match in _INLINE_RE.finditer(text):
        start, _ = _inline_json_start(text, match.end())
        if start is not None:
            starts.append(start)
    return starts


def _has_marker_with_json(text: str) -> bool:
    """tool_call 标记后紧跟（或下一行是）``{`` / ``[``：意图明确是调用而非提及。"""
    return isinstance(text, str) and bool(_marker_json_starts(text))


def _ends_inside_json(text: str, start: int) -> bool:
    """从 start 的 ``{`` / ``[`` 扫到文本结尾，仍停在未闭合的字符串或括号里。

    括号错配算写错，不算截断：截断是生成停在半截 token 上，不会多写出一个对不上的括号。
    结尾的空白和一个代码块收尾 ```` ``` ```` 不算额外内容。
    """
    body = re.sub(r"(?:\r?\n)?[ \t]*```[ \t]*\Z", "", text[start:]).rstrip()
    stack: list[str] = []
    in_str, esc = False, False
    for ch in body:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack.pop() != ch:
                return False
    return in_str or bool(stack)


def truncated_tool_call(text: str, calls, tools=None) -> bool:
    """回复有意发出工具调用，但 JSON 一直坏到文本末尾：生成被截断，不是写错。

    判定只看坏掉的位置：从标记后的 ``{`` / ``[`` 起，标准解析和末尾补括号都失败，
    且扫描停在未闭合的字符串或括号里。括号错配、或者错误之后还有内容的，是格式错误，
    重发一次原样内容即可。已经解析出调用的不在这里判断。
    """
    if calls or not isinstance(text, str):
        return False
    if unknown_tool_names(text, tools):
        return False
    decoder = json.JSONDecoder()
    for start in _marker_json_starts(text):
        if _decode_lenient(text, start, decoder)[1] is not None:
            continue
        return _ends_inside_json(text, start)
    return False


def build_tool_call_correction(problems: list[str], tools=None, truncated: bool = False) -> str:
    """工具调用有问题时，在同一 thread 追发的纠正提示（CHATMSG-B12）。

    ``truncated`` 时说明上一条是被截断而非写错：原样重发还会断在同一个地方，
    必须把长参数拆成先 Write 文件、再 Bash 执行的短调用。
    """
    has_search = any(t["name"] == "ToolSearch" for t in _normalize_tools(tools))
    lines = ["【客户端提示】你上一条回复里的工具调用无法执行，已作废：",
             *("- " + p for p in problems)]
    if truncated:
        lines.append(
            "这条回复在 JSON 字符串中间被截断了，所以不是格式问题，重发同样的内容还会断在同一个地方。"
            "把长的命令、脚本或文件内容拆开：先用 Write 把内容写成文件，再用 Bash 执行这个短命令；"
            "每个 ```tool_call 代码块里只放一个短的、完整的 JSON "
            '{"name": "工具名", "arguments": {参数}}，然后结束回复。')
    else:
        lines += ["请重新输出正确的调用：```tool_call 代码块内放一个完整、合法的 JSON 对象 "
                  '{"name": "工具名", "arguments": {参数}}，只用清单里的工具和参数名，然后结束回复。',
                  "长脚本或多行内容不要塞进命令参数：先用 Write 写成文件，再用 Bash 执行。"]
    if has_search:
        lines.append('需要 mcp__ 开头的 MCP 工具时，先调用 ToolSearch，参数 '
                     '{"query": "select:工具全名", "max_results": 5}。')
    return "\n".join(lines)


def build_unknown_tool_correction(names: list[str], tools=None) -> str:
    """模型调用了客户端没有的工具时，在同一 thread 里追发的纠正提示。"""
    has_search = any(t["name"] == "ToolSearch" for t in _normalize_tools(tools))
    lines = [
        "【客户端提示】你刚才调用的 %s 不是客户端工具，客户端无法执行，上一条回复作废。"
        % "、".join(names),
        "请只用前文客户端工具清单里的工具重新完成用户的请求，按 ```tool_call 格式输出，然后结束回复。",
    ]
    if has_search:
        lines.append('需要 mcp__ 开头的 MCP 工具时，先调用 ToolSearch，参数 '
                     '{"query": "select:工具全名", "max_results": 5}。')
    return "\n".join(lines)


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
    """透传普通文本，发现工具标记、沙箱拒答话术或全文纯 JSON 调用时暂存余下内容供最终解析。"""

    _MARKERS = ("```tool_call", "<tool_call", "tool_call")
    _REFUSAL_MARKERS = (
        "侧边聊天", "云端沙箱", "这个聊天", "这个会话",
        "跑不到", "查不到", "不能直接跑", "没有你说的",
    )

    def __init__(self):
        self.pending = ""
        self.held = False
        self.seen_nonspace = False

    def feed(self, chunk: str) -> str:
        if self.held:
            self.pending += chunk
            return ""

        self.pending += chunk
        if not self.seen_nonspace:
            stripped = self.pending.lstrip()
            if not stripped:
                return ""
            self.seen_nonspace = True
            if stripped[0] in "{[":
                self.held = True
                return ""

        if _SANDBOX_REFUSAL_RE.search(self.pending):
            self.pending = self.pending.lstrip()
            self.held = True
            return ""

        lower = self.pending.lower()
        positions = [lower.find(marker) for marker in self._MARKERS]
        positions = [pos for pos in positions if pos >= 0]
        if positions:
            pos = min(positions)
            if pos >= 2 and self.pending[pos - 2:pos] == "**":
                pos -= 2
            elif pos >= 1 and self.pending[pos - 1] == "`":
                pos -= 1
            ready = self.pending[:pos]
            self.pending = self.pending[pos:]
            self.held = True
            return ready

        # Retain only a suffix that could be the beginning of a marker split
        # across chunks; ordinary text is forwarded without a fixed-size delay.
        keep = 0
        for marker in (*self._MARKERS, *self._REFUSAL_MARKERS):
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

    normalized = _normalize_tools(tools)
    names = {tool["name"] for tool in normalized}
    if not names:
        return None, text
    schemas = {tool["name"]: tool["parameters"] for tool in normalized}

    decoder = json.JSONDecoder()

    # 纯 JSON 优先处理：如果整段就是一个 JSON 值，就不再到字符串内部找标记。
    whole = _whole_text_call(text, names, decoder, schemas)
    if whole is not None:
        return whole

    candidates = []
    opening_spans = [(match.start(), match.end())
                     for match in _TAG_OPEN_RE.finditer(text)]
    opening_spans.extend((match.start(), match.end())
                         for match in _FENCE_OPEN_RE.finditer(text))
    _scan_delimited(text, _TAG_OPEN_RE, _TAG_CLOSE_RE, names, decoder, 0,
                    candidates, schemas)
    _scan_delimited(text, _FENCE_OPEN_RE, _FENCE_CLOSE_RE, names, decoder, 1,
                    candidates, schemas)
    for match in _INLINE_RE.finditer(text):
        # 开启标记里的 tool_call 是标签/代码块的一部分，不能降级成行内标记。
        if any(start <= match.start() < end
               for start, end in opening_spans):
            continue
        start, _ = _inline_json_start(text, match.end())
        if start is None:
            continue
        obj, end = _decode_lenient(text, start, decoder, eat_fence=True)
        if end is None:
            continue
        found = _as_tool_calls(obj, names, schemas)
        if found:
            span_start = match.start()
            if span_start >= 2 and text[span_start - 2:span_start] == "**" and text[match.end():match.end() + 2] == "**":
                span_start -= 2
            elif span_start >= 1 and text[span_start - 1] == "`" and match.end() < len(text) and text[match.end()] == "`":
                span_start -= 1
            candidates.append((span_start, end, 2, found))

    if not candidates:
        if claims_sandbox_refusal(text):
            rescued = _rescue_sandbox_refusal_calls(text, names, schemas)
            if rescued:
                return rescued, ""
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
