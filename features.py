#!/usr/bin/env python3
"""Measure a finished reel so its performance can be modelled later.

The point is to record what a clip *is* while the file still exists. Uploading
with --delete-after removes the mp4, and once it is gone none of this can be
recovered from the local machine - only by downloading the post back.

Everything here is measured on the bottom pane (the original clip), not the
Desmos animation, since that is the part a viewer actually reacts to.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys

import numpy as np

W, H = 1080, 1920
SAMPLE_FPS = 4.0
SW, SH = 128, 114          # downsample for the visual stats


def probe(path: str) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                        "-show_format", "-show_streams", path],
                       capture_output=True)
    return json.loads(r.stdout or "{}")


def visual(path: str, dims=None) -> dict:
    """Pace and look of the source clip: motion, cuts, brightness, colour.

    The bottom half is taken from the file's OWN dimensions, not a fixed
    1080x1920. Instagram re-encodes everything it serves back to 720x1280, so
    a hardcoded crop silently yields no frames and every visual feature comes
    out blank - on exactly the clips that have view counts attached.
    """
    w, h = dims if dims else (W, H)
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path,
         "-vf", (f"crop={w}:{h//2}:0:{h//2},fps={SAMPLE_FPS},"
                 f"scale={SW}:{SH},format=rgb24"),
         "-f", "rawvideo", "-"], capture_output=True)
    n = len(r.stdout) // (SW * SH * 3)
    if n < 2:
        return {}
    a = np.frombuffer(r.stdout[:n*SW*SH*3], np.uint8).reshape(n, SH, SW, 3).astype(np.float32)
    lum = a.mean(axis=3)
    mx, mn = a.max(axis=3), a.min(axis=3)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)

    d = np.abs(np.diff(lum, axis=0)).mean(axis=(1, 2))     # per-frame change
    # A cut is a step change, not motion. Threshold relative to the clip's own
    # typical movement so a busy scene is not read as one long cut.
    thr = max(12.0, float(np.median(d) * 4))
    cuts = int((d > thr).sum())
    dur = n / SAMPLE_FPS
    return {
        "motion_mean": round(float(d.mean()), 3),
        "motion_std": round(float(d.std()), 3),
        "cuts": cuts,
        "cuts_per_sec": round(cuts / dur, 3) if dur else 0.0,
        "brightness": round(float(lum.mean()), 2),
        "contrast": round(float(lum.std()), 2),
        "saturation": round(float(sat.mean()), 4),
    }


def audio(path: str) -> dict:
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", path, "-af",
                        "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    out = {}
    for key, tag in (("mean_volume", "loudness_db"), ("max_volume", "peak_db")):
        for line in r.stderr.splitlines():
            if key + ":" in line:
                try:
                    out[tag] = float(line.split(key + ":")[1].split("dB")[0])
                except ValueError:
                    pass
    if "loudness_db" in out and "peak_db" in out:
        out["dynamic_range_db"] = round(out["peak_db"] - out["loudness_db"], 2)
    return out


def features_for(path: str, tx: dict) -> dict:
    j = probe(path)
    fmt = j.get("format", {})
    dur = float(fmt.get("duration", 0) or 0)
    # strip the extension first, then the ".reel" marker: a name that is not
    # "<clip>.reel.mp4" (an Instagram download, say) otherwise keeps its
    # extension and silently fails to join to anything later.
    base = os.path.splitext(os.path.basename(path))[0]
    if base.endswith(".reel"):
        base = base[:-5]
    row = {
        "clip": base,
        "duration_s": round(dur, 2),
        "size_mb": round(int(fmt.get("size", 0)) / 1e6, 2),
        "bitrate_mbps": round(int(fmt.get("bit_rate", 0)) / 1e6, 2),
        "compilation": base.split("_")[0],
    }
    v = next((x for x in j.get("streams", []) if x["codec_type"] == "video"), {})
    dims = (int(v["width"]), int(v["height"])) if v.get("width") else None
    row["width"], row["height"] = (dims or ("", ""))
    row.update(visual(path, dims))
    row.update(audio(path))
    t = tx.get(base, {})
    words = t.get("words", 0)
    row.update({
        "season": t.get("season", ""), "episode": t.get("episode", ""),
        "words": words,
        "words_per_sec": round(words / dur, 3) if dur else 0.0,
        "unique_words": len(set(t.get("text", "").split())),
        # bitrate is a decent proxy for how much is going on visually: a static
        # shot compresses far smaller than a busy one at fixed CRF
        "mb_per_sec": round(row["size_mb"] / dur, 3) if dur else 0.0,
    })
    return row


def main() -> int:
    folder = sys.argv[1] if len(sys.argv) > 1 else "out/reels"
    out_csv = sys.argv[2] if len(sys.argv) > 2 else "features.csv"
    tx = json.load(open("transcripts.json")) if os.path.exists("transcripts.json") else {}

    existing = {}
    if os.path.exists(out_csv):                       # never re-measure, and
        with open(out_csv, newline="") as f:          # never lose a row whose
            for r in csv.DictReader(f):               # file has since gone
                existing[r["clip"]] = r
    files = sorted(f for f in os.listdir(folder)
                   if f.lower().endswith((".mp4", ".mov")))
    rows = dict(existing)
    for i, f in enumerate(files, 1):
        base = os.path.splitext(f)[0]
        if base.endswith(".reel"):
            base = base[:-5]
        if base in rows:
            continue
        rows[base] = features_for(os.path.join(folder, f), tx)
        print(f"  [{i}/{len(files)}] {base}", flush=True)
    cols = ["clip", "compilation", "season", "episode", "duration_s",
            "width", "height",
            "size_mb", "bitrate_mbps", "mb_per_sec", "motion_mean", "motion_std",
            "cuts", "cuts_per_sec", "brightness", "contrast", "saturation",
            "loudness_db", "peak_db", "dynamic_range_db",
            "words", "words_per_sec", "unique_words"]
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for k in sorted(rows):
            w.writerow(rows[k])
    print(f"\n{len(rows)} row(s) -> {out_csv} "
          f"({len(rows)-len(existing)} newly measured)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
