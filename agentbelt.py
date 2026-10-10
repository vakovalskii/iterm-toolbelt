#!/usr/bin/env python3
"""agentbelt: an iTerm2 sidebar for AI coding agents (Claude Code, Codex, pi, omp).

Tabs (View → Toolbelt):
  ◆ Git & PR         branch, ahead/behind, changed files with diffs, PR and CI, worktrees
  ◆ Agent actions    what Claude Code is doing in the active pane: commands, edits, reads
  ◆ Sessions         Claude Code, Codex, pi and omp sessions: projects, resume, running agents
  Settings (⚙ in the Sessions tab): agent proxy and flags, tabs, repo checks

Pages are served by an HTTP server on 127.0.0.1 (port from the config) and shown by iTerm2
in Toolbelt tabs. Everything iTerm-related (active pane, new windows) goes through its Python API.
Config: ~/.config/agentbelt/config.json.
"""
import asyncio
import copy
import glob
import json
import os
import re
import shlex
import sys
import threading
import time
import urllib.parse

import iterm2

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.expanduser(os.getenv("AGENTBELT_HOME") or os.getenv("ITERM_TOOLBELT_HOME") or "~/.config/agentbelt")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CLAUDE_DIR = os.path.expanduser("~/.claude")
CODEX_DIR = os.path.expanduser("~/.codex")
PI_DIR = os.path.expanduser("~/.pi")
OMP_DIR = os.path.expanduser("~/.omp")
VERSION = "0.1.0"
BOOT = str(int(time.time()))

DEFAULTS = {
    "port": 47811,
    "title_prefix": "◆ ",
    "tabs": {"git": True, "agent": True, "sessions": True, "settings": False},
    "open_in": "window",
    "agents": {
        "claude": {"prefix": "", "flags": "", "skip_permissions": False},
        "codex": {"prefix": "", "flags": ""},
        "pi": {"prefix": "", "flags": ""},
        "omp": {"prefix": "", "flags": ""},
    },
    # Network block in Agent actions (local only, see "Network" below)
    "proxies": [],
    "servers": [],
    "keenetic": {},
    # Custom checks in the Git & PR tab: if `file` exists in the repo root, `cmd` is run.
    # Exit code 0 is shown green, anything else red.
    "repo_checks": [],
    # show the Toolbelt in every new iTerm window (once per window:
    # if you hide it by hand, it stays hidden)
    "auto_show_toolbelt": True,
    # snapshot of all windows every 5 minutes into ~/.config/itermsnap/snaps (TermDeck format)
    "autosave": True,
    "onboarded": False,
}


def load_config() -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            user = json.load(f)
    except (OSError, ValueError):
        return cfg
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            for k2, v2 in v.items():
                if isinstance(v2, dict) and isinstance(cfg[k].get(k2), dict):
                    cfg[k][k2].update(v2)
                else:
                    cfg[k][k2] = v2
        else:
            cfg[k] = v
    return cfg


def save_config(cfg: dict):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)  # may contain a proxy with a password
    os.replace(tmp, CONFIG_FILE)


CFG = load_config()

NOPROXY_ENV = {k: v for k, v in os.environ.items()
               if k.lower() not in ("https_proxy", "http_proxy", "all_proxy")}
NOPROXY_ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:" + NOPROXY_ENV.get("PATH", "")
NOPROXY_ENV["GH_PROMPT_DISABLED"] = "1"
NOPROXY_ENV["GIT_TERMINAL_PROMPT"] = "0"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

STATE = {"session_id": None, "session_name": "", "cwd": None, "agent": None, "tty": ""}
CONN: dict = {}
CACHE: dict[str, tuple[float, object]] = {}
_RUNNING: set[str] = set()
_FETCHED: dict[str, float] = {}
_PI_RESUMES: dict[tuple[int, int], tuple[object, float]] = {}
_PI_RESUMES_LOCK = threading.Lock()
_PI_RESUME_TIMEOUT = 30.0


async def run(cmd: list[str], cwd: str | None = None, timeout: float = 10.0) -> tuple[int, str]:
    p = None
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, cwd=cwd, env=NOPROXY_ENV,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return p.returncode or 0, ANSI.sub("", out.decode("utf-8", "replace"))
    except asyncio.TimeoutError:
        try:
            p and p.kill()
        except Exception:  # noqa: BLE001
            pass
        return 124, "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


async def cached(key: str, ttl: float, fn):
    hit = CACHE.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = await fn()
    CACHE[key] = (now, val)
    return val


def cached_bg(key: str, ttl: float, fn):
    """Slow calls never block a page: return what we have, refresh stale values in the background."""
    hit = CACHE.get(key)
    now = time.monotonic()
    if (not hit or now - hit[0] >= ttl) and key not in _RUNNING:
        _RUNNING.add(key)

        async def _upd():
            try:
                CACHE[key] = (time.monotonic(), await fn())
            finally:
                _RUNNING.discard(key)
        asyncio.create_task(_upd())
    return hit[1] if hit else None


SECRET = re.compile(r"(?i)((?:pw|pass(?:word)?|token|secret|api[_-]?key)\w*\s*[=:]\s*)('[^']*'|\"[^\"]*\"|\S+)"
                    r"|\b(sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})"
                    r"|(?<=://)([^/\s:@]+):([^/\s@]+)@")


def mask(s: str) -> str:
    def rep(m):
        if m.group(1):
            return m.group(1) + "***"
        if m.group(4):
            return m.group(4) + ":***@"
        return "***"
    return SECRET.sub(rep, s or "")


# ─────────────── active iTerm pane ───────────────

async def session_cwd(session) -> str | None:
    """Pane directory: the `path` variable (shell integration), else the cwd of the
    foreground process, else the shell's cwd."""
    try:
        path = await session.async_get_variable("path")
        if path and os.path.isdir(path):
            return path
    except Exception:  # noqa: BLE001
        pass
    for var in ("jobPid", "pid"):
        try:
            pid = await session.async_get_variable(var)
        except Exception:  # noqa: BLE001
            pid = None
        if not pid:
            continue
        _, out = await run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], timeout=3)
        for line in out.splitlines():
            if line.startswith("n") and os.path.isdir(line[1:]):
                return line[1:]
    return None


async def session_agent(session) -> dict | None:
    """claude running in this pane: a process on its tty that has
    ~/.claude/sessions/<pid>.json (sessionId and cwd live there)."""
    try:
        tty = await session.async_get_variable("tty")
    except Exception:  # noqa: BLE001
        tty = None
    if not tty:
        return None
    _, out = await run(["ps", "-t", tty.replace("/dev/", ""), "-o", "pid="], timeout=3)
    for pid in out.split():
        meta = os.path.join(CLAUDE_DIR, "sessions", f"{pid}.json")
        if os.path.isfile(meta):
            try:
                with open(meta) as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            return {"pid": d.get("pid"), "sid": d.get("sessionId"), "cwd": d.get("cwd"),
                    "name": d.get("name"), "status": d.get("status")}
    return None


async def refresh_session(app, session_id: str | None):
    if not session_id:
        return
    session = app.get_session_by_id(session_id)
    if session is None:
        return
    STATE["session_id"] = session_id
    try:
        STATE["session_name"] = (await session.async_get_variable("name")) or session.name or ""
    except Exception:  # noqa: BLE001
        STATE["session_name"] = session.name or ""
    try:
        STATE["tty"] = os.path.basename((await session.async_get_variable("tty")) or "")
    except Exception:  # noqa: BLE001
        STATE["tty"] = ""
    STATE["cwd"] = await session_cwd(session)
    STATE["agent"] = await session_agent(session)


# ─────────────── Git & PR tab ───────────────

async def git_local(root: str) -> dict:
    _, branch = await run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root)
    branch = branch.strip()
    _, head = await run(["git", "log", "-1", "--format=%h %s"], root)
    rc, ab_main = await run(["git", "rev-list", "--left-right", "--count", "origin/main...HEAD"], root)
    behind_main = ahead_main = None
    if rc == 0 and ab_main.split():
        behind_main, ahead_main = (int(x) for x in ab_main.split()[:2])
    rc, up = await run(["git", "rev-parse", "--abbrev-ref", "@{upstream}"], root)
    upstream = up.strip() if rc == 0 else None
    behind_up = ahead_up = None
    if upstream:
        rc, ab = await run(["git", "rev-list", "--left-right", "--count", f"{upstream}...HEAD"], root)
        if rc == 0 and ab.split():
            behind_up, ahead_up = (int(x) for x in ab.split()[:2])
    # no -uall: an untracked dir comes as one line, otherwise tool logs
    # flood the whole list
    _, st = await run(["git", "status", "--porcelain=v1"], root)
    files = [{"st": line[:2], "path": line[3:]} for line in st.splitlines() if len(line) > 3]
    files.sort(key=lambda f: (f["st"] == "??", f["path"]))
    _, stat = await run(["git", "diff", "HEAD", "--shortstat"], root)
    return {"branch": branch, "head": head.strip(), "behind_main": behind_main,
            "ahead_main": ahead_main, "upstream": upstream, "behind_up": behind_up,
            "ahead_up": ahead_up, "files": files[:200], "files_total": len(files),
            "shortstat": stat.strip()}


async def maybe_fetch(root: str):
    """Quiet fetch every 5 minutes so "behind main" stays honest."""
    now = time.monotonic()
    if now - _FETCHED.get(root, 0) < 300:
        return
    _FETCHED[root] = now
    asyncio.create_task(run(["git", "fetch", "-q", "origin"], root, timeout=60))


def _checks(rollup) -> dict:
    c = {"pass": 0, "fail": 0, "pending": 0}
    for r in rollup or []:
        concl = (r.get("conclusion") or r.get("state") or "").upper()
        status = (r.get("status") or "").upper()
        if concl in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            c["pass"] += 1
        elif concl in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"):
            c["fail"] += 1
        elif status in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING") or not concl:
            c["pending"] += 1
    return c


async def gh_branch_pr(root: str, branch: str) -> dict | None:
    if not branch or branch in ("HEAD", "main", "master"):
        return None
    rc, out = await run(["gh", "pr", "view", branch, "--json",
                         "number,title,state,isDraft,url,mergeable,statusCheckRollup"], root, timeout=20)
    if rc != 0:
        return None
    try:
        d = json.loads(out)
    except ValueError:
        return None
    d["checks"] = _checks(d.pop("statusCheckRollup", None))
    return d


async def gh_my_prs(root: str) -> list:
    rc, out = await run(["gh", "pr", "list", "--author", "@me", "--limit", "15", "--json",
                         "number,title,isDraft,url,headRefName,statusCheckRollup"], root, timeout=20)
    if rc != 0:
        return []
    try:
        rows = json.loads(out)
    except ValueError:
        return []
    for r in rows:
        r["checks"] = _checks(r.pop("statusCheckRollup", None))
    return rows


async def repo_checks(root: str) -> list:
    out = []
    for chk in CFG.get("repo_checks") or []:
        f = chk.get("file")
        if f and not os.path.exists(os.path.join(root, f)):
            continue
        cmd = chk.get("cmd")
        if isinstance(cmd, str):
            cmd = shlex.split(cmd)
        if not cmd:
            continue
        rc, text = await run(cmd, root, timeout=float(chk.get("timeout", 25)))
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]
        out.append({"name": chk.get("name", " ".join(cmd)), "rc": rc,
                    "lines": lines[: int(chk.get("max_lines", 6))]})
    return out


async def worktrees(root: str) -> list:
    _, out = await run(["git", "worktree", "list", "--porcelain"], root)
    rows, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                rows.append(cur)
            cur = {}
        elif line.startswith("worktree "):
            cur["path"] = line[9:]
        elif line.startswith("branch "):
            cur["branch"] = line[7:].replace("refs/heads/", "")
        elif line == "detached":
            cur["branch"] = "(detached)"
    return rows


async def git_state() -> dict:
    cwd = STATE["cwd"]
    out = {"v": BOOT, "session": STATE["session_name"], "cwd": cwd, "repo": None, "ts": time.strftime("%H:%M:%S"),
           "onboarded": CFG.get("onboarded")}
    if not cwd:
        return out
    rc, root = await run(["git", "rev-parse", "--show-toplevel"], cwd, timeout=3)
    if rc != 0:
        return out
    root = root.strip()
    _, common = await run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], root, timeout=3)
    common = common.strip()
    await maybe_fetch(root)
    local = await cached(f"local:{root}", 2.5, lambda: git_local(root))
    name = os.path.basename(os.path.dirname(common)) if common.endswith(".git") else os.path.basename(root)
    out["repo"] = {"root": root, "name": name, **local}
    br = local.get("branch")
    out["pr"] = cached_bg(f"pr:{root}:{br}", 60, lambda: gh_branch_pr(root, br))
    out["my_prs"] = cached_bg(f"my:{common}", 120, lambda: gh_my_prs(root))
    out["checks"] = cached_bg(f"chk:{common}", 45, lambda: repo_checks(root))
    out["worktrees"] = cached_bg(f"wt:{common}", 30, lambda: worktrees(root))
    return out


async def file_diff(path: str) -> str:
    cwd = STATE["cwd"]
    if not cwd:
        return ""
    rc, root = await run(["git", "rev-parse", "--show-toplevel"], cwd, timeout=3)
    if rc != 0:
        return ""
    root = root.strip()
    _, st = await run(["git", "status", "--porcelain=v1", "--", path], root)
    if not st.strip():
        return "(no changes)"
    if st.startswith("??") and path.endswith("/"):
        _, ls = await run(["git", "ls-files", "--others", "--exclude-standard", "--", path], root)
        names = ls.splitlines()
        return f"untracked directory, files: {len(names)}\n" + "\n".join(names[:300])
    if st.startswith("??"):
        try:
            with open(os.path.join(root, path), "r", encoding="utf-8", errors="replace") as f:
                body = f.read(200_000)
            return "".join("+" + l + "\n" for l in body.splitlines())
        except OSError as e:
            return f"(cannot read: {e})"
    _, d = await run(["git", "diff", "HEAD", "--no-color", "--", path], root, timeout=10)
    return d[:400_000] or "(empty diff)"


# ─────────────── Agent actions tab ───────────────

TRANSCRIPTS: dict[str, dict] = {}


def transcript_path(c: dict) -> str | None:
    slug = re.sub(r"[^A-Za-z0-9]", "-", c.get("cwd") or "")
    p = os.path.join(CLAUDE_DIR, "projects", slug, f"{c['sid']}.jsonl")
    if os.path.isfile(p):
        return p
    hits = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", f"{c['sid']}.jsonl"))
    return hits[0] if hits else None


def _summary(name: str, inp: dict) -> tuple[str, str]:
    g = inp.get
    if name == "Bash":
        return "bash", g("command", "")
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return "edit", g("file_path") or g("notebook_path") or ""
    if name == "Read":
        return "read", g("file_path", "")
    if name in ("Grep", "Glob"):
        return "search", f"{g('pattern', '')}  {g('path', '') or ''}".strip()
    if name in ("Agent", "Task", "Workflow"):
        return "agent", g("description") or (g("prompt") or "")[:120]
    if name in ("WebFetch", "WebSearch"):
        return "web", g("url") or g("query", "")
    if name.startswith("mcp__"):
        return "mcp", name.split("__", 2)[-1] + " " + json.dumps(inp, ensure_ascii=False)[:160]
    return "other", json.dumps(inp, ensure_ascii=False)[:160]


def _detail(name: str, inp: dict) -> str:
    g = inp.get
    if name == "Bash":
        return (f"# {g('description')}\n" if g("description") else "") + g("command", "")
    if name == "Edit":
        return "\n".join(["@@ " + g("file_path", "")] + ["-" + l for l in (g("old_string") or "").splitlines()]
                         + ["+" + l for l in (g("new_string") or "").splitlines()])
    if name == "MultiEdit":
        parts = ["@@ " + g("file_path", "")]
        for ed in g("edits") or []:
            parts += ["-" + l for l in (ed.get("old_string") or "").splitlines()]
            parts += ["+" + l for l in (ed.get("new_string") or "").splitlines()] + ["@@"]
        return "\n".join(parts)
    if name == "Write":
        return "\n".join(["@@ " + g("file_path", "") + " (new/overwritten)"] +
                         ["+" + l for l in (g("content") or "").splitlines()[:400]])
    return json.dumps(inp, ensure_ascii=False, indent=1)[:20000]


def read_transcript(path: str) -> dict:
    """Reads the jsonl from where it stopped last time, pairs tool_use with its result."""
    t = TRANSCRIPTS.setdefault(path, {"off": 0, "items": [], "idx": {}})
    try:
        size = os.path.getsize(path)
    except OSError:
        return t
    if size < t["off"]:
        t.update(off=0, items=[], idx={})
    if size == t["off"]:
        return t
    with open(path, "rb") as f:
        f.seek(t["off"])
        chunk = f.read()
    end = chunk.rfind(b"\n") + 1
    t["off"] += end
    for raw in chunk[:end].splitlines():
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        content = (d.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        ts = d.get("timestamp", "")
        for x in content:
            if x.get("type") == "tool_use":
                name, inp = x.get("name", "?"), x.get("input") or {}
                kind, line = _summary(name, inp)
                t["idx"][x.get("id")] = len(t["items"])
                t["items"].append({"ts": ts, "tool": name, "kind": kind, "line": mask(line)[:400],
                                   "detail": mask(_detail(name, inp)), "res": None})
            elif x.get("type") == "tool_result":
                i = t["idx"].get(x.get("tool_use_id"))
                if i is not None:
                    t["items"][i]["res"] = "err" if x.get("is_error") else "ok"
    if len(t["items"]) > 3000:
        cut = len(t["items"]) - 3000
        t["items"] = t["items"][cut:]
        t["idx"] = {k: v - cut for k, v in t["idx"].items() if v >= cut}
    return t


def agent_state(limit: int = 400) -> dict:
    c = STATE.get("agent")
    out = {"v": BOOT, "session": STATE["session_name"], "claude": c, "items": [], "total": 0}
    if not c or not c.get("sid"):
        return out
    path = transcript_path(c)
    if not path:
        return out
    items = read_transcript(path)["items"]
    out["total"] = len(items)
    start = max(0, len(items) - limit)
    out["items"] = [{"i": i, "t": it["ts"], "tool": it["tool"], "kind": it["kind"],
                     "line": it["line"], "res": it["res"]} for i, it in enumerate(items[start:], start)]
    return out


def agent_item(i: int) -> str:
    c = STATE.get("agent")
    path = c and c.get("sid") and transcript_path(c)
    t = TRANSCRIPTS.get(path or "")
    if not t or not (0 <= i < len(t["items"])):
        return ""
    return t["items"][i]["detail"]


# ─────────────── Sessions tab ───────────────

SCAN: dict[str, tuple[float, dict | None]] = {}
SCAN_FILE = os.path.join(CONFIG_DIR, "scan-cache.json")
SCAN_VERSION = 1  # Bump when scanner changes require reparsing unchanged files.
# device, inode, read offset, mtime_ns, last complete name, unfinished line
PI_NAMES: dict[str, tuple[int, int, int, int, str | None, bytes]] = {}
SAFE_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,126}[A-Za-z0-9])?$")


def _first_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") in ("text", "input_text") and x.get("text"):
                return x["text"]
    return ""


def _clean_title(t: str) -> str:
    t = re.sub(r"<[^>]{1,80}>.*?</[^>]{1,80}>", " ", t or "", flags=re.S)
    return " ".join(t.split())[:200]


def _read_head(path: str, max_lines: int = 400, need: tuple = ()) -> list:
    """First lines of a jsonl, at most 512 KB. need: parse only lines containing these
    bytes (Codex lines can be hundreds of KB, json.loads on them is expensive)."""
    out, budget = [], 512 * 1024
    try:
        with open(path, "rb") as f:
            for i, raw in enumerate(f):
                budget -= len(raw)
                if i >= max_lines or budget < 0:
                    break
                if need and not any(n in raw for n in need):
                    continue
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def _tail_title(path: str) -> str:
    """Claude Code writes ai-title/summary as the session goes: take the last one from the tail."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        if ('"ai-title"' in line or '"type":"summary"' in line or '"custom-title"' in line
                or '"session_info"' in line):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("title") or d.get("aiTitle") or d.get("summary") or d.get("customTitle") or d.get("name")
            if isinstance(t, str) and t.strip():
                return t.strip()
    return ""


def _pi_name_from_line(line: bytes) -> str | None:
    if b'"session_info"' not in line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if not isinstance(entry, dict) or entry.get("type") != "session_info":
        return None
    name = entry.get("name")
    return name.strip() if isinstance(name, str) else ""


def _last_pi_name(path: str) -> str | None:
    """Find the latest Pi name; after the first scan, read only appended bytes."""
    try:
        with open(path, "rb") as f:
            st = os.fstat(f.fileno())
            cached = PI_NAMES.get(path)
            if cached:
                dev, ino, offset, mtime, name, pending = cached
                if (dev, ino) == (st.st_dev, st.st_ino) and st.st_size >= offset and (
                        st.st_size > offset or st.st_mtime_ns == mtime):
                    f.seek(offset)
                    appended = f.read()
                    parts = (pending + appended).split(b"\n")
                    for line in parts[:-1]:
                        found = _pi_name_from_line(line)
                        if found is not None:
                            name = found
                    pending = parts[-1]
                    PI_NAMES[path] = (dev, ino, f.tell(), st.st_mtime_ns, name, pending)
                    found = _pi_name_from_line(pending)
                    return found if found is not None else name

            # Pi writes JSONL. Keep the last unterminated line for the next append.
            pos, fragments = st.st_size, []
            if pos:
                f.seek(pos - 1)
                if f.read(1) != b"\n":
                    while pos:
                        start = max(0, pos - 65536)
                        f.seek(start)
                        block = f.read(pos - start)
                        cut = block.rfind(b"\n")
                        fragments.append(block[cut + 1:])
                        pos = start
                        if cut >= 0:
                            break
                    pending = b"".join(reversed(fragments))
                else:
                    pending = b""
            else:
                pending = b""

            name, pos, fragments = None, st.st_size - len(pending), []
            while pos:
                start = max(0, pos - 65536)
                f.seek(start)
                parts = f.read(pos - start).split(b"\n")
                pos = start
                fragments.append(parts[-1])
                if len(parts) == 1:
                    continue
                name = _pi_name_from_line(b"".join(reversed(fragments)))
                if name is not None:
                    break
                for line in reversed(parts[1:-1]):
                    name = _pi_name_from_line(line)
                    if name is not None:
                        break
                if name is not None:
                    break
                fragments = [parts[0]]
            else:
                if fragments:
                    name = _pi_name_from_line(b"".join(reversed(fragments)))

            PI_NAMES[path] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, name, pending)
            found = _pi_name_from_line(pending)
            return found if found is not None else name
    except OSError:
        PI_NAMES.pop(path, None)
        return None


def _scan_claude(path: str) -> dict | None:
    sid = os.path.basename(path)[:-6]
    cwd, first = "", ""
    for d in _read_head(path, need=(b'"cwd"',)):
        cwd = cwd or d.get("cwd") or ""
        if not first and d.get("type") == "user" and not d.get("isMeta"):
            t = _first_text((d.get("message") or {}).get("content"))
            if t and not t.lstrip().startswith(("<command-", "<local-command", "Caveat:")):
                first = t
        if cwd and first:
            break
    if not cwd:
        return None
    return {"id": sid, "tool": "claude", "dir": cwd, "title": _clean_title(_tail_title(path) or first)}


def _scan_codex(path: str) -> dict | None:
    sid, cwd, first = "", "", ""
    meta_seen = False
    for d in _read_head(path, 200, need=(b'"session_meta"', b'"user_message"', b'"role"')):
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if d.get("type") == "session_meta":
            if not meta_seen:
                # Later metadata can belong to a parent copied into this history.
                meta_seen = True
                sid, cwd = p.get("id", ""), p.get("cwd", "")
                source = p.get("source")
                if isinstance(source, dict) and "subagent" in source:
                    return None
        elif not first:
            if d.get("type") == "event_msg" and p.get("type") == "user_message":
                t = p.get("message", "")
            elif d.get("type") == "response_item" and p.get("role") == "user":
                t = _first_text(p.get("content"))
            else:
                continue
            if t and not t.lstrip().startswith(("<environment_context", "<user_instructions", "# AGENTS.md")):
                first = _clean_title(t)
        if sid and cwd and first:
            break
    if not sid:
        return None
    return {"id": sid, "tool": "codex", "dir": cwd, "title": first}


def _scan_pi_like(path: str, tool: str) -> dict | None:
    """Pi and omp share a session header and message format; omp may put a title before it."""
    sid, cwd, title, first = "", "", "", ""
    for d in _read_head(path, need=(b'"cwd"', b'"title"', b'"role"')):
        if d.get("type") == "title" and tool == "omp":
            title = d.get("title") or title
        elif d.get("type") == "session":
            sid, cwd = d.get("id", ""), d.get("cwd", "")
            title = title or d.get("title") or ""
        elif d.get("type") == "message" and not first:
            msg = d.get("message") or {}
            if isinstance(msg, dict) and msg.get("role") == "user":
                first = _first_text(msg.get("content"))
        if tool == "omp" and sid and cwd and (title or first):
            break
    if not isinstance(sid, str) or not sid or not isinstance(cwd, str) or not cwd:
        return None
    name = _last_pi_name(path) if tool == "pi" else None
    return {"id": sid, "tool": tool, "dir": cwd,
            "title": _clean_title((name if name is not None else title) or first)}


def _scan_all() -> list:
    """stat every session file, parse only new/changed ones. The cache lives on disk:
    after a restart only changed files are read again."""
    if not SCAN and os.path.isfile(SCAN_FILE):
        try:
            with open(SCAN_FILE) as f:
                cache = json.load(f)
            if isinstance(cache, dict) and cache.get("version") == SCAN_VERSION:
                SCAN.update({k: tuple(v) for k, v in cache["sessions"].items()})
        except (OSError, ValueError):
            pass
    files = [(p, _scan_claude) for p in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl"))]
    files += [(p, _scan_codex) for p in glob.glob(os.path.join(CODEX_DIR, "sessions", "*", "*", "*", "*.jsonl"))]
    for tool, root in (("pi", PI_DIR), ("omp", OMP_DIR)):
        files += [(p, lambda path, tool=tool: _scan_pi_like(path, tool))
                  for p in glob.glob(os.path.join(root, "agent", "sessions", "*", "*.jsonl"))]
    rows, seen, changed = [], set(), False
    for path, fn in files:
        seen.add(path)
        try:
            mt = os.path.getmtime(path)
        except OSError:
            continue
        hit = SCAN.get(path)
        if not hit or hit[0] != mt:
            try:
                hit = (mt, fn(path))
            except Exception:  # noqa: BLE001
                hit = (mt, None)
            SCAN[path] = hit
            changed = True
        if hit[1]:
            rows.append(dict(hit[1], last=int(mt * 1000)))
    for p in list(SCAN):
        if p not in seen:
            SCAN.pop(p, None)
            PI_NAMES.pop(p, None)
            changed = True
    if changed:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(SCAN_FILE + ".tmp", "w") as f:
                json.dump({"version": SCAN_VERSION, "sessions": SCAN}, f)
            os.replace(SCAN_FILE + ".tmp", SCAN_FILE)
        except OSError:
            pass
    rows.sort(key=lambda r: -r["last"])
    return rows


def _is_pi_command(cmd: str) -> bool:
    """Recognize Pi itself, including Node/Bun launches before Pi sets its process title."""
    args = cmd.split()
    if not args:
        return False
    exe = os.path.basename(args[0])
    if exe in ("pi", "pi-rpc"):
        return True
    return exe in ("node", "bun") and any(
        re.search(r"/@earendil-works/pi-coding-agent/dist/(?:bundle/)?cli\.js$", arg)
        or arg.endswith("/bin/pi") for arg in args[1:])


async def _pi_processes(ps: str) -> list | None:
    """Find Pi processes and their working directories; None means the check failed."""
    out = []
    for line in ps.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, tty, stat, cmd = parts
        if not pid.isdecimal() or stat.startswith("Z") or not _is_pi_command(cmd):
            continue
        rc, cw = await run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], timeout=3)
        d = next((l[1:] for l in cw.splitlines() if l.startswith("n")), "")
        if rc or not d:
            try:
                os.kill(int(pid), 0)
            except ProcessLookupError:
                continue  # the process exited between ps and lsof
            except OSError:
                pass
            return None
        out.append({"tool": "pi", "pid": int(pid), "id": "", "dir": d,
                    "name": "", "status": "", "since": 0, "tty": tty, "stopped": stat.startswith("T")})
    return out


async def _pi_resume_guard(d: str) -> str:
    """Pi does not expose its current session ID, so guard the whole project."""
    if not d:
        return "choose a project before resuming pi"
    rc, ps = await run(["ps", "-xo", "pid=,tty=,stat=,command="], timeout=5)
    if rc:
        return "could not check running pi processes"
    agents = await _pi_processes(ps)
    if agents is None:
        return "could not check running pi processes"
    if any(_same_dir(a["dir"], d) for a in agents):
        return "pi is already running in this project; close it before resuming"
    return ""


def _reserve_pi_resume(d: str) -> tuple[tuple[int, int], object] | None:
    """Reserve one project before the process check; stat also resolves symlinks and case aliases."""
    st = os.stat(d)
    key = (st.st_dev, st.st_ino)
    now = time.monotonic()
    with _PI_RESUMES_LOCK:
        for old_key, (_, until) in list(_PI_RESUMES.items()):
            if until <= now:
                _PI_RESUMES.pop(old_key)
        if key in _PI_RESUMES:
            return None
        ticket = object()
        _PI_RESUMES[key] = (ticket, now + 2 * _PI_RESUME_TIMEOUT)
    return key, ticket


def _release_pi_resume(reservation: tuple[tuple[int, int], object]) -> None:
    key, ticket = reservation
    with _PI_RESUMES_LOCK:
        if _PI_RESUMES.get(key, (None, 0))[0] is ticket:
            _PI_RESUMES.pop(key, None)


def _renew_pi_resume(reservation: tuple[tuple[int, int], object]) -> bool:
    """Keep the reservation briefly after sending while Pi starts."""
    key, ticket = reservation
    with _PI_RESUMES_LOCK:
        if _PI_RESUMES.get(key, (None, 0))[0] is not ticket:
            return False
        _PI_RESUMES[key] = (ticket, time.monotonic() + _PI_RESUME_TIMEOUT)
    return True


def _same_dir(left: str, right: str) -> bool:
    try:
        return os.path.samefile(left, right)
    except OSError:
        return os.path.realpath(left) == os.path.realpath(right)


async def _active_agents() -> list:
    """Running Claude (session metadata), Codex and Pi (processes)."""
    _, ps = await run(["ps", "-xo", "pid=,tty=,stat=,command="], timeout=5)
    ttys, stopped = {}, set()
    for line in ps.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, tty, stat, cmd = parts
        ttys[pid] = tty
        if stat.startswith("T"):
            stopped.add(pid)  # suspended with Ctrl+Z: not a running agent
    claude = {}
    for meta in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            with open(meta) as f:
                d = json.load(f)
            os.kill(int(d["pid"]), 0)
        except Exception:  # noqa: BLE001
            continue
        if str(d["pid"]) in stopped:
            continue
        a = {"tool": "claude", "pid": d["pid"], "id": d.get("sessionId"), "dir": d.get("cwd", ""),
             "name": d.get("name", ""), "status": d.get("status", ""), "since": d.get("startedAt", 0)}
        # the same session resumed in a second process: keep the newest one
        k = a["id"] or f"pid:{a['pid']}"
        if k not in claude or (a["since"] or 0) > (claude[k]["since"] or 0):
            claude[k] = a
    out = list(claude.values())
    for line in ps.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, tty, _stat, cmd = parts
        argv0 = os.path.basename(cmd.split()[0]) if cmd.split() else ""
        is_codex = argv0 == "codex" or re.search(r"/codex(\s|$)", cmd.split(" --")[0])
        if is_codex and "app-server" not in cmd and tty not in ("??", "-") and pid not in stopped:
            _, cw = await run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], timeout=3)
            d = next((l[1:] for l in cw.splitlines() if l.startswith("n")), "")
            out.append({"tool": "codex", "pid": int(pid), "id": "", "dir": d, "name": "", "status": "", "since": 0})
    out.extend(a for a in (await _pi_processes(ps) or []) if not a["stopped"])
    for a in out:
        a["tty"] = ttys.get(str(a["pid"]), "")
    return out


async def sessions_state() -> dict:
    rows = cached_bg("scan:sessions", 20, lambda: asyncio.to_thread(_scan_all))
    active = cached_bg("scan:active", 4, _active_agents) or []
    live = {a["id"] for a in active if a.get("id")}
    return {"v": BOOT, "cwd": STATE["cwd"] or "", "session": STATE["session_name"], "tty": STATE["tty"], "ok": rows is not None,
            "rows": [dict(r, live=r["id"] in live) for r in (rows or [])], "active": active,
            "open_in": CFG.get("open_in", "window"),
            "skip": bool(CFG["agents"]["claude"].get("skip_permissions"))}


NO_PROXY_PREFIX = "env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy -u ALL_PROXY -u all_proxy"


def proxy_prefix(name: str) -> str | None:
    """Env prefix that runs a command through a proxy from the local config ("direct" = none)."""
    if name == "direct":
        return NO_PROXY_PREFIX
    url = next((p["url"] for p in CFG.get("proxies") or [] if p.get("url") and p.get("name") == name), None)
    if not url:
        return None
    q = shlex.quote(url)
    return f"HTTPS_PROXY={q} HTTP_PROXY={q}"


def proxy_export(name: str) -> str | None:
    """Line to paste into a shell: route this shell through the proxy (or drop the proxy)."""
    if name == "direct":
        return "unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy ALL_PROXY all_proxy"
    p = proxy_prefix(name)
    return p and "export " + p


def agent_command(tool: str, sid: str, skip: bool, proxy: str = "") -> str | None:
    """Empty sid = new session. Prefix (e.g. HTTPS_PROXY=...) and flags come from the config;
    `proxy` (a name from the config, or "direct") replaces the configured prefix."""
    q = shlex.quote(sid) if sid else ""
    a = dict(CFG["agents"].get("claude" if tool in ("claude", "claude-ext") else tool, {}))
    proxy = proxy or a.get("proxy") or ""
    if proxy:
        pre = proxy_prefix(proxy)
        if pre is None:
            return None
        a["prefix"] = pre
    if tool in ("claude", "claude-ext"):
        flags = (a.get("flags") or "").replace("--dangerously-skip-permissions", "").strip()
        if skip:
            flags = (flags + " --dangerously-skip-permissions").strip()
        core = f"claude --resume {q}" if sid else "claude"
    elif tool == "codex":
        flags = a.get("flags") or ""
        core = f"codex resume {q}" if sid else "codex"
    elif tool in ("pi", "omp"):
        flags = a.get("flags") or ""
        core = f"{tool} {'--session' if tool == 'pi' else '-r'} {q}" if sid else tool
        if tool == "omp" and not sid:
            flags = (flags + " --config " + shlex.quote(os.path.join(HERE, "resources", "omp-new-session.yml"))).strip()
    elif sid:
        return {"qwen": f"qwen -r {q}", "kilo": f"kilo resume {q}", "opencode": f"opencode -s {q}",
                "cursor": f"cursor-agent --resume {q}"}.get(tool)
    else:
        return None
    return " ".join(x for x in (a.get("prefix") or "", core, flags) if x)


async def open_agent(q: dict) -> str:
    sid, tool, d = q.get("id", ""), q.get("tool", ""), q.get("dir", "")
    if sid and not SAFE_ID.match(sid):
        return "bad id"
    if q.get("proxy"):
        # launched from the network block: current pane's directory, settings from Sessions
        d = d or STATE["cwd"] or ""
        q.setdefault("skip", "1" if CFG["agents"]["claude"].get("skip_permissions") else "0")
        q.setdefault("where", CFG.get("open_in", "window"))
    cmd = agent_command(tool, sid, q.get("skip") == "1", q.get("proxy", ""))
    if not cmd:
        return f"cannot launch {tool}" + (f" via {q['proxy']}" if q.get("proxy") else "")
    if d and not os.path.isdir(d):
        return f"no such directory: {d}"
    conn = CONN.get("c")
    if conn is None:
        return "no connection to iTerm"
    reservation = None
    if tool == "pi" and sid:
        if not d:
            return "choose a project before resuming pi"
        try:
            reservation = _reserve_pi_resume(d)
        except OSError:
            return f"no such directory: {d}"
        if reservation is None:
            return "pi is already starting in this project"
    sent = False
    try:
        if reservation:
            reason = await _pi_resume_guard(d)
            if reason:
                return reason
        app = await iterm2.async_get_app(conn)
        cur = app.current_terminal_window
        if q.get("where") == "tab" and cur is not None:
            tab = await cur.async_create_tab()
            win_id, tab_id = cur.window_id, tab.tab_id
        else:
            w = await iterm2.Window.async_create(conn)
            win_id, tab_id = w.window_id, w.current_tab.tab_id
        # a new window does not hand over its session right away, and a helper bash runs first:
        # re-fetch the session by id and wait for the user's shell, otherwise the text is lost
        shell = os.path.basename(os.environ.get("SHELL", "zsh"))
        sess = None
        for _ in range(50):
            await asyncio.sleep(0.2)
            try:
                await app.async_refresh()
                win = app.get_window_by_id(win_id)
                tab = win and next((t for t in win.tabs if t.tab_id == tab_id), None)
                sess = tab and tab.current_session
                if sess and (await sess.async_get_variable("jobName")) in (shell, "-" + shell):
                    break
            except Exception:  # noqa: BLE001
                sess = None
        if not sess:
            return "iTerm did not return the new session"
        await asyncio.sleep(0.3)
        if reservation and not _renew_pi_resume(reservation):
            return "pi resume request expired; retry"
        sent = True  # sending may succeed even if iTerm reports an error afterward
        await sess.async_send_text((f"cd {shlex.quote(d)} && " if d else "") + cmd + "\n")
        await sess.async_activate(select_tab=True, order_window_front=True)
        return "ok"
    finally:
        if reservation and not sent:
            _release_pi_resume(reservation)


async def focus_tty(tty: str) -> str:
    """Switch to the iTerm pane where the agent runs (by tty)."""
    conn = CONN.get("c")
    if conn is None or not tty:
        return "no connection to iTerm"
    app = await iterm2.async_get_app(conn)
    for w in app.terminal_windows:
        for t in w.tabs:
            for s in t.sessions:
                try:
                    st = await s.async_get_variable("tty")
                except Exception:  # noqa: BLE001
                    continue
                if st and st.endswith(tty):
                    await s.async_activate(select_tab=True, order_window_front=True)
                    await app.async_activate()
                    return "ok"
    return "this window is not in iTerm"


# ─────────────── window snapshots (TermDeck format) ───────────────

SNAP_DIR = os.path.expanduser(os.getenv("ITERMSNAP_HOME", "~/.config/itermsnap")) + "/snaps"
SAFE_SNAP = re.compile(r"^[\w .:-]{1,60}$")
AUTO_PREFIX = "auto-"
AUTO_KEEP = 20
_LAST_AUTO = {"sig": None}


async def _pane_info(session) -> dict | None:
    """Pane directory and the agent in it. The claude session id comes from the process on
    the tty (~/.claude/sessions/<pid>.json), not from the newest file in the directory."""
    agent = await session_agent(session)
    if agent and agent.get("cwd"):
        return {"cwd": agent["cwd"], "session_id": agent.get("sid"), "agent": "claude"}
    cwd = await session_cwd(session)
    if not cwd:
        return None
    return {"cwd": cwd, "session_id": None, "agent": ""}


async def capture_windows(app, only_current: bool = False) -> list:
    """Windows → [{title, split, panes:[{cwd, session_id, agent}]}], TermDeck format.
    only_current: only the window that has focus."""
    tabs = []
    wins = [app.current_terminal_window] if only_current else app.terminal_windows
    for w in [x for x in wins if x]:
        for t in w.tabs:
            panes = []
            for sess in t.sessions:
                info = await _pane_info(sess)
                if info:
                    panes.append(info)
            if not panes:
                continue
            title = ""
            try:
                title = (await t.async_get_variable("titleOverride")) or ""
            except Exception:  # noqa: BLE001
                pass
            if not title:
                title = os.path.basename(panes[0]["cwd"].rstrip("/")) or "~"
            tabs.append({"title": title[:60], "split": "vertical", "panes": panes})
    return tabs


def snap_list() -> list:
    rows = (CACHE.get("scan:sessions") or (0, None))[1] or []
    titles = {r["id"]: r["title"] for r in rows}
    out = []
    for p in sorted(glob.glob(os.path.join(SNAP_DIR, "*.json")), key=os.path.getmtime, reverse=True):
        try:
            with open(p) as f:
                tabs = json.load(f)
        except (OSError, ValueError):
            continue
        name = os.path.basename(p)[:-5]
        out.append({"name": name, "mtime": int(os.path.getmtime(p) * 1000), "auto": name.startswith((AUTO_PREFIX, "autosave")),
                    "tabs": [{"title": t.get("title") or os.path.basename((t.get("panes") or [{}])[0].get("cwd", "")),
                              "split": t.get("split", "vertical"),
                              "panes": len(t.get("panes") or []),
                              "agents": sum(1 for x in t.get("panes") or [] if x.get("session_id")),
                              "detail": [{"dir": os.path.basename((x.get("cwd") or "").rstrip("/")) or "~",
                                          "sid": (x.get("session_id") or "")[:8],
                                          "agent": x.get("agent") or ("claude" if x.get("session_id") else ""),
                                          "title": titles.get(x.get("session_id") or "", "")[:80]}
                                         for x in t.get("panes") or []]} for t in tabs]})
    return out


def _write_snap(name: str, tabs: list):
    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, name + ".json")
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    with open(path + ".tmp", "w") as f:
        json.dump(tabs, f, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)


async def snap_save(name: str, only_current: bool = False) -> str:
    if not SAFE_SNAP.match(name or ""):
        return "name: letters, digits, space, . : - up to 60 chars"
    app = await iterm2.async_get_app(CONN["c"])
    tabs = await capture_windows(app, only_current)
    if not tabs:
        return "nothing to save: no windows with directories"
    _write_snap(name, tabs)
    return "ok"


async def autosave_loop(app):
    """Every 5 minutes, snapshot all windows if something changed and at least one agent
    runs. Keeps the last AUTO_KEEP, so closing all windows at once won't overwrite a good one."""
    delay = 60   # first snapshot a minute after start, then every 5 minutes
    while True:
        await asyncio.sleep(delay)
        delay = 300
        if not CFG.get("autosave", True):
            continue
        try:
            await app.async_refresh()
            tabs = await capture_windows(app)
        except Exception:  # noqa: BLE001
            continue
        if not any(p.get("session_id") for t in tabs for p in t["panes"]):
            continue
        sig = json.dumps(tabs, sort_keys=True)
        if sig == _LAST_AUTO["sig"]:
            continue
        _LAST_AUTO["sig"] = sig
        _write_snap(AUTO_PREFIX + time.strftime("%Y%m%d-%H%M"), tabs)
        autos = sorted(glob.glob(os.path.join(SNAP_DIR, AUTO_PREFIX + "*.json")), key=os.path.getmtime)
        for old in autos[:-AUTO_KEEP]:
            for f in (old, old + ".bak"):
                try:
                    os.remove(f)
                except OSError:
                    pass


async def _wait_shell(app, session_id: str):
    shell = os.path.basename(os.environ.get("SHELL", "zsh"))
    for _ in range(50):
        await asyncio.sleep(0.2)
        try:
            await app.async_refresh()
            s = app.get_session_by_id(session_id)
            if s and (await s.async_get_variable("jobName")) in (shell, "-" + shell):
                return s
        except Exception:  # noqa: BLE001
            pass
    return app.get_session_by_id(session_id)


async def snap_restore(name: str, skip: bool) -> str:
    if not SAFE_SNAP.match(name or ""):
        return "bad name"
    try:
        with open(os.path.join(SNAP_DIR, name + ".json")) as f:
            tabs = json.load(f)
    except (OSError, ValueError):
        return "no such snapshot"
    conn = CONN["c"]
    app = await iterm2.async_get_app(conn)
    # the TermDeck profile (if present) keeps claude from overwriting tab names
    profile = "TermDeck" if os.path.exists(os.path.expanduser(
        "~/Library/Application Support/iTerm2/DynamicProfiles/termdeck.json")) else None
    win = None
    for tab in tabs:
        if win is None:
            win = await iterm2.Window.async_create(conn, profile=profile)
            t = win.current_tab
        else:
            t = await win.async_create_tab(profile=profile)
        first = t.current_session
        sessions = [first]
        for _ in tab.get("panes", [])[1:]:
            sessions.append(await sessions[-1].async_split_pane(vertical=tab.get("split") != "horizontal", profile=profile))
        for sess, pane in zip(sessions, tab.get("panes", [])):
            s = await _wait_shell(app, sess.session_id)
            if not s:
                continue
            cwd = pane.get("cwd") or ""
            cmd = f"cd {shlex.quote(cwd)}" if cwd and os.path.isdir(cwd) else ""
            sid = pane.get("session_id")
            if sid and SAFE_ID.match(sid):
                launch = agent_command(pane.get("agent") or "claude", sid, skip)
                cmd = (cmd + " && " if cmd else "") + launch
            if cmd:
                await s.async_send_text(cmd + "\n")
        try:
            await t.async_set_title(tab.get("title") or "")
        except Exception:  # noqa: BLE001
            pass
    if win:
        await win.async_activate()
    return "ok"


# ─────────────── Settings ───────────────

def settings_state() -> dict:
    # proxy passwords and the router password never go to the page: masked URLs come back
    # unchanged on save and are swapped for the stored ones
    cfg = copy.deepcopy(CFG)
    for px in cfg.get("proxies") or []:
        px["url"] = mask(px.get("url", ""))
    kn = cfg.get("keenetic")
    if isinstance(kn, dict):
        kn["password_set"] = bool(kn.get("password"))
        kn["password"] = ""
    return {"v": BOOT, "version": VERSION, "config": cfg, "config_file": CONFIG_FILE}


PROXY_URL = re.compile(r"^(https?|socks5h?)://([^\s/@]+@)?[\w.-]+:\d{1,5}/?$")


def _save_network(new: dict, cfg: dict) -> str | None:
    """Proxies, servers and the router from the Settings page. Returns an error or None."""
    if isinstance(new.get("proxies"), list):
        old = {px.get("name"): px.get("url", "") for px in cfg.get("proxies") or []}
        out, seen = [], set()
        for px in new["proxies"]:
            if not isinstance(px, dict):
                continue
            name, url = str(px.get("name", "")).strip(), str(px.get("url", "")).strip()
            if not name and not url:
                continue
            if not name or name in seen or name == "direct":
                return f"proxy name «{name}» is empty, taken or reserved"
            if "***" in url:  # untouched masked URL: keep the stored one
                url = old.get(px.get("orig") or name, "")
            if not PROXY_URL.match(url):
                return f"proxy «{name}»: expected scheme://[user:pass@]host:port"
            seen.add(name)
            out.append({"name": name, "url": url})
        cfg["proxies"] = out
    if isinstance(new.get("servers"), list):
        out = []
        for sv in new["servers"]:
            if not isinstance(sv, dict) or not str(sv.get("host", "")).strip():
                continue
            try:
                port = int(sv.get("port") or 22)
            except (TypeError, ValueError):
                return f"server «{sv.get('name')}»: port must be a number"
            item = {"name": str(sv.get("name") or sv["host"]).strip(), "host": str(sv["host"]).strip(), "port": port}
            if sv.get("banner") is False:
                item["banner"] = False
            out.append(item)
        cfg["servers"] = out
    if isinstance(new.get("keenetic"), dict):
        k, cur = new["keenetic"], cfg.get("keenetic") or {}
        host = str(k.get("host", "")).strip()
        if not host:
            cfg.pop("keenetic", None)
        else:
            cfg["keenetic"] = {"host": host, "login": str(k.get("login") or "admin").strip(),
                               "password": k.get("password") or cur.get("password", "")}
    return None


def settings_save(body: bytes) -> str:
    try:
        new = json.loads(body or b"{}")
    except ValueError:
        return "not JSON"
    if not isinstance(new, dict):
        return "not an object"
    cfg = load_config()
    restart = False
    for k in ("open_in", "title_prefix", "onboarded", "auto_show_toolbelt", "autosave"):
        if k in new:
            restart |= k == "title_prefix" and new[k] != cfg.get(k)
            cfg[k] = new[k]
    if isinstance(new.get("agents"), dict):
        for name, vals in new["agents"].items():
            if name in cfg["agents"] and isinstance(vals, dict):
                cfg["agents"][name].update({k: v for k, v in vals.items()
                                            if k in ("prefix", "flags", "skip_permissions", "proxy")})
    if isinstance(new.get("tabs"), dict):
        for k, v in new["tabs"].items():
            if k in cfg["tabs"] and bool(v) != cfg["tabs"][k]:
                cfg["tabs"][k] = bool(v)
                restart = True
    if isinstance(new.get("repo_checks"), list):
        cfg["repo_checks"] = [c for c in new["repo_checks"] if isinstance(c, dict) and c.get("cmd")]
    err = _save_network(new, cfg)
    if err:
        return err
    save_config(cfg)
    CACHE.pop("net:probe", None)
    CFG.clear()
    CFG.update(cfg)
    return "restart" if restart else "ok"


# ─────────────── Network: proxies and servers ───────────────
# Both lists live only in the local config (proxy URLs carry passwords, server IPs are private):
#   "proxies": [{"name": "eu-1", "url": "http://user:pass@host:port"}]
#   "servers": [{"name": "eu-1", "host": "203.0.113.5", "port": 22}]
# Everything is probed from this Mac, so the panel answers "what works from where I sit now".

def _codex_target() -> str:
    """Codex signed in with a ChatGPT account talks to chatgpt.com, with an API key to api.openai.com."""
    try:
        with open(os.path.expanduser("~/.codex/auth.json")) as f:
            if json.load(f).get("auth_mode") == "chatgpt":
                return "https://chatgpt.com/backend-api/codex/models"
    except Exception:  # noqa: BLE001
        pass
    return "https://api.openai.com/v1/models"


# one column per CLI: what each agent actually calls. Without a key every endpoint answers 401,
# which means "reachable"; 403 is a region block, no answer means the path is dead.
def net_targets() -> tuple:
    return (("claude", "https://api.anthropic.com/v1/models"),
            ("codex", _codex_target()),
            ("openai", "https://api.openai.com/v1/models"))
PROXY_RE = re.compile(r"(?:^|\s)(?:HTTPS_PROXY|https_proxy|ALL_PROXY|all_proxy)=(\S+)")


def _hostport(url: str) -> str:
    u = urllib.parse.urlsplit(url if "://" in url else "http://" + url)
    return f"{u.hostname}:{u.port}" if u.hostname else ""


async def session_proxy(pid) -> dict | None:
    """Proxy the running claude was started with: HTTPS_PROXY from its environment
    (`ps eww` shows the environment of our own processes)."""
    if not pid:
        return None
    _, out = await run(["ps", "eww", "-o", "command=", "-p", str(pid)], timeout=3)
    m = PROXY_RE.search(out)
    if not m:
        return {"name": "direct", "url": ""}
    url = m.group(1).strip("'\"")
    hp = _hostport(url)
    name = next((p.get("name") for p in CFG.get("proxies") or [] if _hostport(p.get("url", "")) == hp), None)
    return {"name": name or hp, "url": mask(url)}


async def _curl(proxy: str, url: str) -> dict:
    # the proxy (with its password) goes through stdin, not argv: argv is visible in ps
    conf = (f'proxy = "{proxy}"\n' if proxy else 'noproxy = "*"\n') + f'url = "{url}"\n'
    p = None
    try:
        p = await asyncio.create_subprocess_exec(
            "curl", "-s", "-o", "/dev/null", "-m", "8", "-w", "%{http_code} %{time_total}", "-K", "-",
            env=NOPROXY_ENV, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(conf.encode()), 12)
        code, t = (out.decode().split() + ["0", "0"])[:2]
        return {"code": int(code), "ms": round(float(t) * 1000)}
    except Exception:  # noqa: BLE001
        try:
            p and p.kill()
        except Exception:  # noqa: BLE001
            pass
        return {"code": 0, "ms": None}


async def _probe_proxy(name: str, url: str) -> dict:
    targets = net_targets()
    res = await asyncio.gather(*(_curl(url, u) for _, u in targets))
    return {"name": name, "hp": _hostport(url) if url else "", "res": {k: r for (k, _), r in zip(targets, res)}}


async def _probe_server(s: dict) -> dict:
    host, port = str(s.get("host", "")), int(s.get("port") or 22)
    out = {"name": s.get("name") or host, "host": host, "port": port, "ping": None, "tcp": None, "banner": "",
           "nobanner": s.get("banner") is False}
    rc, txt = await run(["/sbin/ping", "-c", "2", "-t", "4", host], timeout=6)
    m = re.search(r"= [\d.]+/([\d.]+)/", txt)
    out["ping"] = round(float(m.group(1))) if rc == 0 and m else None
    t0 = time.monotonic()
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(host, port), 6)
        out["tcp"] = round((time.monotonic() - t0) * 1000)
        try:
            if not out["nobanner"]:
                out["banner"] = (await asyncio.wait_for(r.readline(), 6)).decode("latin-1").strip()[:40]
        except Exception:  # noqa: BLE001
            out["banner"] = ""  # port open but no greeting: DPI or a stuck sshd
        w.close()
    except Exception:  # noqa: BLE001
        pass
    return out


async def _net_probe() -> dict:
    proxies = [("direct", "")] + [(p.get("name") or _hostport(p.get("url", "")), p["url"])
                                  for p in CFG.get("proxies") or [] if p.get("url")]
    pr, sv = await asyncio.gather(asyncio.gather(*(_probe_proxy(n, u) for n, u in proxies)),
                                  asyncio.gather(*(_probe_server(s) for s in CFG.get("servers") or [] if s.get("host"))))
    return {"at": time.time(), "targets": [k for k, _ in net_targets()], "proxies": list(pr), "servers": list(sv)}


# Keenetic router (optional, local config only): "keenetic": {"host": "192.168.1.1", "login": "admin", "password": "…"}
# Reads WireGuard tunnels, their ping-check state and `dns-proxy route object-group` chains through the RCI API,
# so the panel shows which tunnel every domain group is going through right now.
_KN: dict = {}


def _kn_rci(cmds: list[str]) -> list:
    import hashlib
    import http.cookiejar
    import urllib.request
    k = CFG.get("keenetic") or {}
    base = "http://" + k.get("host", "192.168.1.1")
    if "op" not in _KN:
        _KN["op"] = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                               urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    op = _KN["op"]

    def call():
        req = urllib.request.Request(base + "/rci/", data=json.dumps([{"parse": c} for c in cmds]).encode(),
                                     headers={"Content-Type": "application/json"})
        return json.load(op.open(req, timeout=8))
    try:
        return call()
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
    try:
        op.open(base + "/auth", timeout=5)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        realm, ch = e.headers["X-NDM-Realm"], e.headers["X-NDM-Challenge"]
        md5 = hashlib.md5(f'{k.get("login", "admin")}:{realm}:{k.get("password", "")}'.encode()).hexdigest()
        body = json.dumps({"login": k.get("login", "admin"),
                           "password": hashlib.sha256((ch + md5).encode()).hexdigest()}).encode()
        op.open(urllib.request.Request(base + "/auth", data=body, headers={"Content-Type": "application/json"}), timeout=5)
    return call()


def _kn_state() -> dict:
    run_cfg = (_kn_rci(["show running-config"])[0].get("parse") or {}).get("message") or []
    tunnels, routes, cur = {}, [], None
    for line in run_cfg:
        m = re.match(r"interface (Wireguard\d+)$", line)
        if m:
            cur = m.group(1)
            tunnels[cur] = {"iface": cur, "desc": ""}
            continue
        if cur and line.startswith("    description "):
            tunnels[cur]["desc"] = line.split("description ", 1)[1]
        elif not line.startswith(" "):
            cur = None
        m = re.match(r"\s+route object-group (\S+) (\S+)", line)
        if m:
            routes.append(m.groups())
    names = list(tunnels)
    res = _kn_rci(["show ping-check"] + [f"show interface {n}" for n in names])
    pc = {}
    for prof in ((res[0].get("parse") or {}).get("pingcheck") or []):
        for iface, v in (prof.get("interface") or {}).items():
            pc[iface] = v.get("status")
    now = time.time()
    for n, r in zip(names, res[1:]):
        d = r.get("parse") or {}
        peer = ((d.get("wireguard") or {}).get("peer") or [{}])[0]
        hs = peer.get("last-handshake")
        tunnels[n].update({
            "up": d.get("state") == "up" and d.get("link") == "up",
            "check": pc.get(n),
            "handshake": int(hs) if str(hs or "").isdigit() else None,
            "rx": peer.get("rxbytes"), "tx": peer.get("txbytes"),
        })
    alive = {n for n, t in tunnels.items() if t["up"] and t["check"] != "fail"}
    groups: dict[str, list] = {}
    for g, i in routes:
        groups.setdefault(g, []).append(i)
    return {"at": now, "tunnels": list(tunnels.values()),
            "groups": [{"name": g, "chain": ch, "active": next((i for i in ch if i in alive), None)}
                       for g, ch in groups.items()]}


async def _kn_probe() -> dict:
    try:
        return await asyncio.to_thread(_kn_state)
    except Exception as e:  # noqa: BLE001
        _KN.pop("op", None)
        return {"at": time.time(), "error": f"{type(e).__name__}: {str(e)[:80]}"}


async def net_state() -> dict:
    c = STATE.get("agent") or {}
    return {"v": BOOT, "session": await session_proxy(c.get("pid")),
            "probe": cached_bg("net:probe", 60, _net_probe),
            "keenetic": cached_bg("net:kn", 15, _kn_probe) if (CFG.get("keenetic") or {}).get("password") else None}


# ─────────────── HTTP ───────────────

PAGES = {"/": "git.html", "/agent": "agent.html", "/sessions": "sessions.html", "/settings": "settings.html"}


def page(name: str) -> bytes:
    with open(os.path.join(HERE, "pages", name), "rb") as f:
        return f.read()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        first = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
        method, target = first[0], first[1] if len(first) > 1 else "/"
        u = urllib.parse.urlsplit(target)
        qs = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        m = re.search(rb"(?i)\r\ncontent-length:\s*(\d+)", head)
        body = await reader.readexactly(min(int(m.group(1)), 1_000_000)) if m else b""
        # anything that changes state: POST with our header only. A foreign page in the
        # browser cannot send that without a preflight, and we never pass the preflight.
        mutating = method == "POST" and re.search(rb"(?i)\r\nx-toolbelt:\s*1", head) is not None
        ctype, status, restart = "application/json", b"200 OK", False
        if u.path in PAGES:
            out, ctype = page(PAGES[u.path]), "text/html; charset=utf-8"
        elif u.path == "/state":
            out = json.dumps(await git_state(), ensure_ascii=False).encode()
        elif u.path == "/diff":
            out, ctype = (await file_diff(qs.get("f", ""))).encode(), "text/plain; charset=utf-8"
        elif u.path == "/agent/state":
            out = json.dumps(agent_state(), ensure_ascii=False).encode()
        elif u.path == "/net/state":
            out = json.dumps(await net_state(), ensure_ascii=False).encode()
        elif u.path == "/agent/item":
            i = qs.get("i", "-1")
            out, ctype = agent_item(int(i) if i.lstrip("-").isdigit() else -1).encode(), "text/plain; charset=utf-8"
        elif u.path == "/sessions/state":
            out = json.dumps(await sessions_state(), ensure_ascii=False).encode()
        elif u.path == "/snaps/state":
            out = json.dumps({"v": BOOT, "snaps": snap_list(), "autosave": bool(CFG.get("autosave", True))},
                             ensure_ascii=False).encode()
        elif u.path == "/settings/state":
            out = json.dumps(settings_state(), ensure_ascii=False).encode()
        elif u.path in ("/sessions/open", "/sessions/focus", "/settings/save", "/snaps/save", "/snaps/restore",
                        "/net/copy", "/open-url") and not mutating:
            out, ctype, status = b"forbidden", "text/plain", b"403 Forbidden"
        elif u.path == "/open-url":
            url = qs.get("url", "")
            try:
                parsed = urllib.parse.urlsplit(url)
            except ValueError:
                parsed = None
            ctype = "text/plain; charset=utf-8"
            if (not parsed or parsed.scheme not in ("http", "https") or not parsed.hostname
                    or re.search(r"[\x00-\x20\x7f]", url)):
                out, status = b"invalid URL", b"400 Bad Request"
            else:
                rc, res = await run(["/usr/bin/open", url])
                out = b"ok" if rc == 0 else ("could not open URL: " + res.strip()).encode()
                if rc:
                    status = b"502 Bad Gateway"
        elif u.path == "/net/copy":
            # the line carries the proxy password: it goes straight to the clipboard, never to the page
            line = proxy_export(qs.get("name", ""))
            if line:
                p = await asyncio.create_subprocess_exec("pbcopy", stdin=asyncio.subprocess.PIPE)
                # a trailing newline keeps two pastes in a row from gluing into one command
                await p.communicate((line + "\n").encode())
                res = "copied"
            else:
                res = "unknown proxy"
            out, ctype = res.encode(), "text/plain; charset=utf-8"
        elif u.path == "/sessions/open":
            try:
                res = await open_agent(qs)
            except Exception as ex:  # noqa: BLE001
                res = f"error: {type(ex).__name__}: {ex}"
            print(time.strftime("%H:%M:%S"), "open", qs.get("tool"), qs.get("id") or "(new)", "->", res, flush=True)
            out, ctype = res.encode(), "text/plain; charset=utf-8"
        elif u.path == "/sessions/focus":
            tty = qs.get("tty", "")
            res = await focus_tty(tty) if re.fullmatch(r"ttys?\d+", tty) else "bad tty"
            out, ctype = res.encode(), "text/plain; charset=utf-8"
        elif u.path in ("/snaps/save", "/snaps/restore"):
            try:
                res = await (snap_save(qs.get("name", ""), qs.get("scope") == "window") if u.path == "/snaps/save"
                             else snap_restore(qs.get("name", ""), qs.get("skip") == "1"))
            except Exception as ex:  # noqa: BLE001
                res = f"error: {type(ex).__name__}: {ex}"
            print(time.strftime("%H:%M:%S"), u.path, qs.get("name"), "->", res, flush=True)
            out, ctype = res.encode(), "text/plain; charset=utf-8"
        elif u.path == "/settings/save":
            res = settings_save(body)
            restart = res == "restart"
            out, ctype = res.encode(), "text/plain; charset=utf-8"
        else:
            out, ctype, status = b"not found", "text/plain", b"404 Not Found"
        writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: " + ctype.encode() +
                     b"\r\nCache-Control: no-store\r\nContent-Length: " + str(len(out)).encode() +
                     b"\r\nConnection: close\r\n\r\n" + out)
        await writer.drain()
        if restart:
            # tabs are registered at startup: exit and let launchd start us again
            asyncio.get_running_loop().call_later(0.5, lambda: os._exit(0))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


SHOWN_WINDOWS: set[str] = set()


async def ensure_toolbelt(app, connection):
    """Show the Toolbelt in a window we see for the first time. The "Show Toolbelt" menu item
    acts on the current window and reports a checkmark, so we never toggle needlessly."""
    if not CFG.get("auto_show_toolbelt", True):
        return
    w = app.current_terminal_window
    if not w or w.window_id in SHOWN_WINDOWS:
        return
    SHOWN_WINDOWS.add(w.window_id)
    try:
        st = await iterm2.MainMenu.async_get_menu_item_state(connection, "Show Toolbelt")
        if st.enabled and not st.checked:
            await iterm2.MainMenu.async_select_menu_item(connection, "Show Toolbelt")
    except Exception:  # noqa: BLE001
        pass


TABS = [("git", "Git & PR", "/"), ("agent", "Agent actions", "/agent"),
        ("sessions", "Sessions", "/sessions"), ("settings", "Settings", "/settings")]


async def main(connection):
    port = int(CFG.get("port", 47811))
    server = await asyncio.start_server(handle, "127.0.0.1", port)
    CONN["c"] = connection
    app = await iterm2.async_get_app(connection)
    pre = CFG.get("title_prefix", "")
    for key, title, path in TABS:
        if CFG["tabs"].get(key, True):
            await iterm2.tool.async_register_web_view_tool(
                # identifiers keep the pre-rename prefix: iTerm remembers ticked tabs by them
                connection, pre + title, f"dev.iterm-toolbelt.{key}", False, f"http://127.0.0.1:{port}{path}")
    w = app.current_terminal_window
    if w and w.current_tab and w.current_tab.current_session:
        await refresh_session(app, w.current_tab.current_session.session_id)
    await ensure_toolbelt(app, connection)

    async def poll():
        # the pane directory changes without a focus change too (cd, new worktree, agent start)
        while True:
            await asyncio.sleep(5)
            await refresh_session(app, STATE["session_id"])
    asyncio.create_task(poll())
    asyncio.create_task(autosave_loop(app))

    async with iterm2.FocusMonitor(connection) as mon:
        while True:
            upd = await mon.async_get_next_update()
            sid = None
            if upd.active_session_changed:
                sid = upd.active_session_changed.session_id
            elif upd.selected_tab_changed or upd.window_changed:
                w = app.current_terminal_window
                if w and w.current_tab and w.current_tab.current_session:
                    sid = w.current_tab.current_session.session_id
            if sid:
                await refresh_session(app, sid)
            if upd.window_changed or upd.active_session_changed:
                await ensure_toolbelt(app, connection)
    server.close()


if __name__ == "__main__":
    if "--version" in sys.argv:
        print(VERSION)
        sys.exit(0)
    iterm2.run_forever(main, retry=True)
