"""agentbelt imports the iTerm2 Python API at module level; the tests cover the logic that
does not need a running iTerm, so a stub module stands in for `iterm2`, and every path the
service reads or writes is pointed into a temporary directory."""
import copy
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.modules.setdefault("iterm2", types.ModuleType("iterm2"))
# keep the real ~/.config/agentbelt out of reach before the module computes its paths
os.environ["AGENTBELT_HOME"] = tempfile.mkdtemp(prefix="agentbelt-home-")
os.environ["ITERMSNAP_HOME"] = tempfile.mkdtemp(prefix="itermsnap-home-")

import pytest  # noqa: E402

import agentbelt as tb  # noqa: E402

PROXIES = [{"name": "eu-1", "url": "http://user:s3cret@203.0.113.10:3128"},
           {"name": "us-1", "url": "http://bob:pa55@198.51.100.7:8080"}]


@pytest.fixture
def t(tmp_path, monkeypatch):
    """Fresh module state on temporary paths, with two proxies in the config."""
    cfg_dir = tmp_path / "cfg"
    monkeypatch.setattr(tb, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(tb, "CONFIG_FILE", str(cfg_dir / "config.json"))
    monkeypatch.setattr(tb, "SCAN_FILE", str(cfg_dir / "scan-cache.json"))
    monkeypatch.setattr(tb, "CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setattr(tb, "CODEX_DIR", str(tmp_path / "codex"))
    monkeypatch.setattr(tb, "PI_DIR", str(tmp_path / "pi"))
    monkeypatch.setattr(tb, "OMP_DIR", str(tmp_path / "omp"))
    monkeypatch.setattr(tb, "SNAP_DIR", str(tmp_path / "snaps"))
    cfg = copy.deepcopy(tb.DEFAULTS)
    cfg["proxies"] = copy.deepcopy(PROXIES)
    tb.save_config(cfg)
    tb.CFG.clear()
    tb.CFG.update(tb.load_config())
    for d in (tb.CACHE, tb.SCAN, tb.TRANSCRIPTS):
        d.clear()
    tb.STATE.update({"session_id": None, "session_name": "", "cwd": None, "agent": None, "tty": ""})
    return tb
