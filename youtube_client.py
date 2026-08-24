"""YouTube Data API v3 wrapper with explicit quota costs.

Free tier is 10,000 units/day, which is why every call site accounts for its cost.
"""

import os
import re
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/youtube"]
HERE = Path(__file__).parent
TOKEN = HERE / ".youtube_token.json"

# Documented quota costs.
COST_SEARCH = 100
COST_VIDEOS = 1
COST_PLAYLIST_INSERT = 50
COST_ITEM_INSERT = 50

MUSIC_CATEGORY = "10"

_ISO_DURATION = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


class QuotaExceeded(Exception):
    pass


def _client_config():
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
    missing = [
        name
        for name, value in (("GOOGLE_CLIENT_ID", client_id), ("GOOGLE_CLIENT_SECRET", client_secret))
        if not value
    ]
    if missing:
        raise SystemExit(
            f".env に {', '.join(missing)} が未設定です。\n"
            "Google Cloud Console → Google Auth Platform → クライアント で "
            "「デスクトップ アプリ」のクライアントを作成し、その値を .env に記入してください。"
        )
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def service():
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_config(_client_config(), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _iso_to_seconds(value):
    m = _ISO_DURATION.fullmatch(value or "")
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _guard(exc):
    reason = ""
    try:
        reason = exc.error_details[0].get("reason", "")
    except Exception:
        reason = str(exc)
    if "quotaExceeded" in reason or "quotaExceeded" in str(exc):
        raise QuotaExceeded(str(exc)) from exc


def search(yt, query, max_results=5):
    """COST_SEARCH units. Returns raw candidates without durations."""
    try:
        res = yt.search().list(
            part="snippet",
            q=query,
            type="video",
            videoCategoryId=MUSIC_CATEGORY,
            maxResults=max_results,
        ).execute()
    except HttpError as e:
        _guard(e)
        raise
    return [
        {
            "video_id": item["id"]["videoId"],
            "title": item["snippet"]["title"],
            "channel": item["snippet"]["channelTitle"],
        }
        for item in res.get("items", [])
        if item.get("id", {}).get("videoId")
    ]


def attach_durations(yt, candidates):
    """COST_VIDEOS units for up to 50 ids - cheap accuracy."""
    if not candidates:
        return candidates
    ids = ",".join(c["video_id"] for c in candidates)
    try:
        res = yt.videos().list(part="contentDetails", id=ids).execute()
    except HttpError as e:
        _guard(e)
        raise
    seconds = {
        item["id"]: _iso_to_seconds(item["contentDetails"]["duration"])
        for item in res.get("items", [])
    }
    for c in candidates:
        c["duration_s"] = seconds.get(c["video_id"])
    return candidates


def create_playlist(yt, title, description=""):
    """COST_PLAYLIST_INSERT units."""
    try:
        res = yt.playlists().insert(
            part="snippet,status",
            body={
                "snippet": {"title": title, "description": description},
                "status": {"privacyStatus": "private"},
            },
        ).execute()
    except HttpError as e:
        _guard(e)
        raise
    return res["id"]


def add_to_playlist(yt, playlist_id, video_id):
    """COST_ITEM_INSERT units."""
    try:
        yt.playlistItems().insert(
            part="snippet",
            body={
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            },
        ).execute()
    except HttpError as e:
        _guard(e)
        raise
