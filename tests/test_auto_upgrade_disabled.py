"""默认不做后台静默升级：它会覆盖本地提交、改写 origin 并退出进程。

Run: python tests/test_auto_upgrade_disabled.py
"""
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

with tempfile.TemporaryDirectory() as home:
    os.environ.update(MUSE2API_HOME=home, MUSE2API_PROFILE_ROOT=home, MUSE2API_KEY="test-only")
    os.environ.pop("MUSE2API_AUTO_UPGRADE", None)
    spec = importlib.util.spec_from_file_location("auto_upgrade_target", ROOT / "app.py")
    app = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = app
    spec.loader.exec_module(app)

    def remote(url, *a, **k):
        resp = MagicMock(status_code=200)
        if "version.json" in url:
            resp.json.return_value = {"version": "9.9.9"}
        else:
            resp.json.return_value = [{"sha": "ffffffffff", "commit": {"message": "upstream"}}]
        return resp

    def check():
        app._UPDATE_CACHE["ts"] = 0.0
        with patch("requests.get", side_effect=remote), \
             patch.object(app.threading, "Thread") as thread:
            app._check_update_sync(force=True)
        return thread

    assert app.CFG.auto_upgrade is False
    assert not check().called, "默认不得启动静默升级线程"

    app.CFG.auto_upgrade = True
    assert check().called, "显式开启时仍可自动升级"
    print("PASS: silent auto-upgrade is off by default")
