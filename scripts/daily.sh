#!/bin/sh
# Daily unattended step: pull in Spotify changes, then migrate until the quota runs out.
# Invoked by the LaunchAgent; safe to run by hand.
set -u

cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python

printf '\n===== %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')"

# A Spotify hiccup should not stop the migration itself.
"$PY" migrate.py sync || echo "sync でエラー（移行は続行）"
"$PY" migrate.py run
status=$?

if [ $status -ne 0 ]; then
  echo "run が異常終了 (exit $status)"
  echo "認可切れなら: make reauth-yt して make run"
fi
exit $status
