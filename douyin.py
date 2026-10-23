"""Douyin user video listing via Playwright + direct download.

yt-dlp can neither list a Douyin user nor download from it (needs signed requests), so the user page is
opened in a real browser and its own `aweme/post` API responses are read. Those contain captions, covers
and ready-to-use MP4 links (no watermark) that download with a Douyin Referer. Guests only get the first
page (~20 newest videos); logging in once unlocks the full list.
"""
import os
import re
import time
import urllib.error
import urllib.request

from facebook import DATA_DIR, USER_AGENT, _install_chromium
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

PROFILE_DIR = os.path.join(DATA_DIR, "douyin-profile")
SESSION_MARKER = os.path.join(DATA_DIR, "douyin-session.txt")
POST_API = "/aweme/v1/web/aweme/post/"
LOGIN_WALL_TEXT = "登录后查看更多作品"  # "log in to see more works"
LOGIN_DIALOG_TEXT = "扫码登录"  # "scan QR code to log in"
REFERER = "https://www.douyin.com/"

SCROLL_JS = """() => {
    const links = document.querySelectorAll('a[href*="/video/"]');
    if (links.length) links[links.length - 1].scrollIntoView({block: 'end'});
    for (const el of document.querySelectorAll('div')) {
        if (el.scrollHeight > el.clientHeight + 50 && /(auto|scroll)/.test(getComputedStyle(el).overflowY)) {
            el.scrollTop = el.scrollHeight;
        }
    }
    window.scrollTo(0, document.body.scrollHeight);
}"""


class DownloadExpired(Exception):
    pass


class LoginRequired(Exception):
    pass


def normalize_user_url(url: str) -> str:
    url = url.strip()
    if not url:
        return url
    if not url.startswith("http"):
        url = "https://" + url
    m = re.search(r"douyin\.com/user/([A-Za-z0-9_\-]+)", url)
    return f"https://www.douyin.com/user/{m.group(1)}" if m else url


def video_url(video_id):
    return f"https://www.douyin.com/video/{video_id}"


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
        # full Chromium in "new headless" mode; the old headless shell is blocked by Douyin
        return p.chromium.launch_persistent_context(
            PROFILE_DIR, channel="chromium", headless=headless, user_agent=USER_AGENT, locale="zh-CN",
            viewport=None if not headless else {"width": 1280, "height": 1000})

    try:
        return launch()
    except PlaywrightError as ex:
        if "Executable doesn't exist" not in str(ex):
            raise
        _install_chromium()
        return launch()


def _has_session(ctx):
    return any(c["name"] == "sessionid" and c["value"] for c in ctx.cookies("https://www.douyin.com"))


def login():
    """Open a visible browser for the user to log in (QR code); closes itself once logged in."""
    with sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
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


def _best_mp4(video):
    """Highest-resolution progressive MP4 (video + audio in one file)."""
    candidates = []
    for br in video.get("bit_rate") or []:
        pa = br.get("play_addr") or {}
        if br.get("format") == "mp4" and pa.get("url_list"):
            candidates.append(((pa.get("height") or 0, br.get("bit_rate") or 0), pa["url_list"][0]))
    if candidates:
        return max(candidates)[1]
    urls = (video.get("play_addr") or {}).get("url_list") or []
    return urls[0] if urls else ""


def _caption(aweme):
    for line in (aweme.get("desc") or "").splitlines():
        if line.strip():
            return line.strip()[:100].rstrip()
    return f"douyin_{aweme['aweme_id']}"


def fetch_user_videos(url, on_progress=None, max_idle_rounds=4):
    """Returns ([{id, url, title, thumb, likes, media}], nickname, limited) - limited = guest saw only page 1."""
    pages = []

    def on_response(resp):
        if POST_API in resp.url:
            try:
                pages.append(resp.json())
            except Exception:  # noqa: BLE001 - blocked / non-JSON page
                pass

    with sync_playwright() as p:
        ctx = _launch(p, headless=True)
        try:
            logged_in = _has_session(ctx)
            if is_logged_in() and not logged_in:
                _forget_session()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(30):  # first API page usually arrives within a few seconds
                if pages:
                    break
                page.wait_for_timeout(500)
            idle = 0
            while pages and pages[-1].get("has_more") and idle < max_idle_rounds:
                before = len(pages)
                if on_progress:
                    on_progress(sum(len(pg.get("aweme_list") or []) for pg in pages))
                page.evaluate(SCROLL_JS)
                page.wait_for_timeout(2000)
                if len(pages) == before:
                    idle += 1
                    if not logged_in and page.get_by_text(LOGIN_WALL_TEXT).count():
                        break
                else:
                    idle = 0
            nickname = page.evaluate("() => (document.querySelector('h1') || {}).innerText || ''").strip()
            limited = bool(pages and pages[-1].get("has_more")) and not logged_in
            # after a few guest visits Douyin stops serving the list and shows a login dialog instead
            login_wall = not pages and page.get_by_text(LOGIN_DIALOG_TEXT).count() > 0
        finally:
            ctx.close()
    if login_wall:
        if logged_in:
            _forget_session()
            raise LoginRequired("Douyin session expired. Please reconnect your account.")
        raise LoginRequired("Douyin now requires login to show this profile's videos. Connect Douyin and fetch again.")

    videos, seen = [], set()
    for pg in pages:
        for aweme in pg.get("aweme_list") or []:
            vid = aweme.get("aweme_id")
            video = aweme.get("video") or {}
            if not vid or vid in seen or aweme.get("images"):  # photo posts have no video
                continue
            media = _best_mp4(video)
            if not media:
                continue
            seen.add(vid)
            covers = (video.get("cover") or {}).get("url_list") or []
            nickname = nickname or (aweme.get("author") or {}).get("nickname") or ""
            videos.append({
                "id": vid,
                "url": video_url(vid),
                "title": _caption(aweme),
                "thumb": covers[0] if covers else "",
                "likes": (aweme.get("statistics") or {}).get("digg_count") or 0,
                "media": media,
            })
    if not pages:
        raise RuntimeError("Douyin did not return any videos (the page may require verification). Try again later.")
    return videos, nickname, limited


def download(media_url, path, on_progress=None, should_cancel=None, cancelled_exc=Exception):
    """Stream an MP4 to `path` (via `path.part`)."""
    req = urllib.request.Request(media_url, headers={"User-Agent": USER_AGENT, "Referer": REFERER})
    part = path + ".part"
    try:
        resp = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as ex:
        if ex.code in (403, 404, 410):
            raise DownloadExpired("Download link expired — fetch the list again") from ex
        raise
    with resp, open(part, "wb") as f:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            if should_cancel and should_cancel():
                raise cancelled_exc("Cancelled by user")
            chunk = resp.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if on_progress and total:
                on_progress(done / total)
    os.replace(part, path)
    return path
