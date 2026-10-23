"""Facebook page reels listing via Playwright, using a persistent logged-in profile."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

if getattr(sys, "frozen", False):
    # frozen Playwright looks for browsers inside the bundle; use the normal per-user install instead
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "ms-playwright"))

from playwright._impl._driver import compute_driver_executable, get_driver_env  # noqa: E402
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

DATA_DIR = os.path.join(os.path.expanduser("~"), ".yt-shorts-downloader")
PROFILE_DIR = os.path.join(DATA_DIR, "fb-profile")
COOKIE_FILE = os.path.join(DATA_DIR, "fb-cookies.txt")
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

COLLECT_JS = """
() => [...document.querySelectorAll('a[href*="/reel/"]')].map(a => {
    const m = a.href.match(/\\/reel\\/(\\d+)/);
    const img = a.querySelector('img');
    return {
        id: m ? m[1] : null,
        thumb: img ? img.src : null,
        text: (a.innerText || '').trim(),

    };
}).filter(x => x.id)
"""


class NotLoggedIn(Exception):
    pass


def normalize_reels_url(url: str) -> str:
    """Make sure the page URL points to its /reels tab."""
    url = url.strip().rstrip("/")
    if not url:
        return url
    if not url.startswith("http"):
        url = "https://" + url
    url = re.sub(r"/(videos|photos|about|posts|mentions|followers|reels)$", "", url)
    return url + "/reels/"


def _install_chromium():
    """First run on a machine without Playwright's Chromium: download it with the bundled driver."""
    driver = compute_driver_executable()
    cmd = [*driver] if isinstance(driver, tuple) else [driver]
    subprocess.run([*cmd, "install", "chromium"], env=get_driver_env(), check=True,
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _launch(p, headless):
    os.makedirs(PROFILE_DIR, exist_ok=True)

    def launch():
        return p.chromium.launch_persistent_context(
            PROFILE_DIR, headless=headless, user_agent=USER_AGENT,
            viewport=None if not headless else {"width": 1280, "height": 1000})

    try:
        return launch()
    except PlaywrightError as ex:
        if "Executable doesn't exist" not in str(ex):
            raise
        _install_chromium()
        return launch()


def _has_session(ctx):
    return any(c["name"] == "c_user" for c in ctx.cookies("https://www.facebook.com"))


def login():
    """Open a visible browser for the user to log in; closes itself once logged in."""
    with sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.facebook.com/login")
        try:
            while not _has_session(ctx):
                page.wait_for_timeout(1000)
            page.wait_for_timeout(3000)  # let Facebook finish setting session cookies
            _export_cookies(ctx)
            ctx.close()
        except Exception:  # noqa: BLE001 - user closed the window before logging in
            pass
    return is_logged_in()


def _export_cookies(ctx):
    """Write cookies in Netscape format so yt-dlp can reuse the session."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in ctx.cookies():
        domain = c["domain"]
        lines.append("\t".join([
            domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c["path"],
            "TRUE" if c["secure"] else "FALSE",
            str(int(c["expires"])) if c["expires"] and c["expires"] > 0 else "0",
            c["name"],
            c["value"],
        ]))
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def is_logged_in():
    return os.path.exists(COOKIE_FILE)


def _forget_session():
    try:
        os.remove(COOKIE_FILE)
    except OSError:
        pass


@contextmanager
def cookie_copy():
    """yt-dlp rewrites its cookie file on exit, so each concurrent instance gets its own copy."""
    fd, path = tempfile.mkstemp(prefix="fb-cookies-", suffix=".txt")
    os.close(fd)
    shutil.copyfile(COOKIE_FILE, path)
    try:
        yield path
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _session_expired(page):
    if re.search(r"/(login|checkpoint)(\.php|/|\?|$)", page.url):
        return True
    return page.locator('input[name="pass"]').count() > 0


def _page_name(page, url):
    """Display name of the page, e.g. "Gentle Days Studio"; falls back to the URL slug."""
    name = page.evaluate("() => document.querySelector('meta[property=\"og:title\"]')?.content || document.title || ''")
    name = re.sub(r"^\(\d+\+?\)\s*", "", name or "").split(" | ")[0].strip()
    name = re.sub(r"\s*[-–·]?\s*(Reels|Thước phim)$", "", name, flags=re.IGNORECASE).strip()
    if name and name.lower() != "facebook":
        return name
    slug = re.sub(r"^https?://[^/]+/", "", url).split("/")[0]
    return slug or "facebook"


def fetch_reels(url, on_progress=None, max_idle_rounds=4):
    """Scroll the page's /reels tab; returns ([{id, url, thumb, views}] in page order, page name)."""
    with sync_playwright() as p:
        ctx = _launch(p, headless=True)
        try:
            if not _has_session(ctx):
                _forget_session()
                raise NotLoggedIn("Facebook session expired. Please reconnect your account.")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            if _session_expired(page):
                _forget_session()
                raise NotLoggedIn("Facebook session expired. Please reconnect your account.")
            name = _page_name(page, url)

            reels, seen, idle = [], set(), 0
            while idle < max_idle_rounds:
                new = 0
                for item in page.evaluate(COLLECT_JS):
                    if item["id"] in seen:
                        continue
                    seen.add(item["id"])
                    new += 1
                    reels.append({
                        "id": item["id"],
                        "url": f"https://www.facebook.com/reel/{item['id']}",
                        "thumb": item["thumb"],
                        "views": item["text"].replace("\xa0", " "),
                    })
                idle = 0 if new else idle + 1
                if on_progress:
                    on_progress(len(reels))
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1500)
            _export_cookies(ctx)
            return reels, name
        finally:
            ctx.close()


if __name__ == "__main__":
    import json
    import sys

    start = time.time()
    data, page_name = fetch_reels(normalize_reels_url(sys.argv[1]), lambda n: print("found", n, flush=True))
    print(page_name)
    print(json.dumps(data[:5], ensure_ascii=False, indent=1))
    print("total", len(data), "in", round(time.time() - start), "s")
