"""Post videos to TikTok from the user's Google Chrome Portable profiles.

All profiles live in one Chrome user-data dir, so one Chrome process serves them all. The app starts that
Chrome itself with a remote-debugging port, opens a window of the chosen profile (its own cookies and VPN
extension), and drives TikTok Studio's upload page over CDP: check the exit country, pick the file, fill the
caption, set the schedule and press Post. Chrome keeps running afterwards for the next post.
"""
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.request
import uuid
from datetime import datetime

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

PORT = 9340
UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload?from=creator_center&tab=video"
IP_URL = "https://ipinfo.io/json"
MAX_CAPTION = 4000
COUNTRIES = ["JP", "US", "KR", "VN", "GB", "DE", "FR", "TW", "TH", "ID", "PH", "MY", "SG", "CA", "AU", "BR", "MX"]


class ChromeBusy(Exception):
    pass


class PostError(Exception):
    pass


def chrome_paths(root):
    """(chrome.exe, user-data dir) of a GoogleChromePortable folder."""
    chrome = os.path.join(root, "App", "Chrome-bin", "chrome.exe")
    data = os.path.join(root, "Data", "profile")
    if not os.path.isfile(chrome) or not os.path.isfile(os.path.join(data, "Local State")):
        raise PostError("This folder is not a Google Chrome Portable folder (App\\Chrome-bin\\chrome.exe and "
                        "Data\\profile are missing).")
    return chrome, data


def guess_country(name):
    tokens = re.split(r"[^A-Za-z]+", name or "")
    return next((t.upper() for t in tokens if t.upper() in COUNTRIES), "")


def list_profiles(root):
    """[{dir, name, country}] in Chrome's own order."""
    _, data = chrome_paths(root)
    with open(os.path.join(data, "Local State"), encoding="utf-8") as f:
        info = json.load(f).get("profile") or {}
    cache = info.get("info_cache") or {}
    order = [d for d in info.get("profiles_order") or [] if d in cache] + sorted(d for d in cache if d not in (info.get("profiles_order") or []))
    return [{"dir": d, "name": cache[d].get("name") or d, "country": guess_country(cache[d].get("name"))} for d in order]


def _cdp_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1).close()
        return True
    except OSError:
        return False


def _open_window(root, profile_dir, url):
    chrome, data = chrome_paths(root)
    subprocess.Popen([chrome, f"--user-data-dir={data}", f"--remote-debugging-port={PORT}",
                      f"--profile-directory={profile_dir}",
                      f"--disk-cache-dir={os.path.join(tempfile.gettempdir(), 'GoogleChromePortable')}",
                      "--no-first-run", "--no-default-browser-check", url],
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP, close_fds=True)


def _open_profile_page(p, root, profile_dir):
    """Open a window of the profile and return (browser, page) connected over CDP."""
    token = uuid.uuid4().hex[:12]
    was_up = _cdp_up()
    _open_window(root, profile_dir, f"{IP_URL}?grabber={token}")
    if not was_up:
        for _ in range(40):
            if _cdp_up():
                break
            time.sleep(0.5)
        else:
            raise ChromeBusy("Chrome Portable is already open, so Grabber cannot connect to it. Close every Chrome "
                             "Portable window and post again — Grabber will reopen it.")
    for _ in range(60):  # wait for the window's tab before connecting, so it is in the initial page list
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=2) as resp:
            if any(token in t.get("url", "") for t in json.load(resp)):
                break
        time.sleep(0.5)
    else:
        raise PostError("The Chrome window for this profile did not open.")
    browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{PORT}")
    page = next((pg for ctx in browser.contexts for pg in ctx.pages if token in pg.url), None)
    if not page:
        browser.close()
        raise PostError("Could not attach to the Chrome window of this profile.")
    return browser, page


def _wait_vpn(page, expected, status, timeout=90):
    """Reload an IP lookup until the exit country matches the profile's VPN country."""
    deadline = time.time() + timeout
    country = "?"
    page.wait_for_timeout(3000)  # the VPN extension reconnects when the profile starts
    while time.time() < deadline:
        try:
            page.goto(IP_URL, timeout=20000)
            country = (json.loads(page.inner_text("body")).get("country") or "?").upper()
        except (PlaywrightError, ValueError):
            country = "?"
        if country != "?" and (not expected or country == expected):
            status(f"Exit country: {country}")
            return country
        status(f"Waiting for the VPN to reach {expected}… (now {country})")
        page.wait_for_timeout(3000)
    raise PostError(f"The VPN of this profile is not on {expected} (exit country is {country}). "
                    "Check NordVPN in that Chrome window and post again.")


def _type_caption(page, caption):
    editor = page.locator('[data-e2e="caption_container"] [contenteditable="true"]')
    editor.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    for i, line in enumerate(caption[:MAX_CAPTION].split("\n")):
        if i:
            page.keyboard.press("Escape")  # close a hashtag / mention suggestion so Enter makes a new line
            page.keyboard.press("Enter")
        page.keyboard.type(line, delay=10)
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)


def _set_schedule(page, when):
    page.locator('[data-e2e="schedule_container"] input[name="postSchedule"][value="schedule"]').check(force=True)
    page.wait_for_selector('[aria-label="Select date"]', timeout=10000)

    page.click('[aria-label="Select date"]')
    today = datetime.now()
    for _ in range((when.year * 12 + when.month) - (today.year * 12 + today.month)):
        page.click('[aria-label="Next month"]')
        page.wait_for_timeout(300)
    day = page.locator('.DatePicker__day[data-outside="false"]', has_text=re.compile(rf"^{when.day}$"))
    if not day.count() or day.first.get_attribute("data-disabled") == "true":
        raise PostError("TikTok does not allow scheduling on that date (it accepts today up to ~30 days ahead).")
    day.first.click()
    page.wait_for_timeout(500)

    page.click('[aria-label="Select time"]')
    for label in (f"Hours {when.hour:02d}", f"Minutes {when.minute:02d}"):
        option = page.locator(f'[aria-label="{label}"]')
        if not option.count() or option.first.get_attribute("data-disabled") == "true":
            raise PostError(f"TikTok does not allow {when:%H:%M} on that day — pick a later time.")
        option.first.click()
        page.wait_for_timeout(300)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    shown = page.inner_text('[aria-label="Select time"] .Picker__value').strip()
    if shown != f"{when:%H:%M}":
        raise PostError(f"Could not set the schedule time (TikTok shows {shown}).")


def _wait_posted(page, timeout=180):
    """Wait until TikTok accepts the post: the upload form goes away or the page navigates."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "/tiktokstudio/upload" not in page.url or not page.locator('[data-e2e="post_video_button"]').count():
            return
        dialog = page.locator('[role="dialog"]:visible')
        if dialog.count():
            text = " ".join(dialog.first.inner_text().split())[:300]
            raise PostError(f"TikTok asked for confirmation — finish it in the Chrome window: {text}")
        page.wait_for_timeout(1000)
    raise PostError("TikTok did not confirm the post in time — check the Chrome window.")


def post(root, profile_dir, video, caption, when=None, expected_country="", review=False, status=lambda _t: None):
    """Upload `video` to the profile's TikTok. `when` (local datetime) schedules it. Returns the exit country."""
    with sync_playwright() as p:
        status("Opening the Chrome profile…")
        browser, page = _open_profile_page(p, root, profile_dir)
        try:
            page.bring_to_front()
            country = _wait_vpn(page, expected_country, status)
            status("Opening TikTok Studio…")
            page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("input[type=file]", state="attached", timeout=60000)
            except PlaywrightTimeout:
                if "login" in page.url:
                    raise PostError("This profile is not logged in to TikTok.") from None
                raise PostError("TikTok Studio's upload page did not load.") from None
            status("Uploading the video to TikTok…")
            page.set_input_files("input[type=file]", video)
            page.wait_for_selector('[data-e2e="caption_container"] [contenteditable="true"]', timeout=120000)
            page.wait_for_function("""() => {
                const b = document.querySelector('[data-e2e="post_video_button"]');
                return b && b.getAttribute('aria-disabled') !== 'true' && !b.disabled
                    && document.querySelector('[data-e2e="cover_container"] img');
            }""", timeout=600000)
            status("Filling in the caption…")
            _type_caption(page, caption)
            if when:
                status("Setting the schedule…")
                _set_schedule(page, when)
            if review:
                status("Ready — review it in Chrome and press Post yourself")
                return country
            status("Posting…")
            page.click('[data-e2e="post_video_button"]')
            _wait_posted(page)
            page.close()  # posted: drop this tab so windows don't pile up; failures stay open to inspect
            return country
        finally:
            browser.close()  # only disconnects; Chrome stays open
