#!/usr/bin/env python3
"""Upload finished reels to YouTube as Shorts.

Nothing here can run unattended the first time: Google requires you to click
through an OAuth consent screen once. After that the refresh token is cached in
token.json and uploads are hands-off.

    # 1. list the clips and fill in season/episode
    ./.venv/bin/python upload_youtube.py --make-manifest out/reels

    # 2. see exactly what would be posted, without posting
    ./.venv/bin/python upload_youtube.py --dry-run

    # 3. upload (private by default - flip to public when you trust it)
    ./.venv/bin/python upload_youtube.py --privacy public
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time

CLIENT_SECRETS = "client_secrets.json"
TOKEN = "token.json"
MANIFEST = "upload_manifest.csv"
STATE = "uploaded.json"
# The channel these are meant to land on. Checked before every upload, because
# picking the wrong channel on the consent screen is silent and unfixable after
# the fact - YouTube cannot move a video between channels.
EXPECT_CHANNEL = "@desmos.guy1"
# upload alone is write-only: with just that scope the tool cannot tell you
# WHICH channel it posted to, which is exactly how videos end up on the wrong
# one. readonly costs 1 quota unit per check and makes the target visible.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly"]

# An upload costs ~1600 quota units and a new project gets 10,000/day, so this
# is the real ceiling until you request more from Google.
DAILY_DEFAULT = 6


def build_service(force: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    # A Google account can own several channels (your personal one plus any
    # Brand Account channels). The token is bound to whichever channel you pick
    # on the consent screen, and there is no way to change it afterwards short
    # of authorizing again - so a stale token silently keeps posting to the old
    # channel. force=True throws it away and re-asks.
    if force and os.path.exists(TOKEN):
        os.replace(TOKEN, TOKEN + ".old")
        print(f"discarded {TOKEN} (kept as {TOKEN}.old) - re-authorizing")

    creds = None
    if os.path.exists(TOKEN):
        # Check the scopes RECORDED IN THE FILE, not creds.scopes: passing
        # SCOPES to from_authorized_user_file overwrites that attribute with
        # what we asked for, so it always looks satisfied and the stale token
        # sails through to fail at the first API call instead.
        try:
            granted = set(json.load(open(TOKEN)).get("scopes") or [])
        except (ValueError, OSError):
            granted = set()
        if not set(SCOPES) <= granted:
            print("saved login predates a scope this needs - re-authorizing")
        else:
            try:
                creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
            except ValueError:
                creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRETS):
                raise SystemExit(
                    f"missing {CLIENT_SECRETS}.\n"
                    "  Google Cloud console -> APIs & Services -> Credentials\n"
                    "  -> Create OAuth client ID -> Desktop app -> download JSON\n"
                    f"  and save it here as {CLIENT_SECRETS}")
            # Google hands back the scopes it granted, which may be ordered
            # differently than we asked; without this that raises.
            os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRETS, SCOPES)
            print("\n>>> A browser will open. Pick the Google account, then - if\n"
                  ">>> you are asked 'Choose a channel' - pick the channel you\n"
                  ">>> want the videos ON, not your personal one.\n", flush=True)
            creds = flow.run_local_server(port=0, prompt="consent")
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def channel_matches(ch, want: str) -> bool:
    """Accept a handle (@name), a channel id (UC...) or the display title."""
    if not want:
        return True
    want = want.strip().lower()
    cand = {ch["id"].lower(), ch["title"].lower(),
            ch["url"].lower(), ch["url"].lower().lstrip("@")}
    return want in cand or want.lstrip("@") in cand


def channel_info(svc):
    """Which channel is this token actually going to post to?"""
    r = svc.channels().list(part="snippet,contentDetails,statistics",
                            mine=True).execute()
    items = r.get("items", [])
    if not items:
        return None
    c = items[0]
    return {
        "id": c["id"],
        "title": c["snippet"]["title"],
        "url": c["snippet"].get("customUrl", "") or f"channel/{c['id']}",
        "videos": c.get("statistics", {}).get("videoCount", "?"),
        "uploads": c["contentDetails"]["relatedPlaylists"]["uploads"],
    }


def recent_uploads(svc, playlist: str, n: int = 10):
    """Everything on the channel, private included - this is the real check."""
    r = svc.playlistItems().list(part="snippet,status", playlistId=playlist,
                                 maxResults=n).execute()
    out = []
    for it in r.get("items", []):
        out.append((it["snippet"]["resourceId"]["videoId"],
                    it["snippet"]["title"],
                    it.get("status", {}).get("privacyStatus", "?")))
    return out


def videos_on_channel(svc, ids, channel_id):
    """Split ids into (on this channel, not returned by the API).

    Asks videos.list what channel each video actually belongs to. The obvious
    cheaper approach - intersecting with the channel's recent-uploads page -
    is wrong, and was: it only ever sees the most recent handful, so every
    older video looks like it went to a different channel. That reported
    "140 of 150 not on this channel" for a library that was entirely correct.

    videos.list takes at most 50 ids per call, so batch. Anything the API does
    not return is deleted, or not visible to this account.
    """
    here, gone = set(), set()
    ids = list(ids)
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        try:
            r = svc.videos().list(part="snippet", id=",".join(batch)).execute()
        except Exception as e:                      # nothing here is worth dying for
            print(f"  ! could not check {len(batch)} id(s): {e}", file=sys.stderr)
            continue
        got = {it["id"]: it for it in r.get("items", [])}
        for v in batch:
            it = got.get(v)
            if it is None:
                gone.add(v)
            elif it["snippet"].get("channelId") == channel_id:
                here.add(v)
    return here, gone


def show_whoami(svc) -> int:
    ch = channel_info(svc)
    if not ch:
        print("this account has no YouTube channel at all", file=sys.stderr)
        return 1
    print(f"posting as : {ch['title']}")
    print(f"channel id : {ch['id']}")
    print(f"channel url: https://www.youtube.com/{ch['url']}")
    print(f"videos     : {ch['videos']}")
    print(f"studio     : https://studio.youtube.com/channel/{ch['id']}/videos")
    vids = recent_uploads(svc, ch["uploads"])
    print(f"\nmost recent {len(vids)} upload(s) on this channel:")
    for vid, title, priv in vids:
        print(f"  [{priv:8}] {vid}  {title[:60]}")
    mine = {v["id"] for v in load_state().values()}
    if mine:
        here, gone = videos_on_channel(svc, mine, ch["id"])
        elsewhere = mine - here - gone
        print(f"\nof the {len(mine)} video(s) in {STATE}, "
              f"{len(here)} are on this channel")
        if elsewhere:
            print(f"  on a DIFFERENT channel ({len(elsewhere)}): "
                  + ", ".join(sorted(elsewhere)))
            print("  -> re-authorize with --reauth and pick the right channel")
        if gone:
            print(f"  {len(gone)} not returned by the API - deleted, or private "
                  f"to another account: " + ", ".join(sorted(gone)))
        if not elsewhere and not gone:
            print("  all accounted for")
    return 0


def make_manifest(folder: str, path: str = MANIFEST) -> None:
    """Write a template listing every mp4, for you to fill in."""
    vids = sorted(f for f in os.listdir(folder) if f.lower().endswith(".mp4"))
    if not vids:
        raise SystemExit(f"no .mp4 files in {folder}")
    existing = {}
    if os.path.exists(path):                      # keep anything already typed
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                existing[row["file"]] = (row.get("season", ""), row.get("episode", ""))
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "season", "episode"])
        for v in vids:
            s, e = existing.get(v, ("", ""))
            w.writerow([os.path.join(folder, v), s, e])
    print(f"wrote {path} with {len(vids)} row(s) - fill in season and episode")


def load_manifest(path: str = MANIFEST):
    if not os.path.exists(path):
        raise SystemExit(f"no {path}; run --make-manifest <folder> first")
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            fp = row["file"].strip()
            s, e = row.get("season", "").strip(), row.get("episode", "").strip()
            if not fp:
                continue
            if not os.path.exists(fp):
                print(f"  ! missing file, skipping: {fp}", file=sys.stderr)
                continue
            if not (s and e):
                print(f"  ! no season/episode, skipping: {os.path.basename(fp)}",
                      file=sys.stderr)
                continue
            rows.append((fp, s, e))
    return rows


def caption(season: str, episode: str, extra_tags: str = "") -> str:
    t = f"Season {season}, Episode {episode} in desmos #familyguy #desmos"
    return (t + " " + extra_tags).strip()


def load_state():
    if os.path.exists(STATE):
        with open(STATE) as f:
            return json.load(f)
    return {}


def save_state(d):
    with open(STATE, "w") as f:
        json.dump(d, f, indent=1)


def verify_live(svc, vid: str, want_privacy: str):
    """Confirm the video really is on the channel before deleting the source.

    videos.insert returning an id is not proof enough to delete a local file
    by: ask the API for the video back and check it is there, owned by us, and
    at the visibility we asked for.
    """
    # videos.list does not return a video the instant videos.insert succeeds -
    # YouTube needs a moment to process it. Checking once treats that lag as a
    # failure and needlessly keeps the local file. Retry before believing it.
    items = []
    for wait in (0, 5, 10, 20):
        if wait:
            time.sleep(wait)
        try:
            r = svc.videos().list(part="status", id=vid).execute()
        except Exception as e:
            return False, f"could not verify: {e}"
        items = r.get("items", [])
        if items:
            break
    if not items:
        return False, "not visible on this channel after ~35s"
    st = items[0].get("status", {})
    if st.get("uploadStatus") in ("rejected", "failed"):
        return False, f"upload {st.get('uploadStatus')}: " \
                      f"{st.get('rejectionReason') or st.get('failureReason')}"
    if st.get("privacyStatus") != want_privacy:
        return False, f"privacy is {st.get('privacyStatus')}, not {want_privacy}"
    return True, ""


def region_report(svc, ids):
    """Which of these videos are region-blocked, and in how many countries?

    A Content ID claim does not remove a video - it leaves it public and
    blocks it, so nothing in videos.list().status looks wrong. The only
    signal is contentDetails.regionRestriction. Measured on this channel:
    the two videos ever blocked were the two longest (94s and 90s), both
    blocked in 249 countries, i.e. everywhere.
    """
    out = []
    ids = list(ids)
    for i in range(0, len(ids), 50):
        r = svc.videos().list(part="contentDetails,snippet",
                              id=",".join(ids[i:i + 50])).execute()
        for it in r.get("items", []):
            rr = it["contentDetails"].get("regionRestriction") or {}
            blocked = rr.get("blocked") or []
            allowed = rr.get("allowed")
            if blocked or allowed is not None:
                out.append({"id": it["id"],
                            "title": it["snippet"]["title"],
                            "blocked": len(blocked),
                            "allowed": len(allowed) if allowed is not None else None})
    return out


def print_region_report(svc, ids, note_delay=True):
    hits = region_report(svc, ids)
    if not hits:
        print(f"region check: none of {len(list(ids))} video(s) are blocked")
    else:
        print(f"\n!! region-blocked ({len(hits)}):")
        for h in hits:
            where = (f"blocked in {h['blocked']}" if h["blocked"]
                     else f"viewable in only {h['allowed']}")
            print(f"   {h['id']}  {where} countries  {h['title'][:44]}")
        print("   A Content ID claim, not a strike - the channel is not at "
              "risk, but a blocked video gets no views.")
    if note_delay:
        print("   (claims can land minutes to hours after upload; re-run "
              "--region-report later to be sure)")
    return hits


def upload_one(svc, path: str, title: str, privacy: str):
    from googleapiclient.http import MediaFileUpload
    from googleapiclient.errors import HttpError

    body = {
        "snippet": {
            "title": title[:100],          # YouTube hard limit
            "description": title,
            "tags": ["family guy", "desmos", "shorts", "animation", "math"],
            "categoryId": "23",            # Comedy
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(path, chunksize=-1, resumable=True, mimetype="video/mp4")
    req = svc.videos().insert(part="snippet,status", body=body, media_body=media)

    for attempt in range(5):
        try:
            resp = None
            while resp is None:
                _, resp = req.next_chunk()
            return resp["id"], None
        except HttpError as e:
            # 403 here is nearly always the daily quota; retrying will not help
            if e.resp.status in (500, 502, 503, 504):
                time.sleep(2 ** attempt + random.random())
                continue
            return None, f"HTTP {e.resp.status}: {e}"
        except Exception as e:                       # transport hiccup
            time.sleep(2 ** attempt + random.random())
            if attempt == 4:
                return None, str(e)
    return None, "gave up after retries"


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload reels to YouTube as Shorts")
    ap.add_argument("--make-manifest", metavar="FOLDER",
                    help="write a CSV listing every mp4 in FOLDER, then stop")
    ap.add_argument("--manifest", default=MANIFEST)
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"],
                    default="private",
                    help="private by default so a mistake is not public")
    ap.add_argument("--limit", type=int, default=DAILY_DEFAULT,
                    help="stop after this many uploads (quota is ~6/day)")
    ap.add_argument("--extra-tags", default="",
                    help="appended to every title, e.g. '#shorts'")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be uploaded and exit")
    ap.add_argument("--delete-after", action="store_true",
                    help="delete the local file once the upload is verified "
                         "live on the channel at the requested privacy")
    ap.add_argument("--pause", type=float, default=4.0,
                    help="seconds between uploads")
    ap.add_argument("--region-report", action="store_true",
                    help="check every video ever uploaded for region blocks, "
                         "then stop")
    ap.add_argument("--no-region-check", action="store_true",
                    help="skip the region check after uploading")
    ap.add_argument("--whoami", action="store_true",
                    help="show which channel the saved login posts to, and stop")
    ap.add_argument("--reauth", action="store_true",
                    help="discard the saved login and pick the channel again")
    ap.add_argument("--expect-channel", default=EXPECT_CHANNEL,
                    help="abort unless the login is this channel "
                         "(handle, id or title); '' disables the check")
    args = ap.parse_args()

    if args.make_manifest:
        make_manifest(args.make_manifest, args.manifest)
        return 0

    if args.whoami or args.reauth:
        return show_whoami(build_service(force=args.reauth))

    if args.region_report:
        svc = build_service()
        ids = [v["id"] for v in load_state().values()]
        print(f"checking {len(ids)} uploaded video(s) for region blocks ...")
        print_region_report(svc, ids, note_delay=False)
        return 0

    rows = load_manifest(args.manifest)
    done = load_state()
    todo = [r for r in rows if r[0] not in done][:args.limit]

    print(f"{len(rows)} in manifest, {len(done)} already uploaded, "
          f"{len(todo)} queued (limit {args.limit}, privacy {args.privacy})")
    for fp, s, e in todo:
        print(f"  {os.path.basename(fp)}  ->  {caption(s, e, args.extra_tags)}")
    if args.dry_run or not todo:
        if args.dry_run:
            print("\ndry run - nothing uploaded")
        return 0

    svc = build_service()
    ch = channel_info(svc)
    if not ch:
        print("this account has no YouTube channel", file=sys.stderr)
        return 1
    print(f"\nposting to: {ch['title']}  "
          f"(https://www.youtube.com/{ch['url']})")
    if not channel_matches(ch, args.expect_channel):
        print(f"\n! this login posts to '{ch['title']}', not "
              f"'{args.expect_channel}'.\n"
              "  Nothing was uploaded. Run with --reauth and pick the right\n"
              "  channel on the 'Choose a channel' screen, or pass\n"
              "  --expect-channel '' to upload here anyway.", file=sys.stderr)
        return 1

    ok = 0
    for fp, s, e in todo:
        title = caption(s, e, args.extra_tags)
        print(f"\nuploading {os.path.basename(fp)} ...", flush=True)
        vid, err = upload_one(svc, fp, title, args.privacy)
        if vid:
            done[fp] = {"id": vid, "title": title, "privacy": args.privacy,
                        "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_state(done)
            ok += 1
            print(f"  https://youtu.be/{vid}")
            if args.delete_after:
                live, why = verify_live(svc, vid, args.privacy)
                if live:
                    try:
                        os.remove(fp)
                        done[fp]["deleted_local"] = True
                        save_state(done)
                        print(f"  verified live - deleted {os.path.basename(fp)}")
                    except OSError as ex:
                        print(f"  ! could not delete {fp}: {ex}", file=sys.stderr)
                else:
                    print(f"  ! KEPT local file - {why}", file=sys.stderr)
            if args.pause:
                time.sleep(args.pause)
        else:
            print(f"  ! failed: {err}", file=sys.stderr)
            low = (err or "").lower()
            # Two different daily ceilings, and neither is worth retrying
            # today: the API project quota, and YouTube's own per-channel
            # upload cap (uploadLimitExceeded), which is the tighter of the
            # two - a young channel gets a couple of dozen videos a day.
            if "quota" in low or "uploadlimitexceeded" in low or \
                    "exceeded the number of videos" in low:
                print("  daily upload limit reached - stopping; "
                      "re-run tomorrow and it resumes where it left off",
                      file=sys.stderr)
                break
    print(f"\n{ok} uploaded")
    if ok and not args.no_region_check:
        fresh = [done[fp]["id"] for fp, _, _ in todo if fp in done]
        print()
        print_region_report(svc, fresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
