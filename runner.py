"""The migration loop, shared by the CLI, the web UI and the scheduled agent.

Only one run may be in flight at a time: two concurrent runs would double-spend
the daily quota and race each other's writes.
"""

import fcntl
import time
from datetime import datetime, timezone
from pathlib import Path

import db
import matching
import youtube_client as yt_api

DAILY_BUDGET = 10_000
PACE_SECONDS = 0.2
LOCK_PATH = Path(__file__).parent / ".run.lock"

# One track costs a search, a duration lookup and an insert.
UNITS_PER_TRACK = yt_api.COST_SEARCH + yt_api.COST_VIDEOS + yt_api.COST_ITEM_INSERT
TRACKS_PER_DAY = DAILY_BUDGET // UNITS_PER_TRACK


class AlreadyRunning(Exception):
    pass


class _Lock:
    def __enter__(self):
        self._fh = LOCK_PATH.open("w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            self._fh.close()
            raise AlreadyRunning("別の移行処理が実行中") from e
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def is_running():
    try:
        with _Lock():
            return False
    except AlreadyRunning:
        return True


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def remaining_open(conn):
    return conn.execute(
        f"SELECT COUNT(*) c FROM tracks WHERE status IN ({','.join('?' * len(db.OPEN))})",
        db.OPEN,
    ).fetchone()["c"]


def run_once(budget=DAILY_BUDGET, on_event=lambda *_: None, limit=None):
    """Migrate as much as the quota allows. Returns a summary dict."""
    with _Lock():
        conn = db.connect()
        started_units = db.quota_used(conn)
        stats = {"added": 0, "unmatched": 0, "failed": 0, "outcome": "完了"}

        if started_units >= budget:
            on_event("quota", f"本日のクォータ消化済み ({started_units}/{budget})")
            stats["outcome"] = "クォータ上限"
            return _finish(conn, stats, started_units, budget)

        run_id = conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (_now(),)
        ).lastrowid
        conn.commit()

        try:
            _migrate(conn, budget, on_event, stats, limit)
        except yt_api.QuotaExceeded:
            stats["outcome"] = "クォータ上限"
            on_event("quota", "クォータ上限に到達")
        except Exception as e:  # surfaced to the caller, run still recorded
            stats["outcome"] = f"エラー: {e}"
            on_event("error", str(e))

        result = _finish(conn, stats, started_units, budget)
        conn.execute(
            "UPDATE runs SET finished_at = ?, added = ?, unmatched = ?, failed = ?, "
            "units = ?, outcome = ? WHERE id = ?",
            (_now(), stats["added"], stats["unmatched"], stats["failed"],
             result["units_spent"], stats["outcome"], run_id),
        )
        conn.commit()
        return result


def _finish(conn, stats, started_units, budget):
    used = db.quota_used(conn)
    return {
        **stats,
        "units_spent": used - started_units,
        "units_used_today": used,
        "budget": budget,
        "remaining": remaining_open(conn),
    }


def _afford(conn, budget, cost):
    return db.quota_used(conn) + cost <= budget


def _migrate(conn, budget, on_event, stats, limit):
    yt = yt_api.service()
    placeholders = ",".join("?" * len(db.OPEN))
    playlists = conn.execute(
        f"SELECT * FROM playlists WHERE paused = 0 AND spotify_id IN "
        f"(SELECT DISTINCT playlist_id FROM tracks WHERE status IN ({placeholders})) "
        f"ORDER BY added_at",
        db.OPEN,
    ).fetchall()

    for pl in playlists:
        yt_id = pl["yt_id"]
        if not yt_id:
            if not _afford(conn, budget, yt_api.COST_PLAYLIST_INSERT):
                raise yt_api.QuotaExceeded("budget reached")
            yt_id = yt_api.create_playlist(
                yt, pl["name"], f"Migrated from Spotify playlist {pl['spotify_id']}"
            )
            db.spend(conn, yt_api.COST_PLAYLIST_INSERT)
            conn.execute(
                "UPDATE playlists SET yt_id = ? WHERE spotify_id = ?", (yt_id, pl["spotify_id"])
            )
            conn.commit()
            on_event("playlist", f"{pl['name']} → YouTube プレイリスト作成")
        else:
            on_event("playlist", pl["name"])

        rows = conn.execute(
            f"SELECT * FROM tracks WHERE playlist_id = ? AND status IN ({placeholders}) "
            f"ORDER BY position",
            (pl["spotify_id"], *db.OPEN),
        ).fetchall()

        for tr in rows:
            if limit is not None and stats["added"] >= limit:
                stats["outcome"] = "指定件数に到達"
                return
            _one_track(conn, yt, yt_id, tr, budget, on_event, stats)
            time.sleep(PACE_SECONDS)


def _one_track(conn, yt, yt_id, tr, budget, on_event, stats):
    track = {"title": tr["title"], "artists": tr["artists"], "duration_ms": tr["duration_ms"]}
    video_id = tr["yt_video_id"]

    if not video_id:
        search_cost = yt_api.COST_SEARCH + yt_api.COST_VIDEOS
        if not _afford(conn, budget, search_cost + yt_api.COST_ITEM_INSERT):
            raise yt_api.QuotaExceeded("budget reached")

        candidates = yt_api.search(yt, matching.query_for(track))
        db.spend(conn, yt_api.COST_SEARCH)
        if candidates:
            yt_api.attach_durations(yt, candidates)
            db.spend(conn, yt_api.COST_VIDEOS)

        pick, top = matching.best(candidates, track)
        if not pick:
            conn.execute(
                "UPDATE tracks SET status = ?, note = ? WHERE id = ?",
                (db.UNMATCHED, f"best score {top:.0f}", tr["id"]),
            )
            conn.commit()
            stats["unmatched"] += 1
            on_event("unmatched", f"{tr['title']} — {tr['artists']}")
            return

        video_id = pick["video_id"]
        conn.execute(
            "UPDATE tracks SET yt_video_id = ?, status = ?, note = ? WHERE id = ?",
            (video_id, db.SEARCHED, f"{pick['channel']} | score {top:.0f}", tr["id"]),
        )
        conn.commit()

    if not _afford(conn, budget, yt_api.COST_ITEM_INSERT):
        raise yt_api.QuotaExceeded("budget reached")

    try:
        yt_api.add_to_playlist(yt, yt_id, video_id)
        db.spend(conn, yt_api.COST_ITEM_INSERT)
        conn.execute("UPDATE tracks SET status = ? WHERE id = ?", (db.ADDED, tr["id"]))
        stats["added"] += 1
        on_event("added", f"{tr['title']} — {tr['artists']}")
    except yt_api.QuotaExceeded:
        raise
    except Exception as e:
        db.spend(conn, yt_api.COST_ITEM_INSERT)
        conn.execute(
            "UPDATE tracks SET status = ?, note = ? WHERE id = ?",
            (db.FAILED, str(e)[:200], tr["id"]),
        )
        stats["failed"] += 1
        on_event("failed", f"{tr['title']} — {e}")
    conn.commit()
