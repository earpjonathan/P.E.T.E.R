#!/usr/bin/env python3
"""Find short Family Guy clips on YouTube that are worth rendering.

    ./.venv/bin/python harvest.py --dry-run --count 10   # rank, download nothing
    ./.venv/bin/python harvest.py --count 10             # download the winners

Two stages, because they cost wildly different amounts:

  1. A flat search returns id/title/duration/views for ~40 results per query in
     about a second, without touching the videos.
  2. Only the handful that survive the duration filter get a full metadata
     fetch, which is where upload date and resolution live.

Query wording decides whether this works at all. Measured: "family guy funny
moments" returns 40 results with a median duration of 1457s - hour-long
compilations, zero usable clips. "family guy shorts" returns 15 usable ones out
of 40. Searches that sound right are not necessarily productive, so QUERIES
below is tuned, not guessed.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time

YTDLP = "/opt/homebrew/bin/yt-dlp"
FFPROBE = "/opt/homebrew/bin/ffprobe"
SEEN = "seen.json"
CHANNELS_FILE = "harvest_channels.txt"

# Tuned by measuring how many in-band clips each returns; see module docstring.
#
# Measured 2026-08-29 as *new* (unseen) in-band results out of 40, which is the
# number that matters once seen.json is large. Two patterns held:
#
#   - Generic quality adjectives are structurally dead. "family guy best
#     moment" and "family guy funny scene" each returned 0 in band on two
#     consecutive runs: they surface multi-minute compilations, not 8-45s
#     clips, so no amount of waiting makes them productive. Removed.
#   - Secondary characters are nearly untapped, because the only character
#     queries here were the four leads. herbert 25, consuela 24, carter 22,
#     evil monkey 20, mort 19 - several returning band == new, meaning
#     essentially nothing they surface has ever been seen.
QUERIES = [
    # broad, still productive
    "family guy clip",
    "family guy shorts",
    "family guy funny clip short",
    # structural / recurring bits - a long tail of short self-contained scenes
    "family guy cutaway gag",            # 13, 13 across two runs
    "family guy gag",                    # 17 new
    "family guy flashback scene",        # 13 new
    # the four leads - mined thin, but still yield
    "family guy peter scene",
    "family guy stewie scene",
    "family guy brian scene",
    "family guy quagmire scene",
    # secondary characters - the untapped long tail
    "family guy herbert",                # 25 new
    "family guy consuela",               # 24 new
    "family guy carter pewterschmidt",   # 22 new
    "family guy evil monkey",            # 20 new
    "family guy mort",                   # 19 new
    "family guy tom tucker",             # 15 new
    "family guy mayor adam west",        # 15 new
    "family guy joe swanson",            # 12 new
    "family guy chris griffin",          # 11 new
]

MIN_S, MAX_S = 8.0, 45.0     # 45s ceiling: both Content-ID blocks were >89s
MIN_WIDTH = 320              # 320x240 rendered acceptably; below that is mush

# The source must be landscape. A vertical clip renders to a tall sliver inside
# the top half of the 9:16 reel, wasting most of the frame, and the traced line
# art becomes unreadable. This was let in by enabling Shorts harvesting -
# Shorts are vertical by definition, and MIN_WIDTH alone passes them (a 608x1080
# Short is 608 wide). 4:3 is 1.33 and 16:9 is 1.78, so 1.2 admits both while
# rejecting square (1.0) and portrait (0.56).
MIN_ASPECT = 1.2


def load_seen() -> dict:
    if os.path.exists(SEEN):
        with open(SEEN) as f:
            return json.load(f)
    return {}


def save_seen(d) -> None:
    with open(SEEN, "w") as f:
        json.dump(d, f, indent=1)


def run_json(args, timeout=180):
    """yt-dlp emits one JSON object per line."""
    try:
        r = subprocess.run([YTDLP] + args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return []
    out = []
    for line in r.stdout.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def flat_search(query: str, n: int) -> list:
    return run_json(["--flat-playlist", "--dump-json", "--socket-timeout", "20",
                     "--no-warnings", f"ytsearch{n}:{query}"])


def flat_channel(url: str, n: int) -> list:
    return run_json(["--flat-playlist", "--dump-json", "--socket-timeout", "20",
                     "--no-warnings", "--playlist-end", str(n), url])


def full_meta(video_id: str) -> dict | None:
    r = run_json(["--dump-json", "--socket-timeout", "20", "--no-warnings",
                  f"https://www.youtube.com/watch?v={video_id}"], timeout=90)
    return r[0] if r else None


def score(views: float, age_days: float) -> float:
    """Popularity, age-normalised.

    Views alone let a five-year-old clip with 3M views permanently outrank a
    fresh one doing better per day, so half-weight views-per-day alongside it.
    This is a HYPOTHESIS - that a clip other people watched is a joke that
    lands - and analyze.py tests it against our own play counts once enough
    harvested clips have been posted.
    """
    vpd = views / max(age_days, 1.0)
    import math
    return math.log10(views + 1) + 0.5 * math.log10(vpd + 1)


def gather(count: int, pool_per_query: int, seen: dict, verbose=True) -> list:
    """Stage 1: cheap flat metadata across queries and channels."""
    cands, hit = {}, {}
    # enrich() costs one network round-trip per candidate, so bound the pool.
    hard_cap = max(count * 6, 40)
    unknown_cap = max(count * 3, 20)      # unknown-duration (Shorts) entries
    n_unknown = 0
    sources = [("search", q) for q in QUERIES]
    if os.path.exists(CHANNELS_FILE):
        for line in open(CHANNELS_FILE):
            line = line.split("#")[0].strip()     # trailing "# Channel Name"
            if line:
                sources.append(("channel", line))
    random.shuffle(sources)          # so repeat runs do not re-walk the same order

    for kind, src in sources:
        rows = (flat_search(src, pool_per_query) if kind == "search"
                else flat_channel(src, pool_per_query))
        n_band = 0
        for d in rows:
            vid, dur = d.get("id"), d.get("duration")
            if not vid or vid in seen or vid in cands:
                continue
            if d.get("live_status") in ("is_live", "is_upcoming"):
                continue
            if dur is None:
                # Shorts come back from --flat-playlist with duration=None.
                # Dropping them here made the harvester blind to Shorts
                # entirely - which is exactly where 8-45s clips live. Carry
                # them unfiltered; enrich() fetches the real duration and
                # applies the band there, at the cost of one metadata call.
                if n_unknown >= unknown_cap:
                    continue
                n_unknown += 1
            elif not (MIN_S <= float(dur) <= MAX_S):
                continue
            cands[vid] = {"id": vid, "title": (d.get("title") or "")[:100],
                          "duration": float(dur) if dur is not None else None,
                          "views": float(d.get("view_count") or 0),
                          "channel": d.get("channel") or d.get("uploader") or "?"}
            n_band += 1
            if len(cands) >= hard_cap:
                break
        hit[f"{kind}:{src}"] = (len(rows), n_band)
        if verbose:
            print(f"  {src[:44]:46} {len(rows):3} results, {n_band:2} in band"
                  + (f" ({n_unknown} dur unknown so far)" if n_unknown else ""),
                  flush=True)
        if kind == "channel" and not rows:
            # A renamed handle or a missing /videos tab returns zero rows and
            # looks identical to a channel we have simply exhausted. Both
            # seeded entries were dead this way, unnoticed, for a whole run.
            print(f"  ! dead channel source, fix or remove it: {src}",
                  file=sys.stderr, flush=True)
        # plenty to rank from; stop paying for more searches
        if len(cands) >= hard_cap:
            break
    return list(cands.values())


def enrich(cands: list, verbose=True) -> list:
    """Stage 2: full metadata, only for what survived the duration filter."""
    out = []
    for i, c in enumerate(cands, 1):
        m = full_meta(c["id"])
        if not m:
            continue
        # candidates carried with duration=None (Shorts) get filtered here,
        # where the real duration finally exists
        dur = m.get("duration") or c.get("duration")
        if not dur or not (MIN_S <= float(dur) <= MAX_S):
            continue
        c["duration"] = float(dur)
        w = m.get("width") or 0
        h = m.get("height") or 0
        if w and w < MIN_WIDTH:
            continue
        if w and h and w / h < MIN_ASPECT:
            continue                                   # portrait / square
        if not any(f.get("acodec") not in (None, "none")
                   for f in (m.get("formats") or [])) and not m.get("acodec"):
            continue                                   # no audio -> no caption
        up = m.get("upload_date")                      # YYYYMMDD
        age_days = 3650.0
        if up:
            try:
                age_days = max(1.0, (time.time() -
                    time.mktime(time.strptime(up, "%Y%m%d"))) / 86400)
            except ValueError:
                pass
        c.update(width=w, height=m.get("height") or 0, upload_date=up,
                 age_days=round(age_days, 1),
                 views=float(m.get("view_count") or c["views"]),
                 channel=m.get("channel") or c["channel"])
        c["score"] = round(score(c["views"], age_days), 3)
        out.append(c)
        if verbose and (i % 5 == 0 or i == len(cands)):
            print(f"  enriched {i}/{len(cands)}", flush=True)
    out.sort(key=lambda x: -x["score"])
    return out


def probe_aspect(path: str):
    """Width/height of the real file, or None if ffprobe cannot say."""
    try:
        r = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x", path],
            capture_output=True, timeout=30)
        w, h = r.stdout.decode().strip().split("\n")[0].split("x")[:2]
        return int(w) / int(h)
    except (ValueError, IndexError, OSError, subprocess.SubprocessError):
        return None


def download(c: dict, dst_dir: str) -> str | None:
    os.makedirs(dst_dir, exist_ok=True)
    out = os.path.join(dst_dir, f"yt_{c['id']}.%(ext)s")
    r = subprocess.run(
        [YTDLP, "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
         "--merge-output-format", "mp4", "--no-warnings",
         "--socket-timeout", "30", "-o", out,
         f"https://www.youtube.com/watch?v={c['id']}"],
        capture_output=True, timeout=300)
    p = os.path.join(dst_dir, f"yt_{c['id']}.mp4")
    if r.returncode != 0 or not os.path.exists(p):
        print(f"  ! download failed {c['id']}: "
              f"{r.stderr.decode('utf-8','ignore')[:160]}", file=sys.stderr)
        return None
    # Re-check on the actual file. Format selection can hand back a different
    # rendition than the metadata described, so the aspect test in enrich() is
    # a cheap pre-filter, not the guarantee.
    ar = probe_aspect(p)
    if ar is not None and ar < MIN_ASPECT:
        print(f"  ! {c['id']} is {ar:.2f}:1 (portrait/square), discarding",
              file=sys.stderr)
        os.remove(p)
        return None
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="Find short Family Guy clips")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--pool", type=int, default=40,
                    help="results to pull per query")
    ap.add_argument("--dry-run", action="store_true",
                    help="rank and print, download nothing")
    ap.add_argument("--dir", default="out/harvest")
    ap.add_argument("--no-mark-seen", action="store_true",
                    help="do not record these ids as seen")
    args = ap.parse_args()

    if not os.path.exists(YTDLP):
        raise SystemExit(f"yt-dlp not found at {YTDLP}")

    seen = load_seen()
    print(f"{len(seen)} video(s) already seen\nsearching ...")
    cands = gather(args.count, args.pool, seen)
    print(f"\n{len(cands)} candidate(s) in the {MIN_S:g}-{MAX_S:g}s band; "
          f"fetching full metadata ...")
    if not cands:
        print("nothing found - try widening --pool")
        return 1
    ranked = enrich(cands)

    print(f"\n{'rank':>4} {'score':>6} {'dur':>6} {'views':>12} {'res':>10}  title")
    for i, c in enumerate(ranked[:args.count * 2], 1):
        mark = "  <- take" if i <= args.count else ""
        print(f"{i:>4} {c['score']:>6.2f} {c['duration']:>5.0f}s "
              f"{c['views']:>12,.0f} {c['width']}x{c['height']:<6}"
              f"  {c['title'][:44]}{mark}")

    take = ranked[:args.count]
    if args.dry_run:
        print(f"\ndry run - would download {len(take)}, nothing written")
        return 0

    got = []
    for i, c in enumerate(take, 1):
        print(f"\n[{i}/{len(take)}] {c['title'][:56]}", flush=True)
        p = download(c, args.dir)
        if p:
            c["path"] = p
            got.append(c)
            print(f"  -> {p}")
        if not args.no_mark_seen:
            seen[c["id"]] = {"title": c["title"], "duration": c["duration"],
                             "views": c["views"], "channel": c["channel"],
                             "score": c["score"],
                             "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "downloaded": bool(p)}
            save_seen(seen)
    with open(os.path.join(args.dir, "harvest.json"), "w") as f:
        json.dump(got, f, indent=1)
    print(f"\ndownloaded {len(got)}/{len(take)} -> {args.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
