"""Keep the local queue in step with the Spotify playlist it came from.

Spotify stamps every playlist with a snapshot_id that changes whenever its
contents change, so detecting "is my queue stale?" costs a single cheap call.
"""

from datetime import datetime, timezone

import db
import spotify_client as sp_api


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def queued_playlists(conn):
    return conn.execute("SELECT * FROM playlists ORDER BY added_at").fetchall()


def stale(conn, remote):
    """Given a playlist dict from Spotify, is the queued copy out of date?"""
    row = conn.execute(
        "SELECT snapshot_id FROM playlists WHERE spotify_id = ?", (remote["id"],)
    ).fetchone()
    if row is None:
        return False  # not queued at all
    return row["snapshot_id"] != remote.get("snapshot_id")


def sync_playlist(conn, sp, playlist_id, force=False, known=None):
    """Add newly-added songs, flag deleted ones, and refresh the stored snapshot.

    Returns a summary dict; "changed" is False when Spotify's snapshot already
    matched, meaning nothing was fetched.
    """
    meta = sp_api.playlist_meta(sp, playlist_id, known=known)
    row = conn.execute(
        "SELECT snapshot_id FROM playlists WHERE spotify_id = ?", (playlist_id,)
    ).fetchone()

    if row and not force and row["snapshot_id"] and row["snapshot_id"] == meta["snapshot_id"]:
        return {"changed": False, "added": 0, "removed": 0, "restored": 0,
                "name": meta["name"], "total": meta["total"]}

    conn.execute(
        "INSERT INTO playlists (spotify_id, name, total, added_at) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(spotify_id) DO UPDATE SET name = excluded.name",
        (playlist_id, meta["name"], _now()),
    )

    existing = conn.execute(
        "SELECT id, position, title, status, spotify_track_id FROM tracks WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchall()
    by_track = {r["spotify_track_id"]: r for r in existing if r["spotify_track_id"]}
    by_position = {r["position"]: r for r in existing if not r["spotify_track_id"]}

    added = removed = restored = 0
    seen_ids = set()
    total = 0

    for position, t in sp_api.tracks(sp, playlist_id):
        total = position
        tid = t.get("spotify_track_id")
        row = by_track.get(tid) if tid else None

        # Rows queued before track ids were stored are matched by position once,
        # then carry their id from here on.
        if row is None and tid:
            legacy = by_position.get(position)
            if legacy is not None and legacy["title"] == t["title"]:
                conn.execute(
                    "UPDATE tracks SET spotify_track_id = ? WHERE id = ?", (tid, legacy["id"])
                )
                by_position.pop(position)
                row = legacy
                by_track[tid] = legacy

        if tid:
            seen_ids.add(tid)

        if row is not None:
            conn.execute(
                "UPDATE tracks SET position = ?, title = ?, artists = ?, album = ?, "
                "duration_ms = ? WHERE id = ?",
                (position, t["title"], t["artists"], t["album"], t["duration_ms"], row["id"]),
            )
            if row["status"] == db.REMOVED:
                conn.execute(
                    "UPDATE tracks SET status = ? WHERE id = ?", (db.PENDING, row["id"])
                )
                restored += 1
            continue

        conn.execute(
            "INSERT INTO tracks (playlist_id, position, title, artists, album, duration_ms, "
            "spotify_track_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (playlist_id, position, t["title"], t["artists"], t["album"], t["duration_ms"], tid),
        )
        added += 1

    # Anything we hold that Spotify no longer lists has been deleted there.
    for r in existing:
        gone = (r["spotify_track_id"] not in seen_ids) if r["spotify_track_id"] else (
            r["position"] in by_position
        )
        if gone and r["status"] != db.REMOVED:
            conn.execute("UPDATE tracks SET status = ? WHERE id = ?", (db.REMOVED, r["id"]))
            removed += 1

    conn.execute(
        "UPDATE playlists SET total = ?, snapshot_id = ?, synced_at = ?, name = ? "
        "WHERE spotify_id = ?",
        (total, meta["snapshot_id"], _now(), meta["name"], playlist_id),
    )
    conn.commit()

    return {"changed": True, "added": added, "removed": removed, "restored": restored,
            "name": meta["name"], "total": total}


def sync_all(conn, sp, force=False, listing=None):
    """`listing` を渡すと snapshot の比較に一覧の情報を使うので、変更がなければ
    Spotify への追加の問い合わせが発生しない。"""
    by_id = {p["id"]: p for p in (listing or [])}
    return {
        row["spotify_id"]: sync_playlist(
            conn, sp, row["spotify_id"], force=force, known=by_id.get(row["spotify_id"])
        )
        for row in queued_playlists(conn)
    }
