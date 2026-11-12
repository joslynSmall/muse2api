"""Send verification: a Send click is not delivery proof.

The composer must clear (or a new user bubble must appear) before chat_stream
starts polling; a permanently filled composer fails, and an unreadable page
fails open only after two consecutive read errors.
"""
import importlib.util
import json
import os
from contextlib import contextmanager
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home,
                      MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("chat_polling_send_candidate",
                                                  ROOT / "engine.py")
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

    @contextmanager
    def virtual_time():
        old_monotonic, old_sleep = engine_module.time.monotonic, engine_module.time.sleep
        engine_module.time.monotonic, engine_module.time.sleep = Clock.monotonic, Clock.sleep
        try:
            yield
        finally:
            engine_module.time.monotonic, engine_module.time.sleep = old_monotonic, old_sleep

    def composer(value, users):
        return json.dumps({"v": value, "users": users})

    class Page:
        def __init__(self, replies):
            self.replies = list(replies)
            self.timeouts = []

        def js(self, expr, timeout=None):
            self.timeouts.append(timeout)
            item = self.replies.pop(0) if self.replies else RuntimeError("no reply")
            if isinstance(item, BaseException):
                raise item
            return item

    def make_engine(replies):
        engine = engine_module.MuseEngine.__new__(engine_module.MuseEngine)
        engine.cfg = SimpleNamespace(chat_timeout=30)
        engine.page = Page(replies)
        return engine

    # 1) The composer clears after the click.
    engine = make_engine([composer("prompt", 1), composer("", 1)])
    Clock.now = 0.0
    with virtual_time():
        engine._confirm_sent(1, "clicked")
    assert Clock.now == 0.1, Clock.now
    assert engine.page.timeouts == [5, 5], engine.page.timeouts

    # 2) A new user bubble proves submission even if the textarea lags.
    engine = make_engine([composer("prompt", 1), composer("prompt", 2)])
    Clock.now = 0.0
    with virtual_time():
        engine._confirm_sent(1, "clicked")
    assert Clock.now == 0.1, Clock.now

    # 3) A composer that never clears is a hard failure, not silent success.
    engine = make_engine([composer("prompt", 1) for _ in range(200)])
    Clock.now = 0.0
    with virtual_time():
        try:
            engine._confirm_sent(1, "clicked", timeout=6.0)
        except engine_module.MuseGenerationError as exc:
            assert "仍有内容" in str(exc), exc
        else:
            raise AssertionError("pending composer must fail")
    assert 5.9 <= Clock.now <= 6.1, Clock.now

    # 4) Two consecutive read errors fail open instead of blocking the turn.
    engine = make_engine([RuntimeError("cdp gone"), RuntimeError("cdp gone")])
    Clock.now = 0.0
    with virtual_time():
        engine._confirm_sent(1, "clicked")
    assert Clock.now == 0.1, Clock.now

    # 5) One transient read error followed by a readable pending state still
    #    fails after the deadline (the error counter resets on success).
    engine = make_engine([RuntimeError("blip")] + [composer("prompt", 1) for _ in range(200)])
    Clock.now = 0.0
    with virtual_time():
        try:
            engine._confirm_sent(1, "clicked", timeout=3.0)
        except engine_module.MuseGenerationError:
            pass
        else:
            raise AssertionError("readable pending composer must fail")
    assert Clock.now >= 3.0, Clock.now

print("PASS: send result is verified by composer state / user bubble, not the click")
