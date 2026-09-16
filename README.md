# vid2desmos

Turn a video into a line-art animation that plays inside Desmos.

Write-up with interactive demos and measurements: https://projects.jonathanearp.xyz/desmos/

```bash
./.venv/bin/python vid2desmos.py myclip.mp4 -o out/myclip --duration 8
```

Or take a whole compilation, cut it into scenes, and render a 9:16 split-screen
video per scene, ready to post:

```bash
./.venv/bin/python vid2desmos.py compilation.mp4 -o out/fg/clip \
    --scenes --reel --fps 24 --budget 500 --eps 1.2 --min-len 20
```

Three files come out:

| file | what it's for |
| --- | --- |
| `myclip.html` | standalone player — just open it, nothing to install |
| `myclip.desmos.js` | paste into the console at desmos.com/calculator |
| `myclip.desmos.json` | raw Desmos graph state |

## Getting it into the real Desmos

1. Open <https://www.desmos.com/calculator>
2. Open the console — <kbd>Cmd</kbd>+<kbd>Option</kbd>+<kbd>J</kbd>
3. Paste the whole `.desmos.js` file and press enter

Chrome blocks console pasting by default the first time; it'll ask you to type
`allow pasting`.

## How it works

Each frame is edge-detected, the edges are walked into polylines, simplified,
and the whole animation is packed into flat lists. **One** parametric
expression draws every curve of the current frame, because Desmos plots a
parametric whose x/y are list-valued as many separate curves:

```
R = [O[J] ... O[J+1]-1]        indices of this frame's points
S = R[D[R] > 0]                drop the points that end a stroke
( K(X[S-1],X[S],X[S+1],X[S+2],t), K(Y[...],t) ),  0 ≤ t ≤ 1
```

`O` is a per-frame offset table, so switching frames slices a range instead of
scanning the whole dataset. A ticker advances `n` at the requested frame rate.

### Selective curves (`--curves smooth`, off by default)

Curves are off by default for a performance reason, not just a taste one.
Desmos samples a parametric adaptively: a straight segment converges
immediately, a cubic has to be subdivided until it looks smooth. Every drawn
segment is a separate curve in the list, so switching them all to cubics
multiplies the per-frame drawing work — and per-frame drawing work is exactly
what caps the frame rate. Straight lines let you spend the same budget on more
frames instead.


`K` is a Catmull-Rom spline written as its equivalent cubic Bézier, with each
tangent scaled by a per-vertex weight:

```
K(a,b,c,d,p,q,t) = (1-t)³b + 3(1-t)²t(b + p(c-a)/24) + 3(1-t)t²(c - q(d-b)/24) + t³c
```

The weights are the whole trick. Curving *everything* looks melted — straight
edges bow and sharp corners round off. So each vertex earns curvature only if
its turn angle lands in a middle band:

| turn at the vertex | weight | why |
| --- | --- | --- |
| `< --turn-lo` (10°) | 0 | a straight run; smoothing only adds wobble |
| between | 4 | a real curve — a cheek, an ear, a cushion |
| `> --turn-hi` (60°) | 0 | a real corner — door frame, teeth. Stay sharp |

At weight 0 both Bézier control points collapse onto the anchors, which draws
an **exactly straight** segment. So one expression covers curves and straight
lines, chosen per vertex by the data. A spline segment needs the point before
and after it, and both are already in the flat list, so this costs no extra
control points — the weights ride along in a list that was already there (see
below). `--curves lines` forces every weight to 0.

### One list does two jobs

`D` holds a tangent weight (0..4) for a point that starts a drawn segment, and
the sentinel 5 for the last point of a stroke. So a single list encodes both
where strokes break *and* how curved each join is. Reading the far end's weight
as `mod(c, 5)` turns the sentinel back into the 0 a stroke end should have
anyway. That's why selective curves are free: they replaced a list rather than
adding one.

### Held frames

Repeated frames cost nothing but an offset — several frame numbers point at the
same span of the point list. Because that breaks monotonic ordering, each frame
carries an explicit end offset (`E`) instead of using the next frame's start.

**Frames are compared by their traced geometry, not by their pixels.** This
matters more than it sounds. An earlier version compared source frames by mean
pixel difference, which dilutes a small movement — a mouth, a hand — across the
whole frame. Measured on the test clip, that metric called 89% of frames
duplicates when only 42% really were: it silently discarded real motion in
**48% of frames**, and the result looked like 5 fps no matter how fast `n`
advanced. Comparing the drawn output instead makes dropping motion impossible.

`--dedupe` is the tolerance in grid units, default 0 = exact match only. Raising
it barely helps (721 → 700 distinct frames at tol 10) because compression noise
changes stroke *topology*, not just point positions — so it isn't a useful knob,
and it can only cost you motion. Leave it at 0.

## Audio

The player embeds the soundtrack as a data URI (ffmpeg, mono AAC 96k) and makes
it the **clock**: the frame is derived from `audio.currentTime` rather than
counted independently.

**The frame update has to be driven by `requestAnimationFrame`, not a timer.**
A timer keeps firing at its own rate whether or not Desmos has finished
drawing, so updates pile up and the picture falls further and further behind
the sound — measured drift on the 39 s clip grew steadily to 75 frames and kept
going. rAF only fires when the browser is ready to paint, so the driver can
never outrun the renderer: if Desmos is slow the extra frames are simply
skipped and you still see whatever the audio is playing now. With that change
drift stays bounded instead of accumulating.

A 250 ms watchdog covers the case where rAF is throttled to nothing (background
tab, or a window that isn't compositing) so the picture can't freeze while the
sound plays on.

Playback starts paused because browsers block autoplay with sound until you
click something. `--no-audio` skips it. Audio only applies to the `.html`
player — there's nowhere to put it on desmos.com.

## Tuning

The knob that matters is `--budget` (line segments per frame). Everything else
is secondary.

```bash
# lighter: fewer frames, fewer segments
./.venv/bin/python vid2desmos.py clip.mp4 -o out/clip --fps 8 --budget 200

# heavier, more detail
./.venv/bin/python vid2desmos.py clip.mp4 -o out/clip --width 720 --budget 600

# high-contrast footage / cartoons: trace region outlines instead of edges
./.venv/bin/python vid2desmos.py clip.mp4 -o out/clip --mode binary

# check the look without opening Desmos at all
./.venv/bin/python vid2desmos.py clip.mp4 -o out/clip --preview
```

`--preview` writes a `.preview.mp4` of exactly the lines that will be drawn —
much faster to iterate on than reloading a graph.

### Where the detail goes

Two mechanisms decide what survives, and they matter more than `--budget`.

**The Canny threshold is tuned per frame to hit `--edge-density`** (default
0.055 — the fraction of pixels that become edges) rather than being fixed.
Both fixed alternatives fail, in opposite directions:

- The textbook `median*(1±sigma)` rule is built for photographs. On a bright
  flat-shaded cartoon it lands around **115/228** and keeps only the boldest
  outlines — a character's eyes and mouth are never detected at all, so no
  amount of budget can bring them back. This silently gutted every face.
- A fixed *low* threshold is just as wrong: a tiled floor then floods the frame
  with faint repeating lines and buries the characters.

Targeting an amount of linework adapts to both — it digs down for the face in a
sparse close-up and backs off to bold outlines in a busy room.

**When a frame is over budget, strokes are ranked by importance, not length.**
`--detail-share` (default 0.65) reserves that fraction of the budget for busy
areas before any is spent on flat background, and `--detail-eps` /`--flat-eps`
simplify the subject less and the background more.

Ranking by length — the obvious choice — is actively wrong: a face is a cluster
of *short* strokes while a couch is one *long* contour, so length-ranking
deletes faces and keeps furniture.

Both of these change what the budget buys, not how much is drawn, so the frame
rate is unaffected: segments per frame stay put while the picture gets
substantially more legible.

Motion would seem like the natural importance signal, and it isn't: these shows
animate on twos and hold poses, so the frame-to-frame difference is often
exactly zero. Measured on a held pose, even a 0.3s baseline barely registered.
Local edge density works on a still frame, so it does the work; motion is only
added as a bonus when present.

**`--min-len` is the underrated one.** Canny leaves a lot of short speckle, and
every speck eats budget that should go to real lines. Raising it from the
default to `--min-len 20` on a cartoon visibly cleaned up the image *and* let
more genuine detail through at identical file size. Push it until features you
care about start disappearing.

Note that `--budget` and `--eps` interact. When a frame busts the budget the
tool coarsens `eps` until it fits, so lowering `--eps` alone does nothing —
it just gets coarsened straight back. Raise both or neither.

Other flags: `--start` / `--duration` to trim, `--curves lines` to disable
curves, `--turn-lo`/`--turn-hi` for the curvature band, `--dedupe`, `--no-audio`,
`--low`/`--high` for manual Canny thresholds, `--invert` for binary mode,
`--color`, `--line-width`.

`--mode binary` is for silhouette-style content. On a cartoon with interior
detail it fills the character in as a blob — good for `life.mp4`-style
footage, wrong for Family Guy.

When a frame goes over budget the tool first simplifies everything more
aggressively, and only drops whole strokes (shortest first) as a last resort —
losing detail evenly looks better than losing whole objects.

## A folder of already-cut clips (recommended)

If you already have one joke per file — cut by hand, or downloaded pre-cut —
skip scene detection entirely:

```bash
./.venv/bin/python vid2desmos.py --batch ~/clips -o ~/clips/out \
    --reel --fps 24 --budget 500 --eps 1.2 --min-len 20
```

Every video in the folder becomes one reel, named after its file. This is the
most reliable path: automatic scene detection has to guess where a joke ends,
and a human cutting clips does not.

Getting clips pre-cut:

- **The official Family Guy YouTube channel posts one scene per video**, free
  and in 1080p. `yt-dlp <url>` pulls a whole clip; `yt-dlp --download-sections
  "*01:30-02:45" <url>` pulls part of one.
- **getyarn.io** is subtitle-indexed and gives a short MP4 per spoken line —
  browser only, it blocks scripted access.
- There is **no Frinkiac equivalent** for Family Guy (that project covers only
  Simpsons, Futurama, Rick & Morty and West Wing).
- YouTube chapter markers would be human-authored joke boundaries, but sampling
  18 Family Guy videos found **zero** with chapters, so that route is dead.

## Cutting a compilation into clips

`--scenes` finds hard cuts by the mean absolute HSV difference between
consecutive frames — a cut moves every pixel at once and spikes far above
ordinary motion. Shots are then accumulated until a scene reaches
`--min-scene`, and anything past `--max-scene` is split.

**Shot detection is not joke detection.** A cutaway gag is a few seconds in a
setting chosen to be unrelated to its setup, so it looks and sounds exactly like
a real clip boundary. Things that do not work, all measured on a Family Guy
compilation:

| approach | result |
| --- | --- |
| silence between clips | zero silences at -32 dB; continuous music bed |
| audio texture change across the cut | 0.030 at a mid-joke cut vs 0.026 and 0.021 at real boundaries — no separation |
| minimum length on both sides | absorbs the cutaway *forward* into the next joke |
| merge shortest shot backwards | merges the wrong pairs elsewhere |

Dialogue *timing* fails too, and it's worth knowing why before reaching for it.
Measured on the same footage, the pause at the cut that wrongly split the bar
scene from its caveman punchline was **0.78s**, while the pause at a cut that
genuinely does start a new clip was **0.40s** — the signal points the wrong way.
Words run straight through real boundaries and stop inside jokes.

### What does work: reading the dialogue

`--group llm` (the default) transcribes the audio once with faster-whisper, then
asks a **local** model, for each cut, whether the lines after it continue the bit
before it. Only the meaning of the lines carries this.

The prompt matters more than the model. Asked flatly whether a scene change
starts a new joke, the model answered "NEW" for all 79 cuts — useless. It needs
to be told what a cutaway *is* (a gag that looks unrelated but is the punchline
to the line before it) and allowed one short reason before committing to a
label. With that it scored 4/5 on hand-checked cases and produced the right
answer on both real failures: the hallway scene splits off, and the bar scene
keeps its caveman punchline.

Decisions are cached in `<base>_jokecuts.json`, so re-runs are instant and you
can edit a single verdict by hand. On the test file, 48 of 79 cuts start a new
joke and 31 are internal.

Set the model with `--llm-model` (any MLX model id). `--group speech` falls back
to dialogue pauses and `--group visual` to cuts alone; the tool degrades to
these automatically if `mlx-lm` or `faster-whisper` is missing.

It's still a judgement call, so the review step stays:

```bash
# 1. detect. writes a numbered contact sheet + an editable scene file
./.venv/bin/python vid2desmos.py compilation.mp4 -o out/fg/clip --scenes --scene-list

# 2. look at out/fg/clip_scenes.jpg, then merge/split entries in
#    out/fg/clip_scenes.json - a merge is just deleting one boundary

# 3. render from your corrected list
./.venv/bin/python vid2desmos.py compilation.mp4 -o out/fg/clip \
    --scenes --scene-file out/fg/clip_scenes.json --reel
```

Each scene writes `<base>_sNN.html`, `.desmos.js`, `.desmos.json` and, with
`--reel`, `<base>_sNN.reel.mp4`. `--scene-limit N` does only the first N, which
is worth doing before committing to a long run. Tune with `--scene-threshold`
(lower finds more cuts), `--min-scene`, `--max-scene`.

## The 9:16 reel

`--reel` produces a 1080x1920 mp4: the Desmos animation on top, the original
clip below, with the scene's own audio.

The top half is a **real screenshot of the Desmos page**, sidebar and expression
list included — a headless Chromium loads the generated player with
`?capture=1` (which hides the transport bar), and the renderer steps `draw(n)`
one frame at a time. Nothing about the Desmos UI is faked. It costs about
90 ms/frame, so a 10 s scene takes roughly 35 s end to end.

**If you only want the mp4, use `--reel-only`** — it skips the `.desmos.js` and
`.desmos.json` and deletes the player html once it has been captured from.

That also removes the main quality ceiling. Graph size only matters when a human
opens the graph; the capture is offline and Playwright waits for each frame, so
Desmos' drawing speed is irrelevant to the result. `--budget` can then go far
higher than the interactive player would tolerate — 1800 instead of 450 roughly
quadruples the linework, which on a 640x360 source is the difference between a
face having an expression and not.

`--reel-scale 2` (default) renders the capture at double size and downscales it,
cleaning up the lines and sidebar text. `--reel-crf` sets x264 quality, 18 by
default.

The cost is time: high settings run about 9x realtime, so a 15s clip takes
~2.5 minutes. And on a low-bitrate source the extra sensitivity finds
compression artifacts as well as detail — keep `--budget` moderate and `--blur`
at 5 for anything below ~360p.

Desmos fills the top half, the clip the bottom half. The clip is 16:9 and its
half isn't, so it sits on a blurred, zoomed copy of itself rather than dead
letterbox bars.

The capture is taken at 4/3 the final size. Desmos' sidebar is a fixed pixel
width, so a larger viewport makes it a smaller share of the frame and leaves
more room for the drawing. Push that factor much higher and the sidebar text
stops being legible once it's scaled down, at which point it no longer reads as
Desmos.

Requires playwright for the capture, plus faster-whisper and mlx-lm for the
dialogue-aware scene grouping:

```bash
uv pip install --python .venv/bin/python playwright faster-whisper mlx-lm
./.venv/bin/playwright install chromium
```

The grouping reuses whatever MLX model is already in your HuggingFace cache —
no separate model server, and nothing downloads if it's already there.

## Size limits

Desmos rejects any list longer than **10,000 elements**, so frames are split
across chunks of parallel lists, each gated to draw only while the current
frame falls inside it. That's automatic; the run report tells you how many
chunks you got.

Total payload is what determines whether Desmos feels snappy. Roughly:

- under ~0.5 MB — comfortable
- 0.5–2 MB — works, noticeably heavier
- over 2 MB — the tool warns you; cut `--fps`, `--budget`, or `--duration`

**Frame rate is worth more than per-frame detail.** Smooth motion reads as
higher quality than extra lines in a stuttering frame, so when you have to
choose, keep `--fps` at the source rate and spend the cut on `--budget`. On the
39 s test clip at 24 fps, budget 400 stays readable while budget 250 loses the
face. Note that a frame update costs Desmos ~5 ms at that size, well inside the
41 ms a 24 fps frame allows — the constraint is payload, not draw speed.

## Gotchas worth knowing

These cost real debugging time, so they're worth writing down:

- **Leave the expressions panel open.** Desmos only evaluates expressions whose
  rows are actually rendered. Hiding the panel — or collapsing the data folder —
  blanks the whole graph. The big lists render as a truncated
  "N element list" chip, so leaving it open costs nothing.
- `lineWidth` and `fillOpacity` in graph state are **latex strings**, not
  numbers. Passing a number crashes the Desmos evaluation worker outright.
- You can't wrap a range in a piecewise to gate it — Desmos evaluates the
  branch anyway and errors with `non-arithmetic-range`. Clamp the index so the
  range is always valid, then gate the drawing with a separate `{a ≤ n ≤ b: 1}`
  multiplier.

## Uploading to YouTube

```bash
./.venv/bin/python upload_youtube.py --make-manifest out/reels   # fill season/episode
./.venv/bin/python upload_youtube.py --whoami                    # WHICH channel?
./.venv/bin/python upload_youtube.py --dry-run --extra-tags "#shorts"
./.venv/bin/python upload_youtube.py --extra-tags "#shorts"
```

**Check `--whoami` before the first upload of a session.** One Google account
can own several channels — your personal one plus any Brand Account channels.
The OAuth consent flow has a *"Choose a channel"* step after the account
picker, and clicking past it binds the token to the wrong channel *silently*:
uploads return 200 with real video ids, and the videos simply are not on the
channel you meant. YouTube cannot move a video between channels, so the only
repair is to delete and re-upload. This happened here — six videos went to the
personal channel before anyone noticed.

Two guards now make that failure loud instead of silent:

- The tool requests `youtube.readonly` alongside `youtube.upload`. Upload alone
  is write-only, so a token that has only it *cannot see where it is posting*.
- `EXPECT_CHANNEL` (`@desmos.guy1`) is verified before any upload and aborts on
  a mismatch. Override per-run with `--expect-channel`, or `''` to skip.

`--reauth` throws the saved login away and re-runs the picker.

One trap when changing scopes: `Credentials.from_authorized_user_file(path,
SCOPES)` *overwrites* `creds.scopes` with whatever you passed in, so checking
that attribute always says the token is fine. Compare against the `scopes` key
read straight out of `token.json` instead.

`--delete-after` removes the local file, but only once `videos.list` confirms
the upload is on the channel at the requested privacy. An id back from
`videos.insert` is not enough to delete a source file on.

**Two separate daily ceilings, and the tighter one is not the API quota.**
The project quota (~1600 units per upload) is the documented limit, but
YouTube also caps uploads per *channel* per day — `uploadLimitExceeded`, a
plain HTTP 400. A young channel gets a couple of dozen. Measured here: 26
uploads in one day, then a hard stop. The uploader now treats both as
end-of-day and stops immediately; re-running resumes from `uploaded.json`.

`token.json` and `client_secrets.json` are live credentials and are gitignored.

## Autopilot

One command finds clips, renders them, posts to YouTube, and leaves copies for
Instagram:

```bash
./.venv/bin/python autopilot.py --count 10
./.venv/bin/python autopilot.py --count 2 --no-upload   # stop before posting
./.venv/bin/python harvest.py --dry-run --count 10      # rank candidates only
```

**Query wording decides whether harvesting works at all.** Measured across 40
results each: `"family guy funny moments"` returns a median duration of 1457s -
hour-long compilations, **zero** usable clips. `"family guy clip"` returns 11
in the 8-45s band and `"family guy shorts"` returns 15. The searches that sound
most on-topic are the least productive, so `QUERIES` in `harvest.py` is tuned
by measurement rather than intuition.

Metadata is fetched in two stages because they cost very differently: a flat
search returns id/title/duration/views for ~40 results in about a second
without touching the videos, and only the few that survive the duration filter
get a full metadata fetch (which is where resolution and upload date live).

Everything that can reject a clip runs **before** the renderer, because
identification costs ~10s and rendering ~5min. In the first live run, 2 of 4
downloaded clips were rejected: one musical number matching on 2 phrase hits,
and one clip whose entire dialogue was a repeated word. Both are failure modes
that had previously wasted a full render each.

### Render cost, and where it actually goes

A render is three stages, and they are wildly unequal. Measured on a 13.8s clip
(331 frames):

| stage | time | share |
|---|---|---|
| trace (8 worker processes) | 6.9s | 4% |
| **browser screenshot capture** | **160.8s** | **94%** |
| ffmpeg encode | 3.9s | 2% |

**`--jobs` tunes the 4%.** Adding tracing workers cannot help: making tracing
instantaneous would save 4% of a render. The stage that matters is `reel.py`
screenshotting the Desmos page one frame at a time, so `--capture-jobs` is the
flag that changes how long a render takes.

#### The capture was quietly wrong, not just slow

Chasing the speed question turned up a correctness bug. `__settle` waited for the
canvas fingerprint to stop changing for `stable` consecutive animation frames,
with `stable = 2` - about 33ms at 60fps. That is not long enough: Desmos pauses
longer than that *in the middle* of an update, so the screenshot caught the
background redrawn but the moving character not yet redrawn.

Ground truth was established by raising the threshold until the output stopped
changing (`stable=12` and `stable=24` are byte-identical, so 12 is converged):

| setting | vs converged output |
|---|---|
| stable=2 (old default) | **39 of 40 frames wrong** |
| stable=4 | 40/40 identical |
| stable=6 (new default) | 40/40 identical |

So every reel rendered before this had subtly incomplete linework on
essentially every frame. The artifacts are small - typically well under 1% of
pixels - which is why they were never noticed.

#### Why capture is timing-sensitive at all

| condition | frames differing (of 40) |
|---|---|
| serial, idle, run twice | 0 |
| serial, under CPU load | 4 |
| 4 parallel browsers, old stable=2 | 22 |

Serial capture on an idle machine is perfectly deterministic, which is what hid
the bug: it was consistently wrong. Any contention changes the timing, so
parallelism exposed it immediately. Raising `stable` fixes both.

#### Current settings

`REEL_STABLE` (default 6) and `REEL_MAXFRAMES` (default 120) tune the settle;
`--capture-jobs` (default: auto) sets parallel browsers. Auto caps at **4**
even on a 15-core machine, because Chromium wants roughly one *performance*
core per page and this box has 5. Measured at 120 frames:

| workers | ms/frame | speedup | byte-identical |
|---|---|---|---|
| 1 | 728 | - | reference |
| 4 | 318 | 2.29x | yes |
| 6 | 291 | 2.51x | yes at stable=6, **no at stable=4** |

Six workers buy 8% for 50% more memory and less timing margin, so 4 is the
default. Four browsers peak around 9 GB.

**Net: a full render of the same clip went 171.6s -> 110.4s (1.55x) while
becoming pixel-correct.** The per-frame capture cost is 486ms -> 318ms. Note
that `--capture-jobs 1` is now *slower* than the old default (728ms vs 486ms):
the correctness fix costs real time, and parallelism is what pays for it.

Render cost still scales with clip length; the ratio is now roughly **8x clip
duration** (was ~12x), so a batch ETA is about `8 * sum(clip_durations)`. It is
reliable in aggregate, not per clip - the old 12x ratio ran 7.3x to 15.5x
across ten clips, driven by how much frame reuse the tracer finds.

### Which search queries are worth running

`QUERIES` in `harvest.py` is tuned by measurement, and the metric that matters
is not in-band results but **new** in-band results - ones not already in
`seen.json`. Once the corpus is large, a query that returns 40 famous clips you
have all posted is worth nothing.

Two patterns held when 18 candidates were probed (new in-band out of 40, on
2026-08-29):

**Generic quality adjectives are structurally dead.** `family guy best moment`
and `family guy funny scene` each returned **0 in band on two consecutive
runs**. They surface multi-minute compilations, not 8-45s clips, so this is not
saturation that time will fix - waiting does not help. Both removed.

**Secondary characters are nearly untapped**, because the only character
queries were the four leads:

| query | new in-band |
|---|---|
| `family guy herbert` | 25 |
| `family guy consuela` | 24 |
| `family guy carter pewterschmidt` | 22 |
| `family guy evil monkey` | 20 |
| `family guy mort` | 19 |
| `family guy gag` | 17 |
| `family guy tom tucker` | 15 |
| `family guy mayor adam west` | 15 |

Several returned band == new: essentially nothing they surface had ever been
seen. Against the previous champion `family guy cutaway gag` at 13, eight
candidates beat it. Adding them took a dry-run pool from **37 to 60**.

Two things this does *not* do. It does not make reels perform better - nothing
computable predicts plays here (see the analysis section); it fixes *yield*,
the duplicate rejections that turn a 10-clip run into 8. And broader queries
surface off-topic clips: a 46.8M-view *Croods* clip ranked first in the dry
run, because the score is view-driven. That is harmless and self-correcting -
`identify()` rejects it as "no episode match", and every attempted candidate is
written to `seen.json` whether or not it downloaded, so it costs one download
once and never returns. A title filter would be worse: legitimate clips like
"Cartermiroquai" never say "family guy".

### Source clips must be landscape

`MIN_ASPECT = 1.2` in `harvest.py`, checked twice: on metadata in `enrich()`
(cheap, avoids the download) and again with `ffprobe` on the file that actually
arrived, because format selection can return a different rendition than the
metadata described. 4:3 is 1.33 and 16:9 is 1.78, so 1.2 admits both and
rejects square (1.0) and portrait (0.56).

Do not add `/shorts` tabs to `harvest_channels.txt`. Every entry costs a
metadata fetch and none survive the aspect filter.


### Plays settle at 48h - measured, not assumed

Two insight fetches 53 hours apart let this be checked directly rather than
inferred. Growth in watch time over that window, bucketed by how old each post
already was at the first fetch:

| age at first fetch | n | median growth | max |
|---|---|---|---|
| <24h | 11 | **+35.3%** | +18664% |
| 24-48h | 12 | +12.7% | +132% |
| 48-72h | 12 | +0.9% | +17% |
| 72-96h | 20 | +0.6% | +7.7% |
| >96h | 15 | +0.5% | +1.8% |

Posts already past 48h grew a **median of +0.7%**, and 1 of 47 grew more than
10%. The `age_h >= 48` maturity filter in `analyze.py` is correct.

The practical consequence: a post's numbers mean nothing before 48h, and a
batch published today will look like a failure tomorrow no matter how good it
is. Of 1,202 hours gained across those two days, 62% came from posts still
inside their first 48h and only 7% from the 11 posts published in between -
which is what "my new videos are doing badly" looks like when it is really just
the ramp.

**Beware of using a backup file's mtime as "when this data was fetched".** `cp`
stamps the copy with the current time, which made every post look days older
than it was and inverted this entire conclusion on the first attempt.


### Why there is no view-prediction model

Ranking candidates by predicted retention would order them **at random** with
respect to views. Measured on 68 posts:

| relationship | r |
|---|---|
| predicted retention (from duration) vs plays | **+0.02** |
| actual measured retention vs plays | +0.40 |
| duration vs plays | -0.15 |

The retention model is real (out-of-sample R^2 = 0.49) but it is duration in
disguise, and duration does not separate winners from losers - the top ten
posts by plays run 8s-32s and the bottom ten run 6s-40s. The predictive part of
retention only exists *after* posting.

So selection uses hard filters plus one external signal: **the view count the
original uploader got**, on the theory that a clip other people watched is a
joke that lands. That is a hypothesis, not a result. `source_meta.json` records
it for every autopilot post so `analyze.py` can test it against real play
counts once enough have accumulated.

### Three silent failures found on the first 10-clip run

None of these threw an error. All three looked like normal operation.

**1. Shorts were invisible to the harvester.** `--flat-playlist` reports
`duration: None` for every Short, and `gather()` dropped anything without a
duration (`if not vid or not dur: continue`), so no Short could ever be
considered. Unknown-duration entries are now carried through to `enrich()`,
capped by `unknown_cap` since each costs a metadata round-trip.

**This fix was, in practice, a mistake, and the correction matters more than
the fix.** It grew the candidate pool from 81 to 120 (+48%), which looked like
a clear win - but Shorts are *portrait*, and a portrait source renders to a
tall sliver inside the top half of a 9:16 reel with unreadable line art. Three
vertical clips reached the channel before anyone noticed. Measured afterwards:
**20 of 20 Shorts from one channel were 608x1080**, and all 20 are now rejected
by `MIN_ASPECT`. The pool grew; the *usable* pool did not.

Two lessons worth keeping. A metric that moves in the right direction (pool
size) is not evidence the change was good - the pool was never the constraint.
And `MIN_WIDTH = 320` gave false confidence that resolution was being checked
at all: a 608x1080 Short passes a width test comfortably while being exactly
the wrong shape.

**2. The curated channel list was dead.** Both seeded handles were guessed
rather than checked: `@SophLoaf` has no `/videos` tab and `@Raizu` 404s. A dead
channel prints `0 results, 0 in band` - indistinguishable from a channel we had
simply exhausted - so the entire curated half of the harvest contributed
nothing for its whole existence. `harvest.py` now prints a loud
`! dead channel source` warning, and the file parser strips trailing
`# Channel Name` comments so entries can be labelled. Verify before adding:

```bash
yt-dlp --flat-playlist --dump-json --playlist-end 5 <url>
```

**3. The learning loop could not close.** `dataset.csv` keys rows by
`ig_<media_id>` (the copy Instagram serves back) while `source_meta.json` keys
by render tag (`auto_s03e09`), and nothing joined them - so a post's play count
could never be traced to the source clip it was harvested from, making the
"did source popularity predict anything" question unanswerable no matter how
many posts accumulated. Season/episode is not a fallback: those columns are
empty in all 68 rows.

Duration looks like the obvious key and is not. Matching on duration gives at
best **26 unique hits of 68** (+/-0.10s) with 12 ambiguous and 30 unmatched,
and *tightening* the tolerance makes it worse (12 unique at +/-0.05s) because
Instagram's re-encode shifts duration by more than a frame.

Dialogue is the key that works - it survives re-encoding and rescaling, and
everything is transcribed anyway. `link_posts.py` transcribes each Instagram
mp4, matches it against `transcripts.json` by n-gram overlap (the same test
`autopilot.py` dedupes with), and requires a margin over the runner-up so a
near-tie is recorded as ambiguous rather than guessed:

```bash
./.venv/bin/python link_posts.py           # writes post_links.csv
./.venv/bin/python analyze.py              # now ends with a SOURCE POPULARITY test
```

The test reports `n` and refuses to interpret fewer than 12 matured posts. It
prints the threshold it would need to beat, so a null result is as legible as a
positive one.

Measured on the first run: **56 of 68 posts linked**, median overlap 0.95 and
median margin 0.94, with **zero** ambiguous near-ties - against 26 for the best
duration-based attempt. Of the 12 that did not link, 4 have almost no dialogue
(unmatchable by design) and 8 score *exactly* 0.00, meaning their source render
is not in `transcripts.json` at all - legacy posts from before transcription
was routine. Exact zeros are the expected result for unrelated clips, since
4-gram overlap between different dialogue is genuinely nil; that specificity is
what makes the key trustworthy. Autopilot always writes a transcript, so new
posts always link.

As of the first 10-clip run the test still reports **n = 0**: autopilot posts
exist on YouTube but Instagram posting is manual, so none yet have both source
data and a matured play count.

## Identifying the episode

`episodes.py match` transcribes a clip and scores word 4-grams against a local
index of every episode script.

The source site lists three season-5 episodes a **second time** as season 6
(the Family Guy 5/6 numbering split) with identical dialogue. Both copies score
identically, which halved the runner-up margin and dragged genuinely perfect
matches down into "unidentifiable": the crack clip matched 80 of its 103 grams
and still scored 0.30 confidence — the same score a clip with no dialogue gets.
`identify()` now collapses tied-and-identical episodes to the lower season
before measuring the margin. That alone rescued two clips from the reject pile.

Confidence is not the whole story: a 5-word clip can score 1.00 off two gram
hits. Weigh hit *count* too, and treat a compilation's internal ordering as
corroboration — the `fg_*` set maps to season 11 in ascending episode order,
which independently backs up its thinnest matches.

## Setup

```bash
brew install ffmpeg yt-dlp
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/playwright install chromium
cp harvest_channels.example.txt harvest_channels.txt   # optional
```

Only `numpy` and `opencv-python-headless` are needed to render a clip with
`vid2desmos.py`. The rest are for harvesting, transcription, capture and upload.

`serve.py` is a small static server for previewing generated players locally.

## License

MIT, see `LICENSE`.
