#!/bin/sh
set -eu
LABEL=com.spotify-to-ytmusic.daily
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "自動実行を解除した。"
