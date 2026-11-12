"""Real Chromium DOM regression for anchor preservation in chat snapshots.

No Muse account or network is used.
"""
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("chat_links_engine", ROOT / "engine.py")
engine_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = engine_module
spec.loader.exec_module(engine_module)

with tempfile.TemporaryDirectory(prefix="muse-links-test-", dir=Path.home()) as home:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    cfg = SimpleNamespace(
        chromium=shutil.which("chromium") or shutil.which("google-chrome"),
        cdp_port=port, home_dir=str(Path.home()), extra_path="",
        profile_dir=home + "/profile", data_dir=home, download_dir=home + "/downloads")
    Path(cfg.download_dir).mkdir()
    engine = engine_module.MuseEngine(cfg)
    try:
        engine.start()
        engine.page = engine._open_page()
        page = engine.page
        page.js("document.body.innerHTML=" + __import__("json").dumps(
            '<main><div class="hatch-chat-groupable-bubble hatch-agent-bubble-bg">'
            '<p>Read <a href="https://docs.example/guide?x=1&y=2">LangChain 官方文档</a>.</p>'
            '<p><a href="https://l.meta.ai/?u=https%3A%2F%2Fdocs.example%2Fwrapped&amp;h=signature">包装链接</a></p>'
            '<pre><code>&lt;a href="https://code.example"&gt;snippet&lt;/a&gt;</code></pre>'
            '<p><a href="javascript:alert(1)">危险链接</a></p>'
            '<p style="display:none"><a href="https://hidden.example">hidden</a></p>'
            '</div></main>'))
        snapshot = engine._poll_chat()[1]
        assert "[LangChain 官方文档](<https://docs.example/guide?x=1&y=2>)" in snapshot, snapshot
        assert "[包装链接](<https://docs.example/wrapped>)" in snapshot, snapshot
        assert "l.meta.ai" not in snapshot and "signature" not in snapshot, snapshot
        assert '<a href="https://code.example">snippet</a>' in snapshot, snapshot
        assert "危险链接" in snapshot and "javascript:" not in snapshot, snapshot
        assert "hidden.example" not in snapshot and "hidden" not in snapshot, snapshot

        first = "Read [LangChain 官方文档](<https://docs.example/"
        whole = first + "guide?x=1&y=2>)"
        delta, emitted = engine_module.MuseEngine._stream_delta("", first)
        tail, emitted = engine_module.MuseEngine._stream_delta(emitted, whole)
        assert delta + tail == whole and "https://docs.example/guide?x=1&y=2" in delta + tail
        print("PASS: visible links retain HTTP(S) targets; code, hidden and unsafe links stay safe")
    finally:
        engine.stop()
