#!/bin/sh
# Apply local archon workflow edits and restart the board poller after a
# deploy (repo pull with poller changes, or an archon reinstall).
# Run from anywhere in the repo:
#   automation/deploy.sh
set -e
cd "$(dirname "$0")/.."
python3 automation/apply_workflow_edits.py
launchctl kickstart -k "gui/$(id -u)/com.bradley-mankoff.news-board-poller"
echo "board poller restarted"
