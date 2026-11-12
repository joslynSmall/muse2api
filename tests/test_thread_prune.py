"""CHATPRUN 旁聊清理；不连 Muse 账号。Run: python tests/test_thread_prune.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import thread_prune  # noqa: E402
from thread_prune import deletion_candidates, prune  # noqa: E402


def side(i, **kw):
    row = {"session_id": f"s{i:03d}", "updated_at_ms": 1_700_000_000_000 + i * 1000,
           "is_primary": False, "is_thread": True, "pinned": False, "archived": False}
    row.update(kw)
    return row


def test_keeps_newest_and_returns_oldest_first_by_age():  # CHATPRUN-B01
    rows = [side(i) for i in range(55)]
    out = deletion_candidates(rows, set(), keep=50, max_delete=100)
    assert out == ["s004", "s003", "s002", "s001", "s000"]


def test_under_limit_deletes_nothing():  # CHATPRUN-B02
    assert deletion_candidates([side(i) for i in range(50)], set(), keep=50) == []


def test_protected_kinds_neither_deleted_nor_counted():  # CHATPRUN-B01 不变量
    rows = [side(i) for i in range(50)]
    special = [side(900, is_primary=True), side(901, pinned=True), side(902, archived=True),
               side(903, channel="slack"), side(904, read_only_reason="peer"),
               side(905, peer=True), side(906, is_thread=False), side(907)]
    rows += [dict(r, updated_at_ms=1) for r in special]  # 全部最旧
    out = deletion_candidates(rows, {"s907"}, keep=50, max_delete=100)
    assert out == []


def test_seconds_timestamps_sort_with_ms():
    rows = [side(1, updated_at_ms=1_700_000_100), side(2, updated_at_ms=1_700_000_000_000)]
    assert deletion_candidates(rows, set(), keep=1, max_delete=5) == ["s002"]


def test_incremental_cap():
    rows = [side(i) for i in range(80)]
    assert len(deletion_candidates(rows, set(), keep=50)) == thread_prune.INCREMENTAL_MAX_DELETE


class FakePage:
    def __init__(self, sessions, delete_results=None, list_error=None):
        self.sessions, self.results = sessions, list(delete_results or [])
        self.list_error, self.deleted = list_error, []

    def js(self, expr, **_):
        if "sessions.list" in expr:
            if self.list_error:
                raise self.list_error
            return json.dumps({"ok": True, "sessions": self.sessions})
        sid = json.loads(expr.rsplit("(", 1)[1].rstrip(")\n "))
        res = self.results.pop(0) if self.results else {"ok": True}
        if res.get("ok"):
            self.deleted.append(sid)
        return json.dumps(res)


def test_prune_deletes_beyond_limit():  # CHATPRUN-B01
    page = FakePage([side(i) for i in range(52)])
    out = prune(page, set(), keep=50)
    assert (out["deleted"], out["planned"], out["kept"], out["error"]) == (2, 2, 50, "")
    assert page.deleted == ["s001", "s000"]


def test_dry_run_deletes_nothing():  # CHATPRUN-B06
    page = FakePage([side(i) for i in range(60)])
    out = prune(page, set(), keep=50, max_delete=1 << 30, dry_run=True)
    assert out["planned"] == 10 and out["deleted"] == 0 and page.deleted == []
    assert out["targets"][0] == "s009" and out["targets"][-1] == "s000"


def test_list_failure_skips():  # CHATPRUN-B03
    page = FakePage([], list_error=RuntimeError("cdp"))
    out = prune(page, set())
    assert out["deleted"] == 0 and out["error"].startswith("list")


def test_unconfirmed_delete_stops():  # CHATPRUN-B04
    page = FakePage([side(i) for i in range(53)],
                    delete_results=[{"ok": True}, {"ok": False, "err": "not-confirmed"}])
    out = prune(page, set(), keep=50)
    assert out["deleted"] == 1 and out["planned"] == 3 and "not-confirmed" in out["error"]
    assert page.deleted == ["s002"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS:", name)
