import asyncio
import json
import os
import re
import shutil
import subprocess
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


# ─────────────── running agents ───────────────


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


def test_open_agent_rejects_bad_input_before_touching_iterm(t, tmp_path):
    assert asyncio.run(t.open_agent({"id": "../../etc", "tool": "claude"})) == "bad id"
    assert asyncio.run(t.open_agent({"tool": "claude", "proxy": "missing"})) == "cannot launch claude via missing"
    assert asyncio.run(t.open_agent({"tool": "claude", "dir": str(tmp_path / "nope")})).startswith("no such directory")
    t.CONN.clear()
    assert asyncio.run(t.open_agent({"tool": "claude"})) == "no connection to iTerm"


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
