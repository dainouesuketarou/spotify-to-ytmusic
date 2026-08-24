#!/bin/sh
# Keep the web UI running so the phone can reach it without anyone opening a terminal.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LABEL=com.spotify-to-ytmusic.web
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$ROOT/.venv/bin/python</string>
    <string>$ROOT/web.py</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$ROOT/web-server.log</string>
  <key>StandardErrorPath</key><string>$ROOT/web-server.log</string>
  <key>ProcessType</key><string>Background</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true

# bootout returns before the job is fully gone, so bootstrap can lose the race.
i=0
while ! launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -ge 10 ] && { echo "launchctl bootstrap に失敗しました"; exit 1; }
  sleep 1
done

echo "登録した: $PLIST"
echo "Mac 起動時に自動で立ち上がり、落ちても再起動します。"
echo "ログ: $ROOT/web-server.log"
