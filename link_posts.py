#!/usr/bin/env python3
"""Link each Instagram post back to the local render it came from.

    ./.venv/bin/python link_posts.py            # transcribe + match, writes post_links.csv
    ./.venv/bin/python link_posts.py --report   # just show what is already linked

Why this exists: dataset.csv keys rows by `ig_<media_id>` - the copy Instagram
serves back - while source_meta.json keys by the render tag (`auto_s03e09`).
Nothing joined the two, so a post's play count could never be traced to the
source clip it was harvested from, and the whole "did source popularity predict
anything" question was unanswerable.

Duration looked like the obvious key and is not: matching dataset.csv against
the local renders on duration gives at best 26 unique hits out of 68 (+/-0.10s)
with 12 ambiguous, and TIGHTENING the tolerance makes it worse (12 unique at
+/-0.05s) because Instagram's re-encode moves duration by more than a frame.

Dialogue is the key that works. It survives re-encoding, rescaling and
recompression, and we already transcribe everything anyway. This is the same
n-gram overlap test autopilot.py uses to reject duplicates, with an added
margin requirement so a near-tie is reported as ambiguous rather than guessed.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episodes as E

IG_DIR = "out/ig"
TRANSCRIPTS = "transcripts.json"
IG_TRANSCRIPTS = "ig_transcripts.json"
LINKS = "post_links.csv"

# A correct match sits near 1.0 overlap; a wrong one sits near 0.1. 0.45 is the
# same bar autopilot.py dedupes at, and MARGIN keeps two different cuts of one
# scene from being silently resolved to whichever scored a hair higher.
MIN_OVERLAP, MIN_MARGIN = 0.45, 0.15


def load_json(p, default):
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default


def transcribe_all(paths, cache):
    """Transcribe only what is not already cached - this is the slow part."""
    todo = [p for p in paths if os.path.basename(p)[:-4] not in cache]
    if not todo:
        return cache
    from faster_whisper import WhisperModel
    model = WhisperModel("base.en", device="cpu", compute_type="int8")
    for i, p in enumerate(todo, 1):
        key = os.path.basename(p)[:-4]
        segs, _ = model.transcribe(p, vad_filter=True)
        cache[key] = E.normalise(" ".join(s.text for s in segs))
        print(f"  transcribed {i}/{len(todo)}  {key}", flush=True)
        with open(IG_TRANSCRIPTS, "w") as f:
            json.dump(cache, f, indent=1)
    return cache


def best_match(text, local):
    """Return (tag, overlap, margin) or (None, overlap, margin) if unclear."""
    g = E.grams(text)
    if not g:
        return None, 0.0, 0.0
    scored = []
    for tag, t in local.items():
        gb = E.grams(t)
        if not gb:
            continue
        scored.append((len(g & gb) / min(len(g), len(gb)), tag))
    if not scored:
        return None, 0.0, 0.0
    scored.sort(reverse=True)
    top, tag = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    margin = top - second
    if top < MIN_OVERLAP or margin < MIN_MARGIN:
        return None, top, margin
    return tag, top, margin


def main() -> int:
    ap = argparse.ArgumentParser(description="Link IG posts to local renders")
    ap.add_argument("--report", action="store_true",
                    help="print existing post_links.csv, transcribe nothing")
    args = ap.parse_args()

    if args.report:
        if not os.path.exists(LINKS):
            print(f"{LINKS} does not exist yet - run without --report")
            return 1
        rows = list(csv.DictReader(open(LINKS)))
        linked = [r for r in rows if r["clip_tag"]]
        print(f"{len(linked)}/{len(rows)} posts linked")
        for r in linked[:20]:
            print(f"  {r['media_id']:20} -> {r['clip_tag']:22} "
                  f"overlap {float(r['overlap']):.2f} margin {float(r['margin']):.2f}")
        return 0

    paths = sorted(glob.glob(os.path.join(IG_DIR, "ig_*.mp4")))
    if not paths:
        print(f"no mp4s in {IG_DIR}/ - run instagram.py download first",
              file=sys.stderr)
        return 1
    print(f"{len(paths)} instagram mp4(s)")

    cache = transcribe_all(paths, load_json(IG_TRANSCRIPTS, {}))

    local = {k: v.get("text", "") for k, v in load_json(TRANSCRIPTS, {}).items()
             if isinstance(v, dict) and v.get("text")
             and not k.startswith("_pending_")}
    print(f"{len(local)} local render transcript(s) to match against\n")

    out, n_ok, n_amb = [], 0, 0
    for p in paths:
        key = os.path.basename(p)[:-4]           # ig_<media_id>
        mid = key[3:]
        tag, ov, mg = best_match(cache.get(key, ""), local)
        if tag:
            n_ok += 1
        elif ov > 0:
            n_amb += 1
        out.append({"media_id": mid, "clip": key, "clip_tag": tag or "",
                    "overlap": round(ov, 3), "margin": round(mg, 3)})

    with open(LINKS, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["media_id", "clip", "clip_tag",
                                          "overlap", "margin"])
        w.writeheader()
        w.writerows(out)

    print(f"linked   {n_ok}/{len(paths)}")
    print(f"unclear  {n_amb}  (below {MIN_OVERLAP} overlap or under "
          f"{MIN_MARGIN} margin - left blank rather than guessed)")
    print(f"-> {LINKS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
