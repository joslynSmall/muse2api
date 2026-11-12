"""把阻塞的网页生成桥接为可取消、带心跳的 SSE，不改变协议事件本身。"""
from __future__ import annotations

import asyncio
import contextvars
import queue
import threading
from dataclasses import dataclass
from typing import TypeVar
from collections.abc import Callable, Iterator, AsyncIterator


@dataclass
class ChatControl:
    cancelled: threading.Event


_control: contextvars.ContextVar[ChatControl | None] = contextvars.ContextVar(
    "chat_control", default=None)


def current_control() -> ChatControl | None:
    return _control.get()


async def stream_with_heartbeat(source: Callable[[], Iterator[str]], heartbeat: str,
                                interval: float = 10.0,
                                on_done: Callable[[], None] | None = None) -> AsyncIterator[str]:
    """生成阻塞时仍输出心跳；断流时通知源停止，满队列不会永久占住生产线程。

    ``on_done`` 在生产线程结束后调用，用来释放请求级资源（如会话占用）：
    源生成器停在首个 yield 时被关闭也能释放，且一定晚于生成线程停止。
    """
    control = ChatControl(threading.Event())
    output: queue.Queue = queue.Queue(maxsize=16)

    def put(kind, value):
        while not control.cancelled.is_set():
            try:
                output.put((kind, value), timeout=0.1)
                return
            except queue.Full:
                pass

    def produce():
        token = _control.set(control)
        iterator = None
        try:
            iterator = iter(source())
            while not control.cancelled.is_set():
                try:
                    value = next(iterator)
                except StopIteration:
                    break
                put("data", value)
        except Exception as exc:
            put("error", exc)
        finally:
            if iterator is not None:
                close = getattr(iterator, "close", None)
                if close:
                    close()
            put("done", None)
            _control.reset(token)
            if on_done is not None:
                on_done()

    context = contextvars.copy_context()
    worker = threading.Thread(target=context.run, args=(produce,), daemon=True,
                              name="chat-sse")
    worker.start()
    loop = asyncio.get_running_loop()
    last_output = loop.time()
    try:
        while True:
            try:
                kind, value = output.get_nowait()
            except queue.Empty:
                if control.cancelled.is_set():
                    break
                if loop.time() - last_output >= interval:
                    yield heartbeat
                    last_output = loop.time()
                await asyncio.sleep(0.02)
                continue
            if kind == "done":
                break
            if kind == "error":
                raise value
            yield value
            last_output = loop.time()
    finally:
        control.cancelled.set()



T = TypeVar("T")


async def run_cancellable(fn: Callable[[], T], request=None, poll: float = 0.5) -> T:
    """在线程中运行阻塞生成；客户端断开或协程被取消时设置取消信号，并等线程结束再返回。

    非流式请求没有 SSE 断流信号，靠轮询 ``request.is_disconnected()`` 发现断开。
    """
    control = ChatControl(threading.Event())

    def run():
        token = _control.set(control)
        try:
            return fn()
        finally:
            _control.reset(token)

    context = contextvars.copy_context()
    task = asyncio.ensure_future(asyncio.to_thread(context.run, run))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll)
            if done:
                return task.result()
            if request is not None and await request.is_disconnected():
                control.cancelled.set()
    except asyncio.CancelledError:
        control.cancelled.set()
        raise
    finally:
        if not task.done():
            control.cancelled.set()
            # 线程不可强杀；等它停下，调用方才能安全释放会话占用。
            await asyncio.shield(asyncio.wait({task}))
