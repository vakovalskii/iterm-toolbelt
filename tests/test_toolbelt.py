import asyncio
import json
import os
import re
import runpy
import shlex
import shutil
import subprocess
import sys
from unittest.mock import AsyncMock
from urllib.parse import urlencode

import pytest

from conftest import PROXIES, ROOT

# ─────────────── masking ───────────────


@pytest.mark.parametrize("raw, gone", [
    ("curl -H 'Authorization: x' https://u:hunter2@host/x", "hunter2"),
    ("export OPENAI_API_KEY=sk-abcdefghijklmnop1234", "sk-abcdefghijklmnop1234"),
    ("gh auth login --with-token ghp_" + "a" * 30, "ghp_" + "a" * 30),
    ("PASSWORD='p@ss w0rd' ./run.sh", "p@ss w0rd"),
    ("token: abc123secret", "abc123secret"),
])
def test_mask_hides_secrets(t, raw, gone):
    out = t.mask(raw)
    assert gone not in out
    assert "***" in out


def test_mask_keeps_plain_text(t):
    s = "git log --oneline -5 && ls -la /tmp"
    assert t.mask(s) == s
    assert t.mask("") == "" and t.mask(None) == ""


def test_mask_keeps_user_and_host_of_proxy_url(t):
    assert t.mask("http://me:pw@203.0.113.10:48921") == "http://me:***@203.0.113.10:48921"


# ─────────────── small helpers ───────────────


@pytest.mark.parametrize("url, hp", [
    ("http://u:p@1.2.3.4:3128", "1.2.3.4:3128"),
    ("1.2.3.4:8080", "1.2.3.4:8080"),
    ("socks5://host.example:1080", "host.example:1080"),
    ("", ""),
])
def test_hostport(t, url, hp):
    assert t._hostport(url) == hp


def test_ci_checks_rollup(t):
    roll = [{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}, {"conclusion": "FAILURE"},
            {"state": "ERROR"}, {"status": "IN_PROGRESS"}, {}]
    assert t._checks(roll) == {"pass": 2, "fail": 2, "pending": 2}
    assert t._checks(None) == {"pass": 0, "fail": 0, "pending": 0}


@pytest.mark.parametrize("name, inp, kind", [
    ("Bash", {"command": "ls"}, "bash"),
    ("Edit", {"file_path": "/a.py"}, "edit"),
    ("NotebookEdit", {"notebook_path": "/n.ipynb"}, "edit"),
    ("Read", {"file_path": "/a.py"}, "read"),
    ("Grep", {"pattern": "foo", "path": "src"}, "search"),
    ("Agent", {"description": "find things"}, "agent"),
    ("WebFetch", {"url": "https://x"}, "web"),
    ("mcp__playwright__browser_click", {"ref": "e1"}, "mcp"),
    ("Something", {"a": 1}, "other"),
])
def test_action_kinds(t, name, inp, kind):
    assert t._summary(name, inp)[0] == kind


def test_edit_detail_is_a_diff(t):
    d = t._detail("Edit", {"file_path": "/a.py", "old_string": "x = 1", "new_string": "x = 2\ny = 3"})
    assert d.splitlines() == ["@@ /a.py", "-x = 1", "+x = 2", "+y = 3"]
    w = t._detail("Write", {"file_path": "/b.py", "content": "a\nb"})
    assert w.startswith("@@ /b.py (new/overwritten)") and "+a" in w and "+b" in w


# ─────────────── Agent actions: transcript ───────────────


def _jl(path, rows, mode="a"):
    with open(path, mode) as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _use(i, name, inp):
    return {"timestamp": "2026-10-10T07:00:00Z", "message": {"content": [
        {"type": "tool_use", "id": i, "name": name, "input": inp}]}}


def _res(i, err=False):
    return {"message": {"content": [{"type": "tool_result", "tool_use_id": i, "is_error": err}]}}


def test_transcript_pairs_results_masks_and_reads_incrementally(t, tmp_path):
    p = tmp_path / "s.jsonl"
    _jl(p, [_use("a", "Bash", {"command": "curl https://u:topsecret@h/x"}), _res("a"),
            _use("b", "Read", {"file_path": "/etc/hosts"}), _res("b", err=True)], "w")
    tr = t.read_transcript(str(p))
    assert [i["kind"] for i in tr["items"]] == ["bash", "read"]
    assert [i["res"] for i in tr["items"]] == ["ok", "err"]
    assert "topsecret" not in tr["items"][0]["line"] and "topsecret" not in tr["items"][0]["detail"]
    off = tr["off"]
    # a half-written line is left for the next read
    with open(p, "a") as f:
        f.write(json.dumps(_use("c", "Edit", {"file_path": "/a"})) + "\n" + '{"message": {"con')
    tr = t.read_transcript(str(p))
    assert len(tr["items"]) == 3 and tr["off"] > off
    with open(p, "a") as f:
        f.write('tent": [{"type": "tool_result", "tool_use_id": "c"}]}}\n')
    assert t.read_transcript(str(p))["items"][2]["res"] == "ok"


def test_transcript_restarts_when_the_file_shrinks(t, tmp_path):
    p = tmp_path / "s.jsonl"
    _jl(p, [_use("a", "Bash", {"command": "one"}), _use("b", "Bash", {"command": "two"})], "w")
    assert len(t.read_transcript(str(p))["items"]) == 2
    _jl(p, [_use("z", "Read", {"file_path": "/x"})], "w")
    items = t.read_transcript(str(p))["items"]
    assert [i["kind"] for i in items] == ["read"]


def test_agent_state_and_item_follow_the_focused_session(t, tmp_path):
    cwd = "/Users/me/proj"
    proj = os.path.join(t.CLAUDE_DIR, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
    os.makedirs(proj)
    _jl(os.path.join(proj, "sid1.jsonl"), [_use("a", "Bash", {"command": "make test", "description": "tests"})], "w")
    t.STATE["agent"] = {"sid": "sid1", "cwd": cwd}
    st = t.agent_state()
    assert st["total"] == 1 and st["items"][0]["line"] == "make test"
    assert t.agent_item(0).startswith("# tests")
    assert t.agent_item(5) == ""
    t.STATE["agent"] = None
    assert t.agent_state()["items"] == []


# ─────────────── Sessions: scanner ───────────────


def test_titles(t):
    assert t._first_text([{"type": "image"}, {"type": "text", "text": "hi"}]) == "hi"
    assert t._first_text("plain") == "plain"
    assert t._clean_title("<cmd>x</cmd> fix   the\nbug") == "fix the bug"


def _claude_session(t, sid, cwd, rows):
    d = os.path.join(t.CLAUDE_DIR, "projects", re.sub(r"[^A-Za-z0-9]", "-", cwd))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{sid}.jsonl")
    _jl(p, rows, "w")
    return p


def test_scan_claude_skips_commands_and_prefers_ai_title(t):
    p = _claude_session(t, "abc", "/w/repo", [
        {"type": "user", "cwd": "/w/repo", "message": {"content": "<command-name>/clear</command-name>"}},
        {"type": "user", "cwd": "/w/repo", "message": {"content": [{"type": "text", "text": "Fix the login page"}]}},
    ])
    assert t._scan_claude(p) == {"id": "abc", "tool": "claude", "dir": "/w/repo", "title": "Fix the login page"}
    _jl(p, [{"type": "ai-title", "title": "Login page fix"}])
    assert t._scan_claude(p)["title"] == "Login page fix"


def test_scan_codex(t, tmp_path):
    p = tmp_path / "rollout.jsonl"
    _jl(p, [{"type": "session_meta", "payload": {"id": "cx-1", "cwd": "/w/r"}},
            {"type": "response_item", "payload": {"role": "user", "content": [
                {"type": "input_text", "text": "<environment_context>..."}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "Add tests"}}], "w")
    assert t._scan_codex(str(p)) == {"id": "cx-1", "tool": "codex", "dir": "/w/r", "title": "Add tests"}


def _pi_like_session(t, tool, sid, rows):
    root = t.PI_DIR if tool == "pi" else t.OMP_DIR
    d = os.path.join(root, "agent", "sessions", "project")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"2026-10-10T00-00-00_{sid}.jsonl")
    _jl(p, rows, "w")
    return p


@pytest.mark.parametrize("tool", ["pi", "omp"])
def test_scan_pi_like_reads_session_and_first_user_message(t, tool):
    rows = [{"type": "session", "version": 3, "id": "session-1234", "cwd": "/w/repo"},
            {"type": "model_change", "id": "event-1"},
            {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": "ignore"}]}},
            {"type": "message", "message": {"role": "user", "content": [
                {"type": "image", "data": "..."}, {"type": "text", "text": "Fix the login page"}]}}]
    p = _pi_like_session(t, tool, "session-1234", rows)
    assert t._scan_pi_like(p, tool) == {"id": "session-1234", "tool": tool,
                                         "dir": "/w/repo", "title": "Fix the login page"}


def test_scan_omp_accepts_title_before_session_header(t):
    p = _pi_like_session(t, "omp", "session-2345", [
        {"type": "title", "title": "Login page fix"},
        {"type": "session", "version": 3, "id": "session-2345", "cwd": "/w/repo"},
        {"type": "message", "message": {"role": "user", "content": [
            {"type": "text", "text": "Fix the login page"}]}}])
    assert t._scan_pi_like(p, "omp") == {"id": "session-2345", "tool": "omp",
                                        "dir": "/w/repo", "title": "Login page fix"}


def test_scan_pi_prefers_session_name(t):
    p = _pi_like_session(t, "pi", "named-1234", [
        {"type": "session", "id": "named-1234", "cwd": "/w/repo"},
        {"type": "message", "message": {"role": "user", "content": "Original prompt"}},
        {"type": "session_info", "name": "Chosen name"}])
    assert t._scan_pi_like(p, "pi")["title"] == "Chosen name"


def test_scan_pi_keeps_early_name_after_long_session(t):
    p = _pi_like_session(t, "pi", "named-5678", [
        {"type": "session", "id": "named-5678", "cwd": "/w/repo"},
        {"type": "message", "message": {"role": "user", "content": "Original prompt"}},
        {"type": "session_info", "name": "Chosen name"},
        {"type": "usage", "details": "x" * 70_000}])
    assert t._scan_pi_like(p, "pi")["title"] == "Chosen name"


def test_scan_pi_uses_last_name_between_head_and_tail(t):
    p = _pi_like_session(t, "pi", "named-9012", [
        {"type": "session", "id": "named-9012", "cwd": "/w/repo"},
        {"type": "message", "message": {"role": "user", "content": "Original prompt"}},
        {"type": "session_info", "name": "Old name"},
        {"type": "usage", "details": "x" * 600_000},
        {"type": "session_info", "name": "Latest name"},
        {"type": "usage", "details": "x" * 70_000}])
    assert t._scan_pi_like(p, "pi")["title"] == "Latest name"


def test_scan_pi_cleared_name_falls_back_to_first_message(t):
    p = _pi_like_session(t, "pi", "named-3456", [
        {"type": "session", "id": "named-3456", "cwd": "/w/repo"},
        {"type": "message", "message": {"role": "user", "content": "Original prompt"}},
        {"type": "session_info", "name": "Old name"},
        {"type": "usage", "details": "x" * 70_000},
        {"type": "session_info", "name": "  "},
        {"type": "usage", "details": "x" * 70_000}])
    assert t._scan_pi_like(p, "pi")["title"] == "Original prompt"


def test_pi_name_scan_reads_only_appended_bytes(t, monkeypatch):
    rows = [{"type": "session", "id": "growing-1234", "cwd": "/w/repo"}]
    rows += [{"type": "usage", "details": "x" * 200_000} for _ in range(8)]
    p = _pi_like_session(t, "pi", "growing-1234", rows)
    real_open = open
    read_bytes = 0

    class CountingFile:
        def __init__(self, file):
            self.file = file

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.file.__exit__(*args)

        def __getattr__(self, attr):
            return getattr(self.file, attr)

        def read(self, *args):
            nonlocal read_bytes
            data = self.file.read(*args)
            read_bytes += len(data)
            return data

    def counting_open(path, mode="r", *args, **kwargs):
        file = real_open(path, mode, *args, **kwargs)
        return CountingFile(file) if path == p and mode == "rb" else file

    monkeypatch.setattr(t, "open", counting_open, raising=False)
    assert t._last_pi_name(p) is None
    assert read_bytes > 1_000_000
    read_bytes = 0
    _jl(p, [{"type": "message", "message": {"role": "assistant", "content": "done"}}], "a")
    assert t._last_pi_name(p) is None
    assert read_bytes < 1000


def test_pi_name_scan_handles_partial_line_replacement_and_truncation(t):
    p = _pi_like_session(t, "pi", "changing-1234", [
        {"type": "session", "id": "changing-1234", "cwd": "/w/repo"},
        {"type": "session_info", "name": "Old name"}])
    assert t._last_pi_name(p) == "Old name"

    with open(p, "ab") as f:
        f.write(b'{"type":"session_info","name":"New')
    assert t._last_pi_name(p) == "Old name"
    with open(p, "ab") as f:
        f.write(b' name"}\n')
    assert t._last_pi_name(p) == "New name"
    _jl(p, [{"type": "session_info", "name": "  "}], "a")
    assert t._last_pi_name(p) == ""

    replacement = p + ".new"
    _jl(replacement, [{"type": "session_info", "name": "Replacement"}], "w")
    os.replace(replacement, p)
    assert t._last_pi_name(p) == "Replacement"

    with open(p, "wb"):
        pass
    assert t._last_pi_name(p) is None
    _jl(p, [{"type": "session_info", "name": "After truncate"}], "w")
    assert t._last_pi_name(p) == "After truncate"

    old = os.stat(p)
    _jl(p, [{"type": "session_info", "name": "Another title!"}], "w")
    assert os.path.getsize(p) == old.st_size
    os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns + 1_000_000))
    assert t._last_pi_name(p) == "Another title!"


def test_scan_omp_uses_legacy_header_title(t):
    p = _pi_like_session(t, "omp", "legacy-1234", [
        {"type": "session", "id": "legacy-1234", "cwd": "/w/repo", "title": "Older title"}])
    assert t._scan_pi_like(p, "omp")["title"] == "Older title"


def test_scan_pi_like_ignores_files_without_session_header(t):
    p = _pi_like_session(t, "omp", "missing", [
        {"type": "title", "title": "Orphaned title"},
        {"type": "message", "message": {"role": "user", "content": "hello"}}])
    assert t._scan_pi_like(p, "omp") is None


def test_scan_all_caches_on_disk_and_drops_deleted_files(t):
    a = _claude_session(t, "s-a", "/w/a", [{"type": "user", "cwd": "/w/a", "message": {"content": "first"}}])
    b = _claude_session(t, "s-b", "/w/b", [{"type": "user", "cwd": "/w/b", "message": {"content": "second"}}])
    os.utime(a, (1, 1_000_000))
    os.utime(b, (1, 2_000_000))
    rows = t._scan_all()
    assert [r["id"] for r in rows] == ["s-b", "s-a"]  # newest first
    assert os.path.isfile(t.SCAN_FILE)
    os.remove(b)
    assert [r["id"] for r in t._scan_all()] == ["s-a"]
    assert b not in json.load(open(t.SCAN_FILE))


def test_scan_all_finds_and_caches_pi_and_omp(t, monkeypatch):
    pi = _pi_like_session(t, "pi", "pi-1234", [
        {"type": "session", "id": "pi-1234", "cwd": "/w/pi"},
        {"type": "message", "message": {"role": "user", "content": [{"type": "text", "text": "Pi task"}]}}])
    omp = _pi_like_session(t, "omp", "omp-1234", [
        {"type": "title", "title": "Omp task"},
        {"type": "session", "id": "omp-1234", "cwd": "/w/omp"}])
    os.utime(pi, (1, 1_000_000))
    os.utime(omp, (1, 2_000_000))
    rows = t._scan_all()
    assert [(r["tool"], r["id"], r["dir"]) for r in rows] == [
        ("omp", "omp-1234", "/w/omp"), ("pi", "pi-1234", "/w/pi")]
    assert pi in t.SCAN and omp in t.SCAN
    assert pi in json.load(open(t.SCAN_FILE)) and omp in json.load(open(t.SCAN_FILE))

    monkeypatch.setattr(t, "_scan_pi_like", lambda *_: pytest.fail("unchanged session was parsed again"))
    assert [r["id"] for r in t._scan_all()] == ["omp-1234", "pi-1234"]


# ─────────────── running agents ───────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="uses the macOS system lsof")
def test_pi_processes_with_restricted_service_path(t, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.chdir(tmp_path)
    service = runpy.run_path(t.__file__)
    processes = asyncio.run(service["_pi_processes"](f"{os.getpid()} ?? S pi"))
    assert processes and os.path.samefile(processes[0]["dir"], tmp_path)


def test_active_agents_hide_suspended_and_duplicate_processes(t, monkeypatch):
    sess = os.path.join(t.CLAUDE_DIR, "sessions")
    os.makedirs(sess)
    for pid, sid, started in ((101, "S1", 10), (102, "S1", 20), (103, "S2", 5), (104, "S3", 7)):
        json.dump({"pid": pid, "sessionId": sid, "cwd": "/w", "startedAt": started}, open(f"{sess}/{pid}.json", "w"))
    ps = "\n".join(["101 ttys002 S+ claude", "102 ttys002 S+ claude", "103 ttys004 T claude",
                    "104 ttys005 S+ claude", "200 ttys006 S+ /usr/local/bin/codex", "201 ?? S codex app-server",
                    "202 ttys007 T codex"])

    async def fake_run(cmd, cwd=None, timeout=10.0):
        if cmd[0] == "ps":
            return 0, ps
        if cmd[0] == "lsof":
            return 0, "p200\nn/w/codexdir\n"
        return 1, ""
    monkeypatch.setattr(t, "run", fake_run)
    monkeypatch.setattr(t.os, "kill", lambda pid, sig: None)
    out = asyncio.run(t._active_agents())
    claude = sorted((a["id"], a["pid"]) for a in out if a["tool"] == "claude")
    assert claude == [("S1", 102), ("S3", 104)]  # 101 is an older process of S1, 103 is stopped
    codex = [a for a in out if a["tool"] == "codex"]
    assert [(a["pid"], a["dir"], a["tty"]) for a in codex] == [(200, "/w/codexdir", "ttys006")]


def test_active_agents_includes_pi_without_a_terminal_but_not_suspended(t, monkeypatch):
    ps = "300 ?? S pi\n301 ttys001 T pi\n302 ?? S node /opt/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"

    async def fake_run(cmd, cwd=None, timeout=10.0):
        if cmd[0] == "ps":
            return 0, ps
        return 0, f"p{cmd[3]}\nn/w/project\n"

    monkeypatch.setattr(t, "run", fake_run)
    out = asyncio.run(t._active_agents())
    assert [(a["pid"], a["tty"], a["dir"]) for a in out if a["tool"] == "pi"] == [
        (300, "??", "/w/project"), (302, "??", "/w/project")]


# ─────────────── launching agents ───────────────


def test_proxy_prefix_and_export(t):
    assert t.proxy_prefix("eu-1") == "HTTPS_PROXY=http://user:s3cret@203.0.113.10:3128 " \
                                     "HTTP_PROXY=http://user:s3cret@203.0.113.10:3128"
    assert t.proxy_prefix("direct").startswith("env -u HTTPS_PROXY")
    assert t.proxy_prefix("nope") is None
    assert t.proxy_export("eu-1").startswith("export HTTPS_PROXY=")
    assert t.proxy_export("direct").startswith("unset HTTPS_PROXY")
    assert t.proxy_export("nope") is None


def test_proxy_prefix_quotes_shell_characters(t):
    t.CFG["proxies"] = [{"name": "odd", "url": "http://u:p$a;ss@1.2.3.4:1"}]
    assert "'http://u:p$a;ss@1.2.3.4:1'" in t.proxy_prefix("odd")


def test_agent_command_claude(t):
    t.CFG["agents"]["claude"].update(prefix="FOO=1", flags="--model opus --dangerously-skip-permissions")
    assert t.agent_command("claude", "", False) == "FOO=1 claude --model opus"
    assert t.agent_command("claude", "abc", True) == "FOO=1 claude --resume abc --model opus --dangerously-skip-permissions"
    # a proxy from the list replaces the prefix
    cmd = t.agent_command("claude", "", True, "us-1")
    assert cmd.startswith("HTTPS_PROXY=http://bob:pa55@198.51.100.7:8080") and "FOO=1" not in cmd
    assert t.agent_command("claude", "", True, "missing") is None


def test_agent_command_default_proxy_from_settings(t):
    t.CFG["agents"]["codex"]["proxy"] = "eu-1"
    assert t.agent_command("codex", "", False).startswith("HTTPS_PROXY=http://user:s3cret@203.0.113.10:3128")
    assert t.agent_command("codex", "", False, "direct").startswith("env -u HTTPS_PROXY")  # explicit wins
    assert t.agent_command("codex", "r1", False).endswith("codex resume r1")


def test_agent_command_other_tools(t):
    assert t.agent_command("opencode", "x1", False) == "opencode -s x1"
    assert t.agent_command("qwen", "x1", False) == "qwen -r x1"
    assert t.agent_command("opencode", "", False) is None
    assert t.agent_command("claude", "id with space", False) == "claude --resume 'id with space'"


@pytest.mark.parametrize("tool, resume", [("pi", "pi --session"), ("omp", "omp -r")])
def test_agent_command_pi_and_omp(t, tool, resume):
    omp_new_config = os.path.join(t.HERE, "resources", "omp-new-session.yml")
    new = tool if tool == "pi" else f"omp --config {shlex.quote(omp_new_config)}"
    assert t.agent_command(tool, "", False) == new
    assert t.agent_command(tool, "session-1234", False) == f"{resume} session-1234"
    assert t.agent_command(tool, "id with space", False) == f"{resume} 'id with space'"
    t.CFG["agents"][tool].update(prefix="FOO=1", flags="--model fast")
    assert t.agent_command(tool, "session-1234", False) == f"FOO=1 {resume} session-1234 --model fast"
    if tool == "omp":
        assert t.agent_command(tool, "", False) == f"FOO=1 omp --model fast --config {shlex.quote(omp_new_config)}"
        with open(omp_new_config) as f:
            assert f.read().strip().endswith("autoResume: false")
    else:
        assert t.agent_command(tool, "", False) == "FOO=1 pi --model fast"


def test_omp_new_quotes_config_path_with_spaces(t, monkeypatch):
    monkeypatch.setattr(t, "HERE", "/tmp/my tools")
    assert t.agent_command("omp", "", False) == "omp --config '/tmp/my tools/resources/omp-new-session.yml'"


def test_omp_new_overlay_follows_user_config(t):
    t.CFG["agents"]["omp"]["flags"] = "--config /tmp/user.yml"
    overlay = shlex.quote(os.path.join(t.HERE, "resources", "omp-new-session.yml"))
    assert t.agent_command("omp", "", False) == f"omp --config /tmp/user.yml --config {overlay}"


def test_open_agent_rejects_bad_input_before_touching_iterm(t, tmp_path):
    assert asyncio.run(t.open_agent({"id": "../../etc", "tool": "claude"})) == "bad id"
    assert asyncio.run(t.open_agent({"id": "x", "tool": "pi"})) == "no connection to iTerm"
    assert asyncio.run(t.open_agent({"tool": "claude", "proxy": "missing"})) == "cannot launch claude via missing"
    assert asyncio.run(t.open_agent({"tool": "claude", "dir": str(tmp_path / "nope")})).startswith("no such directory")
    t.CONN.clear()
    assert asyncio.run(t.open_agent({"tool": "claude"})) == "no connection to iTerm"


@pytest.mark.parametrize("stat,tty", [("S", "??"), ("T", "ttys001")])
def test_open_agent_blocks_pi_resume_in_project_with_pi_process(t, monkeypatch, tmp_path, stat, tty):
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    monkeypatch.setitem(t.CONN, "c", object())

    async def fake_run(cmd, cwd=None, timeout=10.0):
        if cmd[0] == "ps":
            return 0, f"300 {tty} {stat} pi\n"
        return 0, f"p300\nn{project}\n"

    monkeypatch.setattr(t, "run", fake_run)
    assert asyncio.run(t.open_agent({"tool": "pi", "id": "session-1", "dir": str(alias)})) == (
        "pi is already running in this project; close it before resuming")


def test_open_agent_reserves_pi_project_during_process_check(t, monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(project, target_is_directory=True)
    monkeypatch.setitem(t.CONN, "c", object())
    monkeypatch.setattr(t, "_PI_RESUMES", {})
    calls = 0

    async def scenario():
        nonlocal calls
        entered, proceed = asyncio.Event(), asyncio.Event()

        async def guard(_):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await proceed.wait()
            return "could not check running pi processes"

        monkeypatch.setattr(t, "_pi_resume_guard", guard)
        first = asyncio.create_task(t.open_agent({"tool": "pi", "id": "session-1", "dir": str(project)}))
        await entered.wait()
        assert await t.open_agent({"tool": "pi", "id": "session-1", "dir": str(alias)}) == (
            "pi is already starting in this project")
        assert calls == 1
        proceed.set()
        assert await first == "could not check running pi processes"
        assert await t.open_agent({"tool": "pi", "id": "session-1", "dir": str(alias)}) == (
            "could not check running pi processes")
        assert calls == 2  # an error before launch releases the reservation

    asyncio.run(scenario())


def test_open_agent_keeps_pi_reservation_after_send(t, monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setitem(t.CONN, "c", object())
    monkeypatch.setattr(t, "_PI_RESUMES", {})
    commands = []

    class Session:
        async def async_get_variable(self, name):
            return os.path.basename(os.environ.get("SHELL", "zsh"))

        async def async_send_text(self, command):
            commands.append(command)

        async def async_activate(self, **kwargs):
            pass

    session = Session()
    tab = type("Tab", (), {"tab_id": "tab", "current_session": session})()
    window = type("Window", (), {"window_id": "window", "current_tab": tab, "tabs": [tab]})()

    class WindowAPI:
        @staticmethod
        async def async_create(conn):
            return window

    class App:
        current_terminal_window = None

        async def async_refresh(self):
            pass

        def get_window_by_id(self, window_id):
            return window

    async def get_app(conn):
        return App()

    async def guard(_):
        return ""  # Pi has not yet appeared in ps

    monkeypatch.setattr(t.iterm2, "Window", WindowAPI, raising=False)
    monkeypatch.setattr(t.iterm2, "async_get_app", get_app, raising=False)
    monkeypatch.setattr(t, "_pi_resume_guard", guard)

    async def scenario():
        q = {"tool": "pi", "id": "session-1", "dir": str(project)}
        assert await t.open_agent(q) == "ok"
        assert await t.open_agent(q) == "pi is already starting in this project"
        assert len(commands) == 1

    asyncio.run(scenario())


def test_open_agent_releases_pi_reservation_if_iterm_fails(t, monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setitem(t.CONN, "c", object())
    monkeypatch.setattr(t, "_PI_RESUMES", {})

    async def guard(_):
        return ""

    async def get_app(conn):
        raise RuntimeError("iTerm is unavailable")

    monkeypatch.setattr(t, "_pi_resume_guard", guard)
    monkeypatch.setattr(t.iterm2, "async_get_app", get_app, raising=False)
    with pytest.raises(RuntimeError, match="iTerm is unavailable"):
        asyncio.run(t.open_agent({"tool": "pi", "id": "session-1", "dir": str(project)}))
    assert not t._PI_RESUMES


def test_expired_pi_reservation_does_not_let_old_request_release_new_one(t, monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(t, "_PI_RESUMES", {})
    first = t._reserve_pi_resume(str(project))
    key, _ = first
    t._PI_RESUMES[key] = (first[1], 0)
    second = t._reserve_pi_resume(str(project))
    assert second is not None
    t._release_pi_resume(first)
    assert t._PI_RESUMES[key][0] is second[1]


def test_pi_resume_guard_checks_only_pi_in_same_project(t, monkeypatch, tmp_path):
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    ps = ("300 ?? S pi\n"
          "301 ?? S bun /opt/node_modules/@oh-my-pi/pi-coding-agent/dist/cli.js\n"
          "302 ?? S python /tmp/pi_helper.py\n")

    async def fake_run(cmd, cwd=None, timeout=10.0):
        if cmd[0] == "ps":
            return 0, ps
        return 0, f"p300\nn{other}\n"

    monkeypatch.setattr(t, "run", fake_run)
    assert asyncio.run(t._pi_resume_guard(str(project))) == ""


def test_pi_resume_guard_stops_if_process_check_fails(t, monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    async def fake_run(cmd, cwd=None, timeout=10.0):
        return (0, "300 ?? S pi\n") if cmd[0] == "ps" else (1, "")

    monkeypatch.setattr(t, "run", fake_run)
    monkeypatch.setattr(t.os, "kill", lambda pid, sig: None)
    assert asyncio.run(t._pi_resume_guard(str(project))) == "could not check running pi processes"


def test_pi_resume_guard_ignores_process_that_exited(t, monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    async def fake_run(cmd, cwd=None, timeout=10.0):
        return (0, "300 ?? S pi\n") if cmd[0] == "ps" else (1, "")

    def exited(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(t, "run", fake_run)
    monkeypatch.setattr(t.os, "kill", exited)
    assert asyncio.run(t._pi_resume_guard(str(project))) == ""


# ─────────────── settings ───────────────


def test_settings_state_masks_passwords(t):
    t.CFG["keenetic"] = {"host": "192.168.1.1", "login": "admin", "password": "routerpw"}
    st = t.settings_state()
    blob = json.dumps(st)
    assert "s3cret" not in blob and "pa55" not in blob and "routerpw" not in blob
    assert st["config"]["proxies"][0]["url"] == "http://user:***@203.0.113.10:3128"
    assert st["config"]["keenetic"]["password_set"] is True
    assert t.CFG["proxies"][0]["url"] == PROXIES[0]["url"]  # the live config is untouched


def _save(t, body):
    return t.settings_save(json.dumps(body).encode())


def test_settings_round_trip_keeps_stored_passwords(t):
    t.CFG["keenetic"] = {"host": "192.168.1.1", "login": "admin", "password": "routerpw"}
    t.save_config(t.CFG)
    shown = t.settings_state()["config"]
    body = {"proxies": [dict(p, orig=p["name"]) for p in shown["proxies"]],
            "keenetic": {"host": "192.168.1.1", "login": "admin", "password": ""}}
    assert _save(t, body) == "ok"
    saved = json.load(open(t.CONFIG_FILE))
    assert saved["proxies"] == PROXIES
    assert saved["keenetic"]["password"] == "routerpw"
    assert os.stat(t.CONFIG_FILE).st_mode & 0o777 == 0o600


def test_settings_save_keeps_pi_and_omp_launch_options(t):
    agents = {"pi": {"prefix": "PI_DEBUG=1", "flags": "--model fast", "proxy": "direct"},
              "omp": {"prefix": "OMP_DEBUG=1", "flags": "--model quick", "proxy": ""}}
    assert _save(t, {"agents": agents}) == "ok"
    saved = t.load_config()["agents"]
    for tool in ("pi", "omp"):
        assert saved[tool] == agents[tool]


def test_settings_rename_proxy_keeps_its_password(t):
    shown = t.settings_state()["config"]["proxies"]
    body = {"proxies": [{"orig": "eu-1", "name": "eu-main", "url": shown[0]["url"]},
                        {"orig": "", "name": "new", "url": "socks5://10.0.0.1:1080"}]}
    assert _save(t, body) == "ok"
    assert t.CFG["proxies"] == [{"name": "eu-main", "url": PROXIES[0]["url"]},
                                {"name": "new", "url": "socks5://10.0.0.1:1080"}]


@pytest.mark.parametrize("proxies, err", [
    ([{"name": "x", "url": "ftp://h:1"}], "expected scheme"),
    ([{"name": "x", "url": "http://nohost"}], "expected scheme"),
    ([{"name": "a", "url": "http://h:1"}, {"name": "a", "url": "http://h:2"}], "taken"),
    ([{"name": "direct", "url": "http://h:1"}], "reserved"),
    ([{"name": "", "url": "http://h:1"}], "empty"),
])
def test_settings_rejects_bad_proxies_without_saving(t, proxies, err):
    before = open(t.CONFIG_FILE).read()
    assert err in _save(t, {"proxies": proxies})
    assert open(t.CONFIG_FILE).read() == before
    assert t.CFG["proxies"] == PROXIES


def test_settings_servers_and_router(t):
    body = {"servers": [{"name": "a", "host": "1.2.3.4", "port": "8443", "banner": False},
                        {"name": "", "host": "5.6.7.8"}, {"name": "empty", "host": ""}],
            "keenetic": {"host": "10.0.0.1", "login": "", "password": "pw"}}
    assert _save(t, body) == "ok"
    assert t.CFG["servers"] == [{"name": "a", "host": "1.2.3.4", "port": 8443, "banner": False},
                                {"name": "5.6.7.8", "host": "5.6.7.8", "port": 22}]
    assert t.CFG["keenetic"] == {"host": "10.0.0.1", "login": "admin", "password": "pw"}
    assert "port must be a number" in _save(t, {"servers": [{"name": "b", "host": "h", "port": "x"}]})
    assert _save(t, {"keenetic": {"host": ""}}) == "ok"
    assert "keenetic" not in json.load(open(t.CONFIG_FILE))


def test_settings_agents_tabs_and_garbage(t):
    assert _save(t, {"agents": {"claude": {"proxy": "us-1", "evil": 1}, "nobody": {"prefix": "x"}}}) == "ok"
    assert t.CFG["agents"]["claude"]["proxy"] == "us-1" and "evil" not in t.CFG["agents"]["claude"]
    assert "nobody" not in t.CFG["agents"]
    assert _save(t, {"tabs": {"settings": True}}) == "restart"
    assert t.settings_save(b"{nope") == "not JSON"
    assert t.settings_save(b"[1]") == "not an object"


# ─────────────── network ───────────────


def test_codex_target_follows_its_login(t, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert "api.openai.com" in t._codex_target()  # no codex at all
    os.makedirs(tmp_path / ".codex")
    json.dump({"auth_mode": "chatgpt"}, open(tmp_path / ".codex" / "auth.json", "w"))
    assert "chatgpt.com" in t._codex_target()
    json.dump({"auth_mode": "apikey"}, open(tmp_path / ".codex" / "auth.json", "w"))
    assert "api.openai.com" in t._codex_target()
    assert [k for k, _ in t.net_targets()] == ["claude", "codex", "openai"]


def test_session_proxy_is_read_from_the_agent_environment(t, monkeypatch):
    env = {"out": "claude HOME=/x HTTPS_PROXY='http://bob:pa55@198.51.100.7:8080' TERM=xterm"}

    async def fake_run(cmd, cwd=None, timeout=10.0):
        return 0, env["out"]
    monkeypatch.setattr(t, "run", fake_run)
    assert asyncio.run(t.session_proxy(1)) == {"name": "us-1", "url": "http://bob:***@198.51.100.7:8080"}
    env["out"] = "claude HOME=/x HTTPS_PROXY=http://9.9.9.9:1 TERM=x"
    assert asyncio.run(t.session_proxy(1))["name"] == "9.9.9.9:1"
    env["out"] = "claude HOME=/x"
    assert asyncio.run(t.session_proxy(1)) == {"name": "direct", "url": ""}
    assert asyncio.run(t.session_proxy(None)) is None


def test_probe_proxy_reports_every_cli(t, monkeypatch):
    seen = []

    async def fake_curl(proxy, url):
        seen.append((proxy, url))
        return {"code": 401, "ms": 100}
    monkeypatch.setattr(t, "_curl", fake_curl)
    r = asyncio.run(t._probe_proxy("eu-1", PROXIES[0]["url"]))
    assert set(r["res"]) == {"claude", "codex", "openai"} and r["hp"] == "203.0.113.10:3128"
    assert all(p == PROXIES[0]["url"] for p, _ in seen)


def test_keenetic_state_parsing(t, monkeypatch):
    conf = ["interface Wireguard0", "    description NL-ams", "    up", "!",
            "interface Wireguard1", "    description US-east", "!",
            "dns-proxy",
            "    route object-group vpn-ai Wireguard1 auto", "    route object-group vpn-ai Wireguard0 auto",
            "    route object-group vpn-tg Wireguard0 auto"]
    replies = {
        "show running-config": {"parse": {"message": conf}},
        "show ping-check": {"parse": {"pingcheck": [{"interface": {"Wireguard0": {"status": "pass"},
                                                                    "Wireguard1": {"status": "fail"}}}]}},
        "show interface Wireguard0": {"parse": {"state": "up", "link": "up",
                                                "wireguard": {"peer": [{"last-handshake": "12", "rxbytes": 5}]}}},
        "show interface Wireguard1": {"parse": {"state": "up", "link": "up", "wireguard": {"peer": [{}]}}},
    }
    monkeypatch.setattr(t, "_kn_rci", lambda cmds: [replies[c] for c in cmds])
    st = t._kn_state()
    tun = {x["iface"]: x for x in st["tunnels"]}
    assert tun["Wireguard0"]["desc"] == "NL-ams" and tun["Wireguard0"]["handshake"] == 12
    assert tun["Wireguard1"]["check"] == "fail"
    groups = {g["name"]: g for g in st["groups"]}
    assert groups["vpn-ai"]["chain"] == ["Wireguard1", "Wireguard0"]
    assert groups["vpn-ai"]["active"] == "Wireguard0"  # Wireguard1 fails its ping-check


# ─────────────── snapshots ───────────────


def test_snapshots_list_and_backup(t):
    tabs = [{"title": "", "split": "vertical", "panes": [{"cwd": "/w/repo", "session_id": "abcdef123456"},
                                                         {"cwd": "/w/other"}]}]
    t._write_snap("auto-1", tabs)
    t._write_snap("auto-1", tabs)
    assert os.path.isfile(os.path.join(t.SNAP_DIR, "auto-1.json.bak"))
    t.CACHE["scan:sessions"] = (0, [{"id": "abcdef123456", "title": "Fix login"}])
    snap = t.snap_list()[0]
    assert snap["name"] == "auto-1" and snap["auto"] is True
    tab = snap["tabs"][0]
    assert tab["title"] == "repo" and tab["panes"] == 2 and tab["agents"] == 1
    assert tab["detail"][0] == {"dir": "repo", "sid": "abcdef12", "agent": "claude", "title": "Fix login"}


# ─────────────── HTTP ───────────────


async def _http(t, raw: bytes) -> tuple[int, str]:
    server = await asyncio.start_server(t.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(raw)
        await w.drain()
        data = await r.read()
        w.close()
    finally:
        server.close()
    head, _, body = data.partition(b"\r\n\r\n")
    return int(head.split()[1]), body.decode()


def _req(method, path, header=False):
    return (f"{method} {path} HTTP/1.1\r\nHost: x\r\n" + ("X-Toolbelt: 1\r\n" if header else "")
            + "Content-Length: 0\r\n\r\n").encode()


@pytest.mark.parametrize("path", ["/sessions/open?tool=claude", "/sessions/focus?tty=ttys001", "/settings/save",
                                  "/snaps/save?name=x", "/snaps/restore?name=x", "/net/copy?name=eu-1",
                                  "/open-url?url=https%3A%2F%2Fgithub.com%2Fowner%2Frepo%2Fpull%2F1"])
def test_http_mutating_endpoints_need_post_and_header(t, monkeypatch, path):
    run = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(t, "run", run)
    assert asyncio.run(_http(t, _req("POST", path)))[0] == 403
    assert asyncio.run(_http(t, _req("GET", path)))[0] == 403
    assert asyncio.run(_http(t, _req("GET", path, header=True)))[0] == 403
    assert asyncio.run(_http(t, _req("OPTIONS", path, header=True)))[0] == 403
    run.assert_not_awaited()


@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo/pull/1",
    "https://github.example.com/owner/repo/pull/2",
    "http://github.internal:8080/owner/repo/pull/3",
    "https://github.com/owner/repo/pull/4?label=needs+review&next=a%26b#discussion_r123",
])
def test_http_open_url_launches_browser_with_exact_url(t, monkeypatch, url):
    run = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(t, "run", run)
    path = "/open-url?" + urlencode({"url": url})
    assert asyncio.run(_http(t, _req("POST", path, header=True))) == (200, "ok")
    run.assert_awaited_once_with(["/usr/bin/open", url])


@pytest.mark.parametrize("url", [
    "",
    "/owner/repo/pull/1",
    "//github.com/owner/repo/pull/1",
    "github.com/owner/repo/pull/1",
    "https://",
    "https:///owner/repo/pull/1",
    "https://[broken/pull/1",
    "file:///etc/hosts",
    "javascript:alert(1)",
    "ftp://github.com/owner/repo/pull/1",
    "-a Safari",
    "https://github.com/owner/repo/pull/1 with spaces",
    "https://github.com/owner/repo/pull/1\n",
    "https://github.com/owner/repo/pull/1\r",
    "https://github.com/owner/repo/pull/1\t",
    "https://github.com/owner/repo/pull/1\x00",
])
def test_http_open_url_rejects_invalid_urls(t, monkeypatch, url):
    run = AsyncMock(return_value=(0, ""))
    monkeypatch.setattr(t, "run", run)
    path = "/open-url?" + urlencode({"url": url})
    assert asyncio.run(_http(t, _req("POST", path, header=True))) == (400, "invalid URL")
    run.assert_not_awaited()


def test_http_open_url_reports_launch_failure(t, monkeypatch):
    monkeypatch.setattr(t, "run", AsyncMock(return_value=(1, "No application knows how to open the URL")))
    path = "/open-url?" + urlencode({"url": "https://github.com/owner/repo/pull/1"})
    assert asyncio.run(_http(t, _req("POST", path, header=True))) == (
        502, "could not open URL: No application knows how to open the URL")


def test_http_copy_puts_one_line_with_newline_on_clipboard(t, monkeypatch):
    got = []

    class FakeProc:
        async def communicate(self, data):
            got.append(data.decode())

    async def fake_exec(*args, **kw):
        assert args == ("pbcopy",)
        return FakeProc()

    monkeypatch.setattr(t.asyncio, "create_subprocess_exec", fake_exec)
    for name in ("direct", "eu-1"):
        assert asyncio.run(_http(t, _req("POST", f"/net/copy?name={name}", header=True))) == (200, "copied")
    assert got[0] == t.proxy_export("direct") + "\n"
    assert got[1] == t.proxy_export("eu-1") + "\n" and got[1].count("\n") == 1


def test_http_basics(t):
    code, body = asyncio.run(_http(t, _req("POST", "/net/copy?name=nope", header=True)))
    assert (code, body) == (200, "unknown proxy")
    assert asyncio.run(_http(t, _req("GET", "/nothing")))[0] == 404
    code, body = asyncio.run(_http(t, _req("GET", "/settings/state")))
    assert code == 200 and "s3cret" not in body and json.loads(body)["config"]["proxies"][0]["name"] == "eu-1"
    for path in t.PAGES:
        code, body = asyncio.run(_http(t, _req("GET", path)))
        assert code == 200 and body.lstrip().lower().startswith("<!doctype html>")


# ─────────────── pages ───────────────


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
@pytest.mark.parametrize("page", sorted(os.listdir(os.path.join(ROOT, "pages"))))
def test_page_scripts_parse(page, tmp_path):
    html = open(os.path.join(ROOT, "pages", page)).read()
    for n, js in enumerate(re.findall(r"<script>([\s\S]*?)</script>", html)):
        f = tmp_path / f"{page}.{n}.js"
        f.write_text(js)
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
