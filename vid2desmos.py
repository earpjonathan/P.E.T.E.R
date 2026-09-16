#!/usr/bin/env python3
"""
vid2desmos - turn a video into a line-art animation that plays inside Desmos.

Pipeline:
    decode -> downscale -> edge map -> trace to polylines -> simplify (RDP)
    -> budget per frame -> quantize -> pack into flat Desmos lists -> emit graph

The whole animation is ONE parametric expression. Desmos plots a parametric
whose x/y are list-valued as many separate curves, so a single expression can
draw every segment of the current frame at once:

    R = [O[n] ... O[n+1]-1]        indices of frame n's points   (O(k) slice)
    S = R[D[R] > 0]                drop points that end a stroke
    ( (1-t)X[S] + tX[S+1], (1-t)Y[S] + tY[S+1] ),  0 <= t <= 1

A ticker steps n at the requested frame rate.
"""

from __future__ import annotations

import argparse
import base64
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass

import cv2
import numpy as np

DESMOS_API = "https://www.desmos.com/api/v1.11/calculator.js?apiKey=dcb31709b452b1cf9dc26972add0fda6"


# --------------------------------------------------------------------------
# edge tracing
# --------------------------------------------------------------------------

# neighbour offsets in a flattened, 1px-padded array, with their (dx, dy)
_DIRS = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))


def trace_edges(edge: np.ndarray) -> list[np.ndarray]:
    """Walk a thin binary edge map into open/closed polylines.

    Returns a list of (N, 2) float arrays of (x, y) pixel coordinates.

    Endpoints are seeded first so lines are traced end-to-end; whatever is left
    over is loops, which get seeded arbitrarily. Junctions simply split a stroke
    in two, which is visually identical once drawn.
    """
    h, w = edge.shape
    W = w + 2
    pad = np.zeros((h + 2, W), dtype=bool)
    pad[1:-1, 1:-1] = edge > 0
    flat = pad.ravel()

    offs = tuple(dy * W + dx for dx, dy in _DIRS)
    idxs = np.flatnonzero(flat)
    if idxs.size == 0:
        return []

    # 8-neighbour degree, used only to find line endpoints
    deg = np.zeros(flat.size, dtype=np.int16)
    for o in offs:
        deg[idxs] += flat[idxs + o]
    endpoints = idxs[deg[idxs] == 1]

    strokes: list[np.ndarray] = []
    for seeds in (endpoints, idxs):
        for seed in seeds:
            if not flat[seed]:
                continue
            flat[seed] = False
            path = [int(seed)]
            cur = int(seed)
            pdx = pdy = 0
            while True:
                best_k = -1
                best_score = -1e9
                for k in range(8):
                    c = cur + offs[k]
                    if flat[c]:
                        dx, dy = _DIRS[k]
                        # keep going as straight as possible, prefer orthogonal
                        score = dx * pdx + dy * pdy - (0.5 if dx and dy else 0.0)
                        if score > best_score:
                            best_score = score
                            best_k = k
                if best_k < 0:
                    break
                cur += offs[best_k]
                flat[cur] = False
                path.append(cur)
                pdx, pdy = _DIRS[best_k]

            if len(path) < 2:
                continue
            arr = np.asarray(path, dtype=np.int64)
            yy, xx = np.divmod(arr, W)
            pts = np.stack([xx - 1, yy - 1], axis=1).astype(np.float32)
            # close the loop if we walked back around to where we started
            if len(pts) > 2 and np.abs(pts[0] - pts[-1]).max() <= 1:
                pts = np.vstack([pts, pts[:1]])
            strokes.append(pts)
    return strokes


def contour_strokes(binary: np.ndarray) -> list[np.ndarray]:
    """Region outlines from a filled binary image (fast, C-speed)."""
    cnts, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    out = []
    for c in cnts:
        p = c.reshape(-1, 2).astype(np.float32)
        if len(p) >= 3:
            out.append(np.vstack([p, p[:1]]))  # close it
    return out


# --------------------------------------------------------------------------
# simplification
# --------------------------------------------------------------------------


def rdp(pts: np.ndarray, eps: float) -> np.ndarray:
    """Ramer-Douglas-Peucker, iterative so long strokes can't blow the stack."""
    n = len(pts)
    if n < 3:
        return pts
    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        seg = pts[j] - pts[i]
        rel = pts[i + 1 : j] - pts[i]
        l2 = float(seg @ seg)
        if l2 == 0.0:
            d = np.hypot(rel[:, 0], rel[:, 1])
        else:
            d = np.abs(rel[:, 0] * seg[1] - rel[:, 1] * seg[0]) / np.sqrt(l2)
        k = int(np.argmax(d))
        if d[k] > eps:
            m = i + 1 + k
            keep[m] = True
            stack.append((i, m))
            stack.append((m, j))
    return pts[keep]


W_MAX = 4  # tangent weights are stored as small ints 0..4 to keep the text short


def tangent_weights(pts: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """Per-point tangent strength: 0 = crisp join, W_MAX = full Catmull-Rom.

    This is what stops the drawing looking uniformly "melted". A vertex only
    earns curvature if its turn lands in a middle band:

      turn < lo   -> a straight run. Smoothing it just adds wobble.   -> 0
      lo..hi      -> a genuine curve (a cheek, an ear, a cushion).    -> W_MAX
      turn > hi   -> a real corner (door frame, teeth). Keep it sharp -> 0

    Weight 0 collapses that end's Bezier control point onto the anchor, so the
    segment is drawn exactly straight. Curvature therefore becomes per-vertex
    data rather than a global mode, and it costs one small int per point.
    """
    n = len(pts)
    w = np.zeros(n, dtype=np.int32)
    if n < 3:
        return w
    v = np.diff(pts, axis=0).astype(np.float64)
    ln = np.linalg.norm(v, axis=1, keepdims=True)
    ln[ln == 0] = 1.0
    u = v / ln
    dot = np.clip(np.sum(u[:-1] * u[1:], axis=1), -1.0, 1.0)
    turn = np.degrees(np.arccos(dot))              # 0 = dead straight

    # trapezoid: ramp in above `lo`, ramp back out approaching `hi`
    ramp_in = np.clip((turn - lo) / 8.0, 0.0, 1.0)
    ramp_out = np.clip((hi - turn) / 10.0, 0.0, 1.0)
    w[1:-1] = np.rint(np.minimum(ramp_in, ramp_out) * W_MAX)
    return w                                        # stroke ends stay 0


def polyline_length(pts: np.ndarray) -> float:
    if len(pts) < 2:
        return 0.0
    return float(np.hypot(*(np.diff(pts, axis=0).T)).sum())


# --------------------------------------------------------------------------
# per-frame processing
# --------------------------------------------------------------------------


@dataclass
class Opts:
    mode: str
    grid: float
    scale: float          # px -> grid units
    eps: float
    min_len: float
    budget: int
    blur: int
    low: int
    high: int
    sigma: float
    edge_density: float   # target fraction of pixels that are edges
    invert: bool
    height: int           # working frame height, for the y flip
    smooth: bool
    turn_lo: float        # below this many degrees a join stays straight
    turn_hi: float        # above this it's a corner and stays sharp
    detail_share: float   # fraction of the budget reserved for busy areas
    eps_lo: float         # eps multiplier where motion is high (more detail)
    eps_hi: float         # eps multiplier where nothing moves (less detail)


def frame_to_strokes(item, o: Opts) -> list[np.ndarray]:
    gray, motion = item
    if o.blur >= 3:
        k = o.blur | 1
        gray = cv2.GaussianBlur(gray, (k, k), 0)

    if o.mode == "binary":
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if o.invert:
            bw = 255 - bw
        strokes = contour_strokes(bw)
    else:
        if o.low < 0 or o.high < 0:
            # Aim for a fixed AMOUNT of linework rather than a fixed threshold.
            #
            # The usual median*(1±sigma) rule is built for photographs; on a
            # bright flat-shaded cartoon it lands near 115/228 and keeps only
            # the boldest outlines, dropping every interior line - eyes, mouths,
            # fingers - which is the part carrying the joke. But a fixed low
            # threshold is just as wrong the other way: a tiled floor or a
            # panelled wall then floods the frame with faint repeating lines
            # and buries the characters.
            #
            # Targeting an edge-pixel budget adapts to both. In a sparse
            # close-up it digs down and finds the face; in a busy room it backs
            # off to the bold character outlines, which are the strongest edges
            # present.
            gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
            gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
            mag = cv2.magnitude(gx, gy)
            qlo, qhi = 0.80, 0.995
            edge = None
            for _ in range(5):
                q = 0.5 * (qlo + qhi)
                hi = float(np.clip(np.quantile(mag, q), 25.0, 240.0))
                edge = cv2.Canny(gray, hi / 3.0, hi, L2gradient=True)
                frac = float((edge > 0).mean())
                if frac > o.edge_density:
                    qlo = q          # too much linework, raise the threshold
                else:
                    qhi = q
        else:
            edge = cv2.Canny(gray, o.low, o.high, L2gradient=True)
        strokes = trace_edges(edge)

    # How much each stroke matters.
    #
    # Local edge DENSITY does the heavy lifting: a face packs eyes, mouth, chin
    # and hair into a small area, while a couch is one long isolated line. That
    # holds on a still frame, which matters because these characters spend most
    # of their time holding a pose - measured on this footage, the frame-to-
    # frame difference is flat zero (the show animates on twos) and even a 0.3s
    # baseline barely registers. Motion is therefore only a bonus on top.
    if o.mode == "binary":
        edge = cv2.morphologyEx(bw, cv2.MORPH_GRADIENT, np.ones((3, 3), np.uint8))
    dens = cv2.boxFilter((edge > 0).astype(np.float32), -1, (21, 21),
                         normalize=True)
    q = float(np.quantile(dens, 0.995))
    dens = np.clip(dens / q, 0, 1) if q > 1e-4 else np.zeros_like(dens)
    if motion is not None:
        dens = np.clip(dens + 0.5 * motion, 0, 1)

    def importance(px: np.ndarray) -> float:
        h, w = dens.shape
        xs = np.clip(px[:, 0].astype(np.int32), 0, w - 1)
        ys = np.clip(px[:, 1].astype(np.int32), 0, h - 1)
        v = dens[ys, xs]
        # upper quantile, not the mean: a long contour that clips a busy area
        # at one end is still worth keeping
        return float(np.quantile(v, 0.75)) if v.size else 0.0

    # px -> desmos grid units, y flipped so the image is upright
    out = []
    for s in strokes:
        if polyline_length(s) < o.min_len:
            continue
        imp = importance(s)
        g = np.empty_like(s)
        g[:, 0] = s[:, 0] * o.scale
        g[:, 1] = (o.height - 1 - s[:, 1]) * o.scale
        # Spend precision where it matters: simplify the busy parts less and
        # the static background more, which costs nothing overall.
        g = rdp(g, o.eps * (o.eps_hi + (o.eps_lo - o.eps_hi) * imp))
        if len(g) >= 2:
            out.append((g, imp))

    # Over budget? Coarsen everything first. Losing detail uniformly reads far
    # better than deleting whole shapes, so dropping strokes is the last resort.
    # A gentle ratio matters: coarsening in big jumps overshoots the budget and
    # throws away detail that would have fit.
    segs = sum(len(g) - 1 for g, _ in out)
    eps = o.eps
    for _ in range(12):
        if segs <= o.budget:
            break
        eps *= 1.25
        out = [(g, i) for g, i in ((rdp(g, eps * (o.eps_hi + (o.eps_lo - o.eps_hi) * i)), i)
                                   for g, i in out) if len(g) >= 2]
        segs = sum(len(g) - 1 for g, _ in out)

    if segs > o.budget:
        # Two passes. Reserve most of the budget for the strokes that matter,
        # then spend what is left on the longest remaining ones so the scene
        # still has structure.
        #
        # Ranking purely by length - which is what this used to do - deletes
        # faces first and keeps furniture: a face is a cluster of short strokes
        # while a couch is one long contour.
        kept, used, taken = [], 0, set()
        reserve = int(o.budget * o.detail_share)
        for order, cap in ((sorted(range(len(out)), key=lambda k: -out[k][1]), reserve),
                           (sorted(range(len(out)), key=lambda k: -polyline_length(out[k][0])),
                            o.budget)):
            for k in order:
                if k in taken:
                    continue
                c = len(out[k][0]) - 1
                if used + c > cap:
                    continue
                taken.add(k)
                kept.append(out[k])
                used += c
        out = kept

    out = [g for g, _ in out]

    # quantize, drop points that collapsed onto each other, then score the
    # curvature of what survived. Weights are computed last so they describe
    # the geometry Desmos will actually receive.
    final = []
    for s in out:
        q = np.rint(s).astype(np.int32)
        keepm = np.ones(len(q), dtype=bool)
        keepm[1:] = np.any(q[1:] != q[:-1], axis=1)
        q = q[keepm]
        if len(q) < 2:
            continue
        w = (tangent_weights(q, o.turn_lo, o.turn_hi) if o.smooth
             else np.zeros(len(q), dtype=np.int32))
        final.append(np.column_stack([q, w]).astype(np.int32))
    return final


_WORKER_OPTS: Opts | None = None


def _init_worker(o: Opts) -> None:
    global _WORKER_OPTS
    _WORKER_OPTS = o
    cv2.setNumThreads(1)


def _work(gray: np.ndarray) -> list[np.ndarray]:
    assert _WORKER_OPTS is not None
    return frame_to_strokes(gray, _WORKER_OPTS)


# --------------------------------------------------------------------------
# decoding
# --------------------------------------------------------------------------


def sample_frames(path: str, fps: float, width: int, start: float, duration: float,
                  crop=None):
    """Yield downscaled grayscale frames at `fps`, plus (w, h) of the first."""
    if is_image(path):
        # a still image is just a one-frame movie; everything downstream works
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            raise SystemExit(f"could not read image: {path}")
        h, w = img.shape[:2]
        nh = max(2, int(round(width * h / w)))
        yield cv2.cvtColor(cv2.resize(img, (width, nh), interpolation=cv2.INTER_AREA),
                           cv2.COLOR_BGR2GRAY)
        return

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"could not open video: {path}")
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if not np.isfinite(src_fps) or src_fps <= 0:
        src_fps = 30.0
    if start > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)

    step = src_fps / fps
    next_at = 0.0
    i = 0
    size = None
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if duration > 0 and i / src_fps > duration:
            break
        if i >= next_at:
            next_at += step
            if crop:
                cw, ch, cx, cy = crop
                frame = frame[cy:cy + ch, cx:cx + cw]
            h, w = frame.shape[:2]
            if size is None:
                nh = max(2, int(round(width * h / w)))
                size = (width, nh)
            small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
            yield cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        i += 1
    cap.release()


def transcribe(video: str, cache: str, model: str = "base.en"):
    """Dialogue with timestamps, cached next to the outputs.

    Returns None if faster-whisper isn't installed, so scene detection can fall
    back to pure vision.
    """
    if os.path.exists(cache):
        with open(cache) as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get("v") == 2:
            return d
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    print(f"  transcribing with whisper {model} (once, then cached) ...",
          flush=True)
    m = WhisperModel(model, device="cpu", compute_type="int8")
    # Word timestamps matter here. Segment boundaries are padded by the VAD, so
    # a line can appear to run a second past where the speaker actually stopped
    # and mask a real cut just after it.
    segs, _ = m.transcribe(video, vad_filter=True, word_timestamps=True,
                           vad_parameters={"min_silence_duration_ms": 300})
    words, lines = [], []
    for s in segs:
        lines.append({"start": round(s.start, 2), "end": round(s.end, 2),
                      "text": s.text.strip()})
        for w in (s.words or []):
            words.append([round(w.start, 2), round(w.end, 2)])
    d = {"v": 2, "words": words, "segments": lines}
    with open(cache, "w") as f:
        json.dump(d, f, indent=1)
    print(f"  {len(lines)} speech segments, {len(words)} words")
    return d


def speech_gap(speech, t: float) -> float:
    """Length of the pause in dialogue containing t; 0 if someone is talking."""
    words = speech["words"] if isinstance(speech, dict) else \
        [[s["start"], s["end"]] for s in speech]
    for a, b in words:
        if a <= t <= b:
            return 0.0
    before = max((b for a, b in words if b <= t), default=0.0)
    after = min((a for a, b in words if a >= t), default=t)
    return max(0.0, after - before)


def find_cuts(path: str, threshold: float):
    """Frame indices of hard cuts, de-duplicated, plus fps and frame count."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit(f"could not open video: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    prev, scores = None, []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        h = cv2.cvtColor(cv2.resize(f, (213, 120)), cv2.COLOR_BGR2HSV).astype(np.int16)
        if prev is not None:
            scores.append(float(np.abs(h - prev).mean()))
        prev = h
    cap.release()
    if not scores:
        return [], fps, 0

    # One transition often trips the detector on several consecutive frames
    cuts = []
    for i, v in enumerate(scores):
        if v > threshold and (not cuts or (i + 1 - cuts[-1]) / fps > 0.4):
            cuts.append(i + 1)
    return cuts, fps, len(scores) + 1


LLM_PROMPT = """Family Guy compilations are built from separate clips joined end to end.

Inside a single clip the picture cuts constantly: between camera angles, and \
into short cutaway gags (a flashback, a fantasy, a "this is like the time..." \
joke). A cutaway looks totally unrelated but is STILL PART OF THE SAME JOKE - \
it is the punchline to the line right before it.

Below is the dialogue on either side of one picture cut.

--- BEFORE the cut ---
{before}

--- AFTER the cut ---
{after}

Decide:
SAME - the lines after continue, illustrate or pay off what came before \
(same conversation, a cutaway, a flashback, its punchline).
NEW  - a completely different clip begins: different characters, different \
situation, and nothing after refers to anything before.

Reply with one short reason (max 12 words), then on the last line write \
exactly SAME or NEW."""


def llm_joke_cuts(cuts_s, speech, cache: str, model: str, window: float = 14.0):
    """Ask a local LLM which scene changes actually start a new joke.

    Every low-level signal fails on cutaway gags by construction: the cutaway is
    set somewhere unrelated to its setup, so it looks like a hard cut, sounds
    like a hard cut, and (measured on this footage) its dialogue pause is
    *shorter* than the ones inside a scene. Only the meaning of the lines says
    where a joke ends, so that is what gets asked.
    """
    done = {}
    if os.path.exists(cache):
        with open(cache) as f:
            done = json.load(f)

    todo = [t for t in cuts_s if f"{t:.2f}" not in done]
    if todo:
        try:
            from mlx_lm import load, generate
        except ImportError:
            return None
        print(f"  asking {model} about {len(todo)} scene changes ...", flush=True)
        m, tok = load(model)
        lines = speech["segments"] if isinstance(speech, dict) else speech
        for i, t in enumerate(todo, 1):
            before = "\n".join(s["text"] for s in lines
                               if t - window <= s["end"] <= t) or "(silence)"
            after = "\n".join(s["text"] for s in lines
                              if t <= s["start"] <= t + window) or "(silence)"
            msg = [{"role": "user", "content": LLM_PROMPT.format(
                before=before[-900:], after=after[:900])}]
            p = tok.apply_chat_template(msg, add_generation_prompt=True,
                                        tokenize=False)
            # a brief reason before the label measurably improves accuracy,
            # so take the LAST label word rather than the first match
            out = generate(m, tok, prompt=p, max_tokens=60, verbose=False)
            tags = [w.strip(".:*") for w in out.replace("*", "").split()
                    if w.strip(".:*") in ("SAME", "NEW")]
            done[f"{t:.2f}"] = tags[-1] if tags else "NEW"
            print(f"\r    {i}/{len(todo)}", end="", flush=True)
        print()
        with open(cache, "w") as f:
            json.dump(done, f, indent=1)

    return [t for t in cuts_s if done.get(f"{t:.2f}") == "NEW"]


def detect_scenes(path: str, threshold: float, min_len: float, max_len: float,
                  speech=None, min_gap: float = 1.2, llm_cuts=None):
    """Find hard cuts, then group shots into clip-sized scenes.

    The score is the mean absolute HSV difference between consecutive frames —
    a cut moves every pixel at once and spikes far above normal motion. Adjacent
    shots are merged until a scene reaches `min_len` (a compilation cuts between
    camera angles inside one gag), and anything past `max_len` is split.
    """
    cuts, fps, total = find_cuts(path, threshold)
    if total <= 1:
        return [], fps

    # Keep only the cuts where the DIALOGUE also stops.
    #
    # A cutaway gag is set somewhere deliberately unrelated to its setup, so no
    # visual or audio measure can tell it from a real clip boundary. The speech
    # can: a joke runs continuously and ends with a real pause, while a cut
    # inside a joke lands mid-sentence or in a beat of well under a second.
    # Measured on a Family Guy compilation, 26% of visual cuts have someone
    # talking straight through them, and the cut that wrongly split the bar
    # scene from its caveman punchline sits in a 0.78s pause against 1.6-6.9s
    # at genuine boundaries.
    if llm_cuts is not None:
        keep = set(round(t, 2) for t in llm_cuts)
        cuts = [c for c in cuts if round(c / fps, 2) in keep]
        bounds = [0]
        for c in cuts:
            if (c - bounds[-1]) / fps >= min_len:
                bounds.append(c)
        bounds.append(total)
    elif speech and min_gap > 0:
        # Rank by pause length and take the strongest first, keeping every
        # boundary at least min_len apart. A plain threshold doesn't work:
        # comedy is full of pauses, and a 1.6s beat inside a gag outranks
        # nothing while sitting close to a 2.1s pause that is the real join.
        # Taking the strongest first lets the genuine boundary win its
        # neighbourhood and suppress the beat next to it.
        span = min_len * fps
        cand = sorted(((c, speech_gap(speech, c / fps)) for c in cuts),
                      key=lambda x: -x[1])
        keep: list[int] = []
        for c, g in cand:
            if g < min_gap or c < span or (total - c) < span:
                continue
            if all(abs(c - k) >= span for k in keep):
                keep.append(c)
        bounds = [0] + sorted(keep) + [total]
    else:
        bounds = [0]
        for c in cuts:
            if (c - bounds[-1]) / fps >= min_len:
                bounds.append(c)
        bounds.append(total)

    # Split over-long scenes into equal parts. Chopping max_len off the front
    # instead leaves a sliver at the end (a 31s scene became 30s + 1s).
    scenes = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        dur = (b - a) / fps
        if dur < 1.0:
            continue
        parts = max(1, int(np.ceil(dur / max_len)))
        step = (b - a) / parts
        for k in range(parts):
            scenes.append(((a + k * step) / fps, (a + (k + 1) * step) / fps))
    return scenes, fps


def contact_sheet(video: str, scenes, path: str, cols: int = 6, tw: int = 320):
    """Numbered thumbnail per scene, so a bad split is obvious at a glance."""
    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    tiles = []
    for a, b in scenes:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int((a + min(1.0, (b - a) / 3)) * fps))
        ok, f = cap.read()
        if not ok:
            f = np.full((180, tw, 3), 40, np.uint8)
        tiles.append(cv2.resize(f, (tw, int(tw * f.shape[0] / f.shape[1]))))
    cap.release()
    if not tiles:
        return False

    th = max(t.shape[0] for t in tiles)
    rows = (len(tiles) + cols - 1) // cols
    sheet = np.full((rows * (th + 26), cols * tw, 3), 255, np.uint8)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        y, x = r * (th + 26), c * tw
        sheet[y + 26:y + 26 + t.shape[0], x:x + t.shape[1]] = t
        a, b = scenes[i]
        cv2.putText(sheet, f"{i+1}  {a:.1f}-{b:.1f}s ({b-a:.1f})",
                    (x + 6, y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1,
                    cv2.LINE_AA)
    cv2.imwrite(path, sheet)
    return True


def batched(it, n):
    buf = []
    for x in it:
        buf.append(x)
        if len(buf) == n:
            yield buf
            buf = []
    if buf:
        yield buf


# --------------------------------------------------------------------------
# desmos graph construction
# --------------------------------------------------------------------------


# Desmos refuses any list longer than 10,000 elements, so frames are split
# across parallel sets of lists ("chunks"). Each chunk draws only while the
# current frame falls inside it.
CHUNK_CAP = 9000


BREAK = W_MAX + 1  # sentinel in the control list: "no segment starts here"


def frames_match(a: list[np.ndarray], b: list[np.ndarray], tol: float) -> bool:
    """True when two traced frames would draw the same picture."""
    if len(a) != len(b):
        return False
    for p, q in zip(a, b):
        if len(p) != len(q):
            return False
        if tol <= 0:
            if not np.array_equal(p[:, :2], q[:, :2]):
                return False
        elif np.abs(p[:, :2] - q[:, :2]).max() > tol:
            return False
    return True


def dedupe_traced(traced: list[list[np.ndarray]], tol: float):
    """Collapse only frames that trace to the same geometry.

    Comparing the drawn output rather than the source pixels is the whole
    point: a mean-difference test on pixels dilutes small movements (a mouth,
    a hand) across the whole frame and silently drops them, which reads as a
    low frame rate no matter how fast `n` advances. If the lines move, the
    frame is kept.
    """
    uniq: list[list[np.ndarray]] = []
    fids: list[int] = []
    for f in traced:
        if uniq and frames_match(uniq[-1], f, tol):
            fids.append(len(uniq) - 1)
        else:
            uniq.append(f)
            fids.append(len(uniq) - 1)
    return uniq, fids


def pack_frame(strokes: list[np.ndarray], smooth: bool):
    """Flatten one frame's strokes into (xs, ys, cs).

    `cs` carries two things at once. For a point that starts a drawn segment it
    holds that point's tangent weight (0..W_MAX); for the last point of a stroke
    it holds BREAK. So one list encodes both where strokes end and how curved
    each join is, which is a whole list cheaper than keeping them separate.
    Reading the far end's weight as mod(c, BREAK) turns the sentinel back into
    the 0 that a stroke end should have anyway.
    """
    if not strokes:
        # never let a frame be empty: the offset list must stay strictly
        # increasing or the range [O[j]...O[j+1]-1] would count backwards
        strokes = [np.zeros((2, 3), dtype=np.int32)]
    xs, ys, cs = [], [], []
    for s in strokes:
        xs.extend(int(v) for v in s[:, 0])
        ys.extend(int(v) for v in s[:, 1])
        w = s[:, 2] if smooth else np.zeros(len(s), dtype=np.int32)
        cs.extend(int(v) for v in w[:-1])
        cs.append(BREAK)                       # last point of the stroke
    return xs, ys, cs


def build_state(uniq, fids, fps, xmax, ymax, color, width, smooth=True):
    """`uniq` holds the distinct traced frames; `fids[k]` is the one frame k shows.

    Held frames cost nothing but an offset: several frame numbers simply point
    at the same span of the point list. That is what makes a high frame rate
    affordable on animation, which is full of frames that don't change.
    """
    packed = [pack_frame(s, smooth) for s in uniq]

    # Pack frames into chunks under the list-length cap. A frame whose data is
    # already in the current chunk adds nothing.
    chunks, cur, seen, used = [], [], set(), 0
    for f, uid in enumerate(fids):
        add = 0 if uid in seen else len(packed[uid][0])
        if cur and used + add > CHUNK_CAP:
            chunks.append(cur)
            cur, seen, used = [], set(), 0
            add = len(packed[uid][0])
        cur.append(f)
        seen.add(uid)
        used += add
    if cur:
        chunks.append(cur)

    nf = len(fids)
    lst = lambda v: "\\left[" + ",".join(map(str, v)) + "\\right]"
    folder = "f_data"
    exprs = []
    total_pts = 0

    for ci, idxs in enumerate(chunks, start=1):
        # One dummy point at each end of the chunk. A segment starting at S
        # reads S-1 and S+2, and these guarantee both stay inside the list
        # without padding every single stroke.
        cx, cy, cd = [0], [0], [BREAK]
        starts, ends, local = [], [], {}
        for f in idxs:
            uid = fids[f]
            if uid not in local:
                lo = len(cx) + 1              # desmos lists are 1-indexed
                xs, ys, ds = packed[uid]
                cx.extend(xs); cy.extend(ys); cd.extend(ds)
                local[uid] = (lo, len(cx))
            lo, hi = local[uid]
            starts.append(lo)
            ends.append(hi)
        cx.append(0); cy.append(0); cd.append(BREAK)
        total_pts += len(cx)

        a = idxs[0] + 1     # first frame of this chunk (1-based)
        b = idxs[-1] + 1    # last frame
        k = a - 1           # global -> local frame shift
        m = len(idxs)
        s = "_{%d}" % ci
        X, Y, D, O = "X" + s, "Y" + s, "D" + s, "O" + s
        E, J, G, R, S = "E" + s, "J" + s, "G" + s, "R" + s, "S" + s

        # weights at the two ends of each segment; mod turns the BREAK
        # sentinel at a stroke's last point back into a weight of 0
        wa = "%s\\left[%s\\right]" % (D, S)
        wb = "\\operatorname{mod}\\left(%s\\left[%s+1\\right],%d\\right)" % (D, S, BREAK)

        def lerp(name):
            if smooth:
                # all four control points come straight out of the flat list
                return ("K\\left(%s\\left[%s-1\\right],%s\\left[%s\\right],"
                        "%s\\left[%s+1\\right],%s\\left[%s+2\\right],%s,%s,t\\right)" % (
                            name, S, name, S, name, S, name, S, wa, wb))
            return ("\\left(1-t\\right)%s\\left[%s\\right]"
                    "+t%s\\left[%s+1\\right]" % (name, S, name, S))

        exprs += [
            {"type": "expression", "id": "d_x%d" % ci, "folderId": folder,
             "latex": X + "=" + lst(cx), "hidden": True},
            {"type": "expression", "id": "d_y%d" % ci, "folderId": folder,
             "latex": Y + "=" + lst(cy), "hidden": True},
            {"type": "expression", "id": "d_d%d" % ci, "folderId": folder,
             "latex": D + "=" + lst(cd), "hidden": True},
            {"type": "expression", "id": "d_o%d" % ci, "folderId": folder,
             "latex": O + "=" + lst(starts), "hidden": True},
            # explicit end offsets: with held frames sharing data, the starts
            # are no longer monotonic, so the next frame's start can't serve
            # as this frame's end
            {"type": "expression", "id": "d_e%d" % ci, "folderId": folder,
             "latex": E + "=" + lst(ends), "hidden": True},
            # J clamps into range so R is ALWAYS a valid ascending range.
            # Wrapping the range itself in a piecewise does not work: Desmos
            # still evaluates the branch and errors with non-arithmetic-range.
            {"type": "expression", "id": "e_j%d" % ci, "folderId": folder,
             "latex": "%s=\\min\\left(\\max\\left(n-%d,1\\right),%d\\right)"
                      % (J, k, m), "hidden": True},
            # G is 1 inside this chunk and undefined outside, which blanks the
            # curve without disturbing any index arithmetic.
            {"type": "expression", "id": "e_g%d" % ci, "folderId": folder,
             "latex": "%s=\\left\\{%d\\le n\\le %d:1\\right\\}" % (G, a, b),
             "hidden": True},
            {"type": "expression", "id": "e_r%d" % ci, "folderId": folder,
             "latex": "%s=\\left[%s\\left[%s\\right]...%s\\left[%s\\right]\\right]"
                      % (R, O, J, E, J), "hidden": True},
            {"type": "expression", "id": "e_s%d" % ci, "folderId": folder,
             "latex": "%s=%s\\left[%s\\left[%s\\right]<%d\\right]" % (S, R, D, R, BREAK),
             "hidden": True},
            {"type": "expression", "id": "e_draw%d" % ci,
             "latex": "\\left(%s\\left(%s\\right),%s\\left(%s\\right)\\right)"
                      % (G, lerp(X), G, lerp(Y)),
             # note: lineWidth/fillOpacity are latex STRINGS in graph state.
             # passing numbers here crashes the Desmos evaluation worker.
             "color": color, "lines": True, "points": False,
             "lineWidth": "%g" % width, "fillOpacity": "0",
             "parametricDomain": {"min": "0", "max": "1"}},
        ]

    head = [
        # the folder MUST stay expanded and non-secret: Desmos skips evaluating
        # expressions whose rows aren't rendered, so collapsing it blanks the
        # whole graph. Big lists show as a truncated "N element list" chip
        # anyway, so leaving it open costs nothing.
        {"type": "folder", "id": folder, "collapsed": False,
         "title": "frame data - %s points, %d chunk(s)" % (format(total_pts, ","), len(chunks))},
        {"type": "expression", "id": "e_nf", "latex": "N_{f}=%d" % nf},
        {"type": "expression", "id": "e_n", "latex": "n=1",
         "slider": {"hardMin": True, "hardMax": True, "min": "1",
                    "max": str(nf), "step": "1"}},
    ]
    if smooth:
        # Catmull-Rom through b and c as the equivalent cubic Bezier, with the
        # two tangents scaled by per-vertex weights p and q (0..W_MAX, hence the
        # 6*W_MAX divisor). p=q=0 pulls both control points onto the anchors,
        # which draws an exactly straight segment - so one expression covers
        # curves and straight lines, chosen per vertex by the data.
        head.append({
            "type": "expression", "id": "e_k",
            "latex": ("K\\left(a,b,c,d,p,q,t\\right)=\\left(1-t\\right)^{3}b"
                      "+3\\left(1-t\\right)^{2}t\\left(b+\\frac{p\\left(c-a\\right)}{%d}\\right)"
                      "+3\\left(1-t\\right)t^{2}\\left(c-\\frac{q\\left(d-b\\right)}{%d}\\right)"
                      "+t^{3}c") % (6 * W_MAX, 6 * W_MAX),
        })

    pad = 0.02 * xmax
    state = {
        "version": 11,
        "graph": {
            "viewport": {"xmin": -pad, "xmax": xmax + pad,
                         "ymin": -pad, "ymax": ymax + pad},
            "showGrid": False, "showXAxis": False, "showYAxis": False,
            "xAxisNumbers": False, "yAxisNumbers": False,
            "squareAxes": True,
        },
        "expressions": {
            "list": head + exprs,
            "ticker": {
                "handlerLatex": "n\\to\\operatorname{mod}\\left(n,N_{f}\\right)+1",
                "minStepLatex": "%.4f" % (1000.0 / fps),
                "open": True, "playing": True,
            },
        },
    }
    return state, total_pts, len(chunks)


# Shared frame driver. The graph carries a Desmos ticker, which is the
# idiomatic (and persistent) way to animate. But a ticker only advances while
# the tab is visible and painting, and it does not always resume from a
# setState. This watchdog waits, checks whether the ticker actually moved, and
# only then falls back to stepping `n` from JS.
DRIVER_JS = """
  function nLatex(c) {
    var e = c.getState().expressions.list.filter(function (x) { return x.id === "e_n"; })[0];
    return e ? e.latex : null;
  }
  function tickerOn(c) {
    var t = c.getState().expressions.ticker;
    return !!(t && t.playing);
  }
  function setTicker(c, on) {
    var s = c.getState();
    if (s.expressions.ticker) { s.expressions.ticker.playing = on; c.setState(s); }
  }
  function makeDriver(c, nf, fps) {
    var loop = null, frame = 1;
    function stop() { if (loop) { clearInterval(loop); loop = null; } }
    function start() {
      stop();
      loop = setInterval(function () {
        frame = (frame % nf) + 1;
        c.setExpression({ id: "e_n", latex: "n=" + frame });
      }, 1000 / fps);
    }
    // if the ticker is doing its job, stay out of the way
    var before = nLatex(c);
    setTimeout(function () {
      if (!loop && nLatex(c) === before) start();
    }, 1500);
    return {
      playing: function () { return loop !== null || tickerOn(c); },
      pause: function () { stop(); setTicker(c, false); },
      play: function () { if (!tickerOn(c)) start(); },
      seek: function (k) { stop(); setTicker(c, false); frame = k;
                           c.setExpression({ id: "e_n", latex: "n=" + k }); }
    };
  }
"""


JS_TEMPLATE = """/* {name} - {nf} frames @ {fps} fps, {pts} points
 *
 * 1. open https://www.desmos.com/calculator
 * 2. open the browser console (Cmd+Option+J)
 * 3. paste this whole file and press enter
 *    (Chrome may make you type "allow pasting" first)
 */
(function () {{
  var state = {state};
  if (typeof Calc === "undefined") {{
    console.error("no `Calc` here - run this on https://www.desmos.com/calculator");
    return;
  }}
{driver}
  Calc.setState(state);
  makeDriver(Calc, {nf}, {fps});
  console.log("loaded {nf} frames / {pts} points");
}})();
"""


HTML_TEMPLATE = """<!doctype html>
<meta charset="utf-8">
<title>{name} - desmos</title>
<style>
  html,body{{margin:0;height:100%;background:#101010;color:#e8e8e8;
    font:13px/1.5 ui-sans-serif,-apple-system,system-ui,sans-serif}}
  #calc{{position:absolute;inset:0 0 40px 0}}
  #bar{{position:absolute;left:0;right:0;bottom:0;height:40px;display:flex;
    align-items:center;gap:12px;padding:0 12px;box-sizing:border-box;
    border-top:1px solid #2a2a2a}}
  button{{font:inherit;background:#1d1d1d;color:#e8e8e8;border:1px solid #3a3a3a;
    border-radius:5px;padding:3px 11px;cursor:pointer}}
  button:hover{{background:#282828}}
  #scrub{{flex:1;accent-color:#7aa7d9}}
  span{{color:#8a8a8a;white-space:nowrap}}
  /* ?capture=1 strips the chrome so the offline renderer records just Desmos */
  body.capture #bar{{display:none}}
  body.capture #calc{{inset:0}}
</style>
<div id="calc"></div>
<div id="bar">
  <button id="play">play</button>
  <input id="scrub" type="range" min="1" max="{nf}" value="1" step="1">
  <span id="lbl"></span>
  <button id="panel">expressions</button>
</div>
<script src="{api}"></script>
<script>
  var state = {state};
  var AUDIO = {audio};
  var NF = {nf}, FPS = {fps};
  var CAPTURE = /[?&]capture=1/.test(location.search);
  if (CAPTURE) {{ document.body.className = "capture"; AUDIO = null; }}
  // the panel starts open on purpose: Desmos only evaluates expressions whose
  // rows are rendered, so hiding it can blank the graph.
  var showPanel = true;
  var calc = Desmos.GraphingCalculator(document.getElementById("calc"), {{
    expressions: showPanel, settingsMenu: false, zoomButtons: true, border: false
  }});
  calc.setState(state);
{driver}
  var play = document.getElementById("play"),
      scrub = document.getElementById("scrub"),
      lbl = document.getElementById("lbl"),
      shown = 0, drv = null, audio = null;

  function draw(k) {{
    if (k === shown) return;
    shown = k;
    calc.setExpression({{ id: "e_n", latex: "n=" + k }});
  }}

  if (AUDIO) {{
    // The soundtrack is the clock, and the frame is recomputed from
    // currentTime every time the browser is about to paint.
    //
    // This MUST be driven by requestAnimationFrame rather than a timer. A timer
    // keeps firing at its own rate whether or not the graph has finished
    // drawing, so updates pile up and the picture falls further and further
    // behind the sound. rAF only fires when the browser is ready to paint, so
    // we can never outrun the renderer: if Desmos is slow the extra frames are
    // skipped, and what you see is still whatever the audio is playing now.
    setTicker(calc, false);
    audio = new Audio(AUDIO);
    audio.preload = "auto";
    var frameNow = function () {{
      return Math.min(NF, Math.floor(audio.currentTime * FPS) + 1);
    }};
    var lastRaf = 0;
    (function loop() {{
      requestAnimationFrame(loop);
      lastRaf = performance.now();
      if (!audio.paused) draw(frameNow());
    }})();
    // Watchdog for when rAF is throttled to nothing (background tab, or a
    // window that isn't compositing). Without this the picture would freeze
    // while the sound kept playing.
    setInterval(function () {{
      if (!audio.paused && performance.now() - lastRaf > 250) draw(frameNow());
    }}, 250);
    setInterval(function () {{
      scrub.value = shown || 1;
      lbl.textContent = "{name} \u00b7 " + (shown || 1) + "/{nf} \u00b7 "
                      + audio.currentTime.toFixed(1) + "s \u00b7 {pts} pts";
    }}, 150);
    audio.onended = function () {{ play.textContent = "play"; }};
    play.onclick = function () {{
      if (audio.paused) {{ audio.play(); play.textContent = "pause"; }}
      else {{ audio.pause(); play.textContent = "play"; }}
    }};
    scrub.oninput = function () {{
      audio.currentTime = (+scrub.value - 1) / FPS;
      draw(+scrub.value);
    }};
  }} else if (CAPTURE) {{
    // the offline renderer calls draw(n) itself, one frame at a time
    setTicker(calc, false);
    window.__ready = true;
  }} else {{
    drv = makeDriver(calc, NF, FPS);
    play.textContent = "pause";
    play.onclick = function () {{
      if (drv.playing()) {{ drv.pause(); play.textContent = "play"; }}
      else {{ drv.play(); play.textContent = "pause"; }}
    }};
    scrub.oninput = function () {{ drv.seek(+scrub.value); play.textContent = "play"; }};
    setInterval(function () {{
      var m = /n=(\\d+)/.exec(nLatex(calc) || "");
      if (m) {{ scrub.value = m[1]; }}
      lbl.textContent = "{name} \u00b7 " + (m ? m[1] : "?") + "/{nf} \u00b7 "
                      + "{fps} fps \u00b7 {pts} pts";
    }}, 120);
  }}

  document.getElementById("panel").onclick = function () {{
    showPanel = !showPanel;
    calc.updateSettings({{ expressions: showPanel }});
  }};
</script>
"""


# --------------------------------------------------------------------------
# preview render
# --------------------------------------------------------------------------


def extract_audio(video: str, start: float, duration: float, dst: str) -> str | None:
    """Pull the soundtrack out with ffmpeg, trimmed to match the frames."""
    ff = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if is_image(video) or not os.path.exists(ff):
        return None
    cmd = [ff, "-y", "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:g}"]          # before -i: fast seek
    cmd += ["-i", video]
    if duration > 0:
        cmd += ["-t", f"{duration:g}"]
    cmd += ["-vn", "-ac", "1", "-c:a", "aac", "-b:a", "96k", dst]
    try:
        r = subprocess.run(cmd, capture_output=True)
    except OSError:
        return None
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        return None
    with open(dst, "rb") as f:
        return "data:audio/mp4;base64," + base64.b64encode(f.read()).decode()


def catmull_rom(stroke: np.ndarray, steps: int = 8) -> np.ndarray:
    """Same weighted curve Desmos draws, evaluated here so the preview matches.

    `stroke` is (N, 3): x, y, tangent weight.
    """
    pts = stroke[:, :2].astype(np.float64)
    if len(pts) < 3:
        return pts
    w = (stroke[:, 2:3].astype(np.float64)) / (6.0 * W_MAX)
    p = np.vstack([pts[:1], pts, pts[-1:]])
    a, b, c, d = p[:-3], p[1:-2], p[2:-1], p[3:]
    c1 = b + w[:-1] * (c - a)      # weight of the segment's start vertex
    c2 = c - w[1:] * (d - b)       # weight of its end vertex
    t = np.linspace(0, 1, steps, endpoint=False)[:, None, None]
    seg = ((1 - t) ** 3 * b + 3 * (1 - t) ** 2 * t * c1
           + 3 * (1 - t) * t ** 2 * c2 + t ** 3 * c)      # (steps, nseg, 2)
    out = seg.transpose(1, 0, 2).reshape(-1, 2)
    return np.vstack([out, pts[-1:]])


def write_preview(path: str, frames, fps: float, xmax: int, ymax: int,
                  smooth: bool = True, out_w: int = 720):
    sc = out_w / xmax
    h = max(2, int(round(ymax * sc)))
    vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), fps, (out_w, h))
    if not vw.isOpened():
        return False
    for strokes in frames:
        img = np.full((h, out_w, 3), 255, np.uint8)
        for s in strokes:
            p = (catmull_rom(s) if smooth else s[:, :2]).astype(np.float32) * sc
            p[:, 1] = h - 1 - p[:, 1]
            cv2.polylines(img, [np.rint(p).astype(np.int32)], False, (20, 20, 20),
                          1, cv2.LINE_AA)
        vw.write(img)
    vw.release()
    return True


# --------------------------------------------------------------------------


def make_reel(args, base, info, start, duration) -> bool:
    try:
        import reel
    except ImportError:
        print("  ! --reel needs playwright:  uv pip install playwright "
              "&& playwright install chromium", file=sys.stderr)
        return False
    out = base + ".reel.mp4"
    ok = reel.render(info["html"], args.video, start, duration, args.fps,
                     info["frames"], out,
                     width=args.reel_width, height=args.reel_height,
                     scale=args.reel_scale, crf=args.reel_crf,
                     crop=info.get("crop"), workers=args.capture_jobs)
    if ok and args.reel_only:
        # the player was only ever the capture source
        for p in (info["html"],):
            if os.path.exists(p):
                os.remove(p)
    print(f"  reel     {out}" if ok else "  ! reel render failed")
    return ok


VIDEO_EXT = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi")
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff")


def is_image(path: str) -> bool:
    return path.lower().endswith(IMAGE_EXT)


def video_duration(path: str) -> float:
    if is_image(path):
        return 0.0
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
    n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    cap.release()
    return float(n) / fps if fps > 0 else 0.0



def detect_crop(path: str, samples: int = 8):
    """Find pillarbox/letterbox bars. Returns (w, h, x, y) or None.

    Some clips are 4:3 content sitting inside a 16:9 frame. Tracing the bars
    wastes budget on two straight lines and, worse, forces the whole animation
    into the wrong shape.

    ffmpeg's cropdetect reports the bounding box of non-black pixels, so a dark
    scene alone would report a box far smaller than the real picture. Taking the
    UNION over several samples avoids that: bars are constant, dark scenes are
    not, and a union can only ever crop less than the truth.
    """
    ff = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    cap = cv2.VideoCapture(path)
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    dur = video_duration(path)
    if not (W and H and dur):
        return None

    x0, y0, x1, y1 = W, H, 0, 0
    found = False
    for i in range(samples):
        t = dur * (i + 0.5) / samples
        r = subprocess.run([ff, "-hide_banner", "-ss", f"{t:g}", "-t", "0.8",
                            "-i", path, "-vf", "cropdetect=24:2:0", "-f", "null", "-"],
                           capture_output=True, text=True)
        m = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", r.stderr)
        if not m:
            continue
        w, h, x, y = (int(v) for v in m[-1])
        x0, y0 = min(x0, x), min(y0, y)
        x1, y1 = max(x1, x + w), max(y1, y + h)
        found = True
    if not found:
        return None

    w, h = x1 - x0, y1 - y0
    if w >= 0.98 * W and h >= 0.98 * H:
        return None                     # nothing worth cropping

    # bar edges are fuzzy after compression, so snap a near-4:3 result to exact
    if abs((w / h) - 4 / 3) < 0.05:
        w = int(round(h * 4 / 3))
        x0 = max(0, (W - w) // 2)
    w -= w % 2; h -= h % 2              # keep dimensions even for x264
    return (w, h, x0, y0)


def run_split(args, base, start, duration) -> bool:
    """Render a long clip in chunks and join them.

    Capture cost per frame grows with the size of the whole graph - Desmos has
    more list data to wade through on every frame - so one 85s graph is far
    slower per frame than five 17s ones. Measured: 0.40s/frame on a 1.4MB graph
    against 1.2s/frame on a 13.4MB one. Chunking keeps every graph small, and
    the pieces are concatenated losslessly afterwards.
    """
    step = args.split
    parts, tags = [], []
    n = max(1, int(np.ceil(duration / step)))
    for i in range(n):
        t0 = start + i * step
        dur = min(step, start + duration - t0)
        if dur < 0.2:
            break
        tag = f"{base}_part{i:03d}"
        print(f"\n  -- chunk {i + 1}/{n}  ({t0:.1f}s +{dur:.1f}s) --")
        info = convert(args, tag, t0, dur)
        if not info or not make_reel(args, tag, info, t0, dur):
            return False
        parts.append(os.path.abspath(tag + ".reel.mp4"))
        tags.append(tag)

    out = base + ".reel.mp4"
    lst = base + "_parts.txt"
    with open(lst, "w") as f:
        for p in parts:
            f.write("file '%s'\n" % p.replace("'", r"'\''"))
    ff = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    # identical encoder settings across chunks, so this is a stream copy
    r = subprocess.run([ff, "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", lst, "-c", "copy", out], capture_output=True)
    ok = r.returncode == 0
    if not ok:
        print(r.stderr.decode()[:400], file=sys.stderr)
    for p in parts + [lst]:
        if os.path.exists(p):
            os.remove(p)
    for t in tags:
        for ext in (".html", ".desmos.js", ".desmos.json"):
            if os.path.exists(t + ext):
                os.remove(t + ext)
    print(f"  joined   {out}" if ok else "  ! concat failed")
    return ok


def run_batch(args, base) -> int:
    """Process a folder of already-cut clips - one reel per file.

    No scene detection at all: each file is taken as one joke, which is exactly
    right when the clips came pre-cut or were trimmed by hand.
    """
    src = args.batch
    files = sorted(f for f in os.listdir(src)
                   if f.lower().endswith(VIDEO_EXT) and not f.startswith("."))
    if not files:
        raise SystemExit(f"no video files in {src}")

    print(f"{len(files)} clip(s) in {src}")
    done = 0
    for i, fn in enumerate(files, 1):
        path = os.path.join(src, fn)
        raw = os.path.splitext(fn)[0]
        # LosslessCut names its exports "<source>-<in>-<out>-segNN"; that tail
        # is the only distinguishing part, and a plain truncation would throw
        # it away and collide.
        m = re.search(r"seg\d+$", raw)
        stem = m.group(0) if m else "".join(
            c if c.isalnum() or c in "-_" else "_" for c in raw)[-40:].strip("_")
        tag = f"{base}_{stem or f'{i:02d}'}"
        # a long batch gets interrupted sooner or later; don't redo finished work
        finished = tag + (".reel.mp4" if args.reel else ".html")
        if os.path.exists(finished) and not args.overwrite:
            print(f"\n[{i}/{len(files)}] {fn}  - already done, skipping")
            done += 1
            continue
        print(f"\n[{i}/{len(files)}] {fn}")
        args.video = path                      # convert() reads this
        dur = video_duration(path)
        # long clips go through the chunked path too, or a single huge graph
        # makes every frame of them slow to capture
        if args.reel and args.split > 0 and dur > args.split * 1.5:
            if run_split(args, tag, 0.0, dur):
                done += 1
            continue
        info = convert(args, tag, 0.0, 0.0)
        if not info:
            continue
        if args.reel:
            make_reel(args, tag, info, 0.0, 0.0)
        done += 1
    print(f"\n{done} clip(s) written next to {base}*")
    return 0


def run_scenes(args, base) -> int:
    sf = args.scene_file or (base + "_scenes.json")
    if args.scene_file and os.path.exists(args.scene_file):
        with open(args.scene_file) as f:
            scenes = [(float(s["start"]), float(s["end"]))
                      for s in json.load(f)["scenes"]]
        print(f"{len(scenes)} scenes from {args.scene_file}")
    else:
        print(f"scanning {os.path.basename(args.video)} for scene cuts ...",
              flush=True)
        speech = llm_cuts = None
        if args.group != "visual":
            speech = transcribe(args.video, base + "_transcript.json",
                                args.whisper_model)
            if speech is None:
                print("  ! faster-whisper not installed - vision only.\n"
                      "    uv pip install --python .venv/bin/python "
                      "faster-whisper", file=sys.stderr)

        if speech and args.group == "llm":
            cuts_f, fps_f, _ = find_cuts(args.video, args.scene_threshold)
            llm_cuts = llm_joke_cuts([c / fps_f for c in cuts_f], speech,
                                     base + "_jokecuts.json", args.llm_model)
            if llm_cuts is None:
                print("  ! mlx-lm not installed - falling back to speech gaps.\n"
                      "    uv pip install --python .venv/bin/python mlx-lm",
                      file=sys.stderr)
            else:
                print(f"  {len(llm_cuts)} of {len(cuts_f)} scene changes start "
                      f"a new joke")

        scenes, src_fps = detect_scenes(args.video, args.scene_threshold,
                                        args.min_scene, args.max_scene,
                                        speech, args.speech_gap, llm_cuts)
        if not scenes:
            raise SystemExit("no scenes detected")
        with open(sf, "w") as f:
            json.dump({"video": args.video,
                       "scenes": [{"start": round(a, 2), "end": round(b, 2)}
                                  for a, b in scenes]}, f, indent=2)
        print(f"{len(scenes)} scenes (source {src_fps:.2f} fps)")

    for i, (a, b) in enumerate(scenes, 1):
        print(f"  {i:3d}  {a:8.2f}s -> {b:8.2f}s  ({b - a:5.2f}s)")
    sheet = base + "_scenes.jpg"
    if contact_sheet(args.video, scenes, sheet):
        print(f"\n  contact sheet  {sheet}")
    print(f"  scene file     {sf}")
    if args.scene_list:
        print(f"\ncheck the sheet; to fix a split, edit {os.path.basename(sf)} "
              f"and re-run with\n  --scene-file {sf}")
        return 0

    todo = scenes[:args.scene_limit] if args.scene_limit else scenes
    done = 0
    for i, (a, b) in enumerate(todo, 1):
        tag = f"{base}_s{i:02d}"
        print(f"\n[{i}/{len(todo)}] {a:.2f}s -> {b:.2f}s  ({b - a:.2f}s)")
        info = convert(args, tag, a, b - a)
        if not info:
            continue
        if args.reel:
            make_reel(args, tag, info, a, b - a)
        done += 1
    print(f"\n{done} scene(s) written next to {base}*")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="vid2desmos",
        description="Turn a video into a Desmos line-art animation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("video", nargs="?", default=None,
                    help="input video; omit when using --batch")
    ap.add_argument("-o", "--out", default=None,
                    help="output basename (default: alongside the video)")
    ap.add_argument("--fps", type=float, default=12.0, help="animation frame rate")
    ap.add_argument("--width", type=int, default=480,
                    help="working resolution; higher = more detail, slower")
    ap.add_argument("--start", type=float, default=0.0, help="start time (s)")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="seconds to convert, 0 = all")
    ap.add_argument("--budget", type=int, default=300,
                    help="max line segments per frame")
    ap.add_argument("--mode", choices=["edges", "binary"], default="edges",
                    help="edges = Canny (any footage); binary = Otsu outlines "
                         "(flat/high-contrast footage, cartoons)")
    ap.add_argument("--eps", type=float, default=2.5,
                    help="simplification tolerance, in grid units")
    ap.add_argument("--min-len", type=float, default=10.0,
                    help="discard strokes shorter than this many pixels; raising "
                         "this clears speckle and frees budget for real lines")
    ap.add_argument("--no-autocrop", action="store_true",
                    help="keep pillarbox/letterbox bars instead of cropping "
                         "them off before tracing")
    ap.add_argument("--blur", type=int, default=3, help="pre-blur kernel, <3 = off")
    ap.add_argument("--low", type=int, default=-1, help="Canny low threshold")
    ap.add_argument("--high", type=int, default=-1, help="Canny high threshold")
    ap.add_argument("--sigma", type=float, default=0.33,
                    help="unused legacy auto-Canny spread")
    ap.add_argument("--edge-density", type=float, default=0.055,
                    help="target fraction of pixels that become edges. The "
                         "Canny threshold is tuned per frame to hit this, so "
                         "sparse close-ups gain facial detail while busy rooms "
                         "fall back to bold outlines. Raise for more linework")
    ap.add_argument("--invert", action="store_true", help="binary mode: flip fg/bg")
    ap.add_argument("--grid", type=float, default=1000.0,
                    help="Desmos x-axis width in graph units")
    ap.add_argument("--color", default="#2d70b3", help="line color")
    ap.add_argument("--line-width", type=float, default=2.0)
    ap.add_argument("--detail-share", type=float, default=0.65,
                    help="fraction of the per-frame budget reserved for the "
                         "moving parts of the shot (faces, characters) before "
                         "any is spent on static background. 0 = rank by "
                         "length only, which loses faces first")
    ap.add_argument("--detail-eps", type=float, default=0.55,
                    help="eps multiplier where motion is high; below 1 keeps "
                         "more detail on the subject")
    ap.add_argument("--flat-eps", type=float, default=1.7,
                    help="eps multiplier for motionless background; above 1 "
                         "simplifies it harder to pay for the subject")
    ap.add_argument("--curves", choices=["lines", "smooth"], default="lines",
                    help="lines = straight segments, and much cheaper for Desmos "
                         "to draw; smooth = curve the joins that are actually "
                         "curved, at a real cost in frame rate")
    ap.add_argument("--turn-lo", type=float, default=10.0,
                    help="joins turning less than this stay straight")
    ap.add_argument("--turn-hi", type=float, default=60.0,
                    help="joins turning more than this stay sharp corners")
    ap.add_argument("--dedupe", type=float, default=0.0,
                    help="reuse a frame only when its traced lines match the "
                         "previous one, within this many grid units. 0 = exact "
                         "match only (lossless); raising it drops real motion")
    ap.add_argument("--no-audio", action="store_true",
                    help="don't embed the soundtrack in the .html player")
    ap.add_argument("--jobs", type=int, default=0,
                    help="tracing worker processes, 0 = auto (this is only ~4%% "
                         "of render time; see --capture-jobs)")
    ap.add_argument("--capture-jobs", type=int, default=0,
                    help="parallel browsers for the screenshot stage, 0 = auto. "
                         "This stage is ~94%% of render time, so this is the "
                         "flag that actually changes how long a render takes.")
    ap.add_argument("--preview", action="store_true",
                    help="also render a preview .mp4 of the traced result")

    g = ap.add_argument_group("splitting a compilation into clips")
    g.add_argument("--batch", default=None, metavar="DIR",
                   help="process every video in DIR as one already-cut clip; "
                        "no scene detection. Use this with clips you cut "
                        "yourself or downloaded pre-cut")
    g.add_argument("--overwrite", action="store_true",
                   help="--batch: redo clips whose output already exists")
    g.add_argument("--scenes", action="store_true",
                   help="detect scene cuts and process each scene separately")
    g.add_argument("--scene-threshold", type=float, default=27.0,
                   help="cut sensitivity; lower finds more cuts")
    g.add_argument("--min-scene", type=float, default=8.0,
                   help="no scene may be shorter than this (s); short fragments "
                        "get absorbed into the scene before them, which is what "
                        "keeps a cutaway gag attached to its setup")
    g.add_argument("--group", choices=["llm", "speech", "visual"], default="llm",
                   help="how to decide which cuts start a new joke. llm reads "
                        "the dialogue with a local model (most accurate); "
                        "speech uses dialogue pauses; visual uses cuts only")
    g.add_argument("--llm-model",
                   default="mlx-community/Qwen3-VL-8B-Instruct-8bit",
                   help="local MLX model for joke grouping")
    g.add_argument("--speech-gap", type=float, default=0.9,
                   help="only cut where the dialogue pauses at least this long "
                        "(s); this is what keeps a cutaway gag with its setup. "
                        "0 = ignore dialogue and use vision only")
    g.add_argument("--whisper-model", default="base.en",
                   help="faster-whisper model for the dialogue pass")
    g.add_argument("--scene-file", default=None,
                   help="read scenes from this JSON instead of detecting them; "
                        "written automatically so you can edit and re-run")
    g.add_argument("--max-scene", type=float, default=30.0,
                   help="split any scene longer than this (s)")
    g.add_argument("--scene-limit", type=int, default=0,
                   help="only process the first N scenes; 0 = all")
    g.add_argument("--scene-list", action="store_true",
                   help="just print the detected scenes and exit")

    r = ap.add_argument_group("9:16 reel output")
    r.add_argument("--reel", action="store_true",
                   help="render a 9:16 mp4: Desmos (with sidebar) over the original")
    r.add_argument("--split", type=float, default=20.0,
                   help="render clips longer than this many seconds in chunks "
                        "and join them. Capture cost per frame rises with total "
                        "graph size, so chunking keeps long clips fast. 0 = off")
    r.add_argument("--reel-only", action="store_true",
                   help="write just the .mp4 - no player, no graph state. The "
                        "html is still made as the capture source, then deleted")
    r.add_argument("--reel-scale", type=int, default=2,
                   help="capture supersampling; 2 renders at double size and "
                        "downscales, for cleaner lines and sidebar text")
    r.add_argument("--reel-crf", type=int, default=18,
                   help="x264 quality, lower is better (18 is near-transparent)")
    r.add_argument("--reel-width", type=int, default=1080)
    r.add_argument("--reel-height", type=int, default=1920)
    args = ap.parse_args()

    if args.batch:
        if not os.path.isdir(args.batch):
            raise SystemExit(f"not a directory: {args.batch}")
        base = args.out or os.path.join(args.batch, "out")
        os.makedirs(os.path.dirname(os.path.abspath(base)), exist_ok=True)
        return run_batch(args, base)

    if not args.video:
        raise SystemExit("give a video, or a folder of clips with --batch")
    if not os.path.exists(args.video):
        raise SystemExit(f"no such file: {args.video}")

    base = args.out or os.path.splitext(args.video)[0]
    os.makedirs(os.path.dirname(os.path.abspath(base)), exist_ok=True)

    if args.scenes:
        return run_scenes(args, base)
    dur = args.duration or max(0.0, video_duration(args.video) - args.start)
    if args.reel and args.split > 0 and dur > args.split * 1.5:
        return 0 if run_split(args, base, args.start, dur) else 1
    info = convert(args, base, args.start, args.duration)
    if info and args.reel:
        make_reel(args, base, info, args.start, args.duration)
    return 0


def convert(args, base, start, duration, quiet=False):
    """Trace one clip and write its player/state files."""
    name = os.path.basename(base)

    t0 = time.time()
    crop = None if (args.no_autocrop or is_image(args.video)) else detect_crop(args.video)
    if crop:
        print(f"  cropping black bars -> {crop[0]}x{crop[1]} "
              f"({crop[0] / crop[1]:.2f}:1)")
    src = sample_frames(args.video, args.fps, args.width, start, duration, crop)
    try:
        first = next(src)
    except StopIteration:
        raise SystemExit("no frames decoded - is this a video file?")

    fh, fw = first.shape
    opts = Opts(mode=args.mode, grid=args.grid, scale=args.grid / fw,
                eps=args.eps, min_len=args.min_len, budget=args.budget,
                blur=args.blur, low=args.low, high=args.high, sigma=args.sigma, edge_density=args.edge_density,
                invert=args.invert, height=fh,
                smooth=args.curves == "smooth",
                turn_lo=args.turn_lo, turn_hi=args.turn_hi,
                detail_share=args.detail_share,
                eps_lo=args.detail_eps, eps_hi=args.flat_eps)

    def source():
        """Each frame paired with a 0..1 map of what is moving in the shot.

        The comparison is against a frame ~0.3s back, not the previous one.
        Cartoons animate on twos or threes, so adjacent frames are frequently
        identical and differencing them returns a map of pure zeros - which is
        exactly what made the first version of this useless.
        """
        back = max(2, int(round(0.3 * args.fps)))
        buf: list[np.ndarray] = []
        for g in itertools.chain([first], src):
            buf.append(g)
            if len(buf) > back + 1:
                buf.pop(0)
            ref = buf[0] if len(buf) > 1 else None
            if ref is None:
                m = None
            else:
                d = cv2.absdiff(g, ref).astype(np.float32)
                d = cv2.GaussianBlur(d, (0, 0), 7)     # spread onto nearby edges
                hi = float(np.quantile(d, 0.99))
                m = np.clip(d / hi, 0, 1) if hi > 3 else np.zeros_like(d)
            yield g, m

    jobs = args.jobs or min(8, os.cpu_count() or 4)
    traced: list[list[np.ndarray]] = []
    print(f"tracing at {fw}x{fh}, {args.fps} fps, {jobs} workers ...", flush=True)

    if jobs > 1:
        import multiprocessing as mp

        ctx = mp.get_context("fork")
        with ctx.Pool(jobs, initializer=_init_worker, initargs=(opts,)) as pool:
            for batch in batched(source(), jobs * 8):
                traced.extend(pool.map(_work, batch))
                print(f"\r  {len(traced)} frames", end="", flush=True)
    else:
        _init_worker(opts)
        for g in source():
            traced.append(_work(g))
            print(f"\r  {len(traced)} frames", end="", flush=True)
    print()

    if not traced:
        raise SystemExit("nothing traced")

    # Every frame is traced; only frames that draw the same picture collapse.
    uniq, fids = dedupe_traced(traced, args.dedupe)

    smooth = args.curves == "smooth"
    xmax = args.grid
    ymax = args.grid * (fh - 1) / fw
    state, npts, nchunks = build_state(uniq, fids, args.fps, xmax, ymax,
                                       args.color, args.line_width, smooth)
    blob = json.dumps(state, separators=(",", ":"))
    frames = [uniq[i] for i in fids]
    nseg = sum(sum(len(s) - 1 for s in f) for f in frames)

    js_path = base + ".desmos.js"
    html_path = base + ".html"
    json_path = base + ".desmos.json"

    audio = "null"
    if not args.no_audio:
        tmp = base + ".m4a"
        uri = extract_audio(args.video, start, duration, tmp)
        if uri:
            audio = json.dumps(uri)
            print(f"  audio    {len(uri) / 1e6:.2f} MB embedded")
        os.path.exists(tmp) and os.remove(tmp)

    if not args.reel_only:
        with open(json_path, "w") as f:
            f.write(blob)
        with open(js_path, "w") as f:
            f.write(JS_TEMPLATE.format(name=name, nf=len(frames), fps=args.fps,
                                       pts=f"{npts:,}", state=blob,
                                       driver=DRIVER_JS))
    with open(html_path, "w") as f:
        f.write(HTML_TEMPLATE.format(name=name, nf=len(frames), fps=args.fps,
                                     pts=f"{npts:,}", state=blob, api=DESMOS_API,
                                     driver=DRIVER_JS, audio=audio))

    if args.preview:
        p = base + ".preview.mp4"
        if write_preview(p, frames, args.fps, int(xmax), int(ymax), smooth):
            print(f"  preview  {p}")

    mb = len(blob) / 1e6
    held = len(fids) - len(uniq)
    print(f"\n{len(fids)} frames  ({len(uniq)} distinct, {held} held = "
          f"{100 * held / len(fids):.0f}% reused)  {nseg:,} segments  "
          f"{npts:,} points stored  {nseg / len(frames):.0f} seg/frame  "
          f"{nchunks} chunk(s)  ({time.time() - t0:.1f}s)")
    if args.reel_only:
        print(f"  graph    {mb:.2f} MB (temporary, removed after capture)")
    else:
        print(f"  player   {html_path}")
        print(f"  console  {js_path}")
        print(f"  state    {json_path}   ({mb:.2f} MB)")
    if mb > 4.0:
        print("\n  ! that payload is large; Desmos will feel sluggish.\n"
              "    try a lower --budget", file=sys.stderr)
    return {"frames": len(fids), "html": html_path, "mb": mb, "crop": crop}


if __name__ == "__main__":
    sys.exit(main())
