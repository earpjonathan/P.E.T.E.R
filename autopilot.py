#!/usr/bin/env python3
"""One command: find clips, render them, post to YouTube, stage for Instagram.

    ./.venv/bin/python autopilot.py --count 10
    ./.venv/bin/python autopilot.py --count 2 --no-upload   # stop before posting

Order matters and is deliberate. Identification is cheap (~10s a clip) and
rendering is expensive (roughly 8x the clip's duration), so everything that can reject a clip
runs BEFORE the renderer: duration, episode match, and dialogue dedupe. A clip
that cannot be captioned is worthless no matter how good it looks.

Nothing is deleted. --delete-after on the YouTube upload is what destroyed the
only local copy of a reel that still needed posting to Instagram; here the
finished mp4 is copied to out/instagram/ and left there.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import episodes as E

PY = "./.venv/bin/python"
CLIPS = "Clips"
REELS = "out/reels"
IG_DIR = "out/instagram"
SOURCE_META = "source_meta.json"
TRANSCRIPTS = "transcripts.json"

# the bar that has held across 118 uploads: enough distinct phrase hits, clear
# of the runner-up, and not a clip straddling two episodes
MIN_HITS, MIN_CONF, MAX_RUNNER = 4, 0.60, 0.30
DEDUPE_OVERLAP = 0.40

RENDER_FLAGS = ["--reel", "--reel-only", "--fps", "24", "--width", "720",
                "--budget", "800", "--eps", "1.1", "--min-len", "8"]


def load_json(p, default):
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return default


def identify(path, model, idx):
    """Transcribe, match an episode, and measure how clear the match is."""
    segs, _ = model.transcribe(path, vad_filter=True)
    text = E.normalise(" ".join(s.text for s in segs))
    if not text.split():
        return None, text, "no dialogue"
    r = E.identify(text, idx)
    if not r:
        return None, text, "no episode match"
    code, s, e, conf, hits = r
    g = E.grams(text)
    sc = sorted(((sum(1 for x in g if x in v["text"]), c)
                 for c, v in idx.items()), reverse=True)
    runner = sc[1][0] / sc[0][0] if sc[0][0] else 0.0
    if hits < MIN_HITS or conf < MIN_CONF:
        return None, text, f"weak match S{s:02d}E{e:02d} conf {conf:.2f}, {hits} hits"
    if runner > MAX_RUNNER:
        # two episodes' dialogue in one cut - no single caption is correct
        return None, text, f"spans two episodes (runner {runner:.2f})"
    return {"season": s, "episode": e, "conf": conf, "hits": hits,
            "runner": round(runner, 2)}, text, ""


def is_duplicate(text, transcripts):
    """Reject a joke we already posted, comparing dialogue rather than pixels."""
    g = E.grams(text)
    if not g:
        return None
    for name, t in transcripts.items():
        gb = E.grams(t.get("text", ""))
        if not gb:
            continue
        if len(g & gb) / min(len(g), len(gb)) > DEDUPE_OVERLAP:
            return name
    return None


def render(src, tag, quiet=False):
    out = os.path.join(REELS, tag)
    final = out + ".reel.mp4"
    if os.path.exists(final):
        return final
    r = subprocess.run([PY, "vid2desmos.py", src, "-o", out] + RENDER_FLAGS,
                       capture_output=True, timeout=3600)
    if not os.path.exists(final):
        print(f"  ! render failed: {r.stderr.decode('utf-8','ignore')[-300:]}",
              file=sys.stderr)
        return None
    return final


def main() -> int:
    ap = argparse.ArgumentParser(description="Harvest, render, post, stage")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--no-harvest", action="store_true",
                    help="use whatever is already in out/harvest")
    ap.add_argument("--pool-factor", type=float, default=2.0,
                    help="download this many times --count, since some clips "
                         "fail identification or turn out to be duplicates")
    args = ap.parse_args()

    os.makedirs(IG_DIR, exist_ok=True)
    os.makedirs(REELS, exist_ok=True)

    # ---- 1. harvest -------------------------------------------------------
    hjson = os.path.join("out/harvest", "harvest.json")
    if not args.no_harvest:
        want = max(args.count, int(args.count * args.pool_factor))
        print(f"=== harvesting {want} candidate(s) ===", flush=True)
        r = subprocess.run([PY, "harvest.py", "--count", str(want),
                            "--dir", "out/harvest"], timeout=2400)
        if r.returncode != 0:
            print("harvest failed", file=sys.stderr)
            return 1
    got = load_json(hjson, [])
    got = [c for c in got if c.get("path") and os.path.exists(c["path"])]
    if not got:
        print("no clips downloaded", file=sys.stderr)
        return 1
    print(f"\n=== identifying {len(got)} clip(s) ===", flush=True)

    # ---- 2. identify + dedupe (cheap, so it runs before rendering) ---------
    from faster_whisper import WhisperModel
    idx = E.load_index()
    model = WhisperModel("base.en", device="cpu", compute_type="int8")
    transcripts = load_json(TRANSCRIPTS, {})
    accepted, rejected = [], []
    for c in sorted(got, key=lambda x: -x.get("score", 0)):
        info, text, why = identify(c["path"], model, idx)
        if not info:
            rejected.append((c, why))
            print(f"  reject  {c['title'][:44]:46} {why}", flush=True)
            continue
        dup = is_duplicate(text, transcripts)
        if dup:
            rejected.append((c, f"duplicate of {dup}"))
            print(f"  reject  {c['title'][:44]:46} duplicate of {dup}", flush=True)
            continue
        c.update(info)
        c["text"] = text
        accepted.append(c)
        # add to the live corpus so two clips in THIS batch cannot duplicate
        transcripts[f"_pending_{c['id']}"] = {"text": text}
        print(f"  accept  {c['title'][:44]:46} "
              f"S{info['season']:02d}E{info['episode']:02d} conf {info['conf']:.2f}",
              flush=True)
        if len(accepted) >= args.count:
            break

    print(f"\n{len(accepted)} accepted, {len(rejected)} rejected")
    if len(accepted) < args.count:
        print(f"! only {len(accepted)} of {args.count} survived filtering - "
              f"proceeding with what there is", file=sys.stderr)
    if not accepted:
        return 1

    # ---- 3. render --------------------------------------------------------
    transcripts = load_json(TRANSCRIPTS, {})     # drop the _pending_ entries
    srcmeta = load_json(SOURCE_META, {})
    est = sum(c.get("duration", 30) for c in accepted) * 8 / 60
    print(f"\n=== rendering {len(accepted)} clip(s) (~{est:.0f} min total) ===",
          flush=True)
    done = []
    for i, c in enumerate(accepted, 1):
        base = f"s{c['season']:02d}e{c['episode']:02d}"
        n, name = 1, base
        while os.path.exists(os.path.join(CLIPS, name + ".mp4")):
            n += 1
            name = f"{base}_{n}"
        clip_src = os.path.join(CLIPS, name + ".mp4")
        shutil.move(c["path"], clip_src)
        tag = f"auto_{name}"
        print(f"\n[{i}/{len(accepted)}] {tag}  {c['title'][:44]}", flush=True)
        t0 = time.time()
        reel = render(clip_src, tag)
        if not reel:
            continue
        print(f"  rendered in {time.time()-t0:.0f}s -> {reel}")
        c.update(tag=tag, reel=reel, clip_src=clip_src)
        done.append(c)
        transcripts[tag] = {"base": tag, "text": c["text"],
                            "words": len(c["text"].split()),
                            "season": c["season"], "episode": c["episode"],
                            "conf": c["conf"], "hits": c["hits"]}
        # keep the source's own popularity so analyze.py can test whether it
        # predicted anything
        srcmeta[tag] = {"source_video_id": c["id"], "source_views": c["views"],
                        "source_vpd": round(c["views"] / max(c.get("age_days", 1), 1), 1),
                        "source_channel": c["channel"],
                        "source_title": c["title"], "score": c.get("score")}
        with open(TRANSCRIPTS, "w") as f:
            json.dump(transcripts, f, indent=1)
        with open(SOURCE_META, "w") as f:
            json.dump(srcmeta, f, indent=1)

    if not done:
        print("nothing rendered", file=sys.stderr)
        return 1

    # ---- 4. measure BEFORE anything can be removed ------------------------
    print(f"\n=== measuring features ===", flush=True)
    subprocess.run([PY, "features.py", REELS, "features.csv"], timeout=1800)

    # ---- 5. stage for Instagram ------------------------------------------
    import upload_youtube as U
    lines = []
    for c in done:
        dst = os.path.join(IG_DIR, os.path.basename(c["reel"]))
        shutil.copy2(c["reel"], dst)
        lines.append(f"{os.path.basename(dst)}\t{U.caption(c['season'], c['episode'])}")
    with open(os.path.join(IG_DIR, "captions.txt"), "a") as f:
        f.write("\n".join(lines) + "\n")
    print(f"staged {len(done)} reel(s) in {IG_DIR}/ with captions.txt")

    # ---- 6. upload --------------------------------------------------------
    if args.no_upload:
        print("\n--no-upload: nothing posted")
        return 0
    print(f"\n=== uploading {len(done)} to YouTube ===", flush=True)
    svc = U.build_service()
    ch = U.channel_info(svc)
    if not ch or not U.channel_matches(ch, U.EXPECT_CHANNEL):
        print(f"! wrong channel ({ch and ch['title']}); nothing uploaded",
              file=sys.stderr)
        return 1
    print(f"posting to: {ch['title']}")
    state = U.load_state()
    fresh = []
    for c in done:
        title = U.caption(c["season"], c["episode"], "#shorts")
        print(f"\nuploading {c['tag']} ...", flush=True)
        vid, err = U.upload_one(svc, c["reel"], title, "public")
        if not vid:
            print(f"  ! failed: {err}", file=sys.stderr)
            low = (err or "").lower()
            # uploadLimitExceeded arrives as HTTP 400 and does NOT contain the
            # word "quota" - missing it here once burned 30 doomed attempts in
            # a row. Keep this list in sync with upload_youtube.py.
            if ("quota" in low or "uploadlimitexceeded" in low
                    or "exceeded the number of videos" in low):
                print("  daily upload limit reached - stopping", file=sys.stderr)
                print(f"  {len(fresh)} posted; the rest stay in {IG_DIR}/ and "
                      f"out/reels/ - re-run tomorrow to post them",
                      file=sys.stderr)
                break
            continue
        state[c["reel"]] = {"id": vid, "title": title, "privacy": "public",
                            "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        U.save_state(state)
        fresh.append(vid)
        print(f"  https://youtu.be/{vid}")
        time.sleep(4)

    if fresh:
        print()
        U.print_region_report(svc, fresh)
    print(f"\n{len(fresh)} posted; {len(done)} staged in {IG_DIR}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
