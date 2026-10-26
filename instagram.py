"""Instagram profile reels listing via Playwright + direct download.

The reels tab embeds its first page of reels (code, cover, play count) as JSON in the HTML; further pages
come from GraphQL calls that Instagram only answers for logged-in users. So guests get the first ~12 reels
and logging in once unlocks the full list. yt-dlp needs cookies for Instagram, so each reel page is opened
in the same browser profile instead and its embedded media links are downloaded directly: the 1080p DASH
video + audio muxed with ffmpeg, or the 720p progressive MP4 when that is all there is.
"""
import html
import json
import os
import re
import subprocess
import threading
import time

from douyin import DownloadExpired
from douyin import download as _download
from facebook import DATA_DIR, USER_AGENT, _install_chromium
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROFILE_DIR = os.path.join(DATA_DIR, "ig-profile")
SESSION_MARKER = os.path.join(DATA_DIR, "ig-session.txt")
REFERER = "https://www.instagram.com/"
RESERVED_PATHS = {"reel", "reels", "p", "tv", "explore", "accounts", "stories", "direct", "about", "developer"}

JSON_SCRIPTS_JS = """() => [...document.querySelectorAll('script[type="application/json"]')]
    .map(s => s.textContent).filter(t => t.includes('"code"'))"""

# the browser profile can only be opened by one process at a time; downloads run in parallel threads
_profile_lock = threading.Lock()

class LoginRequired(Exception):
    pass


def username_of(url):
    m = re.search(r"instagram\.com/([A-Za-z0-9._]+)", url or "")
    return m.group(1) if m and m.group(1).lower() not in RESERVED_PATHS else ""


def normalize_user_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if not url.startswith("http"):
        url = "https://" + url
    user = username_of(url)
    return f"https://www.instagram.com/{user}/reels/" if user else url


def video_url(code):
    return f"https://www.instagram.com/reel/{code}/"


def is_logged_in():
    return os.path.exists(SESSION_MARKER)


def _forget_session():
    try:
        os.remove(SESSION_MARKER)
    except OSError:
        pass


def _launch(p, headless):
    os.makedirs(PROFILE_DIR, exist_ok=True)

    def launch():
        return p.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chromium", headless=headless, user_agent=USER_AGENT, locale="en-US",
            viewport=None if not headless else {"width": 1280, "height": 1000})

    try:
        return launch()
    except PlaywrightError as ex:
        if "Executable doesn't exist" not in str(ex):
            raise
        _install_chromium()
        return launch()


def _has_session(ctx):
    return any(c["name"] == "sessionid" and c["value"] for c in ctx.cookies("https://www.instagram.com"))


def login():
    """Open a visible browser for the user to log in; closes itself once logged in."""
    with _profile_lock, sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
        try:
            while not _has_session(ctx):
                page.wait_for_timeout(1000)
            page.wait_for_timeout(3000)
            with open(SESSION_MARKER, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            ctx.close()
        except Exception:  # noqa: BLE001 - user closed the window before logging in
            pass
    return is_logged_in()


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _is_video(node):
    return (node.get("product_type") == "clips" or node.get("media_type") == 2
            or bool(node.get("video_versions")) or "Video" in (node.get("__typename") or ""))


def _owner_pk(node):
    owner = node.get("user") or node.get("owner") or {}
    return str(owner.get("pk") or owner.get("id") or "")


def _caption(node):
    cap = node.get("caption")
    if isinstance(cap, dict):
        text = cap.get("text") or ""
    else:
        edges = (node.get("edge_media_to_caption") or {}).get("edges") or [{}]
        text = (edges[0].get("node") or {}).get("text") or ""
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:100].rstrip()
    return ""


def _thumb(node):
    cands = (node.get("image_versions2") or {}).get("candidates") or []
    return node.get("display_uri") or node.get("display_url") or node.get("thumbnail_src") or (
        cands[0].get("url") if cands else "")


def _best_mp4(node):
    versions = [v for v in node.get("video_versions") or [] if v.get("url")]
    if not versions:
        return "", 0
    best = max(versions, key=lambda v: (v.get("height") or 0) * (v.get("width") or 0))
    return best["url"], best.get("height") or 0


def _best_dash(manifest):
    """(video_url, video_height, audio_url) of the best DASH representations, or Nones."""
    best = {"video": None, "audio": None}
    for rep_ in re.finditer(r"<Representation\b([^>]*)>(.*?)</Representation>", manifest or "", re.S):
        attrs, body = rep_.group(1), rep_.group(2)
        mime = re.search(r'mimeType="(\w+)/', attrs)
        url = re.search(r"<BaseURL>([^<]+)</BaseURL>", body)
        if not mime or not url:
            continue
        height = int((re.search(r'\bheight="(\d+)"', attrs) or [0, 0])[1])
        bandwidth = int((re.search(r'\bbandwidth="(\d+)"', attrs) or [0, 0])[1])
        key = (height, bandwidth)
        kind = mime.group(1)
        if kind in best and (best[kind] is None or key > best[kind][0]):
            best[kind] = (key, html.unescape(url.group(1)))
    video, audio = best["video"], best["audio"]
    return (video[1] if video else None), (video[0][0] if video else 0), (audio[1] if audio else None)


def _media(node):
    mp4, mp4_height = _best_mp4(node)
    video, video_height, audio = _best_dash(node.get("video_dash_manifest"))
    if video and audio and video_height > mp4_height:
        return {"mp4": mp4, "video": video, "audio": audio, "height": video_height}
    return {"mp4": mp4, "height": mp4_height}


def _media_nodes(payloads):
    for payload in payloads:
        for node in _walk(payload):
            if isinstance(node.get("code"), str) and (node.get("pk") or node.get("id")) and _is_video(node):
                yield node


def _parse(texts):
    out = []
    for text in texts:
        try:
            out.append(json.loads(text))
        except ValueError:
            pass
    return out


def fetch_user_reels(url, on_progress=None, max_idle_rounds=4):
    """Returns ([{id, url, title, thumb, plays}], username, limited) - limited = guest saw only page 1."""
    user = username_of(url)
    responses = []

    def on_response(resp):
        if "/graphql" in resp.url or "/api/v1/clips/" in resp.url:
            try:
                responses.append(resp.text())
            except Exception:  # noqa: BLE001 - body already gone / non-text
                pass

    with _profile_lock, sync_playwright() as p:
        ctx = _launch(p, headless=True)
        try:
            logged_in = _has_session(ctx)
            if is_logged_in() and not logged_in:
                _forget_session()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            if "/accounts/login" in page.url:
                if logged_in:
                    _forget_session()
                    raise LoginRequired("Instagram session expired. Please reconnect your account.")
                raise LoginRequired("Instagram asks to log in before showing this profile. Connect Instagram and fetch again.")

            reels, seen, owner, idle = [], set(), "", 0
            while idle < max_idle_rounds:
                payloads = _parse(page.evaluate(JSON_SCRIPTS_JS)) + _parse(responses)
                if not owner:
                    owner = next((str(n.get("pk") or n.get("id")) for n in _walk(payloads)
                                  if n.get("username") == user and (n.get("pk") or n.get("id"))), "")
                new = 0
                for node in _media_nodes(payloads):
                    code = node["code"]
                    if code in seen or (owner and _owner_pk(node) and _owner_pk(node) != owner):
                        continue
                    seen.add(code)
                    new += 1
                    reels.append({
                        "id": code,
                        "url": video_url(code),
                        "title": _caption(node),
                        "thumb": _thumb(node),
                        "plays": node.get("play_count") or node.get("view_count") or node.get("video_view_count") or 0,
                    })
                idle = 0 if new else idle + 1
                if on_progress:
                    on_progress(len(reels))
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(2000 if logged_in else 1000)
        finally:
            ctx.close()
    if not reels:
        raise RuntimeError("Instagram did not return any reels (the profile may be private, empty or "
                           "temporarily blocked). Check the link or connect Instagram and try again.")
    return reels, user, not logged_in


def resolve(code):
    """Open the reel page and read its media links and caption: ({mp4, video?, audio?, height}, caption)."""
    with _profile_lock, sync_playwright() as p:
        ctx = _launch(p, headless=True)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(video_url(code), wait_until="domcontentloaded", timeout=60000)
            for _ in range(20):
                for node in _media_nodes(_parse(page.evaluate(JSON_SCRIPTS_JS))):
                    if node["code"] == code and node.get("video_versions"):
                        return _media(node), _caption(node)
                page.wait_for_timeout(500)
        finally:
            ctx.close()
    raise RuntimeError("Could not find the video of this reel (it may be private or removed).")


def _remove(*paths):
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass


def download(media, path, ffmpeg, on_progress=None, should_cancel=None, cancelled_exc=Exception):
    """Save a reel to `path`: DASH video + audio muxed losslessly when available, else the progressive MP4."""
    if not media.get("video"):
        return _download(media["mp4"], path, on_progress, should_cancel, cancelled_exc, referer=REFERER)
    video, audio, muxed = path + ".video", path + ".audio", path + ".mux.mp4"

    def part(start, size):
        return (lambda frac: on_progress(start + frac * size)) if on_progress else None

    try:
        _download(media["video"], video, part(0, 0.85), should_cancel, cancelled_exc, referer=REFERER)
        _download(media["audio"], audio, part(0.85, 0.15), should_cancel, cancelled_exc, referer=REFERER)
        r = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", video, "-i", audio, "-map", "0:v:0", "-map", "1:a:0",
                            "-c", "copy", "-movflags", "+faststart", muxed],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            raise RuntimeError("ffmpeg could not join video and audio: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        os.replace(muxed, path)
    except FileNotFoundError:  # no ffmpeg: settle for the 720p file
        return _download(media["mp4"], path, on_progress, should_cancel, cancelled_exc, referer=REFERER)
    finally:
        _remove(video, audio, video + ".part", audio + ".part", muxed)
    return path


if __name__ == "__main__":
    import sys

    start = time.time()
    data, name, limited = fetch_user_reels(normalize_user_url(sys.argv[1]), lambda n: print("found", n, flush=True))
    print(name, "limited" if limited else "full", len(data), "in", round(time.time() - start), "s")
    print(json.dumps(data[:3], ensure_ascii=False, indent=1))
    if data:
        media, caption = resolve(data[0]["id"])
        print("resolved", caption, media["height"], sorted(media))
