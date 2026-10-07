# iterm-toolbelt

Tabs for the iTerm2 **Toolbelt** sidebar, built for working with AI coding agents (Claude Code, Codex). Everything follows the active terminal pane: switch to another iTerm tab and the sidebar shows that one.

![The Toolbelt next to the terminal: agent actions, window snapshots, git](docs/overview.jpg)

<p><img src="docs/sessions-start.jpg" width="49%" alt="Sessions: pick an agent"> <img src="docs/sessions-active.jpg" width="49%" alt="Sessions: running agents"></p>

| tab | what it shows |
|---|---|
| **◆ Git & PR** | branch, ahead/behind `origin/main` and upstream, changed files with a diff on click, the branch PR and its CI (`gh`), your open PRs, worktrees, custom repo checks |
| **◆ Agent actions** | a live feed of what Claude Code does in this pane: bash commands, file edits with diffs, reads, searches, subagents, web, MCP. Failed calls are marked, passwords and tokens in commands are masked. A **network** block on top shows which proxy this claude runs through and, for every proxy in the config, whether api.anthropic.com and api.openai.com answer through it; optional list of your servers with ping and SSH reachability |
| **◆ Sessions** | Claude Code and Codex: pick an agent → project tiles → project sessions with **Resume** (new iTerm window or tab), **＋ new** session in a project, running agents with a jump to their pane (the focused one is highlighted), **window snapshots** |
| ⚙ settings | opened from the Sessions tab: intro, prefix (e.g. a proxy) and flags for launching agents, where to open sessions, which tabs are on, auto-show of the Toolbelt, custom repo checks |

## Install

```bash
git clone https://github.com/vakovalskii/iterm-toolbelt ~/iterm-toolbelt
~/iterm-toolbelt/install.sh
```

Then in iTerm2:

1. **Settings → General → Magic → Enable Python API.** On first run iTerm asks to allow the script.
2. **View → Toolbelt** and tick the tabs starting with «◆». Show or hide the Toolbelt: ⌘⇧B. New windows open the Toolbelt by themselves.
3. In **◆ Sessions** press **⚙**: a short intro and agent launch settings (e.g. a proxy).

Requires macOS, iTerm2 3.3+ (tested on 3.7) and python3. PR and CI info needs a logged-in [`gh`](https://cli.github.com/).

`install.sh` creates a venv in `~/.config/iterm-toolbelt/venv`, the config `~/.config/iterm-toolbelt/config.json` and the LaunchAgent `dev.iterm-toolbelt`. The service starts at login and restarts if it crashes. Update: `git pull && ./install.sh`. Uninstall: `./uninstall.sh`.

## How it works

- One Python process: a small HTTP server on `127.0.0.1` serves the pages from `pages/`, and iTerm2 shows them as Toolbelt tabs (`iterm2.tool.async_register_web_view_tool`). The same iTerm2 Python API tells us the active pane (`FocusMonitor`), its directory and tty, and opens windows to resume sessions.
- **Agent actions.** The `claude` process on the pane's tty → `~/.claude/sessions/<pid>.json` (holds the session id) → the transcript `~/.claude/projects/<dir>/<id>.jsonl`, read incrementally from where it stopped.
- **Network.** `HTTPS_PROXY` is read from the environment of the running `claude` (`ps eww`). Every 60 s each proxy from `proxies` in the config is probed with curl (the proxy URL goes through stdin, never argv), servers from `servers` get a ping and a TCP connect with an SSH banner read. Everything runs from your Mac, so it shows what works from where you are now. Both lists stay in the local config only.
- **Sessions.** Scans `~/.claude/projects/*/*.jsonl` and `~/.codex/sessions/*/*/*/*.jsonl`, parsing only the needed lines within the first 512 KB of each file. Results are cached on disk: the first pass over a couple thousand sessions takes tens of seconds in the background, after that only changed files are read.
- Idle, the process uses about 90 MB of memory and 0–3% CPU.
- Anything that launches something (`/sessions/open`, `/sessions/focus`, `/snaps/*`, `/settings/save`) accepts only POST with the `X-Toolbelt: 1` header, which a foreign web page cannot send.

## Window snapshots (TermDeck compatible)

The "snapshots" screen in Sessions: **Save** captures the current window or all iTerm windows (tabs, splits, tab names, directories, agent sessions), **Restore** brings a snapshot back in a new window: tabs, splits, `cd` and `claude --resume` in every pane.

- Snapshots live in `~/.config/itermsnap/snaps/` in the [TermDeck](https://github.com/vakovalskii/termdeck) format, so the TermDeck app and the toolbelt see the same snapshots.
- The session id is taken from the `claude` process in the pane (`~/.claude/sessions/<pid>.json`), not guessed from the newest file in the directory.
- Autosave every 5 minutes (`"autosave": true`): only when something changed and at least one agent runs; the last 20 `auto-*` are kept. If all windows close at once, the last good snapshot is not overwritten by an empty one.
- If an iTerm dynamic profile named `TermDeck` exists (title changes disabled), windows are restored with it and tab names stick.

## Config

`~/.config/iterm-toolbelt/config.json` (mode 600, it may hold a proxy with a password). Everything is editable on the ⚙ page in Sessions, or by hand, see `config.example.json`.

```json
{
  "agents": {
    "claude": {"prefix": "HTTPS_PROXY='http://user:pass@host:port'", "flags": "", "skip_permissions": true},
    "codex":  {"prefix": "", "flags": ""}
  },
  "open_in": "window",
  "repo_checks": [
    {"name": "Containers", "file": "docker-compose.yml", "cmd": "docker compose ps --format '{{.Name}} {{.State}}'", "max_lines": 6}
  ]
}
```

`repo_checks` are your own lines in Git & PR: if `file` exists in the repo root, `cmd` runs; exit code 0 is shown green, anything else red.

## What iTerm2 already has

iTerm2 3.7 ships its own Claude Code integration: the Session Status tool, the Cockpit window, Workgroups with diff and review, and Codex support in 3.7.4 beta. iterm-toolbelt does not replace it but complements it: an agent action feed from the transcript, a browser for resuming Claude Code and Codex sessions, window snapshots with agent resume, and git/PR for the active pane.

## License

MIT
