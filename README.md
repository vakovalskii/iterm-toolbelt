# iterm-toolbelt

Вкладки для боковой панели **Toolbelt** в iTerm2, сделанные под работу с ИИ-агентами (Claude Code, Codex). Всё смотрит на активную панель терминала: переключился на другую вкладку iTerm — панель показывает уже её.

[English below](#english)

| вкладка | что показывает |
|---|---|
| **◆ Git и PR** | ветка, отставание от `origin/main` и upstream, изменённые файлы с диффом по клику, PR текущей ветки и CI (`gh`), мои открытые PR, ворктри, свои проверки репо |
| **◆ Агент: действия** | живая лента того, что делает Claude Code в этой панели: bash-команды, правки файлов с диффом, чтение, поиск, агенты, веб, MCP. Упавшие вызовы помечены, пароли и токены в командах скрыты |
| **◆ Сессии** | Claude Code и Codex: выбор агента → проекты плитками → сессии проекта с кнопкой «Восстановить» (новое окно или вкладка iTerm), «＋ новая» сессия в проекте, список запущенных агентов с переходом в их окно, **снимки окон** |
| ⚙ настройки | открываются из вкладки «Сессии»: знакомство, префикс (прокси) и флаги для запуска агентов, где открывать сессии, какие вкладки включены, автопоказ Toolbelt, свои проверки репо |

## Установка

```bash
git clone https://github.com/vakovalskii/iterm-toolbelt ~/iterm-toolbelt
~/iterm-toolbelt/install.sh
```

Дальше в iTerm2:

1. **Settings → General → Magic → Enable Python API.** При первом запуске iTerm спросит разрешение для скрипта.
2. **View → Toolbelt** и отметить вкладки с «◆». Показать или скрыть Toolbelt: ⌘⇧B.
3. Во вкладке **◆ Сессии** нажать **⚙**: там короткое знакомство и настройка запуска агентов (например прокси).

Нужны macOS, iTerm2 3.3+ (проверено на 3.7), python3. Для PR и CI нужен залогиненный [`gh`](https://cli.github.com/).

`install.sh` создаёт venv в `~/.config/iterm-toolbelt/venv`, конфиг `~/.config/iterm-toolbelt/config.json` и LaunchAgent `dev.iterm-toolbelt`. Сервис стартует при входе в систему и перезапускается при падении. Обновление: `git pull && ./install.sh`. Удаление: `./uninstall.sh`.

## Как устроено

- Один процесс на Python: маленький HTTP-сервер на `127.0.0.1` отдаёт страницы из `pages/`, iTerm2 показывает их во вкладках Toolbelt (`iterm2.tool.async_register_web_view_tool`). Через Python API iTerm2 же узнаём активную панель (`FocusMonitor`), её папку и tty, открываем окна для восстановления сессий.
- **Агент: действия.** Процесс `claude` на tty панели → `~/.claude/sessions/<pid>.json` (там id сессии) → транскрипт `~/.claude/projects/<папка>/<id>.jsonl`, который дочитывается с прошлого места.
- **Сессии.** Сканируются `~/.claude/projects/*/*.jsonl` и `~/.codex/sessions/*/*/*/*.jsonl`, из каждого файла разбираются только нужные строки в первых 512 КБ. Результат кэшируется на диск: первый проход на паре тысяч сессий занимает десятки секунд в фоне, дальше читаются только изменённые файлы.
- В простое процесс занимает около 90 МБ памяти и 0–3% CPU.
- Всё, что что-то запускает (`/sessions/open`, `/sessions/focus`, `/settings/save`), принимает только POST с заголовком `X-Toolbelt: 1`: чужая страница в браузере такой запрос послать не может.

## Снимки окон (совместимо с TermDeck)

Экран «снимки» во вкладке «Сессии»: «Сохранить» снимает все окна iTerm (вкладки, сплиты, имена вкладок, папки, сессии агентов), «Восстановить» поднимает снимок в новом окне: вкладки, сплиты, `cd` и `claude --resume` в каждой панели.

- Снимки лежат в `~/.config/itermsnap/snaps/` в формате [TermDeck](https://github.com/vakovalskii/termdeck), так что приложение TermDeck и тулбелт видят одни и те же снимки.
- id сессии берётся точно по процессу `claude` в панели (`~/.claude/sessions/<pid>.json`), а не угадывается по самому свежему файлу в папке.
- Автосохранение раз в 5 минут (`"autosave": true`): только если что-то поменялось и есть хоть один агент; хранятся последние 20 `авто-*`. Если разом закрылись все окна, последний хороший снимок не затрётся пустым.
- Если есть динамический профиль iTerm `TermDeck` (запрет смены заголовка), окна поднимаются в нём, и имена вкладок держатся.

## Конфиг

`~/.config/iterm-toolbelt/config.json` (права 600, в нём может лежать прокси с паролем). Всё правится на странице ⚙ во вкладке «Сессии», руками тоже можно, пример в `config.example.json`.

```json
{
  "agents": {
    "claude": {"prefix": "HTTPS_PROXY='http://user:pass@host:port'", "flags": "", "skip_permissions": true},
    "codex":  {"prefix": "", "flags": ""}
  },
  "open_in": "window",
  "repo_checks": [
    {"name": "Контейнеры", "file": "docker-compose.yml", "cmd": "docker compose ps --format '{{.Name}} {{.State}}'", "max_lines": 6}
  ]
}
```

`repo_checks` — свои строки во вкладке «Git и PR»: если в корне репо есть `file`, выполняется `cmd`, код 0 рисуется зелёным, остальные красным.

## Что уже есть в самом iTerm2

В iTerm2 3.7 появилась своя интеграция с Claude Code: вкладка Session Status, окно Cockpit, Workgroups с диффом и ревью, в 3.7.4 beta ещё и Codex. iterm-toolbelt её не заменяет, а дополняет: лента действий агента из транскрипта, браузер и восстановление сессий Claude Code и Codex, git/PR активной панели.

## English

Toolbelt tabs for iTerm2 built for working with AI coding agents (Claude Code, Codex). Everything follows the active pane.

- **Git & PR** — branch, ahead/behind, changed files with diffs, PR + CI via `gh`, your open PRs, worktrees, custom repo checks.
- **Agent: actions** — live feed of what Claude Code does in the active pane (commands, edits with diffs, reads, searches, MCP), secrets masked.
- **Sessions** — Claude Code and Codex sessions: pick an agent → project tiles → resume in a new iTerm window/tab, start a new session, list of running agents with "jump to its tab".
- **Settings** (⚙ in the Sessions tab) — onboarding, command prefix (e.g. proxy) and flags for agents, tabs on/off, custom repo checks.

Install: `git clone https://github.com/vakovalskii/iterm-toolbelt ~/iterm-toolbelt && ~/iterm-toolbelt/install.sh`, then enable the Python API in iTerm2 (Settings → General → Magic) and tick the «◆» tabs in View → Toolbelt (the Toolbelt opens itself in new windows). UI is in Russian for now.

## Лицензия

MIT
