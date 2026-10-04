#!/usr/bin/env bash
# Останавливает и убирает автостарт. Конфиг и venv не трогает: путь печатается в конце.
set -euo pipefail
LABEL="dev.iterm-toolbelt"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ -f "$PLIST" ]; then
  mv "$PLIST" "$PLIST.removed"
  echo "автостарт убран ($PLIST.removed)"
fi
echo "конфиг и venv остались в ${ITERM_TOOLBELT_HOME:-$HOME/.config/iterm-toolbelt}, удали руками, если не нужны"
