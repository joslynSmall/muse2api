"""旁聊数量清理（流程 CHATPRUN）：同一 Muse 账号只保留最后活动时间最新的若干个旁聊。

在页面里经网页自带的 Gateway ``sendRequest`` 调用 ``sessions.list`` / ``session.delete``，
与网页侧栏「删除」走同一接口。删除不可恢复，所以候选筛选在 Python 侧完成、可单测；
页面脚本只负责列出与逐个删除，遇到任何未确认结果立即停止。
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("muse2api")

KEEP = 50
# 增量清理在回复结束前同步执行，每次通常只需删 1 个；上限既控制附加耗时，
# 也防止异常列表导致大面积删除。存量由手动清理（scripts/prune_threads.py）处理。
INCREMENTAL_MAX_DELETE = 5

# 找到页面里已连接的 Gateway 客户端（与 muse_events 同一来源），没有就返回 null。
_GATEWAY_JS = r"""
function __museGateway(){
  var roots=[document.querySelector('textarea'),document.querySelector('main'),document.body];
  for (var r=0;r<roots.length;r++){
    var n=roots[r]; if(!n) continue;
    var k=Object.keys(n).find(function(x){return x.indexOf('__reactFiber$')===0;});
    for (var f=k&&n[k], i=0; f && i<500; i++, f=f.return){
      var v=f.memoizedProps&&f.memoizedProps.value;
      if (v && typeof v==='object' && typeof v.sendRequest==='function' && v.isReady===true) return v;
    }
  }
  return null;
}
"""

LIST_JS = _GATEWAY_JS + r"""
(async function(){
  var gw=__museGateway(); if(!gw) return JSON.stringify({ok:false,err:'no-gateway'});
  try{
    var r=await gw.sendRequest('sessions.list',{include_peer_sidechats:true});
    var s=(r&&r.sessions)||[];
    return JSON.stringify({ok:true,sessions:s.map(function(x){return {
      session_id:x.session_id,updated_at_ms:x.updated_at_ms,is_primary:x.is_primary,
      is_thread:x.is_thread,pinned:x.pinned,archived:x.archived,channel:x.channel||null,
      read_only_reason:x.read_only_reason||null,peer:!!(x.peer_muse||x.peer_sidechat)};})});
  }catch(e){return JSON.stringify({ok:false,err:String(e&&(e.code||e.message)||e).slice(0,200)});}
})()
"""

DELETE_JS = _GATEWAY_JS + r"""
(async function(id){
  var gw=__museGateway(); if(!gw) return JSON.stringify({ok:false,err:'no-gateway'});
  try{
    var r=await gw.sendRequest('session.delete',{method:'/api/session/delete',session_id:id});
    return JSON.stringify({ok:!!(r&&r.deleted===true),err:r&&r.deleted===true?'':'not-confirmed'});
  }catch(e){return JSON.stringify({ok:false,err:String(e&&(e.code||e.message)||e).slice(0,200)});}
})(%s)
"""


def _ms(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number * 1000 if 0 < number < 1e11 else number  # 兼容秒级时间


def deletion_candidates(sessions: list[dict], protect: set[str], keep: int = KEEP,
                        max_delete: int = INCREMENTAL_MAX_DELETE) -> list[str]:
    """返回应删除的 session_id，最旧的在后。

    只计普通旁聊：主要聊天、置顶、归档、频道、只读/对端会话、受保护 thread 既不删除也不计数。
    """
    pool = []
    for s in sessions:
        if not isinstance(s, dict):
            continue
        sid = s.get("session_id")
        if (not isinstance(sid, str) or not sid or s.get("is_primary") is True
                or s.get("is_thread") is not True or s.get("pinned") is True
                or s.get("archived") is True or s.get("channel")
                or s.get("read_only_reason") or s.get("peer") or sid in protect):
            continue
        pool.append(s)
    pool.sort(key=lambda s: _ms(s.get("updated_at_ms")), reverse=True)
    return [s["session_id"] for s in pool[keep:]][:max(0, max_delete)]


def prune(page, protect: set[str], keep: int = KEEP,
          max_delete: int = INCREMENTAL_MAX_DELETE, dry_run: bool = False) -> dict:
    """列出并删除超出数量的旁聊。任何失败只记日志，不抛出。dry_run 只返回计划。"""
    result = {"deleted": 0, "planned": 0, "error": "", "kept": 0, "targets": []}
    try:
        listing = json.loads(page.js(LIST_JS, await_promise=True, timeout=30) or "{}")
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"list: {type(exc).__name__}"
        return result
    if not listing.get("ok"):
        result["error"] = "list: " + str(listing.get("err") or "unknown")
        return result
    sessions = listing.get("sessions") or []
    targets = deletion_candidates(sessions, protect, keep, max_delete)
    result["planned"] = len(targets)
    result["kept"] = len(deletion_candidates(sessions, protect, 0, 1 << 30)) - len(targets)
    if dry_run:
        result["targets"] = targets
        return result
    for sid in targets:
        try:
            res = json.loads(page.js(DELETE_JS % json.dumps(sid), await_promise=True,
                                     timeout=30) or "{}")
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"delete: {type(exc).__name__}"
            break
        if not res.get("ok"):
            # 未确认删除（含限流、网关切换）：停止，下次新建 thread 时再继续。
            result["error"] = "delete: " + str(res.get("err") or "unknown")
            break
        result["deleted"] += 1
    return result
