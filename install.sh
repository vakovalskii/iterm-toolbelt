#!/usr/bin/env bash
# Установка iterm-toolbelt: venv с модулем iterm2, LaunchAgent (автостарт при входе),
# конфиг по умолчанию. Повторный запуск безопасен: обновит venv и перезапустит сервис.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${ITERM_TOOLBELT_HOME:-$HOME/.config/iterm-toolbelt}"
VENV="$HOME_DIR/venv"
LABEL="dev.iterm-toolbelt"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME_DIR/toolbelt.log"

[ "$(uname)" = "Darwin" ] || { echo "нужен macOS"; exit 1; }
[ -d "/Applications/iTerm.app" ] || echo "! iTerm2 не найден в /Applications, продолжаю"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || { echo "нужен python3 (brew install python)"; exit 1; }

mkdir -p "$HOME_DIR" "$HOME/Library/LaunchAgents"
if [ ! -x "$VENV/bin/python" ]; then
  echo "· venv: $VENV"
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip iterm2

if [ ! -f "$HOME_DIR/config.json" ]; then
  cp "$REPO/config.example.json" "$HOME_DIR/config.json"
  chmod 600 "$HOME_DIR/config.json"
  echo "· конфиг: $HOME_DIR/config.json"
fi

cat > "$PLIST.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$VENV/bin/python</string><string>$REPO/toolbelt.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin</string>
    <key>ITERM_TOOLBELT_HOME</key><string>$HOME_DIR</string>
    <key>SHELL</key><string>${SHELL:-/bin/zsh}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict></plist>
EOF
mv "$PLIST.tmp" "$PLIST"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl enable "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

cat <<EOF

Готово. Осталось в iTerm2:
  1. Settings → General → Magic → Enable Python API (если ещё не включён).
     При первом запуске iTerm спросит разрешение для скрипта — разреши.
  2. View → Toolbelt → отметь вкладки «◆ Git и PR», «◆ Агент: действия», «◆ Сессии».
     Показать/скрыть Toolbelt: ⌘⇧B.
  3. Во вкладке «◆ Сессии» кнопка ⚙: знакомство и прокси для агентов.

Лог: $LOG
Перезапуск: launchctl kickstart -k gui/\$(id -u)/$LABEL
Удаление: $REPO/uninstall.sh
EOF
