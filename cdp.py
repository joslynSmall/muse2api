"""极简 Chrome DevTools Protocol 客户端（仅依赖 websocket-client / requests）。"""
from __future__ import annotations

import json
import time

import threading

import requests
import websocket


class CDPError(RuntimeError):
    pass


class CDP:
    def __init__(self, ws_url: str, timeout: float = 90.0, max_size: int = 256 << 20):
        self.ws = websocket.create_connection(ws_url, timeout=timeout, max_size=max_size)
        self.timeout = timeout
        self._id = 0
        self._lock = threading.Lock()

    def send(self, method: str, params: dict | None = None, timeout: float | None = None):
        with self._lock:
            self._id += 1
            mid = self._id
            try:
                self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            except (TimeoutError, websocket.WebSocketTimeoutException):
                raise TimeoutError(f"CDP {method} 发送超时") from None
            except websocket.WebSocketConnectionClosedException:
                raise CDPError(f"{method}: WebSocket 连接已关闭") from None
            except (OSError, websocket.WebSocketException) as exc:
                raise CDPError(f"{method}: WebSocket 发送失败: {exc}") from None

            # The socket was created with a long default timeout (often 180s); cap
            # every recv by this request's remaining budget so a slow reply cannot
            # outlive the caller's timeout.
            budget = timeout or self.timeout
            deadline = time.monotonic() + budget
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"CDP {method} 超时")
                self.ws.settimeout(remaining)
                try:
                    raw = self.ws.recv()
                except (TimeoutError, websocket.WebSocketTimeoutException):
                    raise TimeoutError(f"CDP {method} 超时") from None
                except websocket.WebSocketConnectionClosedException:
                    raise CDPError(f"{method}: WebSocket 连接已关闭") from None
                except (OSError, websocket.WebSocketException) as exc:
                    raise CDPError(f"{method}: WebSocket 接收失败: {exc}") from None
                if not raw:
                    raise CDPError(f"{method}: WebSocket 连接已关闭")
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise CDPError(f"{method}: {msg['error']}")
                    return msg

    def js(self, expr: str, await_promise: bool = False, timeout: float | None = None):
        r = self.send("Runtime.evaluate",
                      {"expression": expr, "returnByValue": True,
                       "awaitPromise": await_promise}, timeout)
        res = r.get("result", {}).get("result", {})
        if "value" in res:
            return res["value"]
        return r.get("result")

    def pump(self, seconds: float, on_event=None, sock_timeout: float = 2.0):
        """在 seconds 秒内持续读取事件，交给回调处理。"""
        end = time.monotonic() + seconds
        while True:
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            self.ws.settimeout(min(sock_timeout, remaining))
            try:
                raw = self.ws.recv()
            except (TimeoutError, websocket.WebSocketTimeoutException):
                continue
            except (OSError, websocket.WebSocketException):
                break
            if not raw:
                break
            try:
                ev = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if on_event and isinstance(ev, dict) and "method" in ev:
                on_event(ev)

    def close(self):
        with self._lock:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass


def http_json(url: str, timeout: float = 5.0):
    return requests.get(url, timeout=timeout).json()
