"""Sanity checks for the track matcher - no network, no credentials."""

import matching

def track(title, artists, seconds):
    return {"title": title, "artists": artists, "duration_ms": seconds * 1000}

def cand(title, channel, seconds):
    return {"video_id": "x", "title": title, "channel": channel, "duration_s": seconds}

CASES = [
    (
        "Topic channel wins over a cover",
        track("Bohemian Rhapsody - 2011 Remaster", "Queen", 355),
        [
            cand("Bohemian Rhapsody (Live Cover)", "SomeGuy Music", 360),
            cand("Bohemian Rhapsody", "Queen - Topic", 355),
        ],
        "Queen - Topic",
    ),
    (
        "Duration rejects the wrong version",
        track("Blinding Lights", "The Weeknd", 200),
        [
            cand("Blinding Lights (Extended Mix)", "The Weeknd - Topic", 400),
            cand("Blinding Lights", "The Weeknd - Topic", 201),
        ],
        "The Weeknd - Topic",
    ),
    (
        "feat. noise in the Spotify title still matches",
        track("Sunflower (feat. Swae Lee) - Spider-Man: Into the Spider-Verse", "Post Malone, Swae Lee", 158),
        [cand("Sunflower", "Post Malone - Topic", 158)],
        "Post Malone - Topic",
    ),
    (
        "Japanese title with an official artist channel",
        track("Lemon", "米津玄師", 256),
        [
            cand("Lemon 歌ってみた", "カラオケch", 255),
            cand("Lemon", "米津玄師 - Topic", 256),
        ],
        "米津玄師 - Topic",
    ),
]

fails = 0
for name, tr, candidates, want_channel in CASES:
    pick, top = matching.best(candidates, tr)
    ok = pick is not None and pick["channel"] == want_channel
    print(f"{'PASS' if ok else 'FAIL'}  {name}  (score {top:.0f})")
    if not ok:
        fails += 1
        print(f"      got: {pick}")

CASES_ROMANISED = [
    (
        "Romanised artist + decorated Japanese title still matches on runtime",
        track("恋", "Gen Hoshino", 244),
        [cand("星野源 - 恋 (Official Video)", "星野源", 244)],
    ),
    (
        "Romanised artist with a Topic channel in Japanese",
        track("有心論", "RADWIMPS", 289),
        [cand("有心論", "RADWIMPS - Topic", 289)],
    ),
]
for name, tr, candidates in CASES_ROMANISED:
    pick, top = matching.best(candidates, tr)
    ok = pick is not None
    print(f"{'PASS' if ok else 'FAIL'}  {name}  (score {top:.0f})")
    fails += 0 if ok else 1

# Containment must not rescue a wrong-length track.
pick, top = matching.best(
    [cand("星野源 - 恋 (Live 2019)", "星野源", 400)],
    track("恋", "Gen Hoshino", 244),
)
ok = pick is None
print(f"{'PASS' if ok else 'FAIL'}  Containment does not override a bad runtime  (score {top:.0f})")
fails += 0 if ok else 1

# A track with no plausible candidate must be rejected, not force-matched.
pick, top = matching.best(
    [cand("Totally Different Song", "Random Uploads", 900)],
    track("Yesterday", "The Beatles", 125),
)
ok = pick is None
print(f"{'PASS' if ok else 'FAIL'}  Rejects a bad-only candidate  (score {top:.0f})")
fails += 0 if ok else 1

print(f"\n{'all passed' if not fails else str(fails) + ' failed'}")
raise SystemExit(1 if fails else 0)
