#!/usr/bin/env python3
"""Export Instagram Reels performance so it can be joined to clip features.

    ./.venv/bin/python instagram.py setup      # what to do on Meta's side
    ./.venv/bin/python instagram.py fetch      # media + insights -> instagram.csv
    ./.venv/bin/python instagram.py download   # pull the posted videos back
    ./.venv/bin/python instagram.py join       # insights + features -> dataset.csv

A Creator account can use the Instagram API with Instagram Login, which needs
no linked Facebook Page. Put the access token in instagram_token.json as
{"token": "..."} - never on the command line, where it lands in shell history.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://graph.instagram.com/v23.0"
TOKEN_FILE = "instagram_token.json"
MEDIA_JSON = "instagram_media.json"
OUT_CSV = "instagram.csv"

# Meta renames these regularly - `plays` and `impressions` were retired in
# favour of `views`. Ask for everything plausible, then drop whatever the API
# rejects rather than failing the whole export over one bad name.
WANT = ["views", "reach", "likes", "comments", "shares", "saved",
        "total_interactions", "ig_reels_avg_watch_time",
        "ig_reels_video_view_total_time", "reposts",
        "crossposted_views", "facebook_views"]
# `impressions` is dead for anything posted after 2 July 2024 - `views`
# replaced it. Not requested; it would just be dropped.


def token() -> str:
    if os.path.exists(TOKEN_FILE):
        t = json.load(open(TOKEN_FILE)).get("token", "").strip()
        if t:
            return t
    t = os.environ.get("IG_TOKEN", "").strip()
    if t:
        return t
    raise SystemExit(
        f"no access token.\n  Put it in {TOKEN_FILE} as "
        '{"token": "IGAA..."}\n  then: chmod 600 ' + TOKEN_FILE +
        "\n  (run `instagram.py setup` for how to get one)")


def api(path: str, **params):
    params["access_token"] = token()
    url = f"{BASE}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
    for attempt in range(5):
        try:
            with urllib.request.urlopen(url, timeout=45) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            # 4 = app rate limit, 613 = too many calls: both want a wait
            if e.code in (429, 500, 502, 503) or '"code":4' in body:
                time.sleep(2 ** attempt * 3)
                continue
            raise SystemExit(f"HTTP {e.code} on {path}\n{body[:600]}")
        except urllib.error.URLError:
            time.sleep(2 ** attempt)
    raise SystemExit(f"gave up on {path}")


def cmd_setup(_) -> int:
    print("""\
Instagram API with Instagram Login - Creator account, no Facebook Page needed.

The app starts in Development mode, and in that mode it can only read an
Instagram account that holds a role on the app. Signing into Instagram in
another tab does nothing on its own - this is the step everyone misses.

 1. developers.facebook.com -> My Apps -> Create app
    Use case "Other" -> type "Business"
 2. Add product -> Instagram -> "API setup with Instagram login"
 3. INVITE THE ACCOUNT AS A TESTER  <- do this before anything else
      Left sidebar -> App roles -> Roles -> "Add people"
      -> choose "Instagram tester" -> enter your Instagram username
 4. ACCEPT THE INVITE, from the Instagram side:
      instagram.com -> Settings -> Apps and websites -> Tester invites
      (phone: Settings -> Website permissions -> Apps and websites)
    It sits pending until you accept, and step 5 fails silently until then.
 5. Back on "API setup with Instagram login", section 1 -> "Add account".
    It should now connect and list the account.
 6. The token lives in SECTION 1, next to the connected account
    ("Generate token"). Not section 3 - that is the login flow for other
    people's accounts, which you do not need.
 7. Save it:

      echo '{"token": "PASTE_IT_HERE"}' > instagram_token.json
      chmod 600 instagram_token.json

 8. ./.venv/bin/python instagram.py fetch

Section 4 "Complete app review" does NOT apply to you: review is only needed
to read accounts other than your own. Development mode plus the tester role
is enough for your own data.

The banner offering "API setup with Facebook login" is about hashtag search
and account-level totals. Media insights for your own reels - views, reach,
saves, shares, average watch time - all work on this path.

The token is long-lived (60 days); extend it with `fetch --refresh-token`.""")
    return 0


def refresh() -> int:
    r = api("refresh_access_token", grant_type="ig_refresh_token")
    t = r.get("access_token")
    if not t:
        raise SystemExit(f"unexpected response: {r}")
    json.dump({"token": t, "refreshed": time.strftime("%Y-%m-%d")},
              open(TOKEN_FILE, "w"))
    os.chmod(TOKEN_FILE, 0o600)
    print(f"token refreshed, valid ~{r.get('expires_in',0)//86400} more days")
    return 0


def all_media() -> list:
    """Every post, following paging cursors to the end."""
    fields = ("id,caption,media_type,media_product_type,media_url,permalink,"
              "timestamp,thumbnail_url")
    out, after = [], None
    while True:
        p = {"fields": fields, "limit": 100}
        if after:
            p["after"] = after
        r = api("me/media", **p)
        out.extend(r.get("data", []))
        after = (r.get("paging", {}).get("cursors", {}) or {}).get("after")
        if not after or not r.get("data"):
            break
        print(f"  ...{len(out)} media", flush=True)
    return out


def insights_for(mid: str, metrics: list) -> tuple[dict, list]:
    """Insights for one media, dropping metric names the API rejects."""
    m = list(metrics)
    while m:
        try:
            r = api(f"{mid}/insights", metric=",".join(m))
            return ({d["name"]: d["values"][0].get("value")
                     for d in r.get("data", [])}, m)
        except SystemExit as e:
            bad = [x for x in m if x in str(e)]
            if not bad:
                return {}, m               # not a metric problem; skip media
            m = [x for x in m if x not in bad]
            print(f"    (dropping unsupported metric: {', '.join(bad)})")
    return {}, m


def cmd_fetch(args) -> int:
    me = api("me", fields="id,username,account_type,media_count")
    print(f"account: @{me.get('username')}  ({me.get('account_type')})  "
          f"{me.get('media_count')} media")
    if args.refresh_token:
        return refresh()

    media = all_media()
    json.dump(media, open(MEDIA_JSON, "w"), indent=1)
    reels = [m for m in media
             if m.get("media_product_type") == "REELS"
             or m.get("media_type") == "VIDEO"]
    print(f"{len(media)} media, {len(reels)} reel(s)/video(s)")

    metrics, rows = list(WANT), []
    for i, m in enumerate(reels, 1):
        vals, metrics = insights_for(m["id"], metrics)
        row = {"id": m["id"], "permalink": m.get("permalink", ""),
               "timestamp": m.get("timestamp", ""),
               "caption": (m.get("caption") or "").replace("\n", " ").strip()}
        row.update(vals)
        rows.append(row)
        if i % 10 == 0 or i == len(reels):
            print(f"  insights {i}/{len(reels)}", flush=True)
        time.sleep(0.25)

    cols = ["id", "timestamp", "permalink", "caption"] + \
           [c for c in WANT if any(c in r for r in rows)]
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: x.get("timestamp", "")):
            w.writerow(r)
    print(f"\n{len(rows)} row(s) -> {OUT_CSV}")
    v = [int(r["views"]) for r in rows if str(r.get("views", "")).isdigit()]
    if v:
        v.sort()
        print(f"views: min {v[0]:,}  median {v[len(v)//2]:,}  max {v[-1]:,}  "
              f"total {sum(v):,}")
    return 0


def cmd_download(args) -> int:
    """Pull the posted videos back - the only way to measure ones already
    deleted locally by the uploader's --delete-after."""
    if not os.path.exists(MEDIA_JSON):
        raise SystemExit(f"run `fetch` first ({MEDIA_JSON} missing)")
    media = json.load(open(MEDIA_JSON))
    os.makedirs(args.dir, exist_ok=True)
    n = 0
    for m in media:
        url = m.get("media_url")
        if not url or (m.get("media_product_type") != "REELS"
                       and m.get("media_type") != "VIDEO"):
            continue
        dst = os.path.join(args.dir, f"ig_{m['id']}.mp4")
        if os.path.exists(dst):
            continue
        try:
            urllib.request.urlretrieve(url, dst)
            n += 1
            print(f"  {os.path.basename(dst)}", flush=True)
        except Exception as e:
            print(f"  ! {m['id']}: {e}", file=sys.stderr)
        time.sleep(0.4)
    print(f"downloaded {n} new video(s) into {args.dir}")
    return 0


def cmd_join(args) -> int:
    """Attach view counts to clip features, matching on duration.

    Captions cannot identify a clip on their own: several reels share the exact
    same "Season N, Episode M" text. Duration is near-unique per clip, so it is
    the key, with the caption used only to confirm.
    """
    if not os.path.exists(OUT_CSV):
        raise SystemExit(f"run `fetch` first ({OUT_CSV} missing)")
    if not os.path.exists(args.features):
        raise SystemExit(f"no {args.features}; run features.py first")
    ig = list(csv.DictReader(open(OUT_CSV)))
    feats = list(csv.DictReader(open(args.features)))

    dur = {}
    if os.path.exists(args.dir):
        import subprocess
        for f in sorted(os.listdir(args.dir)):
            if not f.endswith(".mp4"):
                continue
            r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "format=duration", "-of", "csv=p=0",
                                os.path.join(args.dir, f)],
                               capture_output=True, text=True)
            try:
                dur[f[3:-4]] = float(r.stdout.strip())
            except ValueError:
                pass

    used, joined, unmatched = set(), [], []
    for r in ig:
        d = dur.get(r["id"])
        best = None
        if d:
            cand = [(abs(float(x["duration_s"]) - d), x) for x in feats
                    if x["clip"] not in used and x["duration_s"]]
            cand.sort(key=lambda t: t[0])
            if cand and cand[0][0] < 0.6:
                best = cand[0][1]
        if best:
            used.add(best["clip"])
            joined.append({**best, **{k: r.get(k, "") for k in r
                                      if k not in ("caption",)}})
        else:
            unmatched.append(r)

    if joined:
        cols = list(joined[0])
        with open(args.out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for j in joined:
                w.writerow(j)
    print(f"joined {len(joined)} reel(s) -> {args.out}")
    print(f"posted but unmatched to a local clip: {len(unmatched)}")
    never = [x["clip"] for x in feats if x["clip"] not in used]
    print(f"rendered but NOT matched to any post: {len(never)}")
    for c in never[:20]:
        print(f"   {c}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="how to get a token")
    f = sub.add_parser("fetch", help="media + insights -> instagram.csv")
    f.add_argument("--refresh-token", action="store_true",
                   help="extend the 60-day token and exit")
    d = sub.add_parser("download", help="download the posted videos")
    d.add_argument("--dir", default="out/ig")
    j = sub.add_parser("join", help="insights + features -> dataset.csv")
    j.add_argument("--features", default="features.csv")
    j.add_argument("--dir", default="out/ig")
    j.add_argument("--out", default="dataset.csv")
    args = ap.parse_args()
    return {"setup": cmd_setup, "fetch": cmd_fetch,
            "download": cmd_download, "join": cmd_join}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
