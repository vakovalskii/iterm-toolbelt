#!/usr/bin/env bash
# Installs agentbelt: a venv with the iterm2 module, a LaunchAgent (autostart at login)
# and a default config. Safe to re-run: updates the venv and restarts the service.
# Moves an install from the old name (iterm-toolbelt): copies its config, retires its LaunchAgent.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${AGENTBELT_HOME:-$HOME/.config/agentbelt}"
OLD_DIR="$HOME/.config/iterm-toolbelt"
OLD_LABEL="dev.iterm-toolbelt"
VENV="$HOME_DIR/venv"
LABEL="dev.agentbelt"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME_DIR/agentbelt.log"

[ "$(uname)" = "Darwin" ] || { echo "macOS required"; exit 1; }
[ -d "/Applications/iTerm.app" ] || echo "! iTerm2 not found in /Applications, continuing"
# python 3.10+ (the code uses `X | None`); a bare ssh or launchd PATH often finds only Xcode's 3.9
PY=""
for c in python3 /opt/homebrew/bin/python3 /usr/local/bin/python3 python3.14 python3.13 python3.12 python3.11 python3.10; do
  p="$(command -v "$c" 2>/dev/null || true)"
  if [ -n "$p" ] && "$p" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "python 3.10+ required (brew install python)"; exit 1; }

mkdir -p "$HOME_DIR" "$HOME/Library/LaunchAgents"

# coming from iterm-toolbelt: keep the config and the session scan cache, stop the old service
if [ -f "$OLD_DIR/config.json" ] && [ ! -f "$HOME_DIR/config.json" ]; then
  cp -p "$OLD_DIR/config.json" "$HOME_DIR/config.json"
  [ -f "$OLD_DIR/scan-cache.json" ] && cp -p "$OLD_DIR/scan-cache.json" "$HOME_DIR/"
  echo "· config copied from $OLD_DIR (the old directory is left as is)"
fi
OLD_PLIST="$HOME/Library/LaunchAgents/$OLD_LABEL.plist"
if [ -f "$OLD_PLIST" ]; then
  launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
  mv "$OLD_PLIST" "$OLD_PLIST.removed"
  echo "· old service $OLD_LABEL stopped"
fi
if [ ! -x "$VENV/bin/python" ] || ! "$VENV/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
  echo "· venv: $VENV ($("$PY" --version))"
  "$PY" -m venv --clear "$VENV"
fi
"$VENV/bin/pip" install -q --upgrade pip iterm2

if [ ! -f "$HOME_DIR/config.json" ]; then
  cp "$REPO/config.example.json" "$HOME_DIR/config.json"
  chmod 600 "$HOME_DIR/config.json"
  echo "· config: $HOME_DIR/config.json"
fi

cat > "$PLIST.tmp" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$VENV/bin/python</string><string>$REPO/agentbelt.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:$HOME/.local/bin</string>
    <key>AGENTBELT_HOME</key><string>$HOME_DIR</string>
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

Done. Left to do in iTerm2:
  1. Settings → General → Magic → Enable Python API (if not enabled yet).
     On first run iTerm asks to allow the script: allow it.
  2. View → Toolbelt → tick "◆ Git & PR", "◆ Agent actions", "◆ Sessions".
     Show/hide the Toolbelt: ⌘⇧B.
  3. The ⚙ button in "◆ Sessions": intro and proxy settings for agents.

Log: $LOG
Restart: launchctl kickstart -k gui/\$(id -u)/$LABEL
Uninstall: $REPO/uninstall.sh
EOF
