"""Read-only Spotify access: playlists, their tracks, and Liked Songs."""

import os
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth

SCOPE = "playlist-read-private playlist-read-collaborative user-library-read"
CACHE = Path(__file__).parent / ".spotify_token.json"

LIKED_ID = "__liked__"  # pseudo playlist id for Liked Songs


def client():
    return spotipy.Spotify(
        auth_manager=SpotifyOAuth(
            scope=SCOPE,
            cache_path=str(CACHE),
            redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
            # Always show the approval dialog so the acting account is visible and switchable.
            show_dialog=True,
        ),
        requests_timeout=30,
    )


def _total(playlist):
    """Track count. Spotify renamed the simplified object's "tracks" field to "items"."""
    for key in ("items", "tracks"):
        holder = playlist.get(key)
        if isinstance(holder, dict) and "total" in holder:
            return holder["total"]
    return 0


def current_user_id(sp):
    return sp.current_user()["id"]


def playlists(sp):
    """All playlists visible to the user, in Spotify's own order.

    Apps in Spotify's development mode can only read the contents of playlists the
    user owns, so each entry carries "owned" to let callers skip the rest early.
    """
    me = current_user_id(sp)
    out, offset = [], 0
    while True:
        page = sp.current_user_playlists(limit=50, offset=offset)
        for p in page["items"]:
            # Spotify occasionally returns sparse or null entries here.
            if not p or not p.get("id"):
                continue
            out.append({
                "id": p["id"],
                "name": p.get("name") or "(名称なし)",
                "total": _total(p),
                "owned": (p.get("owner") or {}).get("id") == me,
                "snapshot_id": p.get("snapshot_id"),
            })
        if not page["next"]:
            return out
        offset += 50


def playlist_meta(sp, playlist_id):
    if playlist_id == LIKED_ID:
        page = sp.current_user_saved_tracks(limit=1)
        return {
            "id": LIKED_ID,
            "name": "Liked Songs",
            "total": page["total"],
            "owned": True,
            # Liked Songs has no snapshot; the running total stands in for one.
            "snapshot_id": f"liked:{page['total']}",
        }
    p = sp.playlist(playlist_id)
    return {
        "id": p.get("id", playlist_id),
        "name": p.get("name") or "(名称なし)",
        "total": _total(p),
        "owned": (p.get("owner") or {}).get("id") == current_user_id(sp),
        "snapshot_id": p.get("snapshot_id"),
    }


def tracks(sp, playlist_id):
    """Yield (position, track dict). Local files, episodes and dead entries are skipped."""
    offset, position = 0, 0
    while True:
        if playlist_id == LIKED_ID:
            page = sp.current_user_saved_tracks(limit=50, offset=offset)
            step = 50
        else:
            page = sp.playlist_items(
                playlist_id,
                limit=100,
                offset=offset,
                additional_types=("track",),
                # Spotify renamed the entry's "track" key to "item"; ask for both.
                fields=(
                    "next,items(is_local"
                    ",item(id,name,duration_ms,artists(name),album(name))"
                    ",track(id,name,duration_ms,artists(name),album(name)))"
                ),
            )
            step = 100

        for item in page["items"]:
            t = item.get("item") or item.get("track")
            if not t or item.get("is_local") or not t.get("name") or not t.get("duration_ms"):
                continue
            position += 1
            yield position, {
                "spotify_track_id": t.get("id"),
                "title": t["name"],
                "artists": ", ".join(a["name"] for a in t.get("artists", []) if a.get("name")),
                "album": (t.get("album") or {}).get("name"),
                "duration_ms": t["duration_ms"],
            }

        if not page.get("next"):
            return
        offset += step
