"""CHATSESS-B14/B15 续轮热页面复用与回收；不连 Muse 账号。

Run: python tests/test_chat_page_reuse.py
"""
import importlib.util
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
THREAD_A = "424a43ce-c2e9-4a30-82de-d35311f33b5c"
THREAD_B = "819e0425-77d4-433f-b457-c3fb91bfdc79"


class FakePage:
    def __init__(self, path, ready=True):
        self.path, self.ready, self.closed = path, ready, False

    def js(self, expr, *a, **k):
        if "location.pathname" in expr:
            return self.path
        return None

    def close(self):
        self.closed = True


with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY="test-only",
                      MUSE2API_TOOL_PROTOCOL="1", MUSE2API_CHAT_PAGE_IDLE="600")
    spec = importlib.util.spec_from_file_location("chat_page_reuse_target", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    engine = app.engine
    engine.browser = None  # _close_page 不发 CDP 命令
    engine._wait_ws_ready = lambda page, timeout=0: page.ready

    def open_page(path, account="acc1"):
        page = FakePage(path)
        engine.page = page
        engine.current_acc_id = account
        return page

    def CHATSESS_B14_park_and_adopt_same_session():
        page = open_page("/thread/" + THREAD_A)
        assert engine.park_current_page("acc1", "sess-1", THREAD_A)
        assert engine.page is None and not page.closed, "停放后线程不再持有，页面不关闭"
        # 不同线程（下一次请求的工作线程）接管
        got = []
        t = threading.Thread(target=lambda: got.append(
            (engine.adopt_parked_page("acc1", THREAD_A, "sess-1", 600), engine.page)))
        t.start(); t.join()
        assert got[0] == (True, page) and not page.closed
        assert engine._parked == {}

    def CHATSESS_B14_mismatch_falls_back_to_cold_start():
        for owner, thread, idle in (("sess-2", THREAD_A, 600),   # 别的会话
                                    ("sess-1", THREAD_B, 600),   # 别的 thread
                                    ("sess-1", THREAD_A, -1)):    # 已超时
            page = open_page("/thread/" + THREAD_A)
            assert engine.park_current_page("acc1", "sess-1", THREAD_A)
            assert engine.adopt_parked_page("acc1", thread, owner, idle) is False
            assert page.closed and engine._parked == {}, (owner, thread, idle)
        # 停放期间页面被导航走 / 断线：接管时校验失败并关闭
        page = open_page("/thread/" + THREAD_A)
        engine.park_current_page("acc1", "sess-1", THREAD_A)
        page.path = "/thread/new"
        assert engine.adopt_parked_page("acc1", THREAD_A, "sess-1", 600) is False
        assert page.closed and engine.page is None
        page = open_page("/thread/" + THREAD_A)
        engine.park_current_page("acc1", "sess-1", THREAD_A)
        page.ready = False
        assert engine.adopt_parked_page("acc1", THREAD_A, "sess-1", 600) is False
        assert page.closed

    def CHATSESS_B15_park_rejects_wrong_page():
        page = open_page("/thread/new")
        assert engine.park_current_page("acc1", "sess-1") is False and page.closed
        page = open_page("/thread/" + THREAD_B)
        assert engine.park_current_page("acc1", "sess-1", THREAD_A) is False and page.closed
        page = open_page("/thread/" + THREAD_A, account="acc2")
        assert engine.park_current_page("acc1", "sess-1", THREAD_A) is False and page.closed
        assert engine._parked == {}

    def CHATSESS_B15_one_page_per_account_and_reclaim():
        first = open_page("/thread/" + THREAD_A)
        engine.park_current_page("acc1", "sess-1", THREAD_A)
        second = open_page("/thread/" + THREAD_B)
        engine.park_current_page("acc1", "sess-2", THREAD_B)
        assert first.closed and not second.closed, "同账号只保留一个热页面"
        engine.discard_parked_unless("acc1", "sess-2")
        assert not second.closed, "本会话自己用账号时保留"
        engine.discard_parked_unless("acc1", "sess-9")
        assert second.closed and engine._parked == {}, "别的会话用账号前关闭"
        page = open_page("/thread/" + THREAD_A)
        engine.park_current_page("acc1", "sess-1", THREAD_A)
        assert engine.discard_parked(max_idle=600) == 0 and not page.closed
        engine._parked["acc1"]["parked_at"] -= 1000
        assert engine.discard_parked(max_idle=600) == 1 and page.closed

    def CHATSESS_B15_account_operation_closes_parked():
        acc = app.store.add_account({"hatch_sess": "x", "hatch_vml": "x",
                                     "hatch_native_auth_device": "x"}, "x")
        page = open_page("/thread/" + THREAD_A, account=acc["id"])
        engine.park_current_page(acc["id"], "sess-1", THREAD_A)
        with app._account_operation(acc["id"]):
            assert page.closed, "维护操作前关闭热页面"
        page = open_page("/thread/" + THREAD_A, account=acc["id"])
        engine.park_current_page(acc["id"], "sess-1", THREAD_A)
        got = app.ACCOUNT_POOL.acquire(account_id=acc["id"], timeout=1, parked_owner="sess-other")
        app.ACCOUNT_POOL.release(got["id"])
        assert page.closed, "其他会话借用账号前关闭"

    def CHATSESS_B14_worker_parks_only_on_success():
        acc = app.store.add_account({"hatch_sess": "w", "hatch_vml": "w",
                                     "hatch_native_auth_device": "w"}, "w")
        pages = []

        def fake_chat_stream(cookies, prompt, *a, **kw):
            if engine.page is None:
                pages.append(open_page("/thread/" + THREAD_A, account=acc["id"]))
            if "FAIL" in prompt:
                raise app.MuseGenerationError("boom")
            yield "ok"

        def run(prompt, thread_id, key: str | None = "sess-w"):
            with patch.object(engine, "start", lambda: None), \
                 patch.object(engine, "chat_stream", side_effect=fake_chat_stream):
                try:
                    return "".join(app.safe_chat_stream(
                        acc["cookies"], prompt, None, 30, acc["id"], thread_id=thread_id,
                        pin_account=True, session_key=key))
                except app.MuseGenerationError:
                    return None

        assert run("first", None) == "ok"
        assert acc["id"] in engine._parked and not pages[0].closed, "成功后停放"
        assert run("second", THREAD_A) == "ok"
        assert len(pages) == 1, "续轮复用同一页面，不新开"
        assert run("FAIL", THREAD_A) is None
        assert pages[0].closed and acc["id"] not in engine._parked, "失败不停放"
        assert run("third", THREAD_A) == "ok" and len(pages) == 2, "失败后冷启动"
        assert run("other", None, key=None) == "ok"
        assert acc["id"] not in engine._parked, "无状态请求不停放，且会关闭他人热页面"

    CHATSESS_B14_park_and_adopt_same_session()
    CHATSESS_B14_mismatch_falls_back_to_cold_start()
    CHATSESS_B15_park_rejects_wrong_page()
    CHATSESS_B15_one_page_per_account_and_reclaim()
    CHATSESS_B15_account_operation_closes_parked()
    CHATSESS_B14_worker_parks_only_on_success()
    print("PASS: CHATSESS-B14/B15 page reuse, mismatch fallback, reclaim, worker park on success")
