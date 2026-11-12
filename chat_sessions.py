"""Claude 会话到 Muse thread 的持久绑定（流程 CHATSESS）。

只存会话键哈希、thread ID、账号 ID 和 user/tool 输入的 SHA-256 指纹，不存正文。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def input_fingerprints(messages: list[Any]) -> list[str]:
    """只对 user/tool 输入取指纹；assistant 输出由网页侧生成，不参与比对。"""
    result = []
    for message in messages:
        role = getattr(message, "role", None)
        if role not in ("user", "tool"):
            continue
        value = {
            "role": role,
            "tool_call_id": getattr(message, "tool_call_id", None),
            "content": getattr(message, "content", None),
        }
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
        result.append(hashlib.sha256(encoded).hexdigest())
    return result


@dataclass
class SessionPlan:
    """一次请求的绑定决策。

    mode: stateless（无会话）/ new（首轮）/ continue（续轮）/ rebind（历史被改写）
    """
    mode: str
    key: str | None = None
    thread_id: str | None = None
    account_id: str | None = None
    messages: list[Any] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)
    # 该 thread 上次完整发送的工具清单指纹；续轮清单相同时只发简短提醒。
    tools_sent: str = ""

    @property
    def bound(self) -> bool:
        return self.mode != "stateless"


class ChatSessionStore:
    def __init__(self, path: str):
        self.path = path
        self.lock = threading.RLock()
        self.sessions = self._load()
        self._active: dict[str, tuple[str, float]] = {}

    def bound_thread_ids(self) -> set[str]:
        """所有已绑定的 Muse thread，供旁聊清理（CHATPRUN）排除。"""
        with self.lock:
            return {row["thread_id"] for row in self.sessions.values()
                    if isinstance(row.get("thread_id"), str) and row["thread_id"]}

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            rows = data.get("sessions", {}) if isinstance(data, dict) else {}
            return {k: v for k, v in rows.items() if isinstance(k, str) and isinstance(v, dict)}
        except (OSError, ValueError, TypeError):
            return {}

    def _save(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="chat-sessions-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 2, "sessions": self.sessions}, f,
                          ensure_ascii=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    @staticmethod
    def key(session_id: str | None, agent_id: str | None = None) -> str | None:
        session_id = (session_id or "").strip()
        agent_id = (agent_id or "").strip()
        if not session_id or len(session_id) > 128 or len(agent_id) > 128:
            return None
        # JSON 编码保证两个字段的边界不会被字段内容伪造。
        identity = json.dumps([session_id, agent_id], ensure_ascii=False)
        return hashlib.sha256(identity.encode()).hexdigest()

    def try_begin(self, key: str, ttl: float) -> str | None:
        """同一会话同时只允许一个请求；返回占用 token，失败返回 None。

        ttl 只兜底进程内异常泄漏；token 保证过期后被新请求接管时，旧请求的 end 不会清掉新占用。
        """
        now = time.monotonic()
        with self.lock:
            current = self._active.get(key)
            if current is not None and now - current[1] < ttl:
                return None
            token = os.urandom(8).hex()
            self._active[key] = (token, now)
            return token

    def end(self, key: str, token: str) -> None:
        with self.lock:
            current = self._active.get(key)
            if current is not None and current[0] == token:
                del self._active[key]

    def get(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.sessions.get(key)
            return dict(row) if row else None

    def plan(self, key: str | None, messages: list[Any]) -> SessionPlan:
        """决定本轮发什么、发到哪个 thread。"""
        if not key:
            return SessionPlan("stateless", messages=list(messages))
        fingerprints = input_fingerprints(messages)
        row = self.get(key)
        if not row or not row.get("thread_id"):
            return SessionPlan("new", key, account_id=(row or {}).get("account_id"),
                               messages=list(messages), fingerprints=fingerprints)
        known = list(row.get("fingerprints") or [])
        account = row.get("account_id")
        if len(fingerprints) < len(known) or fingerprints[:len(known)] != known:
            # /compact 或自动压缩会改写历史前缀；同一账号新开 thread 并重绑。
            return SessionPlan("rebind", key, account_id=account,
                               messages=list(messages), fingerprints=fingerprints)
        inputs = [m for m in messages if getattr(m, "role", None) in ("user", "tool")]
        # 历史与已提交完全相同 = 客户端重发上一轮；只重发最后一条输入，不新开 thread。
        delta = inputs[len(known):] or inputs[-1:]
        return SessionPlan("continue", key, thread_id=row["thread_id"], account_id=account,
                           messages=delta, fingerprints=fingerprints,
                           tools_sent=str(row.get("tools_sig") or ""))

    def commit(self, plan: SessionPlan, thread_id: str | None, account_id: str,
               token: str | None = None, tools_sig: str = "") -> None:
        """本轮正常结束后写入绑定；失败或取消的轮次不调用此函数。

        传入 ``token`` 时，只有仍持有该会话占用的请求才能写入：占用过期后被新请求接管，
        旧请求不能用过时的 plan 覆盖新请求的绑定。
        """
        if not plan.bound or not plan.key:
            return
        key = plan.key
        thread_id = (thread_id or "").strip()
        if not _UUID_RE.fullmatch(thread_id):
            raise ValueError("Muse 未返回有效的 thread ID")
        if plan.mode == "continue" and thread_id != plan.thread_id:
            raise ValueError("Muse thread 在续轮中发生变化")
        with self.lock:
            if token is not None:
                current = self._active.get(key)
                if current is None or current[0] != token:
                    raise ValueError("会话占用已被其他请求接管，本轮结果不写入绑定")
            previous = self.sessions.get(key) or {}
            self.sessions[key] = {
                "thread_id": thread_id,
                "account_id": account_id,
                "fingerprints": plan.fingerprints,
                "tools_sig": tools_sig,
                "created_at": previous.get("created_at", int(time.time())),
                "updated_at": int(time.time()),
            }
            self._save()
