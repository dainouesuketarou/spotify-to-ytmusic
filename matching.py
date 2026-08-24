"""Pick the YouTube video that actually is the Spotify track."""

import re
from difflib import SequenceMatcher

# "(feat. X)", "[Explicit]", "(2011 Remaster)", "(Live at ...)" and friends.
BRACKET_NOISE = re.compile(
    r"\s*[\(\[（【]\s*(?:feat\.?|ft\.?|with\s|prod\.?|remaster|remastered|"
    r"\d{4}\s*remaster|deluxe|bonus|explicit|clean|mono|stereo|live\b|"
    r"from\s|original\s+motion|radio\s+edit|single\s+version|album\s+version|"
    r"expanded|anniversary|edit\b)[^)\]）】]*[\)\]）】]",
    re.I,
)
# " - 2011 Remaster", " - Single Version", " - Live at ..."
DASH_NOISE = re.compile(
    r"\s+-\s+(?:\d{4}\s*)?(?:remaster(?:ed)?(?:\s*\d{4})?|single\s+version|"
    r"album\s+version|radio\s+edit|mono|stereo|live\b.*|from\b.*|"
    r"bonus\s+track.*|deluxe.*|anniversary.*)$",
    re.I,
)
NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
SPACES = re.compile(r"\s+")

# YouTube noise that signals a non-official upload.
BAD_HINTS = re.compile(
    r"\b(cover|karaoke|instrumental|nightcore|sped\s*up|slowed|reverb|"
    r"8d\s*audio|reaction|lyrics?\s*video|tutorial|remix|mashup|live\b|"
    r"歌ってみた|カラオケ|弾いてみた|叩いてみた|作業用)\b",
    re.I,
)


def normalize(text):
    text = BRACKET_NOISE.sub(" ", text or "")
    text = DASH_NOISE.sub(" ", text)
    text = NON_WORD.sub(" ", text.lower())
    return SPACES.sub(" ", text).strip()


def query_for(track):
    """The search string sent to YouTube."""
    primary = track["artists"].split(",")[0].strip()
    return f"{normalize(track['title'])} {primary}".strip()


def _similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


def score(candidate, track):
    """Return (points, detail). Higher points is a better match."""
    title_n = normalize(candidate["title"])
    channel_n = normalize(candidate["channel"])
    want_title = normalize(track["title"])
    artists = [normalize(a) for a in track["artists"].split(",") if a.strip()]

    detail = {"topic": False, "topic_artist": False}
    points = 0.0

    # Auto-generated "<Artist> - Topic" channels carry the official audio.
    if candidate["channel"].strip().endswith("- Topic"):
        detail["topic"] = True
        topic_artist = normalize(candidate["channel"].rsplit("- Topic", 1)[0])
        detail["topic_artist"] = any(_similar(topic_artist, a) > 0.8 for a in artists)
        points += 45 if detail["topic_artist"] else 25
    elif any(a and a in channel_n for a in artists):
        points += 20
    elif any(a and a in title_n for a in artists):
        points += 12
    detail["artist_points"] = points

    detail["title_sim"] = _similar(want_title, title_n)
    # "星野源 - 恋 (Official Video)" scores poorly against "恋" by ratio alone,
    # yet it plainly contains the track. Treat containment as its own signal.
    detail["title_contains"] = bool(want_title) and want_title in title_n
    points += 40 * max(detail["title_sim"], 0.5 if detail["title_contains"] else 0.0)

    # Duration is the strongest disambiguator between versions.
    detail["duration_diff"] = None
    if candidate.get("duration_s"):
        diff = abs(candidate["duration_s"] - track["duration_ms"] / 1000)
        detail["duration_diff"] = diff
        if diff <= 3:
            points += 30
        elif diff <= 7:
            points += 15
        elif diff <= 15:
            points += 0
        else:
            points -= 45

    # Don't penalise words the original title already contains.
    detail["penalised"] = bool(
        BAD_HINTS.findall(candidate["title"]) and not BAD_HINTS.search(track["title"])
    )
    if detail["penalised"]:
        points -= 25

    detail["points"] = points
    return points, detail


ACCEPT_THRESHOLD = 55


def _acceptable(points, d):
    """A single threshold misses matches where one signal is decisive."""
    if points >= ACCEPT_THRESHOLD:
        return True
    if d["penalised"]:
        return False
    diff = d["duration_diff"]
    if diff is None:
        return False
    # Spotify romanises Japanese artist names, so the artist signal often fails
    # even on the correct video. An exact runtime plus a recognisable title is
    # a stronger statement than the artist string.
    if diff <= 3 and (d["title_sim"] >= 0.55 or d["title_contains"]):
        return True
    return bool(d["topic"] and d["topic_artist"] and diff <= 7)


def rank(candidates, track):
    """All candidates, best first, as (points, detail, candidate)."""
    scored = [(*score(c, track), c) for c in candidates]
    return sorted(scored, key=lambda x: x[0], reverse=True)


def best(candidates, track):
    """Return (candidate, score) or (None, best_score) when nothing is good enough."""
    if not candidates:
        return None, 0.0
    points, detail, top = rank(candidates, track)[0]
    return (top if _acceptable(points, detail) else None), points
