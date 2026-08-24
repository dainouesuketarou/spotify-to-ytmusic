#!/bin/sh
set -eu
LABEL=com.spotify-to-ytmusic.web
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Web サーバーの常駐を解除した。"
