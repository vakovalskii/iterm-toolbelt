#!/usr/bin/env python3
"""iterm-toolbelt: вкладки Toolbelt iTerm2 для работы с ИИ-агентами.

Вкладки (View → Toolbelt):
  ◆ Git и PR         ветка, отставание, изменённые файлы с диффом, PR и CI, ворктри
  ◆ Агент: действия  что Claude Code делает в активной панели: команды, правки, чтение
  ◆ Сессии           сессии Claude Code и Codex: проекты, восстановление, активные агенты
  Настройки (⚙ во вкладке «Сессии»): прокси и флаги агентов, вкладки, проверки репо

Страницы отдаёт HTTP-сервер на 127.0.0.1 (порт из конфига), iTerm2 показывает их во
вкладках Toolbelt. Связь с iTerm2 (активная панель, новые окна) идёт через его Python API.
Конфиг: ~/.config/iterm-toolbelt/config.json.
"""
import asyncio
import copy
import glob
import json
import os
import re
import shlex
import sys
import time
import urllib.parse

import iterm2

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.expanduser(os.getenv("ITERM_TOOLBELT_HOME", "~/.config/iterm-toolbelt"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
CLAUDE_DIR = os.path.expanduser("~/.claude")
CODEX_DIR = os.path.expanduser("~/.codex")
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
    },
    # Свои проверки во вкладке «Git и PR»: если в корне репо есть file, выполняется cmd.
    # Код 0 рисуется зелёным, остальные красным.
    "repo_checks": [],
    # открывать боковую панель Toolbelt в каждом новом окне iTerm (один раз на окно:
    # если скрыть руками, больше не лезет)
    "auto_show_toolbelt": True,
    # снимок всех окон раз в 5 минут в ~/.config/itermsnap/snaps (формат TermDeck)
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
    os.chmod(tmp, 0o600)  # там может быть прокси с паролем
    os.replace(tmp, CONFIG_FILE)


CFG = load_config()

NOPROXY_ENV = {k: v for k, v in os.environ.items()
               if k.lower() not in ("https_proxy", "http_proxy", "all_proxy")}
NOPROXY_ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + NOPROXY_ENV.get("PATH", "")
NOPROXY_ENV["GH_PROMPT_DISABLED"] = "1"
NOPROXY_ENV["GIT_TERMINAL_PROMPT"] = "0"
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

STATE = {"session_id": None, "session_name": "", "cwd": None, "agent": None}
CONN: dict = {}
CACHE: dict[str, tuple[float, object]] = {}
_RUNNING: set[str] = set()
_FETCHED: dict[str, float] = {}


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
    """Медленное не держит ответ страницы: отдаём что есть, устаревшее обновляем в фоне."""
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


# ─────────────── активная панель iTerm ───────────────

async def session_cwd(session) -> str | None:
    """Папка панели: переменная path (shell integration), иначе cwd процесса на переднем
    плане, иначе cwd шелла."""
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
    """claude, запущенный в этой панели: процесс на её tty, у которого есть
    ~/.claude/sessions/<pid>.json (там sessionId и cwd)."""
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
    STATE["cwd"] = await session_cwd(session)
    STATE["agent"] = await session_agent(session)


# ─────────────── вкладка «Git и PR» ───────────────

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
    # без -uall: неотслеживаемая папка приходит одной строкой, иначе логи инструментов
    # забивают весь список
    _, st = await run(["git", "status", "--porcelain=v1"], root)
    files = [{"st": line[:2], "path": line[3:]} for line in st.splitlines() if len(line) > 3]
    files.sort(key=lambda f: (f["st"] == "??", f["path"]))
    _, stat = await run(["git", "diff", "HEAD", "--shortstat"], root)
    return {"branch": branch, "head": head.strip(), "behind_main": behind_main,
            "ahead_main": ahead_main, "upstream": upstream, "behind_up": behind_up,
            "ahead_up": ahead_up, "files": files[:200], "files_total": len(files),
            "shortstat": stat.strip()}


async def maybe_fetch(root: str):
    """Тихий fetch раз в 5 минут, чтобы «отстаёт от main» не врало."""
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
        return "(нет изменений)"
    if st.startswith("??") and path.endswith("/"):
        _, ls = await run(["git", "ls-files", "--others", "--exclude-standard", "--", path], root)
        names = ls.splitlines()
        return f"неотслеживаемая папка, файлов: {len(names)}\n" + "\n".join(names[:300])
    if st.startswith("??"):
        try:
            with open(os.path.join(root, path), "r", encoding="utf-8", errors="replace") as f:
                body = f.read(200_000)
            return "".join("+" + l + "\n" for l in body.splitlines())
        except OSError as e:
            return f"(не прочитать: {e})"
    _, d = await run(["git", "diff", "HEAD", "--no-color", "--", path], root, timeout=10)
    return d[:400_000] or "(дифф пустой)"


# ─────────────── вкладка «Агент: действия» ───────────────

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
        return "\n".join(["@@ " + g("file_path", "") + " (новый/перезаписан)"] +
                         ["+" + l for l in (g("content") or "").splitlines()[:400]])
    return json.dumps(inp, ensure_ascii=False, indent=1)[:20000]


def read_transcript(path: str) -> dict:
    """Дочитывает jsonl с прошлого места, собирает tool_use и их результат."""
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


# ─────────────── вкладка «Сессии» ───────────────

SCAN: dict[str, tuple[float, dict | None]] = {}
SCAN_FILE = os.path.join(CONFIG_DIR, "scan-cache.json")
SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{4,128}$")


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
    """Первые строки jsonl, не больше 512 КБ. need: разбирать только строки с этими
    байтами (строки Codex бывают по сотне КБ, json.loads на них дорогой)."""
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
    """ai-title/summary Claude Code пишет по ходу сессии: берём последний из хвоста."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            n = f.tell()
            f.seek(max(0, n - 65536))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        if '"ai-title"' in line or '"type":"summary"' in line or '"custom-title"' in line:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("title") or d.get("aiTitle") or d.get("summary") or d.get("customTitle")
            if isinstance(t, str) and t.strip():
                return t.strip()
    return ""


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
    for d in _read_head(path, 200, need=(b'"session_meta"', b'"user_message"', b'"role":"user"')):
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if d.get("type") == "session_meta":
            sid, cwd = p.get("id", ""), p.get("cwd", "")
        elif not first and d.get("type") == "event_msg" and p.get("type") == "user_message":
            first = p.get("message", "")
        elif not first and d.get("type") == "response_item" and p.get("role") == "user":
            t = _first_text(p.get("content"))
            if t and not t.lstrip().startswith(("<environment_context", "<user_instructions", "# AGENTS.md")):
                first = t
        if sid and cwd and first:
            break
    if not sid:
        return None
    return {"id": sid, "tool": "codex", "dir": cwd, "title": _clean_title(first)}


def _scan_all() -> list:
    """stat всех файлов сессий, разбор только новых/изменённых. Кэш лежит на диске:
    после перезапуска заново читаются только изменённые."""
    if not SCAN and os.path.isfile(SCAN_FILE):
        try:
            with open(SCAN_FILE) as f:
                SCAN.update({k: tuple(v) for k, v in json.load(f).items()})
        except (OSError, ValueError):
            pass
    files = [(p, _scan_claude) for p in glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "*.jsonl"))]
    files += [(p, _scan_codex) for p in glob.glob(os.path.join(CODEX_DIR, "sessions", "*", "*", "*", "*.jsonl"))]
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
            changed = True
    if changed:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(SCAN_FILE + ".tmp", "w") as f:
                json.dump(SCAN, f)
            os.replace(SCAN_FILE + ".tmp", SCAN_FILE)
        except OSError:
            pass
    rows.sort(key=lambda r: -r["last"])
    return rows


async def _active_agents() -> list:
    """Живые claude (по ~/.claude/sessions/<pid>.json) и codex (по процессам)."""
    out = []
    for meta in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            with open(meta) as f:
                d = json.load(f)
            os.kill(int(d["pid"]), 0)
        except Exception:  # noqa: BLE001
            continue
        out.append({"tool": "claude", "pid": d["pid"], "id": d.get("sessionId"), "dir": d.get("cwd", ""),
                    "name": d.get("name", ""), "status": d.get("status", ""), "since": d.get("startedAt", 0)})
    _, ps = await run(["ps", "-axo", "pid=,tty=,command="], timeout=5)
    ttys = {}
    for line in ps.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, tty, cmd = parts
        ttys[pid] = tty
        argv0 = os.path.basename(cmd.split()[0]) if cmd.split() else ""
        is_codex = argv0 == "codex" or re.search(r"/codex(\s|$)", cmd.split(" --")[0])
        if is_codex and "app-server" not in cmd and tty not in ("??", "-"):
            _, cw = await run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"], timeout=3)
            d = next((l[1:] for l in cw.splitlines() if l.startswith("n")), "")
            out.append({"tool": "codex", "pid": int(pid), "id": "", "dir": d, "name": "", "status": "", "since": 0})
    for a in out:
        a["tty"] = ttys.get(str(a["pid"]), "")
    return out


async def sessions_state() -> dict:
    rows = cached_bg("scan:sessions", 20, lambda: asyncio.to_thread(_scan_all))
    active = cached_bg("scan:active", 4, _active_agents) or []
    live = {a["id"] for a in active if a.get("id")}
    return {"v": BOOT, "cwd": STATE["cwd"] or "", "session": STATE["session_name"], "ok": rows is not None,
            "rows": [dict(r, live=r["id"] in live) for r in (rows or [])], "active": active,
            "open_in": CFG.get("open_in", "window"),
            "skip": bool(CFG["agents"]["claude"].get("skip_permissions"))}


def agent_command(tool: str, sid: str, skip: bool) -> str | None:
    """sid пустой = новая сессия. Префикс (например HTTPS_PROXY=...) и флаги из конфига."""
    q = shlex.quote(sid) if sid else ""
    a = CFG["agents"].get("claude" if tool in ("claude", "claude-ext") else tool, {})
    if tool in ("claude", "claude-ext"):
        flags = (a.get("flags") or "").replace("--dangerously-skip-permissions", "").strip()
        if skip:
            flags = (flags + " --dangerously-skip-permissions").strip()
        core = f"claude --resume {q}" if sid else "claude"
    elif tool == "codex":
        flags = a.get("flags") or ""
        core = f"codex resume {q}" if sid else "codex"
    elif sid:
        return {"qwen": f"qwen -r {q}", "kilo": f"kilo resume {q}", "opencode": f"opencode -s {q}",
                "cursor": f"cursor-agent --resume {q}"}.get(tool)
    else:
        return None
    return " ".join(x for x in (a.get("prefix") or "", core, flags) if x)


async def open_agent(q: dict) -> str:
    sid, tool, d = q.get("id", ""), q.get("tool", ""), q.get("dir", "")
    if sid and not SAFE_ID.match(sid):
        return "плохой id"
    cmd = agent_command(tool, sid, q.get("skip") == "1")
    if not cmd:
        return f"не умею запускать {tool}"
    if d and not os.path.isdir(d):
        return f"папки нет: {d}"
    conn = CONN.get("c")
    if conn is None:
        return "нет связи с iTerm"
    app = await iterm2.async_get_app(conn)
    cur = app.current_terminal_window
    if q.get("where") == "tab" and cur is not None:
        tab = await cur.async_create_tab()
        win_id, tab_id = cur.window_id, tab.tab_id
    else:
        w = await iterm2.Window.async_create(conn)
        win_id, tab_id = w.window_id, w.current_tab.tab_id
    # новое окно отдаёт сессию не сразу, а первые доли секунды там служебный bash:
    # берём сессию заново по id и ждём шелл пользователя, иначе текст теряется
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
        return "iTerm не отдал новую сессию"
    await asyncio.sleep(0.3)
    await sess.async_send_text((f"cd {shlex.quote(d)} && " if d else "") + cmd + "\n")
    await sess.async_activate(select_tab=True, order_window_front=True)
    return "ok"


async def focus_tty(tty: str) -> str:
    """Перейти в панель iTerm, где крутится агент (по tty)."""
    conn = CONN.get("c")
    if conn is None or not tty:
        return "нет связи с iTerm"
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
    return "это окно не в iTerm"


# ─────────────── снимки окон (формат TermDeck) ───────────────

SNAP_DIR = os.path.expanduser(os.getenv("ITERMSNAP_HOME", "~/.config/itermsnap")) + "/snaps"
SAFE_SNAP = re.compile(r"^[\w .:-]{1,60}$")
AUTO_PREFIX = "авто-"
AUTO_KEEP = 20
_LAST_AUTO = {"sig": None}


async def _pane_info(session) -> dict | None:
    """Папка панели и агент в ней. id сессии claude берём точно по процессу на tty
    (~/.claude/sessions/<pid>.json), а не по самому свежему файлу в папке."""
    agent = await session_agent(session)
    if agent and agent.get("cwd"):
        return {"cwd": agent["cwd"], "session_id": agent.get("sid"), "agent": "claude"}
    cwd = await session_cwd(session)
    if not cwd:
        return None
    return {"cwd": cwd, "session_id": None, "agent": ""}


async def capture_windows(app) -> list:
    """Все окна → [{title, split, panes:[{cwd, session_id, agent}]}], формат TermDeck."""
    tabs = []
    for w in app.terminal_windows:
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
    out = []
    for p in sorted(glob.glob(os.path.join(SNAP_DIR, "*.json")), key=os.path.getmtime, reverse=True):
        try:
            with open(p) as f:
                tabs = json.load(f)
        except (OSError, ValueError):
            continue
        name = os.path.basename(p)[:-5]
        out.append({"name": name, "mtime": int(os.path.getmtime(p) * 1000), "auto": name.startswith((AUTO_PREFIX, "автосохранение")),
                    "tabs": [{"title": t.get("title") or os.path.basename((t.get("panes") or [{}])[0].get("cwd", "")),
                              "panes": len(t.get("panes") or []),
                              "agents": sum(1 for x in t.get("panes") or [] if x.get("session_id"))} for t in tabs]})
    return out


def _write_snap(name: str, tabs: list):
    os.makedirs(SNAP_DIR, exist_ok=True)
    path = os.path.join(SNAP_DIR, name + ".json")
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    with open(path + ".tmp", "w") as f:
        json.dump(tabs, f, ensure_ascii=False, indent=2)
    os.replace(path + ".tmp", path)


async def snap_save(name: str) -> str:
    if not SAFE_SNAP.match(name or ""):
        return "имя: буквы, цифры, пробел, . : - до 60 знаков"
    app = await iterm2.async_get_app(CONN["c"])
    tabs = await capture_windows(app)
    if not tabs:
        return "нечего сохранять: окон с папками нет"
    _write_snap(name, tabs)
    return "ok"


async def autosave_loop(app):
    """Раз в 5 минут снимок всех окон, только если что-то поменялось и есть хоть один
    агент. Хранятся последние AUTO_KEEP: закрытые разом окна не затрут снимок пустым."""
    while True:
        await asyncio.sleep(300)
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
        return "плохое имя"
    try:
        with open(os.path.join(SNAP_DIR, name + ".json")) as f:
            tabs = json.load(f)
    except (OSError, ValueError):
        return "снимка нет"
    conn = CONN["c"]
    app = await iterm2.async_get_app(conn)
    # профиль TermDeck (если есть) не даёт claude перебивать имена вкладок
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


# ─────────────── вкладка «Настройки» ───────────────

def settings_state() -> dict:
    return {"v": BOOT, "version": VERSION, "config": CFG, "config_file": CONFIG_FILE}


def settings_save(body: bytes) -> str:
    try:
        new = json.loads(body or b"{}")
    except ValueError:
        return "не JSON"
    if not isinstance(new, dict):
        return "не объект"
    cfg = load_config()
    restart = False
    for k in ("open_in", "title_prefix", "onboarded", "auto_show_toolbelt", "autosave"):
        if k in new:
            restart |= k == "title_prefix" and new[k] != cfg.get(k)
            cfg[k] = new[k]
    if isinstance(new.get("agents"), dict):
        for name, vals in new["agents"].items():
            if name in cfg["agents"] and isinstance(vals, dict):
                cfg["agents"][name].update({k: v for k, v in vals.items() if k in ("prefix", "flags", "skip_permissions")})
    if isinstance(new.get("tabs"), dict):
        for k, v in new["tabs"].items():
            if k in cfg["tabs"] and bool(v) != cfg["tabs"][k]:
                cfg["tabs"][k] = bool(v)
                restart = True
    if isinstance(new.get("repo_checks"), list):
        cfg["repo_checks"] = [c for c in new["repo_checks"] if isinstance(c, dict) and c.get("cmd")]
    save_config(cfg)
    CFG.clear()
    CFG.update(cfg)
    return "restart" if restart else "ok"


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
        # всё, что меняет состояние: только POST с нашим заголовком. Чужая страница в
        # браузере не может послать такой запрос без preflight, а его мы не пропускаем.
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
        elif u.path == "/agent/item":
            i = qs.get("i", "-1")
            out, ctype = agent_item(int(i) if i.lstrip("-").isdigit() else -1).encode(), "text/plain; charset=utf-8"
        elif u.path == "/sessions/state":
            out = json.dumps(await sessions_state(), ensure_ascii=False).encode()
        elif u.path == "/snaps/state":
            out = json.dumps({"v": BOOT, "snaps": snap_list()}, ensure_ascii=False).encode()
        elif u.path == "/settings/state":
            out = json.dumps(settings_state(), ensure_ascii=False).encode()
        elif u.path in ("/sessions/open", "/sessions/focus", "/settings/save", "/snaps/save", "/snaps/restore") and not mutating:
            out, ctype, status = b"forbidden", "text/plain", b"403 Forbidden"
        elif u.path == "/sessions/open":
            try:
                res = await open_agent(qs)
            except Exception as ex:  # noqa: BLE001
                res = f"ошибка: {type(ex).__name__}: {ex}"
            print(time.strftime("%H:%M:%S"), "open", qs.get("tool"), qs.get("id") or "(new)", "->", res, flush=True)
            out, ctype = res.encode(), "text/plain; charset=utf-8"
        elif u.path == "/sessions/focus":
            tty = qs.get("tty", "")
            res = await focus_tty(tty) if re.fullmatch(r"ttys?\d+", tty) else "плохой tty"
            out, ctype = res.encode(), "text/plain; charset=utf-8"
        elif u.path in ("/snaps/save", "/snaps/restore"):
            try:
                res = await (snap_save(qs.get("name", "")) if u.path == "/snaps/save"
                             else snap_restore(qs.get("name", ""), qs.get("skip") == "1"))
            except Exception as ex:  # noqa: BLE001
                res = f"ошибка: {type(ex).__name__}: {ex}"
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
            # вкладки регистрируются при старте: выходим, launchd поднимет заново
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
    """Показать Toolbelt в окне, которое видим впервые. Меню «Show Toolbelt» работает
    с текущим окном и показывает галочку, так что лишний раз не переключаем."""
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


TABS = [("git", "Git и PR", "/"), ("agent", "Агент: действия", "/agent"),
        ("sessions", "Сессии", "/sessions"), ("settings", "Настройки", "/settings")]


async def main(connection):
    port = int(CFG.get("port", 47811))
    server = await asyncio.start_server(handle, "127.0.0.1", port)
    CONN["c"] = connection
    app = await iterm2.async_get_app(connection)
    pre = CFG.get("title_prefix", "")
    for key, title, path in TABS:
        if CFG["tabs"].get(key, True):
            await iterm2.tool.async_register_web_view_tool(
                connection, pre + title, f"dev.iterm-toolbelt.{key}", False, f"http://127.0.0.1:{port}{path}")
    w = app.current_terminal_window
    if w and w.current_tab and w.current_tab.current_session:
        await refresh_session(app, w.current_tab.current_session.session_id)
    await ensure_toolbelt(app, connection)

    async def poll():
        # папка панели меняется и без смены фокуса (cd, новый ворктри, запуск агента)
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
