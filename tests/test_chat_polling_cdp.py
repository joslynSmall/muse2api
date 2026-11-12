"""CDP request deadlines must cap ws.recv, and closed sockets must fail fast."""
import json
from contextlib import contextmanager
from pathlib import Path
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import cdp  # noqa: E402


class Clock:
    now = 0.0

    @classmethod
    def monotonic(cls):
        return cls.now

    @classmethod
    def reset(cls):
        cls.now = 0.0


@contextmanager
def virtual_time():
    old = cdp.time.monotonic
    cdp.time.monotonic = Clock.monotonic
    try:
        yield
    finally:
        cdp.time.monotonic = old


class FakeWS:
    def __init__(self, recv):
        self._recv = recv
        self.timeouts = []
        self.sent = []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def send(self, data):
        self.sent.append(json.loads(data))

    def recv(self):
        return self._recv(self)

    def close(self):
        pass


def client_for(ws):
    with patch.object(cdp.websocket, "create_connection", return_value=ws):
        return cdp.CDP("ws://test", timeout=180)


# 1) A request timeout of 2s must cap the socket recv, not inherit the 180s
#    connection default.
def recv_timeout(ws):
    Clock.now += ws.timeouts[-1]
    raise cdp.websocket.WebSocketTimeoutException()


ws = FakeWS(recv_timeout)
client = client_for(ws)
Clock.reset()
with virtual_time():
    try:
        client.send("Runtime.evaluate", timeout=2)
    except TimeoutError as exc:
        assert "超时" in str(exc), exc
    else:
        raise AssertionError("recv deadline must raise TimeoutError")
assert ws.timeouts and ws.timeouts[0] <= 2.005, ws.timeouts
assert 1.99 <= Clock.now <= 2.01, Clock.now

# 2) A closed connection raises CDPError immediately instead of busy-looping
#    until the deadline.
def recv_closed(ws):
    raise cdp.websocket.WebSocketConnectionClosedException()


ws = FakeWS(recv_closed)
client = client_for(ws)
Clock.reset()
with virtual_time():
    try:
        client.send("Runtime.evaluate")
    except cdp.CDPError as exc:
        assert "已关闭" in str(exc), exc
    else:
        raise AssertionError("closed socket must raise CDPError")
assert Clock.now == 0.0, Clock.now

# 3) An empty frame also means the peer is gone.
ws = FakeWS(lambda ws: "")
client = client_for(ws)
try:
    client.send("Runtime.evaluate")
except cdp.CDPError:
    pass
else:
    raise AssertionError("empty frame must raise CDPError")

# 4) Events before the matching response are skipped; the response is returned
#    without waiting for the deadline.
def recv_event_then_result(ws):
    if not ws.sent:
        raise AssertionError("send called first")
    if not getattr(ws, "seen_event", False):
        ws.seen_event = True
        return json.dumps({"method": "Page.loadEventFired", "params": {}})
    return json.dumps({"id": 1, "result": {"ok": True}})


ws = FakeWS(recv_event_then_result)
client = client_for(ws)
Clock.reset()
with virtual_time():
    msg = client.send("Runtime.evaluate")
assert msg == {"id": 1, "result": {"ok": True}}, msg
assert Clock.now == 0.0, Clock.now
assert ws.sent == [{"id": 1, "method": "Runtime.evaluate", "params": {}}], ws.sent

# 5) pump() stops on a closed socket instead of spinning until its window ends.
ws = FakeWS(recv_closed)
client = client_for(ws)
Clock.reset()
with virtual_time():
    client.pump(10, sock_timeout=1)
assert Clock.now == 0.0, Clock.now
assert ws.timeouts == [1], ws.timeouts

# 6) Ordinary socket timeouts keep pump() running until its window ends.
def recv_plain_timeout(ws):
    Clock.now += ws.timeouts[-1]
    raise TimeoutError("timed out")


ws = FakeWS(recv_plain_timeout)
client = client_for(ws)
Clock.reset()
with virtual_time():
    client.pump(3, sock_timeout=1)
assert 2.99 <= Clock.now <= 3.01, Clock.now
assert len(ws.timeouts) == 3, ws.timeouts

print("PASS: CDP recv honors request timeouts and closed sockets fail fast")
