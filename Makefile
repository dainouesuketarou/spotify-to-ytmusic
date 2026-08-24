PY := .venv/bin/python
PL ?=
T ?=

.DEFAULT_GOAL := help

.PHONY: help setup web list add liked all go run sync status retry inspect export test \
        install-agent uninstall-agent install-web uninstall-web agent-status logs reauth-yt clean-token token urls

help:
	@echo "使い方:"
	@echo "  make web                  ブラウザ UI を起動"
	@echo "  make urls                 アクセス用 URL を表示（LAN / Tailscale）"
	@echo "  make install-agent        毎日の自動実行を登録"
	@echo "  make install-web          Web サーバーを常駐させる"
	@echo "  make uninstall-agent      自動実行を解除"
	@echo "  make logs                 自動実行のログを追尾"
	@echo ""
	@echo "  make list                 Spotify のプレイリスト一覧"
	@echo "  make add PL=\"7 15\"        指定したプレイリストをキューに登録"
	@echo "  make liked                Liked Songs をキューに登録"
	@echo "  make all                  自分の全プレイリストをキューに登録"
	@echo "  make run                  クォータの許す範囲で移行を進める"
	@echo "  make go PL=\"7\"            登録して、そのまま今日の分を流す"
	@echo "  make sync                 Spotify 側の変更を取り込む"
	@echo "  make status               進捗表示"
	@echo "  make retry                未マッチ曲を再試行対象に戻す"
	@echo "  make inspect T=\"曲名\"      候補とスコア内訳を表示"
	@echo "  make export               未マッチ曲を unmatched.csv に出力"
	@echo "  make test                 マッチングのテスト"
	@echo "  make reauth-yt            YouTube を再認可（7 日ごとに必要）"
	@echo ""
	@echo "1 日に移行できるのは約 66 曲（YouTube API 無料枠）。"
	@echo "残りは翌日 make run を再実行すれば続きから進む。"
	@echo "OAuth アプリはテストモードのため、認可は 7 日で切れる。"
	@echo "invalid_grant で落ちたら make reauth-yt して make run。"

setup:
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt
	@echo "完了。.env に認証情報を記入して make list"

web:
	@$(PY) web.py

urls:
	@sh scripts/urls.sh

token:
	@$(PY) -c "import secrets; print(secrets.token_urlsafe(32))"

install-agent:
	@sh scripts/install-agent.sh

install-web:
	@sh scripts/install-web-agent.sh

uninstall-web:
	@sh scripts/uninstall-web-agent.sh

uninstall-agent:
	@sh scripts/uninstall-agent.sh

agent-status:
	@launchctl list | grep spotify-to-ytmusic || echo "未登録"

logs:
	@touch run.log && tail -f run.log

sync:
	@$(PY) migrate.py sync

list:
	@$(PY) migrate.py list

add:
	@test -n "$(PL)" || (echo 'PL を指定する: make add PL="7 15"'; exit 1)
	@$(PY) migrate.py add $(PL)

liked:
	@$(PY) migrate.py add --liked

all:
	@$(PY) migrate.py add --all

run:
	@$(PY) migrate.py run

go: add run

status:
	@$(PY) migrate.py status

retry:
	@$(PY) migrate.py retry

inspect:
	@test -n "$(T)" || (echo 'T を指定する: make inspect T="恋"'; exit 1)
	@$(PY) migrate.py inspect "$(T)"

export:
	@$(PY) migrate.py export

test:
	@$(PY) test_matching.py

reauth-yt:
	rm -f .youtube_token.json
	@echo "YouTube のトークンを削除。次の make run でブラウザ認可。"

clean-token:
	rm -f .spotify_token.json .youtube_token.json
	@echo "認証トークンを削除。次回実行時に再認可。"
