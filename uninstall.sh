#!/usr/bin/env bash
# Stops the service and removes autostart. Leaves config and venv alone; their path is printed at the end.
set -euo pipefail
LABEL="dev.iterm-toolbelt"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ -f "$PLIST" ]; then
  mv "$PLIST" "$PLIST.removed"
  echo "autostart removed ($PLIST.removed)"
fi
echo "config and venv are left in ${ITERM_TOOLBELT_HOME:-$HOME/.config/iterm-toolbelt}; delete them by hand if not needed"
