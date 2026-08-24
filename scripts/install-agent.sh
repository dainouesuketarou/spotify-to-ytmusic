#!/bin/sh
# Install (or refresh) the LaunchAgent that runs the migration once a day.
#
# launchd is used instead of cron because it re-fires a missed StartCalendarInterval
# when the Mac wakes up; cron simply skips anything scheduled while asleep.
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LABEL=com.spotify-to-ytmusic.daily
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
HOUR=${HOUR:-17}
MINUTE=${MINUTE:-30}

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>$ROOT/scripts/daily.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>$HOUR</integer>
    <key>Minute</key><integer>$MINUTE</integer>
  </dict>
  <key>StandardOutPath</key><string>$ROOT/run.log</string>
  <key>StandardErrorPath</key><string>$ROOT/run.log</string>
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
echo "毎日 $HOUR:$(printf '%02d' "$MINUTE") に実行。スリープ中に過ぎた場合は起動後に実行。"
echo "ログ: $ROOT/run.log"
