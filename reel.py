"""Render a Desmos player to a 9:16 split-screen video for Reels/Shorts.

The top half is a real screenshot of the Desmos page — sidebar, expression list
and all — driven one frame at a time through a headless browser. The bottom half
is the original clip. Both come from the same frame range, so they stay in sync
by construction.

Playwright is only imported when this actually runs, so the rest of the tool
works without it installed.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile


def ffmpeg() -> str:
    return shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"


# Injected once per page. This encodes the fix for the duplicate-frame bug:
# Desmos evaluates in a web worker and paints on the next animation frame, so
# screenshotting straight after draw(n) captures the PREVIOUS frame - measured
# at 88% duplicates on a heavy graph. Do not "simplify" the two-stage
# moved-then-stable test; waiting only for "stable" passes instantly on a
# canvas that has not repainted yet.
# How many consecutive unchanged animation frames count as "the graph has
# finished drawing", and how long to wait before giving up. The original
# stable>=2 is only ~33ms of quiescence at 60fps; under CPU contention Desmos
# can pause longer than that in the MIDDLE of an update, so the screenshot
# catches a partially redrawn graph. Measured: serial capture on an idle
# machine is perfectly deterministic, but 4 frames of 40 diverge under load and
# 22 of 40 under 4 parallel browsers.
STABLE_FRAMES = int(os.environ.get("REEL_STABLE", "6"))
SETTLE_BUDGET = int(os.environ.get("REEL_MAXFRAMES", "120"))

_SETTLE_JS = """() => {
          window.__fp = () => {
            const c = document.querySelector('.dcg-graph-inner canvas')
                   || document.querySelector('canvas');
            if (!c) return -1;
            // Resolution matters more than it looks. At 192x128 this
            // downsampled a 1440x1280 canvas by 7.5x, which averages a 1px
            // line shift away to nothing - so the fingerprint reported
            // "stable" while the graph was still refining thin strokes. That
            // is invisible when timing is consistent (serial capture is
            // deterministic) but under the CPU contention of parallel
            // browsers it captured half-refined frames: 23 of 40 differed
            // from the serial reference. Sample finer, and every byte.
            const W = 640, H = 480;
            const o = document.createElement('canvas');
            o.width = W; o.height = H;
            const x = o.getContext('2d', { willReadFrequently: true });
            try { x.drawImage(c, 0, 0, W, H); } catch (e) { return -2; }
            const d = x.getImageData(0, 0, W, H).data;
            let h = 0;
            for (let i = 0; i < d.length; i++) h = (h * 31 + d[i]) | 0;
            return h;
          };
          // Wait for the canvas to CHANGE from what it showed before draw(),
          // then for it to stop changing. Waiting only for "stable" is wrong:
          // a canvas that has not repainted yet is perfectly stable, so that
          // test passes instantly and captures the previous frame.
          window.__settle = (baseline, maxFrames) => new Promise(res => {
            let frames = 0, last = baseline, stable = 0, moved = false;
            const step = () => {
              const f = window.__fp();
              if (f !== baseline) moved = true;
              if (moved) {
                if (f === last) stable++; else { stable = 0; last = f; }
                if (stable >= __STABLE__) return res({frames: frames, settled: true});
              }
              // A frame that genuinely draws the same picture never moves, so
              // hitting the cap without moving means "duplicate frame", which
              // is a legitimate stop. Hitting it WHILE still repainting means
              // we ran out of budget mid-draw - report that so the caller can
              // wait longer instead of screenshotting a half-drawn graph.
              if (++frames >= maxFrames) return res({frames: frames,
                                                     settled: !moved});
              requestAnimationFrame(step);
            };
            requestAnimationFrame(step);
          });
        }"""


def auto_workers() -> int:
    """How many browsers to run at once.

    Capped at 4 deliberately. Chromium rendering is effectively one performance
    core per page, and this machine has 5; going to 6 measured only 8% faster
    while cutting the timing margin that keeps frames byte-identical - at
    stable=4 the sixth worker already corrupted a frame. Memory is not the
    limit (4 browsers peak around 9 GB).
    """
    try:
        perf = int(subprocess.run(
            ["sysctl", "-n", "hw.perflevel0.physicalcpu"],
            capture_output=True, text=True).stdout.strip())
    except (ValueError, OSError):
        perf = (os.cpu_count() or 4) // 3
    return max(1, min(4, perf - 1))


def _capture_range(task, progress=None) -> int:
    """Screenshot a contiguous slice of frames in one browser.

    Safe to run many of these at once on disjoint slices: draw(n) sets the
    graph to an absolute frame, so frame n does not depend on frame n-1. Each
    process pays the page-load and list-evaluation cost (~6s) once, which is
    why slices are contiguous and few rather than round-robin.
    """
    from playwright.sync_api import sync_playwright

    html, dst, lo, hi, width, height, scale = task
    url = "file://" + os.path.abspath(html) + "?capture=1"
    written = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": height},
                                device_scale_factor=scale)
        # default is 30s, which a large trace under CPU contention can miss;
        # window.__ready below is the real readiness gate.
        page.goto(url, timeout=180000)
        page.wait_for_function("window.__ready === true", timeout=60000)
        # big list literals keep the worker busy for a moment after load
        page.wait_for_timeout(3500)
        page.evaluate(_SETTLE_JS.replace("__STABLE__", str(STABLE_FRAMES)))

        for n in range(lo, hi + 1):
            r = page.evaluate(
                "async (n) => { const b = window.__fp(); draw(n);"
                " return await window.__settle(b, %d); }" % SETTLE_BUDGET, n)
            if not r["settled"]:
                # Still repainting when the budget ran out. Serial capture is
                # deterministic, but running several browsers at once slows
                # each one enough that 15 animation frames stops being enough,
                # and the screenshot catches a partially drawn graph - measured
                # as 23 of 40 frames differing from the serial reference.
                # baseline -1 can never match, so this just waits for the
                # canvas to stop changing.
                page.evaluate("async () => await window.__settle(-1, 600)")
            # A heavy chunk can push a single screenshot past Playwright's
            # 30s default, and that one exception used to kill the whole batch
            # - taking every clip queued behind it with it. Give it far longer,
            # and retry, because the stall is transient: Chromium is busy, not
            # broken. Only give up if it fails repeatedly.
            for attempt in range(3):
                try:
                    page.screenshot(path=os.path.join(dst, "f" + str(n).zfill(6) + ".png"),
                                    timeout=180000)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    print("\n    screenshot stalled on frame " + str(n)
                          + " (" + type(e).__name__ + "), retrying", flush=True)
                    page.wait_for_timeout(2000)
            written += 1
            if progress and (written % 25 == 0 or n == hi):
                progress(written, hi - lo + 1)
        browser.close()
    return written


def capture_frames(html: str, dst: str, nframes: int, width: int, height: int,
                   scale: int = 1, progress=None, workers: int = 1) -> int:
    """Screenshot the player once per frame. Returns how many were written.

    Capture is ~94% of render wall time (measured: 160.8s of a 171.6s render),
    so this is the only stage where parallelism pays - the tracing pool is
    optimising the other 4%.

    CALLERS MUST GUARD THEIR ENTRY POINT with `if __name__ == "__main__":`
    when workers > 1. This uses the spawn start method, so every child
    re-imports the calling module; a bare module-level call here re-runs
    itself in each child and fork-bombs the machine (observed: 0 frames
    written, load average 9.9, no error message).
    """
    os.makedirs(dst, exist_ok=True)
    if workers <= 0:
        workers = auto_workers()
    if workers <= 1:
        return _capture_range((html, dst, 1, nframes, width, height, scale),
                              progress)

    import multiprocessing as mp
    step = math.ceil(nframes / workers)
    tasks = [(html, dst, lo, min(lo + step - 1, nframes), width, height, scale)
             for lo in range(1, nframes + 1, step)]
    # spawn, not fork: Playwright drives a browser over a socket and does not
    # survive being forked out of a parent that already has one running.
    ctx = mp.get_context("spawn")
    with ctx.Pool(len(tasks)) as pool:
        r = pool.map_async(_capture_range, tasks)
        while not r.ready():
            if progress:
                try:
                    progress(len(os.listdir(dst)), nframes)
                except OSError:
                    pass
            r.wait(2)
        return sum(r.get())


def build_reel(frames_dir: str, src: str, start: float, duration: float,
               fps: float, out: str, width: int = 1080, height: int = 1920,
               crf: int = 18, crop=None) -> bool:
    """Stack the animation over the clip, each filling half the canvas."""
    half = height // 2
    seq = os.path.join(frames_dir, "f%06d.png")

    cmd = [ffmpeg(), "-y", "-v", "error",
           "-framerate", f"{fps:g}", "-i", seq]
    if start > 0:
        cmd += ["-ss", f"{start:g}"]
    cmd += ["-i", src]
    if duration > 0:
        cmd += ["-t", f"{duration:g}"]

    # The capture already has the cell's aspect, so it scales in exactly and
    # Desmos gets the whole top half. The clip is 16:9 and its half isn't, so
    # it sits on a blurred, zoomed copy of itself rather than dead bars.
    top = f"scale={width}:{half},setsar=1[top]"
    pre = ""
    srcv = "[1:v]"
    if crop:
        cw, ch, cx, cy = crop
        pre = f"[1:v]crop={cw}:{ch}:{cx}:{cy},split=2[c1][c2];"
        srcv = "[c1]"
    bot = (f"{srcv}scale={width}:{half}:force_original_aspect_ratio=increase,"
           f"crop={width}:{half},gblur=sigma=24[bg];"
           f"{'[c2]' if crop else '[1:v]'}scale={width}:{half}:"
           f"force_original_aspect_ratio=decrease[fg];"
           f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[bot]")
    cmd += ["-filter_complex",
            f"[0:v]{top};{pre}{bot};[top][bot]vstack=inputs=2[v]",
            "-map", "[v]", "-map", "1:a?",
            "-c:v", "libx264", "-preset", "slow", "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-r", f"{fps:g}",
            "-c:a", "aac", "-b:a", "128k", "-shortest", out]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        print(r.stderr.decode()[:600])
        return False
    return True


def render(html: str, src: str, start: float, duration: float, fps: float,
           nframes: int, out: str, width: int = 1080, height: int = 1920,
           quiet: bool = False, scale: int = 2, crf: int = 18, crop=None,
           workers: int = 1) -> bool:
    """Capture the player and compose the finished 9:16 file."""
    half = height // 2
    # Capture larger than the cell, at the cell's aspect. Desmos' sidebar is a
    # fixed pixel width, so a wider viewport makes it a smaller share of the
    # frame and leaves more room for the drawing. Past ~4/3 the sidebar text
    # stops being legible once downscaled and it no longer reads as Desmos.
    k = 4 / 3
    cw, ch = int(width * k) // 2 * 2, int(half * k) // 2 * 2
    tmp = tempfile.mkdtemp(prefix="reelcap_")
    try:
        def prog(n, total):
            if not quiet:
                print(f"\r    capturing {n}/{total}", end="", flush=True)

        capture_frames(html, tmp, nframes, cw, ch, scale=scale, progress=prog,
                       workers=workers)
        if not quiet:
            print()
        return build_reel(tmp, src, start, duration, fps, out, width, height,
                          crf, crop)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
