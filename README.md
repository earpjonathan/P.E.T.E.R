# vid2desmos

Turns a video into a line-art animation that plays inside Desmos.

Write-up, with interactive demos and measurements: https://projects.jonathanearp.xyz/desmos/

```bash
./.venv/bin/python vid2desmos.py myclip.mp4 -o out/myclip --duration 8
```

Or take a whole compilation, cut it into scenes, and render a 9:16 split-screen
video for each scene, ready to post:

```bash
./.venv/bin/python vid2desmos.py compilation.mp4 -o out/fg/clip \
    --scenes --reel --fps 24 --budget 500 --eps 1.2 --min-len 20
```

You get three files:

| file | what it's for |
| --- | --- |
| `myclip.html` | standalone player. Just open it, there's nothing to install |
| `myclip.desmos.js` | paste into the console at desmos.com/calculator |
| `myclip.desmos.json` | raw Desmos graph state |

## Getting it into the real Desmos

1. Open <https://www.desmos.com/calculator>
2. Open the console (<kbd>Cmd</kbd>+<kbd>Option</kbd>+<kbd>J</kbd>)
3. Paste the whole `.desmos.js` file and press enter

The first time, Chrome blocks pasting into the console and asks you to type
`allow pasting`.

## How it works

Each frame is edge-detected, the edges are walked into polylines and
simplified, and the whole animation is packed into flat lists. One parametric
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

Curves are off by default because of performance. Desmos samples a parametric
adaptively: a straight segment converges immediately, but a cubic has to be
subdivided until it looks smooth. Every drawn segment is a separate curve in
the list, so switching them all to cubics multiplies the drawing work per
frame, and that's what limits the frame rate. With straight lines you can spend
the same budget on more frames.

`K` is a Catmull-Rom spline written as its equivalent cubic Bézier, with each
tangent scaled by a per-vertex weight:

```
K(a,b,c,d,p,q,t) = (1-t)³b + 3(1-t)²t(b + p(c-a)/24) + 3(1-t)t²(c - q(d-b)/24) + t³c
```

The weights do most of the work. If everything is curved, it looks melted:
straight edges bow and sharp corners round off. So a vertex only gets curvature
if its turn angle is in a middle band:

| turn at the vertex | weight | why |
| --- | --- | --- |
| `< --turn-lo` (10°) | 0 | a straight run, where smoothing only adds wobble |
| between | 4 | a real curve, like a cheek, an ear or a cushion |
| `> --turn-hi` (60°) | 0 | a real corner, like a door frame or teeth, so it stays sharp |

At weight 0 both Bézier control points collapse onto the anchors, which draws
an exactly straight segment. So one expression covers curves and straight
lines, and the data picks which one per vertex. A spline segment needs the
point before and after it, and both are already in the flat list, so this
needs no extra control points. The weights go in a list that was already there
(see below). `--curves lines` forces every weight to 0.

### One list does two jobs

`D` holds a tangent weight (0..4) for a point that starts a drawn segment, and
the sentinel 5 for the last point of a stroke. So a single list says both where
strokes break and how curved each join is. Reading the far end's weight as
`mod(c, 5)` turns the sentinel back into the 0 a stroke end should have anyway.
That's why selective curves cost nothing extra: they reuse a list that was
already there.

### Held frames

A repeated frame only costs an offset, because several frame numbers point at
the same span of the point list. That breaks the ordering, so each frame
carries an explicit end offset (`E`) instead of using the next frame's start.

Frames are compared by their traced geometry. An earlier version compared
source frames by mean pixel difference, which spreads a small movement (a
mouth, a hand) across the whole frame until it disappears. On the test clip,
that metric called 89% of frames duplicates when only 42% really were. It threw
away real motion in 48% of frames without any warning, and the result looked
like 5 fps no matter how fast `n` advanced. Comparing the drawn output means
motion can't be dropped that way.

`--dedupe` is the tolerance in grid units, default 0 (exact matches only).
Raising it barely helps (721 → 700 distinct frames at tol 10), because
compression noise changes stroke topology as well as point positions. It isn't
a useful setting and can only cost you motion, so leave it at 0.

## Audio

The player embeds the soundtrack as a data URI (ffmpeg, mono AAC 96k) and uses
it as the clock: the frame number comes from `audio.currentTime` instead of
being counted separately.

The frame update has to be driven by `requestAnimationFrame`. A timer keeps
firing at its own rate whether or not Desmos has finished drawing, so updates
pile up and the picture falls further and further behind the sound. On the
39 s clip the drift grew steadily to 75 frames and kept going. rAF only fires
when the browser is ready to paint, so the driver can't get ahead of the
renderer. If Desmos is slow, the extra frames are skipped and you still see
whatever the audio is playing now. With rAF the drift stays bounded.

A 250 ms watchdog covers the case where rAF is throttled to nothing (a
background tab, or a window that isn't compositing), so the picture can't
freeze while the sound keeps playing.

Playback starts paused because browsers block autoplay with sound until you
click something. `--no-audio` skips it. Audio only works in the `.html` player,
since there's nowhere to put it on desmos.com.

## Tuning

The main setting is `--budget` (line segments per frame).

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

`--preview` writes a `.preview.mp4` of exactly the lines that will be drawn,
which is much faster to iterate on than reloading a graph.

### Where the detail goes

Two things decide what survives, and they matter more than `--budget` does.

The Canny threshold is tuned per frame to hit `--edge-density` (default 0.055,
the fraction of pixels that become edges). Both kinds of fixed threshold fail,
in opposite directions:

- The textbook `median*(1±sigma)` rule is built for photographs. On a bright,
  flat-shaded cartoon it lands around 115/228 and keeps only the boldest
  outlines. A character's eyes and mouth are never detected, so no amount of
  budget can bring them back. It wiped out every face without any warning.
- A fixed low threshold is just as wrong. A tiled floor floods the frame with
  faint repeating lines and buries the characters.

Aiming for a set amount of linework handles both. It digs down for the face in
a sparse close-up and backs off to bold outlines in a busy room.

When a frame is over budget, strokes are ranked by how important they are.
`--detail-share` (default 0.65) reserves that fraction of the budget for busy
areas before any is spent on flat background, and `--detail-eps` /`--flat-eps`
simplify the subject less and the background more.

Ranking by length is the obvious choice, and it's wrong here. A face is a
cluster of short strokes and a couch is one long contour, so ranking by length
deletes faces and keeps furniture.

Both of these change what the budget is spent on without changing how much gets
drawn, so the frame rate stays the same. Segments per frame don't move, and the
picture gets a lot easier to read.

Motion seems like the natural way to judge importance, but these shows animate
on twos and hold poses, so the difference between frames is often exactly zero.
On a held pose, even a 0.3 s baseline barely registered. Local edge density
works on a still frame, so that's the main signal, and motion only adds to it
when there is some.

`--min-len` helps more than you'd expect. Canny leaves a lot of short speckle,
and every speck uses budget that should go to real lines. Raising it to
`--min-len 20` on a cartoon visibly cleaned up the image and let more real
detail through at the same file size. Push it up until features you care about
start disappearing.

`--budget` and `--eps` interact. When a frame goes over the budget, the tool
coarsens `eps` until it fits, so lowering `--eps` on its own does nothing,
because it just gets coarsened straight back. Raise both or neither.

Other flags: `--start` / `--duration` to trim, `--curves lines` to turn off
curves, `--turn-lo`/`--turn-hi` for the curvature band, `--dedupe`,
`--no-audio`, `--low`/`--high` for manual Canny thresholds, `--invert` for
binary mode, `--color`, `--line-width`.

`--mode binary` is for silhouette-style content. On a cartoon with detail inside
the outlines it fills the character in as a blob. That's fine for
`life.mp4`-style footage and wrong for Family Guy.

When a frame goes over budget the tool first simplifies everything more, and
only drops whole strokes (shortest first) as a last resort, because losing
detail evenly looks better than losing whole objects.

## A folder of already-cut clips (recommended)

If you already have one joke per file, cut by hand or downloaded already cut,
skip scene detection:

```bash
./.venv/bin/python vid2desmos.py --batch ~/clips -o ~/clips/out \
    --reel --fps 24 --budget 500 --eps 1.2 --min-len 20
```

Every video in the folder becomes one reel, named after its file. This is the
most reliable way to do it: automatic scene detection has to guess where a joke
ends, and a person cutting clips doesn't.

Getting clips that are already cut:

- The official Family Guy YouTube channel posts one scene per video, free and
  in 1080p. `yt-dlp <url>` pulls a whole clip, and
  `yt-dlp --download-sections "*01:30-02:45" <url>` pulls part of one.
- getyarn.io is indexed by subtitle and gives a short MP4 per spoken line. It
  only works in a browser and blocks scripted access.
- There's no Frinkiac equivalent for Family Guy. That project only covers
  Simpsons, Futurama, Rick & Morty and West Wing.
- YouTube chapter markers would be joke boundaries marked by a person, but out
  of 18 Family Guy videos I sampled, none had chapters.

## Cutting a compilation into clips

`--scenes` finds hard cuts by the mean absolute HSV difference between
consecutive frames. A cut changes every pixel at once, so it spikes far above
ordinary motion. Shots are then added together until a scene reaches
`--min-scene`, and anything past `--max-scene` is split.

Finding shots isn't the same as finding jokes. A cutaway gag is a few seconds
in a setting chosen to have nothing to do with its setup, so it looks and
sounds exactly like a real clip boundary. None of these worked, all measured on
a Family Guy compilation:

| approach | result |
| --- | --- |
| silence between clips | zero silences at -32 dB, because of a continuous music bed |
| audio texture change across the cut | 0.030 at a mid-joke cut vs 0.026 and 0.021 at real boundaries, so no separation |
| minimum length on both sides | pulls the cutaway forward into the next joke |
| merge shortest shot backwards | merges the wrong pairs elsewhere |

Dialogue timing doesn't work either. On the same footage, the pause at the cut
that wrongly split the bar scene from its caveman punchline was 0.78 s, and the
pause at a cut that really does start a new clip was 0.40 s, so the signal
points the wrong way. Dialogue runs straight through real boundaries and stops
inside jokes.

### What works: reading the dialogue

`--group llm` (the default) transcribes the audio once with faster-whisper,
then asks a local model, for each cut, whether the lines after it continue the
bit before it. The meaning of the lines is the only thing that tells you.

The prompt mattered more than the model. Asked plainly whether a scene change
starts a new joke, the model answered "NEW" for all 79 cuts. It has to be told
what a cutaway is (a gag that looks unrelated but is the punchline to the line
before it) and allowed one short reason before it picks a label. With that it
scored 4/5 on cases I checked by hand and got both real failures right: the
hallway scene splits off, and the bar scene keeps its caveman punchline.

Decisions are cached in `<base>_jokecuts.json`, so re-runs are instant and you
can edit a single verdict by hand. On the test file, 48 of 79 cuts start a new
joke and 31 are internal.

Set the model with `--llm-model` (any MLX model id). `--group speech` falls back
to dialogue pauses and `--group visual` to cuts alone, and the tool switches to
these automatically if `mlx-lm` or `faster-whisper` is missing.

It's still a judgement call, so there's a review step:

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
`--reel`, `<base>_sNN.reel.mp4`. `--scene-limit N` only does the first N, which
is worth doing before a long run. Tune with `--scene-threshold` (lower finds
more cuts), `--min-scene` and `--max-scene`.

## The 9:16 reel

`--reel` produces a 1080x1920 mp4: the Desmos animation on top, the original
clip below, with the scene's own audio.

The top half is a real screenshot of the Desmos page, sidebar and expression
list included. A headless Chromium loads the generated player with
`?capture=1` (which hides the transport bar), and the renderer steps `draw(n)`
one frame at a time. Nothing about the Desmos UI is faked. It costs about
90 ms a frame, so a 10 s scene takes roughly 35 s end to end.

If you only want the mp4, use `--reel-only`. It skips the `.desmos.js` and
`.desmos.json` and deletes the player html once the capture is done.

Capturing offline also removes the main limit on quality. Graph size only
matters when a person opens the graph. The capture is offline and Playwright
waits for each frame, so Desmos' drawing speed doesn't affect the result.
`--budget` can go far higher than the interactive player would handle. 1800
instead of 450 roughly quadruples the linework, which on a 640x360 source
decides whether a face has an expression at all.

`--reel-scale 2` (default) renders the capture at double size and scales it
down, which cleans up the lines and sidebar text. `--reel-crf` sets x264
quality, 18 by default.

The cost is time. High settings run about 9x realtime, so a 15 s clip takes
about 2.5 minutes. On a low-bitrate source the extra sensitivity also picks up
compression artifacts, so keep `--budget` moderate and `--blur` at 5 for
anything below ~360p.

Desmos fills the top half and the clip fills the bottom half. The clip is 16:9
and its half isn't, so it sits on a blurred, zoomed copy of itself instead of
black bars.

The capture is taken at 4/3 the final size. Desmos' sidebar is a fixed pixel
width, so a larger viewport makes it a smaller share of the frame and leaves
more room for the drawing. Push that factor much higher and the sidebar text
stops being readable once it's scaled down, and then it doesn't look like
Desmos any more.

The capture needs playwright, and the dialogue-aware scene grouping needs
faster-whisper and mlx-lm:

```bash
uv pip install --python .venv/bin/python playwright faster-whisper mlx-lm
./.venv/bin/playwright install chromium
```

The grouping reuses whatever MLX model is already in your HuggingFace cache.
There's no separate model server, and nothing downloads if it's already there.

## Size limits

Desmos rejects any list longer than 10,000 elements, so frames are split across
chunks of parallel lists, each set to draw only while the current frame falls
inside it. That happens automatically, and the run report tells you how many
chunks you got.

The total payload decides whether Desmos feels quick. Roughly:

- under ~0.5 MB: comfortable
- 0.5–2 MB: works, but noticeably heavier
- over 2 MB: the tool warns you. Cut `--fps`, `--budget` or `--duration`

Frame rate is worth more than detail per frame. Smooth motion looks better than
extra lines in a stuttering frame, so when you have to choose, keep `--fps` at
the source rate and take the cut from `--budget`. On the 39 s test clip at
24 fps, budget 400 stays readable and budget 250 loses the face. A frame update
costs Desmos about 5 ms at that size, well inside the 41 ms a 24 fps frame
allows, so the limit is the payload size and drawing speed is fine.

## Gotchas

These took me a long time to debug:

- Leave the expressions panel open. Desmos only evaluates expressions whose rows
  are actually rendered, so hiding the panel or collapsing the data folder
  blanks the whole graph. The big lists show up as a short "N element list"
  chip, so leaving the panel open costs nothing.
- `lineWidth` and `fillOpacity` in graph state are LaTeX strings. Passing a
  number crashes the Desmos evaluation worker.
- You can't wrap a range in a piecewise to gate it, because Desmos evaluates the
  branch anyway and errors with `non-arithmetic-range`. Clamp the index so the
  range is always valid, then gate the drawing with a separate
  `{a ≤ n ≤ b: 1}` multiplier.

## Uploading to YouTube

```bash
./.venv/bin/python upload_youtube.py --make-manifest out/reels   # fill season/episode
./.venv/bin/python upload_youtube.py --whoami                    # which channel is it logged in to?
./.venv/bin/python upload_youtube.py --dry-run --extra-tags "#shorts"
./.venv/bin/python upload_youtube.py --extra-tags "#shorts"
```

Check `--whoami` before the first upload of a session. One Google account can
own several channels: your personal one plus any Brand Account channels. The
OAuth consent flow has a "Choose a channel" step after the account picker, and
clicking past it binds the token to the wrong channel without any warning.
Uploads return 200 with real video ids, and the videos just aren't on the
channel you meant. YouTube can't move a video between channels, so the only fix
is to delete and re-upload. This happened to me: six videos went to my personal
channel before I noticed.

Two checks now catch it:

- The tool requests `youtube.readonly` as well as `youtube.upload`. The upload
  scope is write-only, so a token with only that scope can't see where it's
  posting.
- `EXPECT_CHANNEL` (`@desmos.guy1`) is checked before any upload, and the run
  stops if it doesn't match. Override it per run with `--expect-channel`, or
  pass `''` to skip it.

`--reauth` throws away the saved login and runs the picker again.

One trap when changing scopes: `Credentials.from_authorized_user_file(path,
SCOPES)` overwrites `creds.scopes` with whatever you passed in, so checking that
attribute always says the token is fine. Compare against the `scopes` key read
straight out of `token.json` instead.

`--delete-after` removes the local file, but only once `videos.list` confirms
the upload is on the channel at the requested privacy. An id back from
`videos.insert` isn't enough proof to delete the source file.

There are two separate daily limits, and the tighter one isn't the API quota.
The project quota (~1600 units per upload) is the documented limit, but YouTube
also caps uploads per channel per day: `uploadLimitExceeded`, a plain HTTP 400.
A young channel gets a couple of dozen. I hit a hard stop after 26 uploads in
one day. The uploader now treats both limits as the end of the day and stops
straight away, and re-running picks up from `uploaded.json`.

`token.json` and `client_secrets.json` are live credentials and are gitignored.

## Autopilot

One command finds clips, renders them, posts to YouTube, and leaves copies for
Instagram:

```bash
./.venv/bin/python autopilot.py --count 10
./.venv/bin/python autopilot.py --count 2 --no-upload   # stop before posting
./.venv/bin/python harvest.py --dry-run --count 10      # rank candidates only
```

The wording of the search query decides whether harvesting works at all. Over
40 results each, `"family guy funny moments"` returns a median duration of
1457 s (long compilations) and zero usable clips. `"family guy clip"` returns
11 in the 8-45 s band and `"family guy shorts"` returns 15. The searches that
sound most on-topic are the least useful, so `QUERIES` in `harvest.py` is
picked by measuring each one.

Metadata is fetched in two stages because they cost very different amounts. A
flat search returns id/title/duration/views for ~40 results in about a second
without touching the videos, and only the few that pass the duration filter get
a full metadata fetch (which is where resolution and upload date are).

Everything that can reject a clip runs before the renderer, because
identification takes ~10 s and rendering ~5 min. In the first live run, 2 of 4
downloaded clips were rejected: a musical number that matched on 2 phrase hits,
and a clip whose entire dialogue was one repeated word. Before this, each of
those would have wasted a full render.

### Render cost, and where it actually goes

A render has three stages, and they take very different amounts of time.
Measured on a 13.8 s clip (331 frames):

| stage | time | share |
|---|---|---|
| trace (8 worker processes) | 6.9s | 4% |
| browser screenshot capture | 160.8s | 94% |
| ffmpeg encode | 3.9s | 2% |

`--jobs` only affects the 4%. Adding tracing workers can't help much, since
making tracing instant would save 4% of a render. The slow stage is `reel.py`
screenshotting the Desmos page one frame at a time, so `--capture-jobs` is the
flag that changes how long a render takes.

#### The capture was also wrong

Looking into the speed turned up a correctness bug. `__settle` waited for the
canvas fingerprint to stop changing for `stable` consecutive animation frames,
with `stable = 2`, about 33 ms at 60 fps. That isn't long enough. Desmos pauses
for longer than that in the middle of an update, so the screenshot caught the
background redrawn but the moving character not yet redrawn.

To get the right answer I raised the threshold until the output stopped
changing (`stable=12` and `stable=24` are byte-identical, so 12 is converged):

| setting | vs converged output |
|---|---|
| stable=2 (old default) | 39 of 40 frames wrong |
| stable=4 | 40/40 identical |
| stable=6 (new default) | 40/40 identical |

So every reel rendered before this had slightly incomplete linework on almost
every frame. The artifacts are small, usually well under 1% of pixels, which is
why nobody noticed.

#### Why capture depends on timing at all

| condition | frames differing (of 40) |
|---|---|
| serial, idle, run twice | 0 |
| serial, under CPU load | 4 |
| 4 parallel browsers, old stable=2 | 22 |

Serial capture on an idle machine is perfectly deterministic, which is what hid
the bug: it was wrong the same way every time. Any contention changes the
timing, so running in parallel showed it straight away. Raising `stable` fixes
both.

#### Current settings

`REEL_STABLE` (default 6) and `REEL_MAXFRAMES` (default 120) tune the settle,
and `--capture-jobs` (default: auto) sets the number of parallel browsers. Auto
stops at 4 even on a 15-core machine, because Chromium wants roughly one
performance core per page and this machine has 5. Measured at 120 frames:

| workers | ms/frame | speedup | byte-identical |
|---|---|---|---|
| 1 | 728 | - | reference |
| 4 | 318 | 2.29x | yes |
| 6 | 291 | 2.51x | yes at stable=6, no at stable=4 |

Six workers are 8% faster for 50% more memory and less timing margin, so 4 is
the default. Four browsers peak around 9 GB.

Overall, a full render of the same clip went from 171.6 s to 110.4 s (1.55x),
and the frames are now correct. The capture cost per frame went from 486 ms to
318 ms. `--capture-jobs 1` is now slower than the old default (728 ms vs
486 ms): the correctness fix costs time, and running in parallel makes it back.

Render time still scales with clip length. It's now roughly 8x the clip
duration (it was ~12x), so a batch takes about `8 * sum(clip_durations)`. That
holds for a batch but not for a single clip. The old 12x ratio ranged from 7.3x
to 15.5x across ten clips, depending on how much frame reuse the tracer finds.

### Which search queries are worth running

`QUERIES` in `harvest.py` is picked by measurement, and the number that matters
is new in-band results, meaning ones not already in `seen.json`. Once you've
posted a lot, a query that returns 40 famous clips you've already posted is
worth nothing.

Two patterns held when I tested 18 candidate queries (new in-band out of 40, on
2026-08-29).

Generic quality words don't work at all. `family guy best moment` and
`family guy funny scene` each returned 0 in band on two runs in a row. They
bring up multi-minute compilations instead of 8-45 s clips, so it isn't a case
of having used them up, and waiting won't help. I removed both.

Secondary characters had barely been searched, because the only character
queries were the four main characters:

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

Several returned band == new, meaning almost nothing they brought up had been
seen before. Eight of them beat the previous best query,
`family guy cutaway gag`, at 13. Adding them took a dry-run pool from 37 to 60.

This doesn't make reels perform better, since nothing I can compute predicts
plays (see the analysis section). What it fixes is yield: the duplicate
rejections that turn a 10-clip run into 8. Broader queries also bring up
off-topic clips. A 46.8M-view Croods clip ranked first in the dry run, because
the score is driven by views. That's harmless and sorts itself out:
`identify()` rejects it as "no episode match", and every candidate it tries is
written to `seen.json` whether or not it downloaded, so it costs one download
once and never comes back. A title filter would be worse, because real clips
like "Cartermiroquai" never say "family guy".

### Source clips must be landscape

`MIN_ASPECT = 1.2` in `harvest.py`, checked twice: on the metadata in
`enrich()` (cheap, and it avoids the download) and again with `ffprobe` on the
file that actually arrived, because format selection can return a different
version than the metadata described. 4:3 is 1.33 and 16:9 is 1.78, so 1.2 lets
both through and rejects square (1.0) and portrait (0.56).

Don't add `/shorts` tabs to `harvest_channels.txt`. Every entry costs a
metadata fetch and none of them pass the aspect filter.

### Plays settle after 48 hours

Two insight fetches 53 hours apart let me check this directly. Growth in watch
time over that window, grouped by how old each post already was at the first
fetch:

| age at first fetch | n | median growth | max |
|---|---|---|---|
| <24h | 11 | +35.3% | +18664% |
| 24-48h | 12 | +12.7% | +132% |
| 48-72h | 12 | +0.9% | +17% |
| 72-96h | 20 | +0.6% | +7.7% |
| >96h | 15 | +0.5% | +1.8% |

Posts already past 48 hours grew a median of +0.7%, and 1 of 47 grew more than
10%. So the `age_h >= 48` maturity filter in `analyze.py` is right.

In practice, a post's numbers mean nothing before 48 hours, and a batch
published today will look like a failure tomorrow however good it is. Of 1,202
hours gained across those two days, 62% came from posts still inside their
first 48 hours, and only 7% from the 11 posts published in between. That's what
"my new videos are doing badly" looks like when it's really just the ramp-up.

Don't use a backup file's mtime as "when this data was fetched". `cp` stamps the
copy with the current time, which made every post look days older than it was
and flipped this whole conclusion the first time I tried it.

### Why there's no view-prediction model

Ranking candidates by predicted retention would order them about as well as
random, as far as views go. Measured on 68 posts:

| relationship | r |
|---|---|
| predicted retention (from duration) vs plays | +0.02 |
| actual measured retention vs plays | +0.40 |
| duration vs plays | -0.15 |

The retention model holds up (out-of-sample R^2 = 0.49), but it's really just
duration, and duration doesn't separate winners from losers: the top ten posts
by plays run 8s-32s and the bottom ten run 6s-40s. The part of retention that
does predict plays can only be measured after posting.

So selection uses hard filters plus one outside signal: the view count the
original uploader got, on the theory that a clip other people watched is a
joke that lands. That's an untested guess. `source_meta.json` records it for
every autopilot post so `analyze.py` can test it against real play counts once
there are enough.

### Three problems the first 10-clip run didn't report

None of these threw an error, and all three looked like normal operation.

#### 1. The harvester couldn't see Shorts

`--flat-playlist` reports `duration: None` for every Short, and `gather()`
dropped anything without a duration (`if not vid or not dur: continue`), so no
Short could ever be considered. Entries with an unknown duration are now passed
through to `enrich()`, capped by `unknown_cap` since each one costs a metadata
round trip.

That fix turned out to be a mistake. It grew the candidate pool from 81 to 120
(+48%), which looked like a clear win, but Shorts are portrait, and a portrait
source renders as a tall sliver inside the top half of a 9:16 reel with line art
you can't read. Three vertical clips reached the channel before I noticed. When
I measured afterwards, 20 of 20 Shorts from one channel were 608x1080, and all
20 are now rejected by `MIN_ASPECT`. The pool grew, but the usable pool didn't.

The pool size going up didn't mean the change was good, because the pool was
never what limited the output. And `MIN_WIDTH = 320` made it look like
resolution was being checked properly: a 608x1080 Short passes a width test
easily and is exactly the wrong shape.

#### 2. The curated channel list was dead

I'd guessed both seeded handles instead of checking them: `@SophLoaf` has no
`/videos` tab and `@Raizu` returns 404. A dead channel prints
`0 results, 0 in band`, which looks the same as a channel that's been used up,
so the curated half of the harvest never contributed anything. `harvest.py`
now prints a `! dead channel source` warning, and the file parser strips
trailing `# Channel Name` comments so entries can be labelled. Check a channel
before adding it:

```bash
yt-dlp --flat-playlist --dump-json --playlist-end 5 <url>
```

#### 3. Posts couldn't be linked to their source clips

`dataset.csv` keys rows by `ig_<media_id>` (the copy Instagram serves back),
while `source_meta.json` keys by render tag (`auto_s03e09`), and nothing joined
them. So a post's play count could never be traced back to the clip it came
from, and the question "does source popularity predict anything?" couldn't be
answered however many posts there were. Season and episode can't be used
instead, because those columns are empty in all 68 rows.

Duration looks like the obvious key, but matching on it gives at best 26 unique
hits out of 68 (+/-0.10 s), with 12 ambiguous and 30 unmatched, and tightening
the tolerance makes it worse (12 unique at +/-0.05 s) because Instagram's
re-encode shifts the duration by more than a frame.

Dialogue works as a key. It survives re-encoding and rescaling, and everything
gets transcribed anyway. `link_posts.py` transcribes each Instagram mp4, matches
it against `transcripts.json` by n-gram overlap (the same test `autopilot.py`
uses to find duplicates), and requires a margin over the runner-up, so a
near-tie is recorded as ambiguous instead of guessed:

```bash
./.venv/bin/python link_posts.py           # writes post_links.csv
./.venv/bin/python analyze.py              # now ends with a SOURCE POPULARITY test
```

The test reports `n` and won't interpret fewer than 12 matured posts. It prints
the threshold it would need to beat, so a null result is as easy to read as a
positive one.

On the first run, 56 of 68 posts linked, with median overlap 0.95, median
margin 0.94 and no ambiguous near-ties. The best duration-based attempt got 26.
Of the 12 that didn't link, 4 have almost no dialogue (so they can't be
matched), and 8 score exactly 0.00, meaning their source render isn't in
`transcripts.json` at all. Those are old posts from before every clip was
transcribed. Exact zeros are what you'd expect for unrelated clips, since
different dialogue shares essentially no 4-grams, and that's why the key can be
trusted. Autopilot always writes a transcript, so new posts always link.

As of the first 10-clip run the test still reports `n = 0`. Autopilot posts are
on YouTube, but Instagram posting is manual, so none of them have both source
data and a matured play count yet.

## Identifying the episode

`episodes.py match` transcribes a clip and scores word 4-grams against a local
index of every episode script.

The source site lists three season-5 episodes a second time as season 6 (the
Family Guy 5/6 numbering split), with identical dialogue. Both copies score the
same, which halved the runner-up margin and dragged perfect matches down to
"unidentifiable". The crack clip matched 80 of its 103 grams and still scored
0.30 confidence, the same score a clip with no dialogue gets. `identify()` now
merges tied, identical episodes into the lower season before measuring the
margin. That alone rescued two clips from the reject pile.

Confidence isn't enough on its own, because a 5-word clip can score 1.00 from
two gram hits. Look at the hit count too, and use a compilation's order as a
cross-check. The `fg_*` set maps to season 11 in ascending episode order, which
backs up its weakest matches.

## Setup

```bash
brew install ffmpeg yt-dlp
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/playwright install chromium
cp harvest_channels.example.txt harvest_channels.txt   # optional
```

Rendering a clip with `vid2desmos.py` only needs `numpy` and
`opencv-python-headless`. The rest are for harvesting, transcription, capture
and upload.

`serve.py` is a small static server for previewing generated players locally.

## License

MIT, see `LICENSE`.
