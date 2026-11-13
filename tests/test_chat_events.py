"""CHATSRC：网页回复事件流采集（虚拟时钟，不连 Muse）。Run: python tests/test_chat_events.py"""
import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import muse_events  # noqa: E402
from muse_events import ReplyAssembler  # noqa: E402

MD = "# 标题\n\n- **列表**\n\n```python\nprint(1)\n```\n\n| a | b |\n|---|---|"


# STOP_VISIBLE_JS 的 Python 等价判定；style 为元素的有效计算样式（visibility 已解析继承/覆盖）。
class FakeElement(SimpleNamespace):
    def __init__(self, parent=None, display="block", visibility="visible", opacity=1.0,
                 attrs=None, aria_hidden=False, disabled=False, rects=1, width=10,
                 connected=True):
        super().__init__(parent=parent, attrs=attrs or {}, aria_hidden=aria_hidden,
                         disabled=disabled, rects=rects, width=width, height=10,
                         connected=connected, style=SimpleNamespace(
                             display=display, visibility=visibility, opacity=float(opacity)))


StopElementSource = list[FakeElement] | Callable[[float], list[FakeElement]]
PollState = Callable[[float], tuple[int, str]]


def stop_visible(elements: list[FakeElement]) -> bool:
    def visible(el: FakeElement) -> bool:
        if (not el.connected or el.rects <= 0 or min(el.width, el.height) <= 0
                or el.style.display == "none"
                or el.style.visibility in ("hidden", "collapse")):
            return False
        opacity, node = 1.0, el
        while node:
            if node.style.display == "none":
                return False
            opacity *= node.style.opacity
            if opacity <= 0:
                return False
            node = node.parent
        return True
    return any(visible(el) for el in elements)


def ev(name, mid, seq=None, **payload):
    return {"e": name, "s": seq, "p": {"message_id": mid, **payload}}


def CHATSRC_B01_markdown_increments():
    a = ReplyAssembler()
    a.feed(ev("delta.message_start", "m1"))
    pieces = [MD[:5], MD[5:20], MD[20:]]
    out = []
    for i, piece in enumerate(pieces):
        a.feed(ev("delta.text_append", "m1", seq=i, text=piece))
        a.feed(ev("delta.text_append", "m1", seq=i, text=piece))  # 重复事件去重
        out.append(a.take())
    a.feed(ev("delta.message_done", "m1", status="completed", display_text=MD))
    out.append(a.take(final=True))
    assert "".join(out) == MD and a.final_text() == MD and a.all_done


def CHATSRC_B03_done_fills_lost_tail():
    a = ReplyAssembler()
    a.feed(ev("delta.message_start", "m1"))
    a.feed(ev("delta.text_append", "m1", seq=0, text="前半段"))
    first = a.take()
    a.feed(ev("delta.message_done", "m1", status="completed", display_text="前半段后半段"))
    assert first + a.take(final=True) == "前半段后半段"
    # 最终文本与已发内容对不上：不重复发送
    b = ReplyAssembler()
    b.feed(ev("delta.message_start", "m1"))
    b.feed(ev("delta.text_append", "m1", seq=0, text="AAA"))
    sent = b.take()
    b.feed(ev("delta.message_done", "m1", status="completed", display_text="完全不同"))
    assert sent == "AAA" and b.take(final=True) == ""


def CHATSRC_B04_failed_status():
    a = ReplyAssembler()
    a.feed(ev("delta.message_start", "m1"))
    a.feed(ev("delta.message_done", "m1", status="failed", error={"message": "boom"}))
    assert a.error == "boom"
    b = ReplyAssembler()
    b.feed(ev("delta.message_start", "m1"))
    b.feed(ev("delta.text_append", "m1", seq=0, text="部分"))
    b.feed(ev("delta.message_done", "m1", status="interrupted"))
    assert b.error is None and b.final_text() == "部分"


def CHATSRC_B05_citation_to_markdown_link():
    a = ReplyAssembler()
    a.feed(ev("delta.message_start", "m1"))
    a.feed(ev("delta.text_append", "m1", seq=0, text="据报道【cite:c1】，另见【cite"))
    early = a.take()
    assert early == "据报道", early  # 链接未知时暂扣
    a.feed(ev("delta.text_append", "m1", seq=1, text=":c2】。"))
    a.feed(ev("delta.message_done", "m1", status="completed", resources=[
        {"kind": "citation", "data": {"citation_id": "c1", "title": "官方[文档]",
                                       "url": "https://l.meta.ai/?u=https%3A%2F%2Fdev.meta.ai%2Fx"}},
        {"kind": "citation", "data": {"citation_id": "c2", "url": "javascript:alert(1)"}},
    ]))
    rest = a.take(final=True)
    assert early + rest == "据报道[官方\\[文档\\]](https://dev.meta.ai/x)，另见。", early + rest


def CHATSRC_B06_ignores_other_messages():
    a = ReplyAssembler()
    a.note_existing(ev("delta.text_append", "old"))
    a.feed(ev("delta.message_start", "old"))
    a.feed(ev("delta.text_append", "old", seq=0, text="历史"))
    a.feed(ev("delta.text_append", "nostart", seq=0, text="没有开始事件"))
    a.feed(ev("delta.message_start", "sub", stream_lane="subagent"))
    a.feed(ev("delta.text_append", "sub", seq=0, text="子任务", stream_lane="subagent"))
    a.feed({"e": "delta.tool_start", "p": {"message_id": "t1"}})
    assert not a.started and a.take() == ""
    a.feed(ev("delta.message_start", "m1", stream_lane="main"))
    a.feed(ev("delta.text_append", "m1", seq=0, text="正文", stream_lane="main"))
    assert a.take() == "正文"


def drain_payload(subs, records):
    return json.dumps({"subs": subs, "dropped": 0, "q": [json.dumps(r) for r in records]})


with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("chat_events_engine", ROOT / "engine.py")
    assert spec is not None and spec.loader is not None
    em = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = em
    spec.loader.exec_module(em)

    class Clock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, s):
            cls.now += s

    @contextmanager
    def virtual_time():
        old = em.time.monotonic, em.time.sleep
        em.time.monotonic, em.time.sleep = Clock.monotonic, Clock.sleep
        try:
            yield
        finally:
            em.time.monotonic, em.time.sleep = old

    class FakePage:
        """模拟页面：事件队列按时间投放；DOM 只给出纯文本（无格式）。"""

        def __init__(self, timeline, subs: int | None = 1, dom=None, stop_until=0.0,
                     stop_elements: StopElementSource | None = None,
                     poll_state: PollState | None = None):
            self.timeline = sorted(timeline, key=lambda x: x[0])
            self.subs, self.dom, self.stop_until = subs, dom, stop_until
            self.stop_elements = stop_elements
            self.poll_state = poll_state
            self.stop_log = []

        def stop_now(self, legacy: bool = False) -> bool:
            source = self.stop_elements
            if source is None:
                return Clock.now < self.stop_until
            if callable(source):
                elements: list[FakeElement] = source(Clock.now)
            else:
                elements = source
            if legacy:
                # 旧 _POLL_CHAT_JS：只要布局矩形 + visibility 可见；透明仍算可见。
                return any(el.connected and el.rects > 0
                           and el.style.visibility not in ("hidden", "collapse")
                           for el in elements)
            return stop_visible(elements)

        def js(self, expr, *a, **k):
            if expr == muse_events.DRAIN_JS:
                if self.subs is None:
                    return None
                ready = [r for t, r in self.timeline if t <= Clock.now]
                self.timeline = [(t, r) for t, r in self.timeline if t > Clock.now]
                return drain_payload(self.subs, ready)
            if expr == muse_events.STOP_VISIBLE_JS:
                return self.stop_now()
            if expr == em.MuseEngine._POLL_CHAT_JS:
                if self.poll_state is None:
                    return None
                cnt, text = self.poll_state(Clock.now)
                # 旧实现未内嵌 STOP_VISIBLE_JS 时按旧判定模拟，让回归测试真的会失败。
                stop = self.stop_now(legacy=muse_events.STOP_VISIBLE_JS not in expr)
                self.stop_log.append(stop)
                return json.dumps({"cnt": cnt, "total": cnt, "txt": text, "stop": stop})
            if "location.pathname" in expr:
                return "/thread/424a43ce-c2e9-4a30-82de-d35311f33b5c"
            return None

    def make(page, poll: Callable[[], tuple[int, str, bool]] | None = None):
        e = em.MuseEngine.__new__(em.MuseEngine)
        e.cfg = SimpleNamespace(chat_timeout=300)
        e.ensure_page = lambda *a, **k: None
        e.reset_thread = lambda **k: None
        e._send = lambda prompt: "clicked"
        e._confirm_sent = lambda *a: None
        e._user_count = lambda: 1
        if poll is not None:
            e._poll_chat = poll
            e.chat_snapshot = lambda base=0: poll()[1]
        e._page_override = page
        return e

    # MuseEngine.page 是线程本地属性；测试里直接替换类属性访问。
    em.MuseEngine.page = property(lambda self: self._page_override)

    def run(page, poll: Callable[[], tuple[int, str, bool]] | None, **kw):
        Clock.now = 0.0
        e = make(page, poll)
        finals, threads, timing = [], [], {}
        with virtual_time():
            chunks = list(e.chat_stream({}, "q", timeout=300, on_final_text=finals.append,
                                        on_thread=threads.append,
                                        on_timing=lambda k, v: timing.__setitem__(k, v), **kw))
        return chunks, finals, threads, timing

    plain = MD.replace("#", "").replace("**", "").replace("```", "")
    def dom_poll():
        # 发送前 1 个助手气泡；发送后出现第 2 个（纯文本，无格式）。
        return (1, "上一轮", False) if Clock.now <= 0 else (2, plain, False)

    def CHATSRC_B01_engine_streams_markdown_before_dom():
        stale = ev("delta.message_start", "old")
        page = FakePage([])
        page.timeline = [(-1, stale)]  # 发送前已在队列：会被当成历史排除
        page.timeline += [(1.0, ev("delta.message_start", "m1")),
                          (1.2, ev("delta.text_append", "m1", seq=0, text=MD[:10])),
                          (1.5, ev("delta.text_append", "m1", seq=1, text=MD[10:])),
                          (1.8, ev("delta.message_done", "m1", status="completed", display_text=MD))]
        chunks, finals, threads, timing = run(page, lambda: (1, "", True))
        assert "".join(chunks) == MD, chunks
        assert finals == [MD] and threads == ["424a43ce-c2e9-4a30-82de-d35311f33b5c"]
        assert timing.get("source_events") == 1 and timing["muse_first_delta_ms"] < 1500

    def CHATSRC_B02_no_subscription_falls_back_to_dom():
        chunks, finals, _, timing = run(FakePage([], subs=0), dom_poll)
        assert "".join(chunks) == plain and finals == [plain] and "source_events" not in timing
        chunks, _, _, _ = run(FakePage([], subs=None), dom_poll)
        assert "".join(chunks) == plain
        # 订阅存在但收不到 message_start、DOM 已有回复：几秒后退回 DOM
        chunks, _, _, timing = run(FakePage([], subs=3), dom_poll)
        assert "".join(chunks) == plain and timing.get("event_fallback") == 1

    def CHATSRC_B03_events_stop_midway_dom_fills_tail():
        page = FakePage([(1.0, ev("delta.message_start", "m1")),
                         (1.2, ev("delta.text_append", "m1", seq=0, text="前半段"))],
                        stop_until=5.0)
        chunks, finals, _, _ = run(page, lambda: (1, "上一轮", True) if Clock.now <= 0
                                   else (2, "前半段后半段", Clock.now < 5.0))
        assert "".join(chunks) == "前半段后半段" and finals == ["前半段后半段"], chunks

    def CHATSRC_B04_failed_raises():
        page = FakePage([(1.0, ev("delta.message_start", "m1")),
                         (1.2, ev("delta.message_done", "m1", status="failed", error="quota"))])
        try:
            run(page, lambda: (1, "", True))
        except em.MuseGenerationError as exc:
            assert "quota" in str(exc)
        else:
            raise AssertionError("failed 不能当成功")

    def CHATSRC_B08_waits_for_followup_message():
        # 真实故障：先一条文字消息完成，Stop 仍在；1 秒多后第二条（工具调用）才开始。
        call = '```tool_call\n{"name": "Bash", "arguments": {"command": "ls"}}\n```'
        intro = "我先做只读检查。"
        page = FakePage([(1.0, ev("delta.message_start", "m1")),
                         (1.1, ev("delta.text_append", "m1", seq=0, text=intro)),
                         (1.2, ev("delta.message_done", "m1", status="completed",
                                  display_text=intro)),
                         (2.5, ev("delta.message_start", "m2")),
                         (2.6, ev("delta.text_append", "m2", seq=0, text=call)),
                         (2.8, ev("delta.message_done", "m2", status="completed",
                                  display_text=call))],
                        stop_until=3.0)
        chunks, finals, _, _ = run(page, lambda: (1, "", Clock.now < 3.0))
        assert "".join(chunks) == intro + "\n\n" + call, chunks
        assert finals == [intro + "\n\n" + call]

    def CHATSRC_B09_stop_visible_conditions():
        cases = [
            (True, [FakeElement()]),
            (False, []),  # 选择器无命中：按钮已从 DOM 移除
            (False, [FakeElement(connected=False), FakeElement(rects=0),
                     FakeElement(width=0)]),
            (False, [FakeElement(opacity=0), FakeElement(display="none"),
                     FakeElement(visibility="hidden"), FakeElement(visibility="collapse")]),
            (True, [FakeElement(disabled=True)]),  # disabled 不改变视觉呈现
            (True, [FakeElement(aria_hidden=True)]),  # aria-hidden 不改变视觉呈现
            (True, [FakeElement(attrs={"hidden": True})]),  # hidden 属性被覆盖仍可见
            (False, [FakeElement(disabled=True, opacity=0),
                     FakeElement(aria_hidden=True, display="none")]),  # 仍按视觉判定
            (False, [FakeElement(parent=FakeElement(opacity=0)),
                     FakeElement(parent=FakeElement(display="none"))]),
            (True, [FakeElement(parent=FakeElement(aria_hidden=True))]),
            (True, [FakeElement(parent=FakeElement(attrs={"hidden": True}))]),
            (True, [FakeElement(parent=FakeElement(opacity=0.5), opacity=0.5)]),
            (False, [FakeElement(parent=FakeElement(visibility="hidden"),
                                 visibility="hidden")]),
            (True, [FakeElement(parent=FakeElement(visibility="hidden"),
                                visibility="visible")]),  # 后代可覆盖祖先 visibility
            (True, [FakeElement(opacity=0), FakeElement()]),  # 任一候选可见即可
        ]
        for expected, elements in cases:
            got = FakePage([], stop_elements=elements).js(muse_events.STOP_VISIBLE_JS)
            assert got is expected, (got, expected, elements)

    def CHATSRC_B10_dom_fallback_reuses_stop_visibility():
        # DOM 兜底轮询必须复用事件流同一 Stop 视觉谓词；旧判定会把透明 Stop 当作可见。
        assert muse_events.STOP_VISIBLE_JS in em.MuseEngine._POLL_CHAT_JS
        assert "getClientRects().length>0&&getComputedStyle" not in em.MuseEngine._POLL_CHAT_JS

        answer = "DOM 兜底完整正文"

        def state(now: float) -> tuple[int, str]:
            return (1, "上一轮") if now <= 0 else (2, answer)

        # 1) Stop 透明：旧判定仍视为可见并持续阻塞；新判定按静默窗口正常收尾。
        page = FakePage([], subs=0, poll_state=state,
                        stop_elements=lambda now: [FakeElement(opacity=0)])
        chunks, finals, threads, timing = run(page, None)
        assert "".join(chunks) == answer and finals == [answer], (chunks, finals)
        assert threads == ["424a43ce-c2e9-4a30-82de-d35311f33b5c"]
        assert "source_events" not in timing and Clock.now < 5.0, Clock.now
        assert page.stop_log and not any(page.stop_log), page.stop_log

        # 2) Stop 可见期间持续阻塞，t3 消失后才按静默窗口结束。
        stop_gone_at = 3.0
        page = FakePage([], subs=0, poll_state=state,
                        stop_elements=lambda now: [FakeElement()] if now < stop_gone_at else [])
        chunks, finals, _, _ = run(page, None)
        assert "".join(chunks) == answer and finals == [answer], (chunks, finals)
        assert Clock.now >= stop_gone_at, ("Stop 可见时提前结束", Clock.now)
        assert True in page.stop_log and page.stop_log[-1] is False, page.stop_log

    def CHATSRC_B07_cancel_returns_without_final():
        import threading
        stop = threading.Event()
        page = FakePage([(1.0, ev("delta.message_start", "m1")),
                         (1.2, ev("delta.text_append", "m1", seq=0, text="x"))])
        Clock.now = 0.0
        e = make(page, lambda: (1, "", True))
        finals = []
        with virtual_time():
            gen = e.chat_stream({}, "q", timeout=300, on_final_text=finals.append, stop_event=stop)
            assert next(gen) == "x"
            stop.set()
            assert list(gen) == []
        assert finals == []

    CHATSRC_B01_markdown_increments()
    CHATSRC_B03_done_fills_lost_tail()
    CHATSRC_B04_failed_status()
    CHATSRC_B05_citation_to_markdown_link()
    CHATSRC_B06_ignores_other_messages()
    CHATSRC_B01_engine_streams_markdown_before_dom()
    CHATSRC_B02_no_subscription_falls_back_to_dom()
    CHATSRC_B03_events_stop_midway_dom_fills_tail()
    CHATSRC_B04_failed_raises()
    CHATSRC_B07_cancel_returns_without_final()
    CHATSRC_B08_waits_for_followup_message()
    CHATSRC_B09_stop_visible_conditions()
    CHATSRC_B10_dom_fallback_reuses_stop_visibility()
    print("PASS: CHATSRC-B01..B10 event stream markdown, fallback, fill, failure, citations, cancel, stop visibility")
