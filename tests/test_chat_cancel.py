"""CHATSESS-B12：取消必须传到浏览器线程，且线程停下后才返回。Run: python tests/test_chat_cancel.py"""
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chat_transport import current_control, run_cancellable, stream_with_heartbeat  # noqa: E402


class Disconnect:
    def __init__(self, after):
        self.at = time.monotonic() + after

    async def is_disconnected(self):
        return time.monotonic() >= self.at


def CHATSESS_B12_nonstream_disconnect_stops_worker():
    stopped = threading.Event()

    def work():
        control = current_control()
        assert control is not None
        while not control.cancelled.wait(0.02):
            pass
        time.sleep(0.2)  # 模拟浏览器线程收尾
        stopped.set()
        return "partial", None

    asyncio.run(run_cancellable(work, Disconnect(0.1), poll=0.05))
    assert stopped.is_set(), "返回前浏览器线程必须已停止"


def CHATSESS_B12_stream_closed_after_first_event_releases():
    released = threading.Event()
    blocked = threading.Event()

    def source():
        yield "event: message_start\n\n"
        blocked.wait(5)  # 模拟网页生成阻塞中
        yield "never-consumed"

    async def drive():
        gen = stream_with_heartbeat(source, ": hb\n\n", on_done=released.set)
        assert (await gen.__anext__()).startswith("event: message_start")
        await gen.aclose()  # 首包后客户端断开
        blocked.set()

    asyncio.run(drive())
    assert released.wait(2), "首包后断开也要释放会话占用"


if __name__ == "__main__":
    CHATSESS_B12_nonstream_disconnect_stops_worker()
    CHATSESS_B12_stream_closed_after_first_event_releases()
    print("PASS: CHATSESS-B12 cancellation reaches worker and releases session")
