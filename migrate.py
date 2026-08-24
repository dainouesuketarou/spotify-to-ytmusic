#!/usr/bin/env python3
"""Migrate Spotify playlists to YouTube Music, a quota-slice per day.

    python migrate.py list
    python migrate.py add 1 3 "Drive"
    python migrate.py run
    python migrate.py status
    python migrate.py export
"""

import argparse
import csv
import re
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import db
import matching
import runner
import spotify_client as sp_api
import sync as sync_mod
import youtube_client as yt_api

DAILY_BUDGET = runner.DAILY_BUDGET
PLAYLIST_URL = re.compile(r"(?:playlist[/:])([A-Za-z0-9]{22})")


# --------------------------------------------------------------------------- list

def cmd_list(args):
    sp = sp_api.client()
    items = sp_api.playlists(sp)
    queued = {r["spotify_id"] for r in db.connect().execute("SELECT spotify_id FROM playlists")}

    print(f"{'#':>3}  {'曲数':>5}  プレイリスト")
    print("-" * 60)
    skipped = 0
    for i, p in enumerate(items, 1):
        if not p["owned"]:
            skipped += 1
            continue
        mark = " ✓" if p["id"] in queued else ""
        print(f"{i:>3}  {p['total']:>5}  {p['name']}{mark}")

    liked = sp.current_user_saved_tracks(limit=1)["total"]
    print(f"{'--':>3}  {liked:>5}  Liked Songs  (--liked で追加)")

    if skipped:
        print(f"\n他人が作成したプレイリスト {skipped} 件は非表示。")
        print("開発モードの Spotify アプリは自分のプレイリストしか中身を読めない。")
    print("\n✓ = 登録済み。`python migrate.py add <番号|名前|URL>` で追加。")


# --------------------------------------------------------------------------- add

def _resolve(targets, items, liked):
    """Map user tokens (index / name / URL) onto Spotify playlist ids."""
    by_index = {str(i): p["id"] for i, p in enumerate(items, 1)}
    by_name = {p["name"].lower(): p["id"] for p in items}
    ids, unknown = [], []

    for t in targets:
        m = PLAYLIST_URL.search(t)
        if m:
            ids.append(m.group(1))
        elif t in by_index:
            ids.append(by_index[t])
        elif t.lower() in by_name:
            ids.append(by_name[t.lower()])
        else:
            unknown.append(t)

    if liked:
        ids.append(sp_api.LIKED_ID)
    return ids, unknown


def cmd_add(args):
    sp = sp_api.client()
    items = sp_api.playlists(sp)

    if args.all:
        ids = [p["id"] for p in items if p["owned"]]
        unknown = []
    else:
        ids, unknown = _resolve(args.targets, items, args.liked)

    if unknown:
        sys.exit(f"見つからない指定: {', '.join(unknown)}")

    # Development-mode apps get 403 on playlists owned by someone else.
    not_owned = {p["id"]: p["name"] for p in items if not p["owned"]}
    blocked = [not_owned[i] for i in ids if i in not_owned]
    if blocked:
        for name in blocked:
            print(f"  - スキップ: {name}（他人のプレイリストは読み取り不可）")
        ids = [i for i in ids if i not in not_owned]
    if not ids:
        sys.exit("登録できるプレイリストがない。")
    if not ids:
        sys.exit("追加するプレイリストがない。`list` で確認するか --all / --liked を使う。")

    conn = db.connect()
    total_tracks = 0
    for pid in dict.fromkeys(ids):
        result = sync_mod.sync_playlist(conn, sp, pid, force=True)
        total_tracks += result["total"]
        print(f"  + {result['name']}  ({result['total']} 曲)")

    per_day = runner.TRACKS_PER_DAY
    days = -(-total_tracks // per_day) if total_tracks else 0
    print(f"\n{len(set(ids))} プレイリスト / {total_tracks} 曲を登録。推定 {days} 日 (約 {per_day} 曲/日)。")
    print("`python migrate.py run` で開始。")


# --------------------------------------------------------------------------- run

ICONS = {"added": "✓", "unmatched": "?", "failed": "×"}


def cmd_run(args):
    def emit(kind, message):
        if kind == "playlist":
            print(f"\n▶ {message}")
        elif kind in ICONS:
            print(f"   {ICONS[kind]}  {message}")
        else:
            print(f"\n{message}")

    try:
        result = runner.run_once(budget=args.budget, on_event=emit, limit=args.limit)
    except runner.AlreadyRunning as e:
        sys.exit(str(e))

    print(f"\n本日 {result['added']} 曲を追加"
          f"（未マッチ {result['unmatched']} / 失敗 {result['failed']}）。"
          f" 消費 {result['units_used_today']}/{result['budget']} units。")
    if result["remaining"]:
        print(f"残り {result['remaining']} 曲。太平洋時間0時のリセット後に再実行。")
    else:
        print("すべて完了。`python migrate.py export` で未マッチ一覧を確認。")


# --------------------------------------------------------------------------- sync

def cmd_sync(args):
    sp = sp_api.client()
    conn = db.connect()
    rows = sync_mod.queued_playlists(conn)
    if not rows:
        sys.exit("登録済みプレイリストなし。")
    for row in rows:
        r = sync_mod.sync_playlist(conn, sp, row["spotify_id"], force=args.force)
        if not r["changed"]:
            print(f"  =  {r['name']}  変更なし")
        else:
            print(f"  ↻  {r['name']}  +{r['added']} 追加 / -{r['removed']} 削除"
                  f" / {r['restored']} 復活  (計 {r['total']} 曲)")


# --------------------------------------------------------------------------- status

def _bar(done, total, width=20):
    filled = int(width * done / total) if total else 0
    return "█" * filled + "░" * (width - filled)


def cmd_status(args):
    conn = db.connect()
    rows = conn.execute("SELECT * FROM playlists ORDER BY added_at").fetchall()
    if not rows:
        print("登録済みプレイリストなし。`add` で追加。")
        return

    for pl in rows:
        counts = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM tracks WHERE playlist_id = ? GROUP BY status",
                (pl["spotify_id"],),
            ).fetchall()
        )
        added = counts.get(db.ADDED, 0)
        unmatched = counts.get(db.UNMATCHED, 0)
        failed = counts.get(db.FAILED, 0)
        total = pl["total"]
        flag = " ✓" if added + unmatched >= total and total else ""
        extra = []
        if unmatched:
            extra.append(f"未マッチ {unmatched}")
        if failed:
            extra.append(f"失敗 {failed}")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        print(f"{pl['name'][:28]:<28} [{_bar(added, total)}] {added:>4}/{total:<4}{flag}{suffix}")

    used = db.quota_used(conn)
    print(f"\n本日のクォータ: {used}/{DAILY_BUDGET} units  ({db.today()} 太平洋時間)")


# --------------------------------------------------------------------------- export

def cmd_export(args):
    conn = db.connect()
    rows = conn.execute(
        "SELECT p.name AS playlist, t.title, t.artists, t.album, t.status, t.note "
        "FROM tracks t JOIN playlists p ON p.spotify_id = t.playlist_id "
        "WHERE t.status IN (?, ?) ORDER BY p.name, t.position",
        (db.UNMATCHED, db.FAILED),
    ).fetchall()
    if not rows:
        print("未マッチ・失敗なし。")
        return
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["playlist", "title", "artists", "album", "status", "note"])
        writer.writerows([tuple(r) for r in rows])
    print(f"{len(rows)} 件を {args.out} に出力。")


# --------------------------------------------------------------------------- retry

def cmd_retry(args):
    conn = db.connect()
    n = conn.execute(
        "UPDATE tracks SET status = ?, yt_video_id = NULL, note = NULL WHERE status IN (?, ?)",
        (db.PENDING, db.UNMATCHED, db.FAILED),
    ).rowcount
    conn.commit()
    cost = n * (yt_api.COST_SEARCH + yt_api.COST_VIDEOS + yt_api.COST_ITEM_INSERT)
    print(f"{n} 曲を再試行対象に戻した。再検索に約 {cost} units 必要 ({cost / DAILY_BUDGET:.1f} 日分)。")


# --------------------------------------------------------------------------- inspect

def cmd_inspect(args):
    conn = db.connect()
    row = conn.execute(
        "SELECT * FROM tracks WHERE title LIKE ? ORDER BY position LIMIT 1",
        (f"%{args.title}%",),
    ).fetchone()
    if not row:
        sys.exit(f"'{args.title}' に一致する曲が見つからない。")

    track = {"title": row["title"], "artists": row["artists"], "duration_ms": row["duration_ms"]}
    query = matching.query_for(track)
    print(f"曲   : {track['title']} — {track['artists']}  ({track['duration_ms'] / 1000:.0f}秒)")
    print(f"検索 : {query}\n")

    yt = yt_api.service()
    candidates = yt_api.search(yt, query, max_results=args.max_results)
    db.spend(conn, yt_api.COST_SEARCH)
    if candidates:
        yt_api.attach_durations(yt, candidates)
        db.spend(conn, yt_api.COST_VIDEOS)

    if not candidates:
        print("候補なし。")
        return

    for points, d, c in matching.rank(candidates, track):
        diff = "-" if d["duration_diff"] is None else f"{d['duration_diff']:+.0f}s"
        flags = "".join([
            "T" if d["topic"] else "-",
            "A" if d["topic_artist"] else "-",
            "C" if d["title_contains"] else "-",
            "!" if d["penalised"] else "-",
        ])
        mark = "✓" if matching._acceptable(points, d) else " "
        print(f"{mark} {points:>6.0f}  [{flags}] sim={d['title_sim']:.2f} {diff:>6}  "
              f"{c['title'][:40]}  |  {c['channel']}")

    print("\nT=Topicチャンネル A=アーティスト一致 C=タイトル包含 !=減点語")
    print(f"閾値 {matching.ACCEPT_THRESHOLD} / 消費 {db.quota_used(conn)}/{DAILY_BUDGET} units")


# --------------------------------------------------------------------------- cli

def main():
    parser = argparse.ArgumentParser(description="Spotify → YouTube Music playlist migrator")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="Spotify のプレイリスト一覧").set_defaults(func=cmd_list)

    p_add = sub.add_parser("add", help="移行キューに登録")
    p_add.add_argument("targets", nargs="*", help="番号 / プレイリスト名 / Spotify URL")
    p_add.add_argument("--all", action="store_true", help="全プレイリスト")
    p_add.add_argument("--liked", action="store_true", help="Liked Songs も含める")
    p_add.set_defaults(func=cmd_add)

    p_run = sub.add_parser("run", help="クォータの許す範囲で移行を進める")
    p_run.add_argument("--budget", type=int, default=DAILY_BUDGET, help="1日に使う units 上限")
    p_run.add_argument("--limit", type=int, default=None, help="追加する曲数の上限")
    p_run.set_defaults(func=cmd_run)

    p_sync = sub.add_parser("sync", help="Spotify 側の変更を取り込む")
    p_sync.add_argument("--force", action="store_true", help="snapshot が同じでも取り込む")
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser("status", help="進捗表示").set_defaults(func=cmd_status)

    sub.add_parser("retry", help="未マッチ・失敗した曲を再試行対象に戻す").set_defaults(func=cmd_retry)

    p_ins = sub.add_parser("inspect", help="1 曲の検索候補とスコア内訳を表示")
    p_ins.add_argument("title", help="曲名の一部")
    p_ins.add_argument("--max-results", type=int, default=5)
    p_ins.set_defaults(func=cmd_inspect)

    p_exp = sub.add_parser("export", help="未マッチ曲を CSV 出力")
    p_exp.add_argument("--out", default="unmatched.csv")
    p_exp.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
