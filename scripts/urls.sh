#!/bin/sh
# Print every address the web UI can be reached on, with the right token attached.
set -u

cd "$(dirname "$0")/.." || exit 1
[ -f .env ] || { echo ".env がありません"; exit 1; }

get() { grep "^$1=" .env | head -1 | cut -d= -f2-; }

PORT=$(get WEB_PORT); PORT=${PORT:-8765}
ADMIN=$(get WEB_TOKEN)
QUEUE=$(get WEB_QUEUE_TOKEN)
VIEW=$(get WEB_VIEW_TOKEN)
HOST=$(get WEB_HOST)

q() { [ -n "$1" ] && printf '?t=%s' "$1"; }

echo "待ち受け: ${HOST:-127.0.0.1}:$PORT"
echo

echo "■ このMacから（操作可）"
echo "   http://127.0.0.1:$PORT/$(q "$ADMIN")"
echo

LAN=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
if [ -n "$LAN" ] && [ "$HOST" = "0.0.0.0" ]; then
  echo "■ 同じ Wi-Fi から"
  [ -n "$QUEUE" ] && echo "   登録: http://$LAN:$PORT/$(q "$QUEUE")"
  echo "   閲覧: http://$LAN:$PORT/$(q "$VIEW")"
  echo
fi

# Tailscale reaches the same server over the private tailnet, so the phone can be
# on any network as long as this Mac is awake.
TS=""
for bin in tailscale /Applications/Tailscale.app/Contents/MacOS/Tailscale; do
  command -v "$bin" >/dev/null 2>&1 && { TS=$bin; break; }
  [ -x "$bin" ] && { TS=$bin; break; }
done

if [ -z "$TS" ]; then
  echo "■ 外出先から"
  echo "   Tailscale が未インストール。次を実行:"
  echo "     brew install --cask tailscale"
  exit 0
fi

TSIP=$("$TS" ip -4 2>/dev/null | head -1)
TSNAME=$("$TS" status --json 2>/dev/null \
  | /usr/bin/python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null)

if [ -z "$TSIP" ]; then
  echo "■ 外出先から"
  echo "   Tailscale にログインしていません。アプリを起動してサインインしてください。"
  exit 0
fi

echo "■ 外出先から（Tailscale 経由）"
if [ -n "$TSNAME" ]; then
  [ -n "$QUEUE" ] && echo "   登録: http://$TSNAME:$PORT/$(q "$QUEUE")"
  echo "   閲覧: http://$TSNAME:$PORT/$(q "$VIEW")"
else
  [ -n "$QUEUE" ] && echo "   登録: http://$TSIP:$PORT/$(q "$QUEUE")"
  echo "   閲覧: http://$TSIP:$PORT/$(q "$VIEW")"
fi
echo
echo "   スマホにも Tailscale を入れて同じアカウントでログインすれば、"
echo "   どのネットワークからでも上記 URL が開けます（この Mac が起動中のとき）。"
