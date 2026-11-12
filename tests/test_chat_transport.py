"""The SSE bridge must emit heartbeats and signal cancellation to producers."""
import asyncio
import threading
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chat_transport import current_control, stream_with_heartbeat


async def verify_heartbeat_and_cancel():
    entered = threading.Event()
    release = threading.Event()
    observed_control = []

    def source():
        observed_control.append(current_control())
        entered.set()
        release.wait(2)
        yield "data: done\n\n"

    stream = stream_with_heartbeat(source, ": keepalive\n\n", interval=0.04)
    first = await asyncio.wait_for(anext(stream), timeout=0.5)
    assert first == ": keepalive\n\n", first
    assert entered.wait(0.2)
    assert observed_control and observed_control[0] is not None
    await stream.aclose()
    assert observed_control[0].cancelled.is_set()
    release.set()


asyncio.run(verify_heartbeat_and_cancel())
print("PASS: SSE heartbeat is prompt and stream close signals producer cancellation")
