"""SQLite state: queued playlists, per-track progress, daily quota ledger."""

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).parent / "migrate.db"

# YouTube Data API quota resets at midnight US Pacific.
PACIFIC = ZoneInfo("America/Los_Angeles")

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    spotify_id  TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    yt_id       TEXT,
    total       INTEGER NOT NULL DEFAULT 0,
    added_at    TEXT NOT NULL,
    snapshot_id TEXT,
    synced_at   TEXT,
    paused      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tracks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    playlist_id      TEXT NOT NULL REFERENCES playlists(spotify_id) ON DELETE CASCADE,
    position         INTEGER NOT NULL,
    title            TEXT NOT NULL,
    artists          TEXT NOT NULL,
    album            TEXT,
    duration_ms      INTEGER NOT NULL,
    yt_video_id      TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',
    note             TEXT,
    spotify_track_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks (playlist_id, status);

-- Identity is the Spotify track, not its position: reordering a playlist or
-- inserting a song in the middle must not look like a set of new tracks.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracks_sid
    ON tracks (playlist_id, spotify_track_id)
    WHERE spotify_track_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS quota (
    day   TEXT PRIMARY KEY,
    units INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    added       INTEGER NOT NULL DEFAULT 0,
    unmatched   INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0,
    units       INTEGER NOT NULL DEFAULT 0,
    outcome     TEXT
);
"""

# Columns added after the first release; applied to existing databases on connect.
MIGRATIONS = [
    ("playlists", "snapshot_id", "TEXT"),
    ("playlists", "synced_at", "TEXT"),
    ("playlists", "paused", "INTEGER NOT NULL DEFAULT 0"),
    ("tracks", "spotify_track_id", "TEXT"),
]

# status values
PENDING = "pending"      # not searched yet
SEARCHED = "searched"    # video_id resolved, not yet inserted
ADDED = "added"          # in the YouTube playlist
UNMATCHED = "unmatched"  # no acceptable candidate
FAILED = "failed"        # API error while inserting
REMOVED = "removed"      # gone from the Spotify playlist since we queued it

# Statuses the runner still has work to do on.
OPEN = (PENDING, SEARCHED, FAILED)


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    for table, column, decl in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
    _drop_position_uniqueness(conn)


def _drop_position_uniqueness(conn):
    """Early versions keyed tracks by position, which breaks when a playlist is reordered."""
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tracks'"
    ).fetchone()
    if not ddl or "UNIQUE (playlist_id, position)" not in ddl["sql"]:
        return

    columns = ("id, playlist_id, position, title, artists, album, duration_ms, "
               "yt_video_id, status, note, spotify_track_id")
    conn.executescript(f"""
        PRAGMA foreign_keys = OFF;
        BEGIN;
        CREATE TABLE tracks_rebuilt (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id      TEXT NOT NULL REFERENCES playlists(spotify_id) ON DELETE CASCADE,
            position         INTEGER NOT NULL,
            title            TEXT NOT NULL,
            artists          TEXT NOT NULL,
            album            TEXT,
            duration_ms      INTEGER NOT NULL,
            yt_video_id      TEXT,
            status           TEXT NOT NULL DEFAULT 'pending',
            note             TEXT,
            spotify_track_id TEXT
        );
        INSERT INTO tracks_rebuilt ({columns}) SELECT {columns} FROM tracks;
        DROP TABLE tracks;
        ALTER TABLE tracks_rebuilt RENAME TO tracks;
        CREATE INDEX idx_tracks_status ON tracks (playlist_id, status);
        CREATE UNIQUE INDEX idx_tracks_sid
            ON tracks (playlist_id, spotify_track_id)
            WHERE spotify_track_id IS NOT NULL;
        COMMIT;
        PRAGMA foreign_keys = ON;
    """)


def today():
    return datetime.now(PACIFIC).strftime("%Y-%m-%d")


def quota_used(conn):
    row = conn.execute("SELECT units FROM quota WHERE day = ?", (today(),)).fetchone()
    return row["units"] if row else 0


def spend(conn, units):
    conn.execute(
        "INSERT INTO quota (day, units) VALUES (?, ?) "
        "ON CONFLICT(day) DO UPDATE SET units = units + excluded.units",
        (today(), units),
    )
    conn.commit()
