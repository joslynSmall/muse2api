"""Virtual-clock regressions for chat polling.

Covers the cases that used to end a tool turn too early or duplicate text:
progress pauses while Stop is visible, Stop disappearing (quiet window restart),
cold first tokens (90s for tools / 40s for plain chat), DOM re-renders that are
not prefix extensions, cancellation, and send rejection.
"""
import importlib.util
import os
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("tool_polling_candidate", ROOT / "engine.py")
    engine_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = engine_module
    spec.loader.exec_module(engine_module)

    class Clock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += seconds

        @classmethod
        def reset(cls):
            cls.now = 0.0

    @contextmanager
    def virtual_time():
        # engine.py does `import time`, so patching the module attributes also
        # replaces the process-wide time functions. Restore them afterwards.
        old_monotonic, old_sleep = engine_module.time.monotonic, engine_module.time.sleep
        engine_module.time.monotonic, engine_module.time.sleep = Clock.monotonic, Clock.sleep
        try:
            yield
        finally:
            engine_module.time.monotonic, engine_module.time.sleep = old_monotonic, old_sleep

    def make_engine(poll):
        engine = engine_module.MuseEngine.__new__(engine_module.MuseEngine)
        engine.cfg = SimpleNamespace(chat_timeout=300)
        engine.ensure_page = lambda *a, **k: None
        engine.reset_thread = lambda **k: None
        engine._send = lambda prompt: "clicked"
        engine._poll_chat = poll
        return engine

    def run(poll, *, expect_tools=True, timeout=300, on_final=None, stop_event=None):
        Clock.reset()
        engine = make_engine(poll)
        chunks = list(engine.chat_stream({}, "request", timeout=timeout,
                                         stop_event=stop_event, expect_tools=expect_tools,
                                         on_final_text=on_final))
        return chunks, Clock.now

    PROGRESS = "已切到热搜页面，正在读取榜单。"
    CALL = PROGRESS + ' tool_call\n{"name":"Read","arguments":{}}'
    PREV = "上一轮回答"

    # 1) Progress pause: Stop stays visible for 70s; the later tool call must
    #    still be captured, and the turn may only end ~4s after Stop disappears.
    def poll_progress():
        if Clock.now < 0.05:
            return 1, PREV, False
        if Clock.now < 70.0:
            return 2, PROGRESS, True
        return 2, CALL, False

    finals = []
    with virtual_time():
        chunks, elapsed = run(poll_progress, on_final=finals.append)
    assert chunks == [PROGRESS, CALL[len(PROGRESS):]], chunks
    assert 73.9 <= elapsed <= 74.3, elapsed
    assert finals == [CALL], finals

    # 2) Stop toggle: silence while Stop exists is never completion; when Stop
    #    disappears the quiet window restarts and a late update is still caught.
    def poll_toggle():
        if Clock.now < 0.05:
            return 1, PREV, False
        if Clock.now < 20.0:
            return 2, PROGRESS, True
        if Clock.now < 23.5:
            return 2, PROGRESS, False
        return 2, PROGRESS + " 完成", False

    with virtual_time():
        chunks, elapsed = run(poll_toggle)
    assert chunks == [PROGRESS, " 完成"], chunks
    assert 27.4 <= elapsed <= 27.8, elapsed

    # 2b) The previous bubble may re-render in place. Same bubble count means it
    #     is not the new reply; only the incremented count starts streaming.
    def poll_old_rerender():
        if Clock.now < 0.05:
            return 1, PREV, False
        if Clock.now < 10.0:
            return 1, PREV + "（重渲染）", False
        return 2, "新回复", False

    with virtual_time():
        chunks, elapsed = run(poll_old_rerender, expect_tools=True)
    assert chunks == ["新回复"], chunks
    assert 13.9 <= elapsed <= 14.2, elapsed

    # 3) Cold first token: tools get 90s, plain chat keeps the 40s window.
    def poll_late():
        if Clock.now < 88.0:
            return 1, PREV, False
        return 2, "late", False

    with virtual_time():
        chunks, elapsed = run(poll_late, expect_tools=True)
    assert chunks == ["late"], chunks
    assert 92.0 <= elapsed <= 92.3, elapsed

    def poll_never():
        return 1, PREV, False

    for expect_tools, limit in ((True, 90.0), (False, 40.0)):
        Clock.reset()
        engine = make_engine(poll_never)
        try:
            with virtual_time():
                list(engine.chat_stream({}, "request", timeout=300,
                                        expect_tools=expect_tools))
        except engine_module.MuseGenerationError as exc:
            assert "首字" in str(exc), exc
        else:
            raise AssertionError("cold start must time out")
        assert limit <= Clock.now <= limit + 0.1, (expect_tools, Clock.now)

    # 4) Non-prefix DOM re-render: a transient shrink and a divergent rewrite
    #    must not re-send the whole bubble; the final callback carries the
    #    authoritative snapshot for tool parsing.
    FIRST = "先查现状。"
    CALL_BLOCK = FIRST + ' tool_call\n{"name":"Read","arguments":{}}'
    SETTLED = FIRST + "工具已就绪。"

    def poll_rerender():
        if Clock.now < 0.05:
            return 1, PREV, False
        if Clock.now < 0.5:
            return 2, FIRST, True
        if Clock.now < 1.0:
            return 2, CALL_BLOCK, True
        if Clock.now < 1.5:
            return 2, FIRST, True
        return 2, SETTLED, False

    finals = []
    with virtual_time():
        chunks, elapsed = run(poll_rerender, on_final=finals.append)
    joined = "".join(chunks)
    assert chunks == [FIRST, CALL_BLOCK[len(FIRST):], "工具已就绪。"], chunks
    assert joined == CALL_BLOCK + "工具已就绪。", joined
    assert joined.count(FIRST) == 1, joined
    assert chunks.count(SETTLED) == 0, chunks
    assert finals == [SETTLED], finals
    assert 5.4 <= elapsed <= 5.7, elapsed

    # 4b) Markdown rewrites can drop markers (`**bold` -> `bold`). The visible
    #     text is already sent, so resync silently and keep streaming growth.
    def poll_markdown():
        if Clock.now < 0.05:
            return 1, PREV, False
        if Clock.now < 0.5:
            return 2, "**粗体", True
        if Clock.now < 1.0:
            return 2, "粗体", True
        if Clock.now < 1.5:
            return 2, "粗体完成", True
        return 2, "粗体完成", False

    finals = []
    with virtual_time():
        chunks, elapsed = run(poll_markdown, on_final=finals.append)
    assert chunks == ["**粗体", "完成"], chunks
    assert "".join(chunks) == "**粗体完成", chunks
    assert finals == ["粗体完成"], finals

    # 4c) A completely different snapshot is never re-sent wholesale.
    def poll_divergent():
        if Clock.now < 0.05:
            return 1, PREV, False
        if Clock.now < 0.5:
            return 2, "alpha", True
        if Clock.now < 1.0:
            return 2, "bravo", True
        if Clock.now < 1.5:
            return 2, "bravo!", True
        return 2, "bravo!", False

    finals = []
    with virtual_time():
        chunks, elapsed = run(poll_divergent, on_final=finals.append)
    assert chunks == ["alpha", "!"], chunks
    assert finals == ["bravo!"], finals

    # 5) Cancellation returns promptly without an error or final snapshot.
    def poll_cancel():
        if Clock.now < 0.05:
            return 1, PREV, False
        return 2, "处理中", True

    Clock.reset()
    engine = make_engine(poll_cancel)
    stop_event = threading.Event()
    finals = []
    with virtual_time():
        stream = engine.chat_stream({}, "request", timeout=300, stop_event=stop_event,
                                    expect_tools=True, on_final_text=finals.append)
        first = next(stream)
        stop_event.set()
        resumed_at = Clock.now
        rest = list(stream)
    assert first == "处理中" and rest == [], (first, rest)
    assert Clock.now - resumed_at <= 0.12, Clock.now - resumed_at
    assert finals == [], finals

    # 6) DOM text extraction keeps anchor targets so clients receive usable URLs.
    assert "getAttribute('href')" in engine_module.MuseEngine._POLL_CHAT_JS
    assert "new URL(raw,document.baseURI)" in engine_module.MuseEngine._POLL_CHAT_JS
    assert "url.protocol==='https:'||url.protocol==='http:'" in engine_module.MuseEngine._POLL_CHAT_JS
    assert "[LangChain 官方文档]" not in engine_module.MuseEngine._POLL_CHAT_JS
    first_link = "教程：LangChain 官方文档"
    linked = first_link + " (https://docs.example/guide)"
    delta, emitted = engine_module.MuseEngine._stream_delta("", linked[:12])
    assert delta == linked[:12]
    delta, emitted = engine_module.MuseEngine._stream_delta(emitted, linked)
    assert delta == linked[12:] and emitted == linked

    # 7) A rejected Send click never enters polling.
    Clock.reset()
    engine = make_engine(poll_never)
    engine._send = lambda prompt: "no-button"
    try:
        with virtual_time():
            list(engine.chat_stream({}, "request", timeout=30, expect_tools=True))
    except engine_module.MuseGenerationError as exc:
        assert "未能提交" in str(exc), exc
    else:
        raise AssertionError("rejected send must fail fast")

print("PASS: tool polling survives pauses, Stop toggles, cold starts, re-renders and cancellation")
