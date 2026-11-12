"""CHATSESS 会话绑定状态机；不连 Muse 账号。Run: python tests/test_chat_sessions.py"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chat_sessions import ChatSessionStore  # noqa: E402

THREAD_A = "424a43ce-c2e9-4a30-82de-d35311f33b5c"
THREAD_B = "819e0425-77d4-433f-b457-c3fb91bfdc79"


def msg(role, content, **kw):
    return SimpleNamespace(role=role, content=content, tool_call_id=kw.get("tool_call_id"),
                           tool_calls=kw.get("tool_calls"), name=None)


def CHATSESS_B01_stateless_without_header(store):
    plan = store.plan(None, [msg("user", "hi")])
    assert plan.mode == "stateless" and not plan.bound
    store.commit(plan, None, "acc")  # 无会话不写 store
    assert store.sessions == {}


def CHATSESS_B02_first_turn_binds_after_uuid(store, key):
    history = [msg("system", "sys"), msg("user", "first")]
    plan = store.plan(key, history)
    assert plan.mode == "new" and plan.thread_id is None and plan.messages == history
    try:
        store.commit(plan, None, "acc-a")
    except ValueError:
        pass
    else:
        raise AssertionError("没拿到 thread uuid 不能写绑定")
    assert store.get(key) is None
    store.commit(plan, THREAD_A, "acc-a")
    row = store.get(key)
    assert row["thread_id"] == THREAD_A and row["account_id"] == "acc-a"
    return history


def CHATSESS_B03_continue_sends_only_new_inputs(store, key, history):
    history = history + [msg("assistant", "answer"), msg("user", "second")]
    plan = store.plan(key, history)
    assert plan.mode == "continue" and plan.thread_id == THREAD_A
    assert plan.account_id == "acc-a"
    assert [m.content for m in plan.messages] == ["second"]
    store.commit(plan, THREAD_A, "acc-a")
    # 客户端原样重发上一轮：仍回到同一 thread，只发最后一条输入。
    again = store.plan(key, history)
    assert again.mode == "continue" and [m.content for m in again.messages] == ["second"]
    return history


def CHATSESS_B06_tool_result_goes_to_same_thread(store, key, history):
    call = {"id": "call-1", "function": {"name": "Read", "arguments": "{}"}}
    history = history + [msg("assistant", None, tool_calls=[call]),
                         msg("tool", "file body", tool_call_id="call-1")]
    plan = store.plan(key, history)
    assert plan.mode == "continue" and plan.thread_id == THREAD_A
    assert [(m.role, m.tool_call_id) for m in plan.messages] == [("tool", "call-1")]
    store.commit(plan, THREAD_A, "acc-a")
    return history


def CHATSESS_B04_compacted_history_rebinds(store, key):
    compacted = [msg("system", "sys"), msg("user", "summary of earlier turns"),
                 msg("user", "next")]
    plan = store.plan(key, compacted)
    assert plan.mode == "rebind" and plan.thread_id is None
    assert plan.account_id == "acc-a", "重绑保持同一账号"
    assert plan.messages == compacted, "重绑发送完整历史"
    store.commit(plan, THREAD_B, "acc-a")
    assert store.get(key)["thread_id"] == THREAD_B


def CHATSESS_B07_failed_turn_does_not_advance(store, key):
    before = store.get(key)
    plan = store.plan(key, [msg("system", "sys"), msg("user", "summary of earlier turns"),
                            msg("user", "next"), msg("assistant", "x"), msg("user", "lost")])
    assert plan.mode == "continue"
    # 失败 / 取消：端点不调用 commit，绑定保持上次成功状态。
    assert store.get(key) == before
    try:
        store.commit(plan, THREAD_A, "acc-a")
    except ValueError:
        pass
    else:
        raise AssertionError("续轮 thread 变化必须拒绝")
    assert store.get(key) == before


def CHATSESS_B08_concurrent_turn_rejected(store, key):
    first = store.try_begin(key, ttl=60)
    assert first and not store.try_begin(key, ttl=60)
    store.end(key, first)
    second = store.try_begin(key, ttl=60)
    assert second
    store.end(key, second)


def CHATSESS_B12_stale_owner_cannot_release_new_turn(store, key):
    stale = store.try_begin(key, ttl=60)
    newer = store.try_begin(key, ttl=0)  # 旧占用过期，被新请求接管
    assert stale and newer and stale != newer
    store.end(key, stale)  # 旧请求结束，不能清掉新请求的占用
    assert not store.try_begin(key, ttl=60), "新请求的占用必须仍然有效"
    store.end(key, newer)
    assert store.try_begin(key, ttl=60)


def run():
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "chat-sessions.json")
        store = ChatSessionStore(path)
        key = store.key("claude-session-1")
        assert key != store.key("claude-session-1", "agent-child")
        assert key != store.key("claude-session-2")

        CHATSESS_B01_stateless_without_header(store)
        history = CHATSESS_B02_first_turn_binds_after_uuid(store, key)
        history = CHATSESS_B03_continue_sends_only_new_inputs(store, key, history)
        CHATSESS_B06_tool_result_goes_to_same_thread(store, ChatSessionStore(path).key("claude-session-1"), history)

        reloaded = ChatSessionStore(path)
        assert reloaded.get(key)["thread_id"] == THREAD_A, "重启后绑定保留"
        assert reloaded.get(reloaded.key("claude-session-1", "agent-child")) is None

        CHATSESS_B04_compacted_history_rebinds(reloaded, key)
        CHATSESS_B07_failed_turn_does_not_advance(reloaded, key)
        CHATSESS_B08_concurrent_turn_rejected(reloaded, key)
        CHATSESS_B12_stale_owner_cannot_release_new_turn(reloaded, key)
        # 会话键的字段边界不能被内容伪造。
        assert reloaded.key("a|agent:b") != reloaded.key("a", "b")

        raw = Path(path).read_text(encoding="utf-8")
        for secret in ("claude-session-1", "first", "second", "file body"):
            assert secret not in raw, "状态文件不能保存会话 ID 或正文"
        assert json.loads(raw)["version"] == 2

    print("PASS: CHATSESS-B01..B08, B12 bindings, delta, rebind, failure, concurrency")


def CHATSESS_B12_stale_owner_cannot_commit():
    with tempfile.TemporaryDirectory() as directory:
        store = ChatSessionStore(str(Path(directory) / "s.json"))
        key = store.key("stale-commit")
        stale = store.try_begin(key, ttl=60)
        plan = store.plan(key, [msg("user", "q")])
        newer = store.try_begin(key, ttl=0)  # 旧占用过期，被新请求接管
        try:
            store.commit(plan, THREAD_A, "acc", token=stale)
        except ValueError:
            pass
        else:
            raise AssertionError("被接管的旧请求不能写入绑定")
        assert store.get(key) is None
        store.commit(plan, THREAD_B, "acc", token=newer)
        assert store.get(key)["thread_id"] == THREAD_B
    print("PASS: CHATSESS-B12 stale owner cannot commit")


if __name__ == "__main__":
    run()
    CHATSESS_B12_stale_owner_cannot_commit()

