"""网页回复事件流采集（流程 CHATSRC）。

Muse 网页的聊天回复走 Noise 加密的 WebSocket，CDP 抓帧只能拿到密文。网页前端解密后经
React context 里的 ``onEvent(name, cb)`` 分发事件；这里在页面里注入脚本订阅这些事件，放进
页面内有界队列，Python 用普通的 ``Runtime.evaluate`` 轮询读取，不需要 CDP 事件通道。

``delta.text_append`` 的 ``text`` 是 Markdown 原文，所以这条路径能保留标题、列表、代码块等格式，
且事件早于 DOM 渲染到达。订阅入口依赖网页内部结构，找不到时由调用方退回 DOM 读取。
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

# 页面加载即运行（Page.addScriptToEvaluateOnNewDocument），也可重复 evaluate：有全局守卫。
# 只订阅回复正文相关的事件；不读取、不转发任何凭据。

BOOTSTRAP_JS = r"""
(function(){
  if (window.__MUSE_EVT__) return;
  var S = window.__MUSE_EVT__ = {q: [], subs: 0, dropped: 0, err: ''};
  var NAMES = ['delta.message_start', 'delta.text_append', 'delta.message_done'];
  var hooked = new WeakSet(), seen = new WeakSet();
  function push(name, payload, meta){
    // 同一事件可能经多个 provider 分发；同一 payload 对象只记一次。
    if (payload && typeof payload === 'object') {
      if (seen.has(payload)) return;
      seen.add(payload);
    }
    try {
      S.q.push(JSON.stringify({e: name, p: payload,
                               s: meta && meta.seq != null ? meta.seq : null}));
    } catch (err) { S.err = String(err).slice(0, 200); return; }
    if (S.q.length > 4000) { S.q.splice(0, S.q.length - 4000); S.dropped++; }
  }
  function hook(node){
    if (!node) return;
    var key = Object.keys(node).find(function(k){ return k.indexOf('__reactFiber$') === 0; });
    var f = key && node[key];
    for (var i = 0; f && i < 500; i++, f = f.return) {
      var v = f.memoizedProps && f.memoizedProps.value;
      if (!v || typeof v !== 'object' || typeof v.onEvent !== 'function'
          || typeof v.sendRequest !== 'function' || v.isReady !== true
          || hooked.has(v.onEvent)) continue;
      hooked.add(v.onEvent);
      NAMES.forEach(function(name){
        try {
          v.onEvent(name, function(p, m){ push(name, p, m); });
          S.subs++;
        } catch (err) { S.err = String(err).slice(0, 200); }
      });
    }
  }
  function scan(){
    try { hook(document.querySelector('textarea')); hook(document.querySelector('main')); }
    catch (err) { S.err = String(err).slice(0, 200); }
  }
  setInterval(scan, 400);
  scan();
})();
"""

DRAIN_JS = (
    "(function(){var S=window.__MUSE_EVT__;if(!S)return null;"
    "return JSON.stringify({subs:S.subs,dropped:S.dropped,q:S.q.splice(0,400)});})()"
)

STOP_VISIBLE_JS = (
    "(function(){var es=[].slice.call(document.querySelectorAll("
    "'[data-testid=\"hatch-composer-stop-button\"],button[aria-label*=\"Stop\" i],"
    "button[aria-label*=\"停止\"]'));return es.some(function(e){"
    "return e.getClientRects().length>0&&getComputedStyle(e).visibility!=='hidden';});})()"
)

_CITE_RE = re.compile(r"【cite:([^】]+)】|【([^】†]+)†[^】]*】|\^\[([^\]\s]+)\]")
# 尚未闭合的引用标记：暂扣，等后续片段或 message_done。
_OPEN_CITE_RE = re.compile(r"【[^】]*$|\^(?:\[[^\]\s]*)?$")


def _is_main(payload: dict) -> bool:
    lane = payload.get("stream_lane")
    surface = payload.get("transcript_surface")
    if lane is None and surface is None:
        return True
    return lane == "main" or surface in ("main", "main_chat")


def _http_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urlparse(value)
        if url.hostname == "l.meta.ai":
            target = (parse_qs(url.query).get("u") or [""])[0]
            if target:
                url = urlparse(target)
                value = target
    except ValueError:
        return None
    if url.scheme in ("http", "https") and url.netloc and not url.username:
        return value
    return None


def _done_text(payload: dict) -> str | None:
    for key in ("display_text", "content"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class ReplyAssembler:
    """把一次发送之后的事件拼成追加式 Markdown 文本。

    只认发送后新出现的主对话消息；多条消息按开始顺序用空行连接。``take`` 只返回追加部分，
    已发出的内容不会被改写；引用标记在链接未知时暂扣，``message_done`` 带来资源后再替换。
    """

    def __init__(self, baseline_ids=()):
        self.baseline = set(baseline_ids)
        self.order: list[str] = []
        self.msgs: dict[str, dict] = {}
        self.citations: dict[str, tuple[str, str]] = {}
        self.emitted = ""
        self.error: str | None = None

    @property
    def started(self) -> bool:
        return bool(self.order)

    @property
    def all_done(self) -> bool:
        return bool(self.order) and all(self.msgs[m]["done"] for m in self.order)

    @property
    def has_text(self) -> bool:
        return any(self.msgs[m]["text"] for m in self.order)

    def note_existing(self, record: dict) -> None:
        """发送前已存在的消息（历史回放等）一律排除。"""
        mid = (record.get("p") or {}).get("message_id")
        if mid:
            self.baseline.add(str(mid))

    def feed(self, record: dict) -> None:
        name = record.get("e")
        payload = record.get("p")
        if not isinstance(payload, dict) or not _is_main(payload):
            return
        mid = payload.get("message_id")
        if not mid:
            return
        mid = str(mid)
        if mid in self.baseline:
            return
        msg = self.msgs.get(mid)
        if name == "delta.message_start":
            if msg is None:
                self.order.append(mid)
                self.msgs[mid] = {"text": "", "done": False, "seqs": set()}
            return
        if msg is None:
            return  # 没见到开始事件的消息不采信，避免串到别的回复
        if name == "delta.text_append":
            seq = record.get("s")
            if seq is not None:
                if seq in msg["seqs"]:
                    return
                msg["seqs"].add(seq)
            text = payload.get("text")
            if isinstance(text, str):
                msg["text"] += text
        elif name == "delta.message_done":
            msg["done"] = True
            final = _done_text(payload)
            # 完整文本以已收到的增量为前缀时才采用，用来补齐丢失的尾部。
            if final and final.startswith(msg["text"]):
                msg["text"] = final
            self._collect_citations(payload.get("resources"))
            status = payload.get("status")
            if status == "failed":
                error = payload.get("error")
                self.error = str(error.get("message") if isinstance(error, dict) else error
                                 or "Muse 生成失败")

    def _collect_citations(self, resources) -> None:
        for res in resources or []:
            if not isinstance(res, dict) or res.get("kind") != "citation":
                continue
            nested = res.get("data")
            data: dict = nested if isinstance(nested, dict) else res
            cid = data.get("citation_id") or res.get("id")
            url = next((u for u in (_http_url(data.get(k)) for k in
                                    ("permalink", "url", "link", "href")) if u), None)
            if cid and url:
                title = data.get("title") or data.get("domain") or url
                self.citations[str(cid)] = (str(title), url)

    def _render(self, final: bool) -> str:
        raw = "\n\n".join(self.msgs[m]["text"] for m in self.order if self.msgs[m]["text"])
        out, pos = [], 0
        for match in _CITE_RE.finditer(raw):
            cid = next(g for g in match.groups() if g)
            link = self.citations.get(cid.strip())
            if link is None and not final:
                # 链接还不知道：从这里暂扣，等 message_done 带来资源。
                return "".join(out) + raw[pos:match.start()]
            out.append(raw[pos:match.start()])
            if link:
                title = link[0].replace("[", "\\[").replace("]", "\\]")
                out.append("[%s](%s)" % (title, link[1].replace(")", "%29")))
            pos = match.end()
        rest = raw[pos:]
        if not final:
            tail = _OPEN_CITE_RE.search(rest)
            if tail:
                rest = rest[:tail.start()]
        out.append(rest)
        return "".join(out)

    def take(self, final: bool = False) -> str:
        """返回自上次以来可以安全发出的追加文本。"""
        visible = self._render(final)
        if not visible.startswith(self.emitted):
            return ""  # 已发内容不可改写；对不上时宁可少发也不重复
        delta = visible[len(self.emitted):]
        self.emitted = visible
        return delta

    def final_text(self) -> str:
        return self._render(final=True)


def parse_drain(raw) -> tuple[int, list[dict]] | None:
    """解析 DRAIN_JS 的结果；页面没有注入脚本时返回 None。"""
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    records = []
    for item in data.get("q") or []:
        try:
            rec = json.loads(item)
        except (TypeError, ValueError):
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return int(data.get("subs") or 0), records
