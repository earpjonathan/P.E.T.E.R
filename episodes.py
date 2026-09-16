#!/usr/bin/env python3
"""Identify which episode a clip came from, by matching its dialogue.

Two steps:

    # once: build a local index of every episode transcript (~4 min)
    ./.venv/bin/python episodes.py build

    # then: identify clips and fill season/episode into the upload manifest
    ./.venv/bin/python episodes.py match out/reels --manifest upload_manifest.csv

Matching is fuzzy on purpose. Whisper mishears these clips often enough
("Stewie" came out as "Stoy"), so a single wrong word must not sink a match:
the clip is cut into overlapping word n-grams and scored by how many of them
appear anywhere in an episode. A distinctive phrase pins one episode down, and
the runner-up margin is reported as confidence so you can eyeball weak hits.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import re
import sys
import time
import urllib.request

SHOW = "family-guy"
BASE = "https://www.springfieldspringfield.co.uk"
INDEX = "episode_index.json"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
NGRAM = 4


def fetch(url: str, tries: int = 3) -> str:
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if i == tries - 1:
                return ""
            time.sleep(1.5 * (i + 1))
    return ""


def normalise(t: str) -> str:
    t = html.unescape(t).lower()
    t = re.sub(r"[^a-z0-9' ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def grams(text: str, n: int = NGRAM):
    w = text.split()
    return {" ".join(w[i:i + n]) for i in range(len(w) - n + 1)}


def build_index(path: str = INDEX) -> int:
    listing = fetch(f"{BASE}/episode_scripts.php?tv-show={SHOW}")
    codes = sorted(set(re.findall(r"episode=(s\d+e\d+)", listing)))
    if not codes:
        raise SystemExit("could not read the episode list")
    print(f"{len(codes)} episodes listed")

    out = {}
    if os.path.exists(path):                       # resume a partial build
        with open(path) as f:
            out = json.load(f)
    for i, code in enumerate(codes, 1):
        if code in out and out[code].get("text"):
            continue
        page = fetch(f"{BASE}/view_episode_scripts.php?tv-show={SHOW}&episode={code}")
        m = re.search(r'<div class="scrolling-script-container">(.*?)</div>',
                      page, re.S)
        text = normalise(re.sub(r"<[^>]+>", " ", m.group(1))) if m else ""
        s, e = re.match(r"s(\d+)e(\d+)", code).groups()
        out[code] = {"season": int(s), "episode": int(e), "text": text}
        if i % 25 == 0 or i == len(codes):
            print(f"\r  {i}/{len(codes)}", end="", flush=True)
            with open(path, "w") as f:
                json.dump(out, f)
        time.sleep(0.35)                           # be polite to the host
    print()
    with open(path, "w") as f:
        json.dump(out, f)
    good = sum(1 for v in out.values() if len(v["text"]) > 500)
    print(f"indexed {good}/{len(out)} episodes with usable transcripts -> {path}")
    return 0


def load_index(path: str = INDEX):
    if not os.path.exists(path):
        raise SystemExit(f"no {path}; run `episodes.py build` first")
    with open(path) as f:
        idx = json.load(f)
    return {k: v for k, v in idx.items() if len(v.get("text", "")) > 500}


def transcribe(video: str, model: str = "base.en") -> str:
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cpu", compute_type="int8")
    segs, _ = m.transcribe(video, vad_filter=True)
    return normalise(" ".join(s.text for s in segs))


def identify(text: str, idx):
    """Return (code, season, episode, confidence, hits)."""
    g = grams(text)
    if not g:
        return None
    scored = []
    for code, v in idx.items():
        t = v["text"]
        hits = sum(1 for x in g if x in t)
        scored.append((hits, code))
    scored.sort(reverse=True)
    if scored[0][0] == 0:
        return None

    # The source site lists three season-5 episodes a second time as season 6
    # (the well-known Family Guy 5/6 split), with byte-for-byte the same
    # dialogue. Both copies therefore score identically, which used to halve
    # the margin and push a *perfect* 80-of-103-gram match down to 0.30
    # confidence - indistinguishable from a clip with no dialogue at all.
    # Collapse the tie to the lower season, then measure the margin against
    # the first genuinely different episode.
    top = scored[0][0]
    tied = [c for h, c in scored if h == top]
    best_code = min(tied, key=lambda c: (idx[c]["season"], idx[c]["episode"]))
    rest = [h for h, c in scored if c not in tied]
    second = rest[0] if rest else 0

    margin = (top - second) / top
    coverage = top / max(1, len(g))
    conf = round(min(1.0, margin * 0.7 + min(coverage * 3, 1.0) * 0.3), 3)
    v = idx[best_code]
    return best_code, v["season"], v["episode"], conf, top


def main() -> int:
    ap = argparse.ArgumentParser(description="Find the episode a clip is from")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="download and index every episode transcript")
    m = sub.add_parser("match", help="identify clips and fill the manifest")
    m.add_argument("folder", help="folder of clips (or a single file)")
    m.add_argument("--manifest", default=None,
                   help="CSV to fill in; omit to only print results")
    m.add_argument("--model", default="base.en", help="whisper model")
    m.add_argument("--min-conf", type=float, default=0.35,
                   help="leave the manifest blank below this confidence")
    args = ap.parse_args()

    if args.cmd == "build":
        return build_index()

    idx = load_index()
    print(f"{len(idx)} episodes in index")
    src = args.folder
    files = ([src] if os.path.isfile(src) else
             sorted(os.path.join(src, f) for f in os.listdir(src)
                    if f.lower().endswith((".mp4", ".mov", ".mkv", ".m4v"))))
    results = {}
    for f in files:
        text = transcribe(f, args.model)
        r = identify(text, idx)
        name = os.path.basename(f)
        if not r:
            print(f"  {name}: no match (no usable dialogue)")
            continue
        code, s, e, conf, hits = r
        flag = "" if conf >= args.min_conf else "   <- low confidence"
        print(f"  {name}: S{s:02d}E{e:02d}  conf {conf:.2f} ({hits} phrase hits){flag}")
        if conf >= args.min_conf:
            results[os.path.abspath(f)] = (s, e)

    if args.manifest and os.path.exists(args.manifest):
        rows = list(csv.reader(open(args.manifest)))
        n = 0
        for row in rows[1:]:
            key = os.path.abspath(row[0])
            if key in results and not (row[1] and row[2]):
                row[1], row[2] = map(str, results[key])
                n += 1
        csv.writer(open(args.manifest, "w", newline="")).writerows(rows)
        print(f"\nfilled {n} row(s) in {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
