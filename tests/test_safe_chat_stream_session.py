"""CHATSESS-B10/B12：走真实 safe_chat_stream 工作线程，只替换浏览器引擎；不连 Muse 账号。

Run: python tests/test_safe_chat_stream_session.py
"""
import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY="test-only")
    spec = importlib.util.spec_from_file_location("safe_chat_stream_target", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)
    ids = [app.store.add_account({"hatch_sess": n, "hatch_vml": n, "hatch_native_auth_device": n}, n)["id"]
           for n in ("a", "b")]  # 多账号：旧代码在这里会吞掉错误

    class FakeEngine:
        def __init__(self, behavior):
            self.behavior = behavior
            self.closed = threading.Event()

        def start(self):
            pass

        def close_current_page(self):
            self.closed.set()

        def chat_stream(self, *a, stop_event=None, **kw):
            return self.behavior(stop_event, kw)

    app._renew_and_persist = lambda *a, **k: None
    app._bind_account_proxy = lambda acc: None
    app._clear_account_proxy = lambda: None

    def run(behavior, **kw):
        app.engine = FakeEngine(behavior)
        return app.safe_chat_stream({}, "prompt", None, 30, account_id=ids[0], **kw)

    def CHATSESS_B10_pinned_auth_error_is_raised():
        def boom(stop_event, kw):
            raise app.MuseAuthError("会话已被登出")
            yield  # noqa: unreachable
        try:
            list(run(boom, pin_account=True, thread_id=None))
        except app.MuseAuthError:
            pass
        else:
            raise AssertionError("绑定会话的认证错误必须抛出，不能变成空的成功回复")
        assert app.ACCOUNT_POOL.busy_accounts == set(), "失败后账号租约已释放"

    def CHATSESS_B10_pinned_generic_error_is_raised():
        def boom(stop_event, kw):
            raise RuntimeError("页面加载失败")
            yield  # noqa: unreachable
        try:
            list(run(boom, pin_account=True))
        except RuntimeError:
            pass
        else:
            raise AssertionError("绑定会话的页面错误必须抛出")

    def CHATSESS_B12_cancel_is_not_success():
        from chat_transport import ChatControl, _control
        control = ChatControl(threading.Event())

        def slow(stop_event, kw):
            yield "部分"
            while not stop_event.is_set():
                time.sleep(0.02)

        token = _control.set(control)
        try:
            gen = run(slow, pin_account=True)
            assert next(gen) == "部分"
            control.cancelled.set()
            try:
                list(gen)
            except app.MuseGenerationError:
                pass
            else:
                raise AssertionError("取消后不能当作正常结束（否则会提交会话绑定）")
        finally:
            _control.reset(token)

    def CHATSESS_B12_early_close_stops_worker_fast():
        engine_stopped = threading.Event()

        def slow(stop_event, kw):
            yield "首段"
            while not stop_event.is_set():
                time.sleep(0.02)
            engine_stopped.set()

        gen = run(slow, pin_account=True)
        assert next(gen) == "首段"
        started = time.monotonic()
        gen.close()  # 调用方异常退出 / 提前关闭生成器，没有外层取消信号
        assert time.monotonic() - started < 3, "提前关闭不能卡到总时限"
        assert engine_stopped.is_set(), "关闭返回前浏览器线程已收到停止信号"

    CHATSESS_B10_pinned_auth_error_is_raised()
    CHATSESS_B10_pinned_generic_error_is_raised()
    CHATSESS_B12_cancel_is_not_success()
    CHATSESS_B12_early_close_stops_worker_fast()
    print("PASS: CHATSESS-B10/B12 real safe_chat_stream worker raises errors and stops on cancel")
