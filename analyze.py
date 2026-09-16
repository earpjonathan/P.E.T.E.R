#!/usr/bin/env python3
"""What actually drives reel performance - with the confounds made explicit.

Two things make naive correlations here misleading:

  1. The account grew from nothing to >1400 followers across these posts, so a
     later post starts with more distribution than an earlier one. Post order
     is a confound on everything, and has to be partialled out.
  2. Plays are wildly skewed - a handful of reels are most of the total - so
     rank statistics and log targets, never raw means.

Honesty rule here: every model score is out-of-sample (K-fold), and compared
against a permutation baseline. An in-sample R^2 on 45 rows means nothing.
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from datetime import datetime

import numpy as np

HOOK_S = 3.0                     # the window that decides whether they stay
SW, SH = 128, 114


def hook_features(path: str) -> dict:
    """Measure only the opening seconds - retention is won or lost there."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                        "-show_entries", "stream=width,height", "-of", "csv=p=0",
                        path], capture_output=True, text=True)
    try:
        w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
    except ValueError:
        return {}
    v = subprocess.run(
        ["ffmpeg", "-v", "error", "-t", str(HOOK_S), "-i", path,
         "-vf", f"crop={w}:{h//2}:0:{h//2},fps=8,scale={SW}:{SH},format=rgb24",
         "-f", "rawvideo", "-"], capture_output=True).stdout
    n = len(v) // (SW * SH * 3)
    out = {}
    if n >= 2:
        a = np.frombuffer(v[:n*SW*SH*3], np.uint8).reshape(n, SH, SW, 3).astype(np.float32)
        lum = a.mean(axis=3)
        d = np.abs(np.diff(lum, axis=0)).mean(axis=(1, 2))
        out["hook_motion"] = round(float(d.mean()), 3)
        out["hook_cuts"] = int((d > max(12.0, float(np.median(d) * 4))).sum())
        out["hook_brightness"] = round(float(lum.mean()), 2)
    a = subprocess.run(["ffmpeg", "-v", "info", "-t", str(HOOK_S), "-i", path,
                        "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    for line in a.splitlines():
        if "mean_volume:" in line:
            try:
                out["hook_loudness_db"] = float(line.split("mean_volume:")[1].split("dB")[0])
            except ValueError:
                pass
    return out


def rank(x):
    return np.argsort(np.argsort(x)).astype(float)


def spearman(a, b):
    ok = ~(np.isnan(a) | np.isnan(b))
    if ok.sum() < 8:
        return float("nan")
    return float(np.corrcoef(rank(a[ok]), rank(b[ok]))[0, 1])


def partial_spearman(a, b, c):
    """Correlation of a and b with the effect of c removed, on ranks."""
    ok = ~(np.isnan(a) | np.isnan(b) | np.isnan(c))
    if ok.sum() < 10:
        return float("nan")
    ra, rb, rc = rank(a[ok]), rank(b[ok]), rank(c[ok])
    def resid(y):
        A = np.c_[np.ones(len(rc)), rc]
        return y - A @ np.linalg.lstsq(A, y, rcond=None)[0]
    return float(np.corrcoef(resid(ra), resid(rb))[0, 1])


def ridge_cv(X, y, folds=5, lam=1.0, seed=0):
    """Out-of-sample R^2. Anything else would flatter the model."""
    n = len(y)
    rng = np.random.default_rng(seed)
    idx = rng.permutation(n)
    preds = np.zeros(n)
    for f in range(folds):
        te = idx[f::folds]
        tr = np.setdiff1d(idx, te)
        mu, sd = X[tr].mean(0), X[tr].std(0) + 1e-9
        Xtr, Xte = (X[tr]-mu)/sd, (X[te]-mu)/sd
        ym = y[tr].mean()
        A = Xtr.T @ Xtr + lam*np.eye(X.shape[1])
        w = np.linalg.solve(A, Xtr.T @ (y[tr]-ym))
        preds[te] = Xte @ w + ym
    ss_res = ((y-preds)**2).sum()
    ss_tot = ((y-y.mean())**2).sum()
    return 1 - ss_res/ss_tot, preds


def source_popularity(rows) -> None:
    """Did the original uploader's view count predict ours?

    This is the one selection signal autopilot.py actually ranks on, and it was
    adopted as an explicit hypothesis, not a finding: ranking by *predicted*
    retention orders candidates at random with respect to plays (r = +0.02),
    so the ranker needed something independent of anything we can compute. The
    only honest way to find out if it works is to post and check.
    """
    links = {}
    if os.path.exists("post_links.csv"):
        for r in csv.DictReader(open("post_links.csv")):
            if r["clip_tag"]:
                links[r["media_id"]] = r["clip_tag"]
    src = json.load(open("source_meta.json")) if os.path.exists("source_meta.json") else {}

    print("\n" + "="*78)
    print("SOURCE POPULARITY  - does the uploader's view count predict ours?")
    print("="*78)
    if not links:
        print("  no post_links.csv yet - run link_posts.py first")
        return
    if not src:
        print("  no source_meta.json - only autopilot-harvested posts carry it")
        return

    pairs = []
    for r in rows:
        m = src.get(links.get(r["media_id"], ""))
        if not m or not m.get("source_views"):
            continue
        try:
            age, plays = float(r["age_h"]), float(r["plays"])
        except (ValueError, TypeError, KeyError):
            continue
        if age < 48 or plays <= 0:          # plays do not settle before 48h
            continue
        pairs.append((float(m["source_views"]),
                      float(m.get("source_vpd") or 0), plays))

    print(f"  linked posts with source data, matured >=48h : n = {len(pairs)}")
    if len(pairs) < 12:
        print("  Not enough to conclude anything. Needs ~20 autopilot posts;")
        print("  until then the ranker is an untested hypothesis, not a model.")
        return
    sv  = np.log10(np.array([p[0] for p in pairs]) + 1)
    vpd = np.log10(np.array([p[1] for p in pairs]) + 1)
    lp  = np.log10(np.array([p[2] for p in pairs]))
    print(f"  log(source views)     vs log(plays) : r = {spearman(sv, lp):+.2f}")
    print(f"  log(source views/day) vs log(plays) : r = {spearman(vpd, lp):+.2f}")
    print("  |r| below ~0.30 at this n is indistinguishable from no signal. If it")
    print("  stays there, drop the ranking and pick at random within the filters -")
    print("  the filters are what carry the pipeline either way.")


def main() -> int:
    rows = list(csv.DictReader(open("dataset.csv")))
    rows.sort(key=lambda r: r["timestamp"])

    cache = "hook_features.json"
    hooks = json.load(open(cache)) if os.path.exists(cache) else {}
    for i, r in enumerate(rows, 1):
        mid = r["media_id"]
        if mid not in hooks:
            hooks[mid] = hook_features(f"out/ig/ig_{mid}.mp4")
            print(f"  hook {i}/{len(rows)}", flush=True)
    json.dump(hooks, open(cache, "w"), indent=1)

    t0 = datetime.strptime(rows[0]["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
    for i, r in enumerate(rows):
        t = datetime.strptime(r["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        r["post_index"] = i
        r["hours_since_first"] = (t - t0).total_seconds()/3600
        r["hour_of_day"] = t.hour
        r.update(hooks.get(r["media_id"], {}))

    def col(c):
        return np.array([float(r[c]) if str(r.get(c, "")) not in ("", "nan", "None")
                         else np.nan for r in rows], float)

    plays = col("plays"); logp = np.log10(plays)
    ret = col("retention")
    order = col("post_index")

    FE = ["duration_s", "cuts_per_sec", "motion_mean", "motion_std", "brightness",
          "contrast", "saturation", "loudness_db", "dynamic_range_db",
          "words", "words_per_sec", "mb_per_sec",
          "hook_motion", "hook_cuts", "hook_brightness", "hook_loudness_db",
          "hour_of_day"]

    print("\n" + "="*78)
    print(f"CONFOUND CHECK   n={len(rows)}")
    print("="*78)
    print(f"  post order vs log(plays) : r = {spearman(order, logp):+.2f}"
          "    <- account growth over the run")
    print(f"  post order vs retention  : r = {spearman(order, ret):+.2f}")

    print("\n" + "="*78)
    print(f"{'feature':20}{'log(plays)':>12}{'partial':>10}{'retention':>12}{'partial':>10}")
    print(f"{'':20}{'':>12}{'(order out)':>10}{'':>12}{'(order out)':>10}")
    print("="*78)
    for c in FE:
        v = col(c)
        if np.isnan(v).all():
            continue
        a, ap = spearman(v, logp), partial_spearman(v, logp, order)
        b, bp = spearman(v, ret),  partial_spearman(v, ret,  order)
        def m(x): return "**" if abs(x) > 0.45 else ("* " if abs(x) > 0.29 else "  ")
        print(f"{c:20}{a:>11.2f}{m(a)}{ap:>9.2f}{m(ap)}{b:>11.2f}{m(b)}{bp:>9.2f}{m(bp)}")
    print("="*78)
    print("* uncorrected p<.05;  ** survives correction for ~17 features tested")

    # --- can any model predict, out of sample? ---
    use = [c for c in FE if not np.isnan(col(c)).all()]
    X = np.c_[[np.nan_to_num(col(c), nan=np.nanmean(col(c))) for c in use]].T
    print("\n" + "="*78)
    print("OUT-OF-SAMPLE PREDICTION (5-fold ridge, R^2 - negative = worse than"
          " guessing the mean)")
    print("="*78)
    for name, y in (("log10(plays)", logp), ("retention", ret)):
        r2, _ = ridge_cv(X, y)
        rng = np.random.default_rng(1)
        null = [ridge_cv(X, rng.permutation(y), seed=s)[0] for s in range(30)]
        print(f"  {name:14} CV R^2 = {r2:+.3f}    "
              f"shuffled baseline {np.mean(null):+.3f} +/- {np.std(null):.3f}")
        print(f"  {'':14} -> {'BEATS chance' if r2 > np.mean(null)+2*np.std(null) else 'NO better than chance'}")

    source_popularity(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
