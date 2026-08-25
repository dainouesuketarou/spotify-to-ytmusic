"""Local web UI for queueing playlists, watching progress and re-syncing Spotify.

The server holds live Spotify and YouTube credentials, so it refuses to listen on
anything but the loopback interface unless WEB_TOKEN is set.
"""

import json
import os
import plistlib
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from spotipy.exceptions import SpotifyException

import db
import runner
import spotify_client as sp_api
import sync as sync_mod

HERE = Path(__file__).parent
TOKEN = os.getenv("WEB_TOKEN", "").strip()
# A token that may queue playlists and start a run, but not delete or edit - for phones.
QUEUE_TOKEN = os.getenv("WEB_QUEUE_TOKEN", "").strip()
# A token that may read but never change anything.
VIEW_TOKEN = os.getenv("WEB_VIEW_TOKEN", "").strip()
HOST = os.getenv("WEB_HOST", "127.0.0.1").strip()
PORT = int(os.getenv("WEB_PORT", "8765"))

VIDEO_ID = re.compile(r"(?:v=|youtu\.be/|/embed/)([A-Za-z0-9_-]{11})")

AGENT_PLIST = Path.home() / "Library/LaunchAgents/com.spotify-to-ytmusic.daily.plist"

app = FastAPI(title="spotify-to-ytmusic")

# --------------------------------------------------------------------------- run state

_run_log = deque(maxlen=400)
_run_state = {"active": False, "summary": None, "error": None}
_lock = threading.Lock()


def _emit(kind, message):
    _run_log.append({"kind": kind, "message": message, "at": time.time()})


def _run_thread(limit):
    try:
        summary = runner.run_once(on_event=_emit, limit=limit)
        with _lock:
            _run_state["summary"] = summary
    except runner.AlreadyRunning as e:
        with _lock:
            _run_state["error"] = str(e)
    except Exception as e:
        _emit("error", str(e))
        with _lock:
            _run_state["error"] = str(e)
    finally:
        with _lock:
            _run_state["active"] = False


# --------------------------------------------------------------------------- spotify

_spotify = {"client": None, "playlists": None, "at": 0.0}

# The last listing Spotify gave us, kept on disk so the picker survives a
# rate-limit window or a server restart.
LISTING_CACHE = HERE / ".spotify_playlists.json"

# While Spotify is rate-limiting us, stop calling it entirely: every extra
# request during the window pushes the reset further out.
_cooldown = {"until": 0.0}


def cooldown_left():
    return max(0, int(_cooldown["until"] - time.time()))


def _retry_after_seconds():
    """spotipy's retry layer discards the response, so ask Spotify directly."""
    try:
        cache = json.loads((HERE / ".spotify_token.json").read_text())
        r = requests.get(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {cache['access_token']}"},
            timeout=10,
        )
        if r.status_code == 429:
            return int(r.headers.get("Retry-After", 60))
        return 0
    except Exception:
        return 60


def _enter_cooldown():
    wait = _retry_after_seconds()
    _cooldown["until"] = time.time() + wait
    _spotify["playlists"] = None
    return wait


def _cooldown_message():
    left = cooldown_left()
    unit = f"{left // 60} 分" if left >= 60 else f"{left} 秒"
    return f"Spotify のレート制限中です。あと約 {unit} お待ちください。"


def _cooldown_response():
    left = cooldown_left()
    return JSONResponse(
        {"detail": _cooldown_message()},
        status_code=429,
        headers={"Retry-After": str(left or 60)},
    )


def spotify():
    if cooldown_left():
        raise HTTPException(429, "cooldown")
    if _spotify["client"] is None:
        cache = HERE / ".spotify_token.json"
        if not cache.exists():
            raise HTTPException(
                503,
                "Spotify が未認可。ターミナルで `make list` を一度実行して認可してください。",
            )
        _spotify["client"] = sp_api.client()
    return _spotify["client"]


def _save_listing(items, liked_total):
    try:
        LISTING_CACHE.write_text(json.dumps(
            {"at": time.time(), "playlists": items, "liked_total": liked_total},
            ensure_ascii=False,
        ))
    except OSError:
        pass


def _load_listing():
    try:
        return json.loads(LISTING_CACHE.read_text())
    except (OSError, ValueError):
        return None


def spotify_playlists(max_age=300):
    if _spotify["playlists"] is None or time.time() - _spotify["at"] > max_age:
        sp = spotify()
        items = sp_api.playlists(sp)
        liked = sp_api.playlist_meta(sp, sp_api.LIKED_ID)["total"]
        _spotify["playlists"] = items
        _spotify["at"] = time.time()
        _save_listing(items, liked)
    return _spotify["playlists"]


# --------------------------------------------------------------------------- auth

# What each role may change, as (method, path, exact?). Anything not listed is
# read-only. Matching on the method matters: a prefix alone would let a queue
# token reach DELETE /api/queue/<id>.
WRITABLE = {
    "admin": None,  # everything
    # Enough to say "migrate this playlist" from a phone: queue it, pull it in,
    # and kick off a run. Deleting, retrying and hand-editing stay on the Mac.
    "queue": (
        ("POST", "/api/queue", True),
        ("POST", "/api/sync", False),  # also /api/sync/<playlist_id>
        ("POST", "/api/run", True),
    ),
    "viewer": (),
}


def _role_for(supplied):
    if not TOKEN and not QUEUE_TOKEN and not VIEW_TOKEN:
        return "admin"
    for token, role in ((TOKEN, "admin"), (QUEUE_TOKEN, "queue"), (VIEW_TOKEN, "viewer")):
        if token and secrets.compare_digest(supplied, token):
            return role
    return None


def _may_write(role, method, path):
    allowed = WRITABLE.get(role, ())
    if allowed is None:
        return True
    return any(
        method == m and (path == route if exact else
                         path == route or path.startswith(route + "/"))
        for m, route, exact in allowed
    )


@app.middleware("http")
async def guard(request: Request, call_next):
    supplied = (
        request.query_params.get("t")
        or request.headers.get("x-token")
        or request.cookies.get("token")
        or ""
    )
    role = _role_for(supplied)
    if role is None:
        return JSONResponse({"detail": "認証が必要"}, status_code=401)
    if request.method not in ("GET", "HEAD") and not _may_write(
        role, request.method, request.url.path
    ):
        detail = ("閲覧専用のため操作できません" if role == "viewer"
                  else "このトークンでは実行できない操作です")
        return JSONResponse({"detail": detail}, status_code=403)

    request.state.role = role
    response = await call_next(request)
    if request.query_params.get("t"):
        response.set_cookie(
            "token", supplied, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 90
        )
    return response


@app.get("/api/me")
def me(request: Request):
    return {"role": getattr(request.state, "role", "admin")}


# --------------------------------------------------------------------------- pages

@app.exception_handler(SpotifyException)
def spotify_error(request: Request, exc: SpotifyException):
    """A 429 must surface as an error, not as a request that never returns."""
    if exc.http_status == 429 or "Max Retries" in str(exc.msg or ""):
        _enter_cooldown()
        return _cooldown_response()
    return JSONResponse({"detail": f"Spotify エラー: {exc.msg or exc}"}, status_code=502)


@app.exception_handler(HTTPException)
def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 429 and exc.detail == "cooldown":
        return _cooldown_response()
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


# --------------------------------------------------------------------------- api

def _next_run():
    """When the LaunchAgent will next fire, read from the installed plist."""
    if not AGENT_PLIST.exists():
        return {"installed": False}
    try:
        when = plistlib.loads(AGENT_PLIST.read_bytes()).get("StartCalendarInterval") or {}
        hour, minute = when.get("Hour"), when.get("Minute", 0)
    except Exception:
        return {"installed": False}
    if hour is None:
        return {"installed": True, "at": None}

    now = datetime.now().astimezone()
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return {"installed": True, "hour": hour, "minute": minute, "at": nxt.isoformat()}


def _quota_resets_at():
    """Next midnight in US Pacific, expressed in this machine's local time."""
    now_pt = datetime.now(db.PACIFIC)
    midnight = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone().isoformat()


def _progress(conn, playlist_id):
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM tracks WHERE playlist_id = ? GROUP BY status",
        (playlist_id,),
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


@app.get("/api/overview")
def overview():
    conn = db.connect()
    used = db.quota_used(conn)
    counts = {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) n FROM tracks GROUP BY status")
    }
    open_left = sum(counts.get(s, 0) for s in db.OPEN)
    last = conn.execute(
        "SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    with _lock:
        active = _run_state["active"]
        error = _run_state["error"]
    return {
        "quota": {"used": used, "budget": runner.DAILY_BUDGET,
                  "tracks_left_today": max(0, (runner.DAILY_BUDGET - used) // runner.UNITS_PER_TRACK),
                  "day": db.today(), "resets_at": _quota_resets_at()},
        "schedule": _next_run(),
        "counts": counts,
        "open": open_left,
        "eta_days": -(-open_left // runner.TRACKS_PER_DAY) if open_left else 0,
        "running": active or runner.is_running(),
        "error": error,
        "last_run": dict(last) if last else None,
    }


@app.get("/api/upcoming")
def upcoming(limit: int = 30):
    """The next tracks the runner will touch, in the order it will touch them."""
    conn = db.connect()
    placeholders = ",".join("?" * len(db.OPEN))
    rows = conn.execute(
        f"SELECT t.id, t.title, t.artists, t.status, p.name AS playlist "
        f"FROM tracks t JOIN playlists p ON p.spotify_id = t.playlist_id "
        f"WHERE t.status IN ({placeholders}) AND p.paused = 0 "
        f"ORDER BY p.added_at, t.position LIMIT ?",
        (*db.OPEN, limit),
    ).fetchall()
    return {"tracks": [dict(r) for r in rows], "per_day": runner.TRACKS_PER_DAY}


def _entry(playlist_id, name, total, row, counts, *, owned=True, stale=False):
    return {
        "id": playlist_id,
        "name": name,
        "total": total,
        "owned": owned,
        "queued": row is not None,
        "paused": bool(row["paused"]) if row else False,
        "stale": stale,
        "synced_at": row["synced_at"] if row else None,
        "yt_id": row["yt_id"] if row else None,
        "counts": counts,
        "done": counts.get(db.ADDED, 0),
    }


@app.get("/api/playlists")
def list_playlists(refresh: bool = False):
    """Queued playlists come from the local database, so progress stays visible
    even when Spotify is unreachable. Spotify only supplies the playlists that
    have not been queued yet."""
    conn = db.connect()
    queued = {r["spotify_id"]: r for r in sync_mod.queued_playlists(conn)}

    remote, error, liked_total, cached_at = [], None, None, None
    try:
        remote = spotify_playlists(max_age=0 if refresh else 300)
        saved = _load_listing() or {}
        liked_total = saved.get("liked_total")
    except (HTTPException, SpotifyException, Exception) as e:
        if isinstance(e, SpotifyException):
            _enter_cooldown()
        if cooldown_left():
            error = _cooldown_message()
        elif isinstance(e, HTTPException):
            error = str(e.detail)
        else:
            error = f"Spotify に接続できません: {e}"

        # Fall back to the last listing we managed to fetch.
        saved = _load_listing()
        if saved:
            remote = saved.get("playlists", [])
            liked_total = saved.get("liked_total")
            cached_at = saved.get("at")

    out, seen = [], set()

    # Everything already queued, straight from the database.
    for pid, row in queued.items():
        seen.add(pid)
        remote_match = next((p for p in remote if p["id"] == pid), None)
        stale = bool(remote_match and row["snapshot_id"] != remote_match.get("snapshot_id"))
        out.append(_entry(
            pid,
            (remote_match or {}).get("name") or row["name"],
            (remote_match or {}).get("total", row["total"]),
            row, _progress(conn, pid), stale=stale,
        ))

    # Anything Spotify knows about that is not queued yet.
    for p in remote:
        if p["id"] in seen or not p["owned"]:
            continue
        out.append(_entry(p["id"], p["name"], p["total"], None, {}))

    if sp_api.LIKED_ID not in seen:
        out.append(_entry(sp_api.LIKED_ID, "Liked Songs", liked_total, None, {}))

    return {
        "playlists": out,
        "hidden_not_owned": sum(1 for p in remote if not p["owned"]),
        "spotify_error": error,
        "listing_cached_at": cached_at,
    }


@app.post("/api/queue")
def enqueue(payload: dict = Body(...)):
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(400, "ids が空")
    conn = db.connect()
    sp = spotify()
    return {"results": [sync_mod.sync_playlist(conn, sp, i, force=True) for i in ids]}


@app.delete("/api/queue/{playlist_id}")
def dequeue(playlist_id: str):
    conn = db.connect()
    conn.execute("DELETE FROM tracks WHERE playlist_id = ?", (playlist_id,))
    conn.execute("DELETE FROM playlists WHERE spotify_id = ?", (playlist_id,))
    conn.commit()
    return {"ok": True}


@app.post("/api/queue/{playlist_id}/pause")
def pause(playlist_id: str, payload: dict = Body(...)):
    conn = db.connect()
    conn.execute(
        "UPDATE playlists SET paused = ? WHERE spotify_id = ?",
        (1 if payload.get("paused") else 0, playlist_id),
    )
    conn.commit()
    return {"ok": True}


@app.get("/api/queue/{playlist_id}/tracks")
def playlist_tracks(playlist_id: str, status: str = "", q: str = ""):
    conn = db.connect()
    sql = "SELECT * FROM tracks WHERE playlist_id = ?"
    params = [playlist_id]
    if status:
        wanted = status.split(",")
        sql += f" AND status IN ({','.join('?' * len(wanted))})"
        params += wanted
    if q:
        sql += " AND (title LIKE ? OR artists LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    sql += " ORDER BY position LIMIT 1000"
    return {"tracks": [dict(r) for r in conn.execute(sql, params)]}


@app.post("/api/sync")
def sync_everything(payload: dict = Body(default={})):
    conn = db.connect()
    listing = None
    try:
        listing = spotify_playlists()
    except Exception:
        pass  # fall back to per-playlist lookups
    results = sync_mod.sync_all(
        conn, spotify(), force=bool(payload.get("force")), listing=listing
    )
    _spotify["playlists"] = None
    return {"results": results}


@app.post("/api/sync/{playlist_id}")
def sync_one(playlist_id: str, payload: dict = Body(default={})):
    conn = db.connect()
    result = sync_mod.sync_playlist(conn, spotify(), playlist_id, force=bool(payload.get("force")))
    _spotify["playlists"] = None
    return result


@app.post("/api/run")
def start_run(payload: dict = Body(default={})):
    with _lock:
        if _run_state["active"]:
            raise HTTPException(409, "すでに実行中")
        _run_state.update(active=True, summary=None, error=None)
    _run_log.clear()
    threading.Thread(target=_run_thread, args=(payload.get("limit"),), daemon=True).start()
    return {"started": True}


@app.get("/api/run/log")
def run_log(after: float = 0.0):
    with _lock:
        state = dict(_run_state)
    return {"events": [e for e in list(_run_log) if e["at"] > after], **state}


@app.post("/api/retry")
def retry(payload: dict = Body(default={})):
    conn = db.connect()
    statuses = payload.get("statuses") or [db.UNMATCHED, db.FAILED]
    n = conn.execute(
        f"UPDATE tracks SET status = ?, yt_video_id = NULL, note = NULL "
        f"WHERE status IN ({','.join('?' * len(statuses))})",
        (db.PENDING, *statuses),
    ).rowcount
    conn.commit()
    return {"reset": n, "units": n * runner.UNITS_PER_TRACK}


@app.post("/api/tracks/{track_id}/resolve")
def resolve(track_id: int, payload: dict = Body(...)):
    """Pin a YouTube video by hand for a track the matcher could not place."""
    raw = (payload.get("video") or "").strip()
    m = VIDEO_ID.search(raw)
    video_id = m.group(1) if m else raw
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise HTTPException(400, "YouTube の動画 ID か URL を指定してください")
    conn = db.connect()
    conn.execute(
        "UPDATE tracks SET yt_video_id = ?, status = ?, note = ? WHERE id = ?",
        (video_id, db.SEARCHED, "手動指定", track_id),
    )
    conn.commit()
    return {"ok": True, "video_id": video_id}


@app.post("/api/tracks/{track_id}/skip")
def skip(track_id: int):
    conn = db.connect()
    conn.execute("UPDATE tracks SET status = ? WHERE id = ?", (db.REMOVED, track_id))
    conn.commit()
    return {"ok": True}


def _lan_address():
    """Best-effort LAN address, so the printed URL is one a phone can actually open."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet is sent; this just picks the route
        return s.getsockname()[0]
    except OSError:
        return HOST
    finally:
        s.close()


def main():
    import uvicorn

    if HOST not in ("127.0.0.1", "localhost") and not TOKEN:
        raise SystemExit(
            f"WEB_HOST={HOST} で公開するには WEB_TOKEN が必須です。\n"
            "  python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "で生成した値を .env の WEB_TOKEN に設定してください。"
        )
    shown = "127.0.0.1" if HOST in ("127.0.0.1", "localhost") else _lan_address()
    print(f"操作用: http://{shown}:{PORT}/" + (f"?t={TOKEN}" if TOKEN else ""), flush=True)
    if QUEUE_TOKEN:
        print(f"登録用: http://{shown}:{PORT}/?t={QUEUE_TOKEN}  (登録と実行・スマホ用)", flush=True)
    if VIEW_TOKEN:
        print(f"閲覧用: http://{shown}:{PORT}/?t={VIEW_TOKEN}  (閲覧のみ)", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
