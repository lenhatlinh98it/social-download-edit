"""Shorts / Reels downloader - pywebview desktop app (HTML/CSS UI) on top of yt-dlp + Playwright."""
import ctypes
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor

import webview

import douyin
import editor
import engine
import facebook
import instagram
import edition
import license
import tiktok

try:  # owner edition only - the commercial branch ships without these modules
    import keygen
    import research
except ImportError:
    keygen = research = None
try:  # owner edition only (local AI: faster-whisper, OpenCV, Ollama)
    import smartcut
except ImportError:
    smartcut = None
try:  # owner edition only (needs the cryptography package)
    import sync
except ImportError:
    sync = None

engine.activate()  # must run before yt_dlp is imported
import yt_dlp  # noqa: E402

if getattr(sys, "frozen", False):
    BASE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
ICON_FILE = os.path.join(BASE_DIR, "assets", "icon.ico")
FFMPEG_DIR = os.path.join(BASE_DIR, "bin")  # filled by setup.ps1; falls back to ffmpeg on PATH
APP_ID = "grabber.shorts-reels-downloader"
SAVED_FILE = os.path.join(facebook.DATA_DIR, "saved.json")
DOWNLOADED_FILE = os.path.join(facebook.DATA_DIR, "downloaded.json")
SETTINGS_FILE = os.path.join(facebook.DATA_DIR, "settings.json")
PRESETS_FILE = os.path.join(facebook.DATA_DIR, "edit_presets.json")
TT_POSTS_FILE = os.path.join(facebook.DATA_DIR, "tiktok_posts.json")
LOG_FILE = os.path.join(facebook.DATA_DIR, "logs.jsonl")
LICENSE_FILE = os.path.join(facebook.DATA_DIR, "license.key")
OWNER = edition.EDITION == "owner"
LOG_MAX_BYTES = 2_000_000
LOG_EVENTS = {"list", "done", "editDone", "batchDone", "queueDone", "stripDone", "mergeDone", "fbExpired",
              "dyLoginRequired", "igLoginRequired"}
TASK_SOURCES = {"_fetch_task": "fetch", "_download_task": "download", "_export_task": "editor", "_batch_task": "editor",
                "_merge_task": "editor", "_strip_task": "editor", "_export_many_task": "editor",
                "_tt_post_task": "tiktok", "_tt_post_many_task": "tiktok", "_smartcut_task": "smartcut"}
DROP_KINDS = {**dict.fromkeys((".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"), "video"),
              **dict.fromkeys((".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"), "audio"),
              **dict.fromkeys((".png", ".jpg", ".jpeg", ".webp"), "image")}
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "shorts")
PARALLEL_DOWNLOADS = 3
FINAL_EXTS = (".mp4", ".webm", ".mkv")
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def normalize_shorts_url(url: str) -> str:
    """Make sure the channel URL points to its /shorts tab."""
    url = url.strip().rstrip("/")
    if not url:
        return url
    if not url.startswith("http"):
        url = "https://" + url
    url = re.sub(r"/(videos|streams|featured|playlists|community|about)$", "", url)
    if not url.endswith("/shorts"):
        url += "/shorts"
    return url


def detect_kind(url):
    host = re.sub(r"^https?://", "", url.strip().lower()).split("/")[0]
    if host.endswith("youtube.com") or host == "youtu.be":
        return "youtube"
    if host.endswith("facebook.com") or host == "fb.com":
        return "facebook"
    if host.endswith("douyin.com") or host.endswith("iesdouyin.com"):
        return "douyin"
    if host.endswith("instagram.com") or host == "instagr.am":
        return "instagram"
    return None


def normalize_url(kind, url):
    if kind == "youtube":
        return normalize_shorts_url(url)
    if kind == "douyin":
        return douyin.normalize_user_url(url)
    if kind == "instagram":
        return instagram.normalize_user_url(url)
    return facebook.normalize_reels_url(url)


def iso_duration(text):
    """YouTube's ISO 8601 duration (PT1M30S, P1DT2H) -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", text or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s or None


def compact_number(n):
    if not n:
        return ""
    for div, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def safe_name(text, fallback="video", limit=150):
    """A file/folder name that is valid on Windows."""
    name = yt_dlp.utils.sanitize_filename(text or "").replace("\\", "＼").strip()[:limit].rstrip(". ")
    if not name:
        return fallback
    return f"_{name}" if name.split(".")[0].upper() in WINDOWS_RESERVED else name


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_settings():
    return _read_json(SETTINGS_FILE, {})


def save_setting(key, value):
    settings = load_settings()
    settings[key] = value
    _write_json(SETTINGS_FILE, settings)


def load_saved():
    return _read_json(SAVED_FILE, [])


def write_saved(items):
    _write_json(SAVED_FILE, items)


def load_downloaded():
    """{"youtube": {id: {...}}, "facebook": {id: {...}}} - ids are only unique per platform."""
    return _read_json(DOWNLOADED_FILE, {})


def write_downloaded(data):
    _write_json(DOWNLOADED_FILE, data)


_downloaded_lock = threading.Lock()
_log_lock = threading.Lock()


def append_log(entry):
    """One JSON object per line; trimmed to the newest half once the file passes LOG_MAX_BYTES."""
    with _log_lock:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            with open(LOG_FILE, encoding="utf-8") as f:
                lines = f.readlines()
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                f.writelines(lines[len(lines) // 2:])


def read_logs(limit=3000):
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    except OSError:
        return []
    entries = []
    for line in reversed(lines):
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return entries


def mark_downloaded(kind, video, title, file):
    with _downloaded_lock:  # parallel downloads finish concurrently
        data = load_downloaded()
        data.setdefault(kind, {})[video["id"]] = {
            "title": title,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "url": video["url"],
            "thumb": video.get("thumb") or "",
            "file": file or "",
        }
        write_downloaded(data)


def video_url(kind, video_id):
    if kind == "youtube":
        return f"https://www.youtube.com/shorts/{video_id}"
    if kind == "douyin":
        return douyin.video_url(video_id)
    if kind == "instagram":
        return instagram.video_url(video_id)
    return f"https://www.facebook.com/reel/{video_id}"


def downloaded_file(info):
    downloads = (info or {}).get("requested_downloads") or [{}]
    return downloads[0].get("filepath") or ""


def reel_time(ydl, video):
    """Upload time of a Facebook reel: the page's "creation_time" next to the reel's own id (the page also
    carries the times of the suggested reels that follow)."""
    try:
        html = ydl.urlopen(video["url"]).read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - the time is optional
        return None
    for m in re.finditer(r'"creation_time":(\d{9,10})', html):
        if video["id"] in html[max(0, m.start() - 3000):m.end() + 3000]:
            return int(m.group(1))
    return None


def reel_caption(info, reel_id):
    """Facebook reels have title 'Video'; the real caption is the description's first line."""
    for line in (info.get("description") or "").splitlines():
        line = line.strip()
        if line:
            return line[:100].rstrip()
    title = (info.get("title") or "").strip()
    return title if title and title.lower() != "video" else f"reel_{reel_id}"


def fetch_youtube(url, _status):
    opts = {"extract_flat": "in_playlist", "quiet": True, "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    videos = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        thumbs = e.get("thumbnails") or []
        videos.append({
            "id": e["id"],
            "title": e.get("title") or e["id"],
            "url": e.get("url") or f"https://www.youtube.com/shorts/{e['id']}",
            "views": compact_number(e.get("view_count")),
            "duration": e.get("duration"),  # the Shorts tab has neither: filled by Api._youtube_meta
            "uploaded": e.get("timestamp"),
            "thumb": thumbs[0]["url"] if thumbs else f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg",
        })
    handle = re.search(r"/(@[^/?#]+)", url)
    source = (info.get("channel") or info.get("uploader") or re.sub(r" - Shorts$", "", info.get("title") or "")
              or (handle.group(1) if handle else ""))
    return videos, source


def fetch_facebook(url, status):
    reels, page_name = facebook.fetch_reels(url, lambda n: status(f"Scrolling reels page… found {n}"))
    return [{
        "id": r["id"],
        "url": r["url"],
        "thumb": r["thumb"],
        "title": "",
        "views": r.get("views") or "",
    } for r in reels], page_name


def fetch_douyin(url, status):
    videos, nickname, limited = douyin.fetch_user_videos(url, lambda n: status(f"Loading Douyin videos\u2026 found {n}"))
    for v in videos:
        v["views"] = compact_number(v["likes"])
        v["stat"] = "likes"
    return videos, nickname, limited


def fetch_instagram(url, status):
    reels, username, limited = instagram.fetch_user_reels(url, lambda n: status(f"Loading Instagram reels\u2026 found {n}"))
    for v in reels:
        v["views"] = compact_number(v["plays"])
    return reels, username, limited


PLATFORM_NAMES = {"youtube": "YouTube", "facebook": "Facebook", "douyin": "Douyin", "instagram": "Instagram"}


class Api:
    """Methods exposed to the web UI. Long tasks run in threads and report back through events."""

    def __init__(self):
        self._window = None
        self._busy = False
        self._lock = threading.Lock()
        self._lists = {"youtube": [], "facebook": [], "douyin": [], "instagram": []}
        self._sources = {"youtube": "", "facebook": "", "douyin": "", "instagram": ""}
        self._gen = {"youtube": 0, "facebook": 0, "douyin": 0, "instagram": 0}
        self._caption_requested = set()
        self._yt_api_gen = None  # YouTube list whose details come from the Data API
        self._meta_pool = ThreadPoolExecutor(max_workers=3)
        self._cancel = threading.Event()
        self._names_lock = threading.Lock()
        self._reserved_names = set()
        self._latest_engine = None
        self._media = editor.MediaServer()
        self._edit_cancel = threading.Event()
        self._notifier = None
        self._fonts = None
        self._research_lock = threading.Lock()
        self._research_cancel = threading.Event()
        self._research_running = False
        self._license = {"ok": True} if OWNER else license.load(LICENSE_FILE)

    # ---------- plumbing ----------
    def _emit(self, type_, **data):
        payload = json.dumps({"type": type_, **data}, ensure_ascii=False)
        if type_ in LOG_EVENTS:
            self._log_event(type_, data)
        try:
            self._window.evaluate_js(f"window.onPy({payload})")
        except Exception:  # noqa: BLE001 - window may be closing
            pass

    def _run(self, target, *args):
        """Start a background task unless another one is running."""
        if not self._license["ok"]:
            self._emit("licenseRequired")
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._emit("busy", busy=True)

        def wrapper():
            try:
                target(*args)
            except Exception as ex:  # noqa: BLE001 - surface any error to the UI
                self._log("error", TASK_SOURCES.get(target.__name__, "app"), str(ex), traceback.format_exc())
                self._emit("error", message=str(ex))
            finally:
                with self._lock:
                    self._busy = False
                self._emit("busy", busy=False)

        threading.Thread(target=wrapper, daemon=True).start()
        return True

    # ---------- logs ----------
    def _log(self, level, source, message, detail=""):
        entry = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "level": level, "source": source,
                 "message": message, "detail": detail}
        try:
            append_log(entry)
        except OSError:
            pass
        self._emit("log", entry=entry)

    def _log_event(self, type_, data):
        """Turn the UI events that end a task into log lines."""
        if type_ == "list":
            hidden = f" ({data['hidden']} already downloaded)" if data.get("hidden") else ""
            self._log("info", "fetch", f"{PLATFORM_NAMES[data['kind']]}: {len(data['videos'])} new videos from "
                                       f"{data.get('source') or data['url']}{hidden}", data["url"])
        elif type_ == "done":
            failed = data.get("failed") or []
            level = "warning" if failed or data.get("cancelled") else "success"
            self._log(level, "download", f"Downloaded {data['ok']}/{data['total']}" + (
                f", {len(failed)} failed" if failed else "") + (f", {data['cancelled']} cancelled" if data.get("cancelled") else ""),
                      "\n".join([data.get("dir") or "", *failed]).strip())
        elif type_ == "editDone":
            self._log("success", "editor", f"Exported {data['name']} ({data['width']}×{data['height']}, {data['encoder']})", data["path"])
        elif type_ in ("batchDone", "queueDone", "stripDone"):
            failed = data.get("failed") or []
            what = {"batchDone": "Batch export", "queueDone": "Export all"}.get(type_, "Metadata strip")
            self._log("warning" if failed else "success", "editor", f"{what}: {data['ok']}/{data['total']} done", "\n".join(failed))
        elif type_ == "mergeDone" and not data.get("cancelled"):
            self._log("success", "editor", f"Merged into {data['name']} ({data['width']}×{data['height']})", data["path"])
        elif type_ in ("fbExpired", "dyLoginRequired", "igLoginRequired"):
            self._log("warning", "fetch", data.get("message") or type_)

    def logs_list(self):
        return read_logs()

    def logs_clear(self):
        with _log_lock:
            try:
                os.remove(LOG_FILE)
            except OSError:
                pass
        return []

    def logs_reveal(self):
        if os.path.isfile(LOG_FILE):
            self.reveal_file(LOG_FILE)

    # ---------- state ----------
    # ---------- license ----------
    def license_info(self):
        lic = self._license
        return {"ok": lic["ok"], "machine": license.machine_id(), "reason": lic.get("reason", ""),
                "issued": lic.get("issued", ""), "expires": lic.get("expires", ""), "edition": edition.EDITION}

    def license_activate(self, key):
        result = license.check(key)
        if not result["ok"]:
            self._log("warning", "app", f"License activation failed: {result['reason']}")
            return {**self.license_info(), "ok": False, "reason": result["reason"]}
        license.save(LICENSE_FILE, key)
        self._license = {**result, "key": key.strip()}
        self._log("success", "app", f"Grabber activated on {license.machine_id()}")
        return self.license_info()

    def _watch_license(self):
        """Re-check the saved key every minute so a key that expires while Grabber is open locks it again."""
        while True:
            time.sleep(60)
            if not self._license["ok"]:
                continue
            result = license.check(self._license.get("key", ""))
            if not result["ok"]:
                self._license = {**result, "key": self._license.get("key", "")}
                self._log("warning", "app", f"License stopped working: {result['reason']}")
                self._emit("licenseRequired")

    # ---------- license keys (owner edition) ----------
    def keys_state(self):
        if not keygen:
            return {"available": False}
        return {"available": True, **keygen.status(), "issued": keygen.issued(), "machine": license.machine_id()}

    def keys_init(self):
        if not keygen:
            return {"ok": False, "error": "Not available in this edition."}
        pub = keygen.init()
        self._log("info", "app", "License signing key ready", keygen.PRIVATE)
        return {"ok": True, "publicKey": pub, **self.keys_state()}

    def keys_issue(self, machine, days=0, note="", minutes=0):
        if not keygen:
            return {"ok": False, "error": "Not available in this edition."}
        try:
            key = keygen.issue(machine, int(days or 0), (note or "").strip(), int(minutes or 0))
        except keygen.KeygenError as ex:
            return {"ok": False, "error": str(ex)}
        self._log("success", "app", f"License key issued for {license.machine_id(license.parse_machine_id(machine))}"
                                    + (f" — {note.strip()}" if (note or "").strip() else ""))
        return {"ok": True, "key": key, "issued": keygen.issued()}

    def init(self):
        threading.Thread(target=self._check_engine, daemon=True).start()
        if not OWNER:
            threading.Thread(target=self._watch_license, daemon=True).start()
        return {
            "edition": edition.EDITION,
            "license": self.license_info(),
            "saved": load_saved(),
            "fbLoggedIn": facebook.is_logged_in(),
            "dyLoggedIn": douyin.is_logged_in(),
            "igLoggedIn": instagram.is_logged_in(),
            "dir": load_settings().get("dir") or DEFAULT_DIR,
            "engine": yt_dlp.version.__version__,
        }

    def choose_dir(self, current):
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=current or "")
        if not result:
            return None
        save_setting("dir", result[0])
        return result[0]

    def open_folder(self, path):
        """Open a folder; one not created yet (nothing saved there so far) opens its nearest existing parent."""
        path = os.path.normpath(path) if path else ""
        while path and not os.path.isdir(path) and os.path.dirname(path) != path:
            path = os.path.dirname(path)
        if path and os.path.isdir(path):
            os.startfile(path)

    # ---------- engine (yt-dlp) ----------
    def _check_engine(self):
        try:
            latest, urls = engine.latest_version()
        except Exception:  # noqa: BLE001 - offline: just skip the check
            return
        self._latest_engine = (latest, urls)
        current = yt_dlp.version.__version__
        self._emit("engine", current=current, latest=latest,
                   updateAvailable=engine.parse_version(latest) > engine.parse_version(current))

    def update_engine(self):
        return self._run(self._update_engine_task)

    def _update_engine_task(self):
        self._emit("status", text="Checking the latest yt-dlp…", progress=None)
        latest, urls = engine.latest_version()
        current = yt_dlp.version.__version__
        if engine.parse_version(latest) <= engine.parse_version(current):
            self._emit("engine", current=current, latest=latest, updateAvailable=False, upToDate=True)
            self._emit("status", text=f"yt-dlp {current} is up to date", progress=0)
            return
        self._emit("status", text=f"Downloading yt-dlp {latest}…", progress=None)
        engine.install(latest, urls)
        self._emit("engine", current=current, latest=latest, updateAvailable=False, installed=latest)
        self._emit("status", text=f"yt-dlp {latest} installed — restart to use it", progress=0)

    def restart(self):
        if getattr(sys, "frozen", False):
            args = [sys.executable, *sys.argv[1:]]
        else:
            args = [sys.executable, os.path.abspath(sys.argv[0]), *sys.argv[1:]]
        subprocess.Popen(args, cwd=os.getcwd())
        self._window.destroy()

    # ---------- fetch ----------
    def fetch(self, kind, url):
        url = normalize_url(kind, url or "")
        if not url:
            return {"ok": False, "error": "Please enter a URL."}
        if detect_kind(url) != kind:
            return {"ok": False, "error": f"This is not a {PLATFORM_NAMES[kind]} link."}
        if kind == "facebook" and not facebook.is_logged_in():
            return {"ok": False, "error": "Connect your Facebook account first."}
        if kind == "douyin" and "/user/" not in url:
            return {"ok": False, "error": "Use a Douyin user link, e.g. https://www.douyin.com/user/MS4wLjABAAAA\u2026"}
        if kind == "instagram" and not instagram.username_of(url):
            return {"ok": False, "error": "Use an Instagram profile link, e.g. https://www.instagram.com/username/reels/"}
        worker = {"youtube": fetch_youtube, "facebook": fetch_facebook, "douyin": fetch_douyin,
                  "instagram": fetch_instagram}[kind]
        if not self._run(self._fetch_task, kind, url, worker):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True, "url": url}

    def _fetch_task(self, kind, url, worker):
        self._emit("status", text="Fetching list…", progress=None)
        try:
            fetched, source, *extra = worker(url, lambda text: self._emit("status", text=text, progress=None))
        except facebook.NotLoggedIn as ex:
            self._emit("fbExpired", message=str(ex))
            self._emit("status", text=str(ex), progress=0)
            return
        except douyin.LoginRequired as ex:
            self._emit("dyLoginRequired", message=str(ex), loggedIn=douyin.is_logged_in())
            self._emit("status", text=str(ex), progress=0)
            return
        except instagram.LoginRequired as ex:
            self._emit("igLoginRequired", message=str(ex), loggedIn=instagram.is_logged_in())
            self._emit("status", text=str(ex), progress=0)
            return
        done_ids = load_downloaded().get(kind, {})
        videos = [v for v in fetched if v["id"] not in done_ids]
        hidden = len(fetched) - len(videos)
        self._gen[kind] += 1
        self._lists[kind] = videos
        self._sources[kind] = source
        limited = bool(extra and extra[0])
        if kind == "douyin":
            self._emit("dyLogin", loggedIn=douyin.is_logged_in())
        if kind == "instagram":
            self._emit("igLogin", loggedIn=instagram.is_logged_in())
        self._emit("list", kind=kind, gen=self._gen[kind], url=url, videos=videos, hidden=hidden,
                   source=source, folder=safe_name(source, fallback=kind), limited=limited)
        if kind == "youtube" and load_settings().get("youtubeApiKey") and any(not v.get("uploaded") for v in videos):
            self._yt_api_gen = self._gen[kind]  # lazy per-card lookups are not needed for this list
            threading.Thread(target=self._youtube_meta, args=(self._gen[kind], videos), daemon=True).start()
        note = f" ({hidden} already downloaded, hidden)" if hidden else ""
        self._emit("status", text=f"Found {len(videos)} new videos{note}", progress=0)
        self._notify("Fetch finished", f"{len(videos)} new videos from {source or url}")

    def load_captions(self, kind, indices):
        """Facebook / Instagram grids show no captions, and the YouTube Shorts tab / Instagram grid no duration or
        upload time: they are resolved lazily for visible cards."""
        if kind not in ("facebook", "instagram", "youtube"):
            return
        gen = self._gen[kind]
        if kind == "youtube" and self._yt_api_gen == gen:
            return  # the YouTube API fills this list in one go
        videos = self._lists[kind]
        for idx in indices:
            key = (gen, idx)
            if key in self._caption_requested or idx >= len(videos) or (videos[idx]["title"] and videos[idx].get("uploaded")):
                continue
            self._caption_requested.add(key)
            self._meta_pool.submit(self._caption_job, kind, gen, idx, videos[idx])

    def _caption_job(self, kind, gen, idx, video):
        if gen != self._gen[kind]:
            return
        if kind == "instagram":
            try:
                video["media"], caption, meta = instagram.resolve(video["id"])
            except Exception:  # noqa: BLE001 - keep the placeholder title
                return
            video["title"] = video["title"] or caption or f"reel_{video['id']}"
            video["duration"] = video.get("duration") or meta["duration"]
            video["uploaded"] = video.get("uploaded") or meta["uploaded"]
        else:
            try:
                if kind == "youtube":
                    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
                        info = ydl.extract_info(video["url"], download=False, process=False)
                else:
                    with facebook.cookie_copy() as cookies, yt_dlp.YoutubeDL(
                            {"cookiefile": cookies, "quiet": True, "no_warnings": True}) as ydl:
                        info = ydl.extract_info(video["url"], download=False)
                        if not info.get("timestamp"):  # yt-dlp gives no upload time for reels
                            info["timestamp"] = reel_time(ydl, video)
            except Exception:  # noqa: BLE001 - keep the placeholder title
                return
            if kind == "facebook":
                video["title"] = reel_caption(info, video["id"])
            video["duration"] = video.get("duration") or info.get("duration")
            video["uploaded"] = video.get("uploaded") or info.get("timestamp") or info.get("release_timestamp")
        if gen == self._gen[kind]:
            self._emit("caption", kind=kind, gen=gen, index=idx, title=video["title"],
                       duration=video.get("duration"), uploaded=video.get("uploaded"))

    def _youtube_meta(self, gen, videos):
        """Duration and upload time of a fetched YouTube list from the Data API (50 videos per call, 1 unit each)."""
        key = load_settings().get("youtubeApiKey") or ""
        by_id = {v["id"]: i for i, v in enumerate(videos)}
        ids = [v["id"] for v in videos if not v.get("uploaded")]
        for start in range(0, len(ids), 50):
            if gen != self._gen["youtube"]:
                return
            query = urllib.parse.urlencode({"part": "contentDetails,snippet", "id": ",".join(ids[start:start + 50]),
                                            "fields": "items(id,contentDetails/duration,snippet/publishedAt)", "key": key})
            try:
                with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/videos?{query}", timeout=20) as r:
                    items = json.load(r).get("items") or []
            except (OSError, ValueError) as ex:
                self._yt_api_gen = None  # fall back to the lazy per-card lookups
                self._log("warning", "fetch", f"YouTube API: could not read durations and upload times ({type(ex).__name__})")
                return
            out = []
            for it in items:
                i = by_id.get(it.get("id"))
                if i is None:
                    continue
                v = videos[i]
                v["duration"] = v.get("duration") or iso_duration((it.get("contentDetails") or {}).get("duration"))
                published = (it.get("snippet") or {}).get("publishedAt") or ""
                try:
                    v["uploaded"] = int(datetime.datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp())
                except ValueError:
                    pass
                out.append({"index": i, "duration": v.get("duration"), "uploaded": v.get("uploaded")})
            if gen == self._gen["youtube"] and out:
                self._emit("meta", kind="youtube", gen=gen, items=out)

    # ---------- facebook login ----------
    def fb_login(self):
        return self._run(self._login_task)

    def _login_task(self):
        self._emit("status", text="Log in to Facebook in the opened browser window…", progress=None)
        ok = facebook.login()
        self._emit("fbLogin", loggedIn=facebook.is_logged_in())
        self._emit("status", text="Facebook connected" if ok else "Facebook login cancelled", progress=0)

    def ig_login(self):
        return self._run(self._ig_login_task)

    def _ig_login_task(self):
        self._emit("status", text="Log in to Instagram in the opened browser window\u2026", progress=None)
        ok = instagram.login()
        self._emit("igLogin", loggedIn=instagram.is_logged_in())
        self._emit("status", text="Instagram connected" if ok else "Instagram login cancelled", progress=0)

    def dy_login(self):
        return self._run(self._dy_login_task)

    def _dy_login_task(self):
        self._emit("status", text="Log in to Douyin in the opened browser window (scan the QR code)\u2026", progress=None)
        ok = douyin.login()
        self._emit("dyLogin", loggedIn=douyin.is_logged_in())
        self._emit("status", text="Douyin connected" if ok else "Douyin login cancelled", progress=0)

    # ---------- download ----------
    def download(self, kind, indices, out_dir):
        videos = self._lists.get(kind) or []
        items = [(i, videos[i]) for i in sorted(set(indices)) if 0 <= i < len(videos)]
        if not items:
            return {"ok": False, "error": "Select at least one video."}
        out_dir = (out_dir or "").strip()
        if not out_dir:
            return {"ok": False, "error": "Choose a folder to save to."}
        if not self._run(self._download_task, kind, items, out_dir):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True}

    def cancel_download(self):
        self._cancel.set()
        self._emit("status", text="Cancelling…", progress=None)

    def mark_as_downloaded(self, kind, indices):
        """Record selected videos as already downloaded (e.g. saved before this app) so fetches hide them."""
        if self._busy:
            return {"ok": False, "error": "Please wait for the current task to finish."}
        videos = self._lists.get(kind) or []
        picked = {i for i in indices if 0 <= i < len(videos)}
        if not picked:
            return {"ok": False, "error": "Select at least one video."}
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        with _downloaded_lock:
            data = load_downloaded()
            entries = data.setdefault(kind, {})
            for i in picked:
                v = videos[i]
                entries[v["id"]] = {
                    "title": v["title"] or f"{kind}_{v['id']}",
                    "at": now,
                    "url": v["url"],
                    "thumb": v.get("thumb") or "",
                    "file": "",
                    "manual": True,
                }
            write_downloaded(data)
        # re-publish the list without them; indices change, so this is a new generation
        self._lists[kind] = [v for i, v in enumerate(videos) if i not in picked]
        self._gen[kind] += 1
        return {"ok": True, "count": len(picked), "gen": self._gen[kind], "videos": self._lists[kind]}

    def _reserve_template(self, target_dir, title, fallback):
        """Output template with a name no other file (or running download) uses: "Title", "Title (2)", ..."""
        base = safe_name(title, fallback=fallback)
        with self._names_lock:
            name, n = base, 2
            while name.lower() in self._reserved_names or any(
                    os.path.exists(os.path.join(target_dir, name + ext)) for ext in FINAL_EXTS):
                name, n = f"{base} ({n})", n + 1
            self._reserved_names.add(name.lower())
        return name, os.path.join(target_dir, name.replace("%", "%%") + ".%(ext)s")

    @staticmethod
    def _remove_partials(target_dir, name):
        prefix = name + "."
        for file in os.listdir(target_dir):
            if file.startswith(prefix) and (file.endswith((".part", ".ytdl")) or re.search(r"\.f\d+\.\w+$", file)):
                try:
                    os.remove(os.path.join(target_dir, file))
                except OSError:
                    pass

    def _download_task(self, kind, items, out_dir):
        target_dir = os.path.join(out_dir, safe_name(self._sources.get(kind), fallback=kind))
        os.makedirs(target_dir, exist_ok=True)
        self._cancel.clear()
        self._reserved_names = set()
        total = len(items)
        fractions = {idx: 0.0 for idx, _ in items}
        results = {"done": 0, "failed": [], "cancelled": 0}
        progress_lock = threading.Lock()
        last_report = [0.0]
        self._emit("downloading", active=True)

        def report(force=False):
            now = time.monotonic()
            if not force and now - last_report[0] < 0.25:
                return
            last_report[0] = now
            with progress_lock:
                overall = sum(fractions.values()) / total
                finished = results["done"] + len(results["failed"]) + results["cancelled"]
            self._emit("status", text=f"Downloading — {finished}/{total} finished", progress=overall)

        def job(idx, v):
            if self._cancel.is_set():
                with progress_lock:
                    results["cancelled"] += 1
                self._emit("item", kind=kind, index=idx, state="cancelled", pct=0)
                return
            label = v["title"] or f"Reel {v['id']}"
            last = [0.0]

            def progress(frac):
                now = time.monotonic()
                if now - last[0] < 0.2:
                    return
                last[0] = now
                with progress_lock:
                    fractions[idx] = frac
                self._emit("item", kind=kind, index=idx, state="downloading", pct=round(frac * 100))
                report()

            def hook(d):
                if self._cancel.is_set():
                    raise yt_dlp.utils.DownloadCancelled("Cancelled by user")
                if d.get("status") != "downloading":
                    return
                size = d.get("total_bytes") or d.get("total_bytes_estimate")
                progress(min(d["downloaded_bytes"] / size, 1) if size else 0)

            opts = {
                "format": "bv*[ext=mp4]+ba[ext=m4a]/bv*+ba/b",
                "merge_output_format": "mp4",
                **({"ffmpeg_location": FFMPEG_DIR} if os.path.isfile(os.path.join(FFMPEG_DIR, "ffmpeg.exe")) else {}),
                "windowsfilenames": True,
                "quiet": True,
                "no_warnings": True,
                "noprogress": True,
                "progress_hooks": [hook],
            }
            self._emit("item", kind=kind, index=idx, state="downloading", pct=0)
            name = None
            try:
                if kind == "facebook":
                    with facebook.cookie_copy() as cookies, yt_dlp.YoutubeDL({**opts, "cookiefile": cookies}) as ydl:
                        info = ydl.extract_info(v["url"], download=False)
                        title = reel_caption(info, v["id"])
                        name, template = self._reserve_template(target_dir, title, f"reel_{v['id']}")
                        info["title"] = title
                        ydl.params["outtmpl"]["default"] = template
                        file = downloaded_file(ydl.process_ie_result(info, download=True))
                elif kind == "douyin":
                    title = v["title"]
                    name, _ = self._reserve_template(target_dir, title, f"douyin_{v['id']}")
                    file = douyin.download(v["media"], os.path.join(target_dir, name + ".mp4"), progress,
                                           self._cancel.is_set, yt_dlp.utils.DownloadCancelled)
                elif kind == "instagram":
                    if not v.get("media"):
                        v["media"], caption, _ = instagram.resolve(v["id"])
                        v["title"] = v["title"] or caption
                    title = v["title"] or f"reel_{v['id']}"
                    name, _ = self._reserve_template(target_dir, title, f"reel_{v['id']}")
                    path = os.path.join(target_dir, name + ".mp4")
                    ffmpeg = self._tools()[0]
                    try:
                        file = instagram.download(v["media"], path, ffmpeg, progress, self._cancel.is_set,
                                                  yt_dlp.utils.DownloadCancelled)
                    except instagram.DownloadExpired:  # cached links went stale; read fresh ones
                        v["media"], _, _ = instagram.resolve(v["id"])
                        file = instagram.download(v["media"], path, ffmpeg, progress, self._cancel.is_set,
                                                  yt_dlp.utils.DownloadCancelled)
                else:
                    title = v["title"]
                    name, template = self._reserve_template(target_dir, title, v["id"])
                    with yt_dlp.YoutubeDL({**opts, "outtmpl": template}) as ydl:
                        file = downloaded_file(ydl.extract_info(v["url"], download=True))
                mark_downloaded(kind, v, title, file)
                with progress_lock:
                    fractions[idx] = 1.0
                    results["done"] += 1
                self._emit("item", kind=kind, index=idx, state="done", pct=100)
            except Exception as ex:  # noqa: BLE001 - keep going with the remaining videos
                if name:
                    self._remove_partials(target_dir, name)
                with progress_lock:
                    fractions[idx] = 0.0
                    if self._cancel.is_set():
                        results["cancelled"] += 1
                    else:
                        results["failed"].append(f"{label}: {ex}")
                state = "cancelled" if self._cancel.is_set() else "failed"
                self._emit("item", kind=kind, index=idx, state=state, pct=0)
            report()

        try:
            with ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS) as pool:
                for idx, v in items:
                    pool.submit(job, idx, v)
        finally:
            self._emit("downloading", active=False)

        done, failed, cancelled = results["done"], results["failed"], results["cancelled"]
        report(force=True)
        summary = f"Downloaded {done}/{total} to {target_dir}" + (f" — {cancelled} cancelled" if cancelled else "")
        self._emit("status", text=summary, progress=done / total)
        self._emit("done", total=total, ok=done, cancelled=cancelled, dir=target_dir, failed=failed[:20])
        self._notify("Download finished", f"{done}/{total} videos saved" + (f", {len(failed)} failed" if failed else ""))

    # ---------- saved links ----------
    def saved_add(self, title, url):
        title, url = (title or "").strip(), (url or "").strip()
        if not title or not url:
            return {"ok": False, "error": "Please enter both Title and Link."}
        kind = detect_kind(url)
        if not kind:
            return {"ok": False, "error": "Only YouTube, Facebook, Douyin and Instagram links are supported."}
        url = normalize_url(kind, url)
        items = load_saved()
        existing = next((it for it in items if it["url"] == url), None)
        if existing:
            existing["title"] = title
        else:
            items.append({"title": title, "url": url})
        write_saved(items)
        return {"ok": True, "saved": items, "updated": bool(existing)}

    def saved_delete(self, url):
        items = [it for it in load_saved() if it["url"] != url]
        write_saved(items)
        return items

    def detect_kind(self, url):
        return detect_kind(url or "")

    # ---------- download history ----------
    def history_list(self):
        posts = {}
        for rec in _read_json(TT_POSTS_FILE, []):
            posts.setdefault(os.path.normcase(rec["file"]), []).append(rec)
        items = []
        for kind, entries in load_downloaded().items():
            for vid, rec in entries.items():
                file = rec.get("file") or ""
                items.append({
                    "kind": kind,
                    "id": vid,
                    "title": rec.get("title") or vid,
                    "at": rec.get("at") or "",
                    "url": rec.get("url") or video_url(kind, vid),
                    "thumb": rec.get("thumb") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if kind == "youtube" else ""),
                    "file": file,
                    "fileExists": bool(file) and os.path.isfile(file),
                    "manual": bool(rec.get("manual")),
                    "posts": [f"{x['profileName']} ({x['when'] or x['at']})" for x in posts.get(os.path.normcase(file), [])
                              if x["status"] == "posted"] if file else [],
                })
        items.sort(key=lambda it: it["at"], reverse=True)
        return items

    def history_remove(self, kind, video_id):
        with _downloaded_lock:
            data = load_downloaded()
            data.get(kind, {}).pop(video_id, None)
            write_downloaded(data)
        return self.history_list()

    def open_url(self, url):
        if detect_kind(url or ""):
            webbrowser.open(url)

    def reveal_file(self, path):
        """Open the file's folder in Explorer with the file selected.
        Not `explorer /select,<path>` from a list: Python quotes the whole "/select,..." argument when the path has
        a space, Explorer cannot parse that and opens Documents instead."""
        if not path or not os.path.isfile(path):
            return
        path = os.path.normpath(os.path.abspath(path))
        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        shell32.ILCreateFromPathW.restype = ctypes.c_void_p
        shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
        shell32.SHOpenFolderAndSelectItems.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
        shell32.ILFree.argtypes = [ctypes.c_void_p]
        ole32.CoInitialize(None)  # js_api calls run on worker threads
        try:
            pidl = shell32.ILCreateFromPathW(path)
            if pidl:
                try:
                    if shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0:
                        return
                finally:
                    shell32.ILFree(pidl)
        finally:
            ole32.CoUninitialize()
        subprocess.Popen(f'explorer /select,"{path}"')  # fallback: quote only the path

    # ---------- niche & channel research ----------
    @staticmethod
    def _research_key():
        """(key, source): the key saved in the app wins over YOUTUBE_API_KEY in the environment / a .env file."""
        saved = load_settings().get("youtubeApiKey") or ""
        if saved:
            return saved, "app"
        key = research.load_api_key(os.path.dirname(os.path.abspath(sys.argv[0])), os.path.dirname(sys.executable),
                                    BASE_DIR, facebook.DATA_DIR)
        return key, ".env" if key else ""

    def _research(self):
        key = self._research_key()[0]

        def log(level, message, detail=""):
            self._log(level, "research", message, detail)

        def progress(stage, text, frac=None):
            self._emit("researchProgress", stage=stage, text=text, value=frac)

        return research.Research(key, facebook.DATA_DIR, self._tools()[0], load_settings().get("ttChromeRoot") or "",
                                 log, progress, self._research_cancel.is_set)

    def research_state(self, market="US"):
        if not research:
            return {"ok": False, "error": "Not available in this edition."}
        try:
            r = self._research()
            has_key, quota, niches = True, r.quota(), r.niche_results(market)
            last = (r.cache.get("runs") or {}).get(market)
        except research.ResearchError:
            has_key, quota, niches, last = False, 0, [], None
        lang = research.MARKETS.get(market, research.MARKETS["US"])["lang"]
        key, source = self._research_key()
        return {"markets": list(research.MARKETS), "hasKey": has_key, "quota": quota, "quotaMax": research.DAILY_QUOTA,
                "keyMasked": f"{key[:4]}…{key[-4:]}" if len(key) > 8 else "", "keySource": source,
                "niches": niches, "running": self._research_running, "last": last,
                "catalog": [{"id": n[0], "name": n[1], "keyword": n[2][lang][0]} for n in research.NICHES],
                "chromeRoot": bool(load_settings().get("ttChromeRoot"))}

    def _start_research(self, name, fn, *args):
        if not self._license["ok"]:
            self._emit("licenseRequired")
            return {"ok": False, "error": "Activate Grabber first."}
        with self._research_lock:
            if self._research_running:
                return {"ok": False, "error": "A research run is already going."}
            self._research_running = True
        self._research_cancel.clear()

        def run():
            try:
                fn(*args)
            except research.Cancelled:
                self._emit("researchDone", kind=name, cancelled=True)
                self._log("info", "research", "Research cancelled")
            except Exception as ex:  # noqa: BLE001 - report every failure in the Research tab and the log
                self._log("error", "research", f"{name}: {ex}", traceback.format_exc())
                self._emit("researchDone", kind=name, error=str(ex))
            finally:
                self._research_running = False
                self._emit("researchQuota", quota=self._research().quota() if self._has_research_key() else 0)

        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}

    def _has_research_key(self):
        try:
            self._research()
            return True
        except research.ResearchError:
            return False

    def research_set_key(self, key):
        """Check the key with one cheap API call (1 unit), then keep it in settings.json."""
        if not research:
            return {"ok": False, "error": "Not available in this edition."}
        key = (key or "").strip()
        if not key:
            settings = load_settings()
            settings.pop("youtubeApiKey", None)
            _write_json(SETTINGS_FILE, settings)
            self._log("info", "research", "YouTube API key removed from the app")
            return {"ok": True}
        if not re.fullmatch(r"[A-Za-z0-9_\-]{30,60}", key):
            return {"ok": False, "error": "This does not look like a Google API key (it usually starts with “AIza”)."}
        try:
            research.YouTube(key, lambda _units: None).call("channels", part="id", forHandle="@YouTube")
        except research.ResearchError as ex:
            return {"ok": False, "error": f"YouTube rejected the key: {ex}"}
        except OSError as ex:
            return {"ok": False, "error": f"Could not reach YouTube to check the key: {ex}"}
        save_setting("youtubeApiKey", key)
        self._log("success", "research", f"YouTube API key saved ({key[:4]}…{key[-4:]})")
        return {"ok": True}

    def research_cancel(self):
        self._research_cancel.set()

    def research_niches(self, market, count=10):
        if not research:
            return {"ok": False, "error": "Not available in this edition."}
        if market not in research.MARKETS:
            return {"ok": False, "error": "Unknown market."}
        return self._start_research("niches", self._research_niches_task, market, int(count or 10))

    def _research_niches_task(self, market, count):
        r = self._research()
        self._log("info", "research", f"Scoring {count} niches for {market}")
        rows = r.research_niches(market, count)
        self._emit("researchDone", kind="niches", market=market, niches=rows)

    def research_channels(self, market, niche):
        """`niche`: a catalog id or a free-text keyword typed by the user."""
        if not research:
            return {"ok": False, "error": "Not available in this edition."}
        niche = (niche or "").strip()
        if market not in research.MARKETS or not niche:
            return {"ok": False, "error": "Choose a market and a niche."}
        if not load_settings().get("ttChromeRoot"):
            return {"ok": False, "error": "Choose your Chrome Portable folder in the TikTok tab first (the TikTok check runs in its 'Research' profile)."}
        return self._start_research("channels", self._research_channels_task, market, niche)

    def _research_channels_task(self, market, niche):
        r = self._research()
        lang = research.MARKETS[market]["lang"]
        typed = niche.casefold()
        entry = research.NICHE_BY_ID.get(niche) or next(
            (n for n in research.NICHES if typed == n[1].casefold() or any(typed == k.casefold() for ks in n[2].values() for k in ks)), None)
        name, keywords = (entry[1], entry[2][lang][:2]) if entry else (niche, [niche])
        topic_words = [name, *keywords, *entry[2]["en"]] if entry else []
        self._log("info", "research", f"Finding Shorts channels for “{name}” in {market}", ", ".join(keywords))
        found = r.find_channels(market, keywords, topic_words)
        candidates = found[:25]
        if candidates:
            r.check_tiktok(candidates, stop_after=5)
        checked = [c for c in candidates if c.get("tiktok")]
        free = [c for c in checked if c["tiktok"]["verdict"] in ("none", "reup")]
        free.sort(key=lambda c: (c["tiktok"]["verdict"] != "none", -c["score"]))
        picks = free[:5]
        for c in found:  # keep the saved run small: a few example Shorts per checked channel
            c["recent"] = c.get("recent", [])[:3] if c in candidates else []
            c.pop("popular", None)
        run = {"market": market, "niche": name, "keywords": keywords, "at": time.strftime("%Y-%m-%d %H:%M"),
               "found": len(found), "picks": [c["id"] for c in picks],
               "channels": checked + [c for c in found if c not in candidates][:20]}
        with r._lock:
            r.cache.setdefault("runs", {})[market] = run
            r._save()
        summary = ", ".join(c["title"] + (" (only re-uploaded)" if c["tiktok"]["verdict"] == "reup" else "")
                            for c in picks) or "none"
        self._log("success" if picks else "warning", "research",
                  f"{name} [{market}]: {len(picks)} promising channels not on TikTok", summary)
        self._emit("researchDone", kind="channels", run=run)
        self._notify("Research finished", f"{name}: {len(picks)} channels not on TikTok yet")

    def research_check(self, url):
        if not research:
            return {"ok": False, "error": "Not available in this edition."}
        if not load_settings().get("ttChromeRoot"):
            return {"ok": False, "error": "Choose your Chrome Portable folder in the TikTok tab first."}
        return self._start_research("check", self._research_check_task, (url or "").strip())

    def _research_check_task(self, url):
        r = self._research()
        c = r.channel_by_url(url)
        r.check_tiktok([c])
        c["recent"] = c.get("recent", [])[:3]
        c.pop("popular", None)
        self._emit("researchDone", kind="check", channel=c)

    def research_open(self, url):
        host = re.sub(r"^https?://", "", (url or "").lower()).split("/")[0]
        if host.endswith(("youtube.com", "tiktok.com")) or host == "console.cloud.google.com":
            webbrowser.open(url)

    def research_login(self):
        """Open the Research Chrome profile on the TikTok login page so the user can sign in there themselves."""
        if not research:
            return {"ok": False, "error": "Not available in this edition."}
        root = load_settings().get("ttChromeRoot")
        if not root:
            return {"ok": False, "error": "Choose your Chrome Portable folder in the TikTok tab first."}
        try:
            tiktok._open_window(root, research.RESEARCH_PROFILE, "https://www.tiktok.com/login")
        except (tiktok.PostError, OSError) as ex:
            return {"ok": False, "error": str(ex)}
        return {"ok": True}

    # ---------- Sync between PCs (owner edition, private GitHub repo, encrypted) ----------
    def _sync(self):
        if not (OWNER and sync):
            raise RuntimeError("Not available in this edition.")
        settings = load_settings()
        if not (settings.get("syncRepo") and settings.get("syncToken") and settings.get("syncPass")):
            raise RuntimeError("Set up Sync first.")
        return settings, sync.GitHub(settings["syncRepo"], settings["syncToken"])

    def sync_state(self):
        settings = load_settings()
        token = settings.get("syncToken") or ""
        return {"available": bool(OWNER and sync), "repo": settings.get("syncRepo") or "",
                "token": f"{token[:4]}…{token[-4:]}" if token else "", "hasPass": bool(settings.get("syncPass")),
                "lastSync": settings.get("syncAt") or ""}

    def sync_setup(self, repo, token, passphrase):
        """Check the repo and token, and that the passphrase opens what is already in the repo, then save them."""
        if not (OWNER and sync):
            return {"ok": False, "error": "Not available in this edition."}
        settings = load_settings()
        token = (token or "").strip() or settings.get("syncToken") or ""
        passphrase = passphrase or settings.get("syncPass") or ""
        if len(passphrase) < 8:
            return {"ok": False, "error": "Use a passphrase of at least 8 characters."}
        try:
            gh = sync.GitHub(repo, token)
            gh.check()
            _sha, blob = gh.download()
            if blob:
                sync.unpack(blob, passphrase)
        except sync.SyncError as ex:
            return {"ok": False, "error": str(ex)}
        if gh.repo != settings.get("syncRepo"):
            for k in ("syncSha", "syncDigest", "syncAt"):
                settings.pop(k, None)
        settings.update(syncRepo=gh.repo, syncToken=token, syncPass=passphrase)
        _write_json(SETTINGS_FILE, settings)
        self._log("success", "sync", f"Sync set up with {gh.repo}")
        return {"ok": True}

    def _sync_done(self, settings, sha, files, when):
        settings.update(syncSha=sha, syncDigest=sync.digest(files), syncAt=when)
        _write_json(SETTINGS_FILE, settings)

    def sync_upload(self, force=False):
        try:
            settings, gh = self._sync()
            files = sync.read_local(facebook.DATA_DIR)
            sha, blob = gh.download()
            if sha and sha != settings.get("syncSha") and not force:
                return {"ok": False, "conflict": "remote", "meta": sync.unpack(blob, settings["syncPass"])["meta"]}
            new_blob, meta = sync.pack(files, settings["syncPass"])
            new_sha = gh.upload(new_blob, sha, f"Sync from {meta['host']}")
        except (RuntimeError, sync.SyncError) as ex:
            return {"ok": False, "error": str(ex)}
        self._sync_done(settings, new_sha, files, meta["time"])
        self._log("success", "sync", f"Uploaded {len(files)} data files to {gh.repo}")
        return {"ok": True, "files": len(files)}

    def sync_fetch(self, force=False):
        if self._busy or self._research_running:
            return {"ok": False, "error": "Wait until the current download or research run finishes."}
        try:
            settings, gh = self._sync()
            sha, blob = gh.download()
            if not sha:
                return {"ok": False, "error": "Nothing uploaded yet. Press Upload on the other PC first."}
            local = sync.read_local(facebook.DATA_DIR)
            changed = sync.digest(local) != settings.get("syncDigest")
            if sha == settings.get("syncSha") and not changed:
                return {"ok": True, "upToDate": True}
            data = sync.unpack(blob, settings["syncPass"])
            if changed and local and not force:
                return {"ok": False, "conflict": "local", "meta": data["meta"]}
            sync.write_local(facebook.DATA_DIR, data["files"])
        except (RuntimeError, sync.SyncError) as ex:
            return {"ok": False, "error": str(ex)}
        except OSError as ex:
            return {"ok": False, "error": f"Could not write the data files: {ex}"}
        self._sync_done(settings, sha, sync.read_local(facebook.DATA_DIR), data["meta"]["time"])
        self._log("success", "sync", f"Fetched data uploaded by {data['meta']['host']} at {data['meta']['time']}")
        return {"ok": True, "meta": data["meta"]}

    # ---------- TikTok posting (Chrome Portable profiles) ----------
    def tt_state(self):
        settings = load_settings()
        root = settings.get("ttChromeRoot") or ""
        countries = settings.get("ttCountries") or {}
        state = {"root": root, "profiles": [], "error": "", "posts": _read_json(TT_POSTS_FILE, [])[-50:][::-1]}
        if root:
            try:
                state["profiles"] = [{**pr, "auto": pr["country"], "country": countries.get(pr["dir"], pr["country"])}
                                     for pr in tiktok.list_profiles(root)]
            except (tiktok.PostError, OSError, ValueError) as ex:
                state["error"] = str(ex)
        state["countries"] = tiktok.COUNTRIES
        return state

    def tt_choose_root(self):
        current = load_settings().get("ttChromeRoot") or os.path.join(os.path.expanduser("~"), "Desktop")
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=current)
        if not result:
            return None
        try:
            tiktok.chrome_paths(result[0])
        except tiktok.PostError as ex:
            return {"error": str(ex)}
        save_setting("ttChromeRoot", result[0])
        return self.tt_state()

    def tt_set_country(self, profile_dir, country):
        countries = load_settings().get("ttCountries") or {}
        countries[profile_dir] = country or ""
        save_setting("ttCountries", countries)
        return True

    def tt_post(self, req):
        root = load_settings().get("ttChromeRoot") or ""
        file = (req or {}).get("file") or ""
        if not root:
            return {"ok": False, "error": "Choose your Chrome Portable folder in the TikTok tab first."}
        if not os.path.isfile(file):
            return {"ok": False, "error": "The video file no longer exists."}
        profile = next((pr for pr in self.tt_state()["profiles"] if pr["dir"] == req.get("profile")), None)
        if not profile:
            return {"ok": False, "error": "Choose a Chrome profile."}
        when = None
        if req.get("schedule"):
            when = datetime.datetime.strptime(req["schedule"], "%Y-%m-%dT%H:%M")
            if when < datetime.datetime.now() + datetime.timedelta(minutes=15):
                return {"ok": False, "error": "Schedule at least 15 minutes from now."}
        if not self._run(self._tt_post_task, root, profile, file, (req.get("caption") or "").strip(), when,
                         bool(req.get("review"))):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True}

    def _save_tt_post(self, record):
        posts = _read_json(TT_POSTS_FILE, [])
        posts.append(record)
        _write_json(TT_POSTS_FILE, posts[-500:])

    def tt_post_many(self, req):
        """Post several videos to one profile: {"profile", "review", "items": [{"file", "caption", "schedule"}]}."""
        root = load_settings().get("ttChromeRoot") or ""
        if not root:
            return {"ok": False, "error": "Choose your Chrome Portable folder in the TikTok tab first."}
        profile = next((pr for pr in self.tt_state()["profiles"] if pr["dir"] == (req or {}).get("profile")), None)
        if not profile:
            return {"ok": False, "error": "Choose a Chrome profile."}
        jobs = []
        soon = datetime.datetime.now() + datetime.timedelta(minutes=15)
        for n, it in enumerate(req.get("items") or [], 1):
            if not os.path.isfile(it.get("file") or ""):
                return {"ok": False, "error": f"Video {n}: the file no longer exists."}
            when = None
            if it.get("schedule"):
                when = datetime.datetime.strptime(it["schedule"], "%Y-%m-%dT%H:%M")
                if when < soon:
                    return {"ok": False, "error": f"Video {n}: schedule at least 15 minutes from now."}
            jobs.append({"video": it["file"], "caption": (it.get("caption") or "").strip(), "when": when})
        if not jobs:
            return {"ok": False, "error": "No videos to post."}
        if not self._run(self._tt_post_many_task, root, profile, jobs, bool(req.get("review"))):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True}

    def _tt_post_many_task(self, root, profile, jobs, review):
        name = profile["name"]
        self._log("info", "tiktok", f"{name}: start {len(jobs)} videos{' (stop before Post)' if review else ''}",
                  "\n".join(os.path.basename(j["video"]) for j in jobs))

        def status(text):
            self._emit("status", text=f"{name}: {text}", progress=None)
            self._log("info", "tiktok", f"{name}: {text}")

        results = [None] * len(jobs)

        def on_item(i, error):
            job = jobs[i]
            when = job["when"]
            record = {"file": job["video"], "profile": profile["dir"], "profileName": name, "caption": job["caption"],
                      "at": time.strftime("%Y-%m-%d %H:%M"), "when": when.strftime("%Y-%m-%d %H:%M") if when else "",
                      "status": "failed" if error else "review" if review else "posted", "error": error or "",
                      "country": profile["country"]}
            results[i] = record
            self._save_tt_post(record)
            if error:
                self._log("error", "tiktok", f"Video {i + 1}/{len(jobs)} to {name} failed: {error}", job["video"])
            else:
                done = "Ready for review in" if review else "Scheduled on" if when else "Posted to"
                self._log("success", "tiktok", f"{done} {name}" + (f" for {record['when']}" if when else "")
                          + f" — {os.path.basename(job['video'])}", job["caption"])
            self._emit("ttBatchItem", index=i, state="failed" if error else "done", error=error or "")
            self._emit("ttPosted", record=record, batch=True)

        try:
            tiktok.post_many(root, profile["dir"], jobs, profile["country"], review, status,
                             shots_dir=os.path.join(facebook.DATA_DIR, "tiktok-errors"), on_item=on_item)
        except Exception as ex:  # noqa: BLE001 - VPN / Chrome failed before any video: every video fails
            error = str(ex) if isinstance(ex, (tiktok.PostError, tiktok.ChromeBusy)) else f"{type(ex).__name__}: {ex}"
            for i in range(len(jobs)):
                if results[i] is None:
                    on_item(i, error)
        ok = sum(1 for r in results if r and r["status"] != "failed")
        verb = "ready for review" if review else "posted"
        self._emit("status", text=f"{name}: {ok}/{len(jobs)} videos {verb}", progress=0)
        self._emit("ttBatchDone", ok=ok, total=len(jobs), review=review)
        self._notify("TikTok", f"{name}: {ok}/{len(jobs)} videos {verb}")

    def _tt_post_task(self, root, profile, file, caption, when, review):
        name = profile["name"]
        self._log("info", "tiktok", f"{name}: start {'scheduling for ' + when.strftime('%Y-%m-%d %H:%M') if when else 'posting now'}"
                                    f"{' (stop before Post)' if review else ''} — {os.path.basename(file)}", caption)

        def status(text):
            self._emit("status", text=f"{name}: {text}", progress=None)
            self._log("info", "tiktok", f"{name}: {text}")

        record = {"file": file, "profile": profile["dir"], "profileName": name, "caption": caption,
                  "at": time.strftime("%Y-%m-%d %H:%M"), "when": when.strftime("%Y-%m-%d %H:%M") if when else "",
                  "status": "", "error": ""}
        try:
            country = tiktok.post(root, profile["dir"], file, caption, when, profile["country"], review, status,
                                  shots_dir=os.path.join(facebook.DATA_DIR, "tiktok-errors"))
            record["status"] = "review" if review else "posted"
            record["country"] = country
        except (tiktok.PostError, tiktok.ChromeBusy) as ex:
            record["status"], record["error"] = "failed", str(ex)
            record["trace"] = traceback.format_exc()
        except Exception as ex:  # noqa: BLE001 - Playwright / network errors: report and keep the app alive
            record["status"], record["error"] = "failed", f"{type(ex).__name__}: {ex}"
            record["trace"] = traceback.format_exc()
        self._save_tt_post(record)
        done = {"posted": "Scheduled on" if when else "Posted to", "review": "Ready for review in"}.get(record["status"])
        if done:
            self._log("success", "tiktok", f"{done} {name}" + (f" for {record['when']}" if when else "")
                      + f" — {os.path.basename(file)}", caption)
        else:
            self._log("error", "tiktok", f"Posting to {name} failed: {record['error']}", record.get("trace", ""))
        self._emit("status", text=f"{done} {name}" if done else f"Posting to {name} failed", progress=0)
        self._emit("ttPosted", record=record)
        self._notify("TikTok", f"{done} {name}" if done else f"Posting to {name} failed")

    # ---------- editor ----------
    @staticmethod
    def _tools():
        return editor.find_tool("ffmpeg", FFMPEG_DIR), editor.find_tool("ffprobe", FFMPEG_DIR)

    def editor_open(self, path=None):
        """Open a video for editing (file dialog when no path); returns its info and a local preview URL."""
        if not path:
            picked = self._window.create_file_dialog(
                webview.FileDialog.OPEN, file_types=("Video files (*.mp4;*.mov;*.mkv;*.webm;*.m4v)", "All files (*.*)"))
            if not picked:
                return {"ok": False, "cancelled": True}
            path = picked[0]
        if not os.path.isfile(path):
            return {"ok": False, "error": "File not found."}
        try:
            info = editor.probe(self._tools()[1], path)
        except (OSError, RuntimeError, ValueError) as ex:
            return {"ok": False, "error": f"Cannot open this video: {ex}"}
        threading.Thread(target=self._detect_encoder, daemon=True).start()
        stem = os.path.splitext(os.path.basename(path))[0]
        return {"ok": True, "path": path, "name": os.path.basename(path), "dir": os.path.dirname(path),
                "suggestedName": f"{stem} (edited)", "url": self._media.url_for(path), "info": info}

    def editor_pick_music(self, path=None):
        """Background music (file dialog when no path); returns its duration and a preview URL."""
        if not path:
            picked = self._window.create_file_dialog(
                webview.FileDialog.OPEN, file_types=("Audio files (*.mp3;*.m4a;*.aac;*.wav;*.ogg;*.flac)", "All files (*.*)"))
            if not picked:
                return {"ok": False, "cancelled": True}
            path = picked[0]
        try:
            info = editor.probe_audio(self._tools()[1], path)
        except (OSError, RuntimeError, ValueError) as ex:
            return {"ok": False, "error": f"Cannot use this file: {ex}"}
        return {"ok": True, "path": path, "name": os.path.basename(path), "duration": info["duration"],
                "url": self._media.url_for(path)}

    def editor_pick_logo(self, path=None):
        if not path:
            picked = self._window.create_file_dialog(
                webview.FileDialog.OPEN, file_types=("Images (*.png;*.jpg;*.jpeg;*.webp)", "All files (*.*)"))
            if not picked:
                return {"ok": False, "cancelled": True}
            path = picked[0]
        if not os.path.isfile(path):
            return {"ok": False, "error": "File not found."}
        return {"ok": True, "path": path, "name": os.path.basename(path), "url": self._media.url_for(path)}

    def list_fonts(self):
        """Installed TrueType/OpenType fonts (one entry per face) with a URL so the preview can load the same file."""
        if self._fonts is None:
            import winreg
            fonts = {}
            key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k = winreg.OpenKey(hive, key)
                except OSError:
                    continue
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(k, i)
                    except OSError:
                        break
                    i += 1
                    path = value if os.path.isabs(value) else os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", value)
                    if os.path.splitext(path)[1].lower() in (".ttf", ".otf", ".ttc") and os.path.isfile(path):
                        fonts[re.sub(r"\s*\((TrueType|OpenType)\)$", "", name)] = path
            self._fonts = [{"name": n, "path": p} for n, p in sorted(fonts.items(), key=lambda kv: kv[0].lower())]
        return self._fonts

    def font_url(self, path):
        return self._media.url_for(path) if path and os.path.isfile(path) else ""

    def editor_probe_many(self, paths=None):
        """Info for several videos (file dialog when no paths) - used by batch and merge lists."""
        if not paths:
            picked = self._window.create_file_dialog(
                webview.FileDialog.OPEN, allow_multiple=True,
                file_types=("Video files (*.mp4;*.mov;*.mkv;*.webm;*.m4v)", "All files (*.*)"))
            if not picked:
                return []
            paths = list(picked)
        ffprobe = self._tools()[1]
        items = []
        for p in paths:
            try:
                i = editor.probe(ffprobe, p)
                items.append({"ok": True, "path": p, "name": os.path.basename(p), "duration": i["duration"],
                              "width": i["width"], "height": i["height"]})
            except (OSError, RuntimeError, ValueError) as ex:
                items.append({"ok": False, "path": p, "name": os.path.basename(p), "error": str(ex)})
        return items

    # ---------- edit presets ----------
    def presets_list(self):
        return _read_json(PRESETS_FILE, [])

    def preset_save(self, name, preset):
        name = (name or "").strip()
        if not name:
            return {"ok": False, "error": "Give the preset a name."}
        items = [p for p in self.presets_list() if p["name"] != name]
        items.append({"name": name, "preset": preset, "saved": time.strftime("%Y-%m-%d %H:%M")})
        _write_json(PRESETS_FILE, items)
        return {"ok": True, "presets": items}

    def preset_delete(self, name):
        items = [p for p in self.presets_list() if p["name"] != name]
        _write_json(PRESETS_FILE, items)
        return items

    def preset_resolve(self, preset, path):
        """Spec of a preset for one video (crop adapted to its frame)."""
        try:
            return {"ok": True, "spec": editor.resolve_preset(preset, editor.probe(self._tools()[1], path))}
        except (OSError, RuntimeError, ValueError) as ex:
            return {"ok": False, "error": str(ex)}

    # ---------- batch export ----------
    def batch_start(self, paths, preset, out_dir, suffix):
        paths = [p for p in (paths or []) if os.path.isfile(p)]
        if not paths:
            return {"ok": False, "error": "Add some videos first."}
        if not preset:
            return {"ok": False, "error": "Choose a preset."}
        if not self._run(self._batch_task, paths, preset, (out_dir or "").strip(), suffix or ""):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True}

    def batch_pick_dir(self, current=""):
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=current or "")
        return result[0] if result else None

    def _batch_task(self, paths, preset, out_dir, suffix):
        ffmpeg, ffprobe = self._tools()
        self._edit_cancel.clear()
        done, failed = 0, []
        for idx, path in enumerate(paths):
            if self._edit_cancel.is_set():
                self._emit("batchItem", index=idx, state="cancelled", pct=0)
                continue
            self._emit("batchItem", index=idx, state="working", pct=0)
            last = [0.0]

            def progress(frac, idx=idx):
                now = time.monotonic()
                if now - last[0] >= 0.25:
                    last[0] = now
                    self._emit("batchItem", index=idx, state="working", pct=round(frac * 100))

            try:
                info = editor.probe(ffprobe, path)
                spec = editor.resolve_preset(preset, info)
                folder = out_dir or os.path.dirname(path)
                os.makedirs(folder, exist_ok=True)
                stem = os.path.splitext(os.path.basename(path))[0]
                dst = editor.unique_output(folder, safe_name(stem + suffix, fallback="edited"))
                editor.export(ffmpeg, path, dst, info, spec, progress, self._edit_cancel.is_set)
                done += 1
                self._emit("batchItem", index=idx, state="done", pct=100, output=dst)
            except editor.Cancelled:
                self._emit("batchItem", index=idx, state="cancelled", pct=0)
            except Exception as ex:  # noqa: BLE001 - keep going with the other files
                failed.append(f"{os.path.basename(path)}: {ex}")
                self._emit("batchItem", index=idx, state="failed", pct=0, error=str(ex)[:200])
        self._emit("batchDone", total=len(paths), ok=done, failed=failed[:20], cancelled=self._edit_cancel.is_set())
        self._notify("Batch export finished", f"{done}/{len(paths)} videos exported")

    # ---------- merge ----------
    def merge_start(self, paths, opts, out_name):
        paths = [p for p in (paths or []) if os.path.isfile(p)]
        if len(paths) < 2:
            return {"ok": False, "error": "Add at least two clips."}
        dst = editor.unique_output(os.path.dirname(paths[0]), safe_name((out_name or "").strip(), fallback="merged"))
        if not self._run(self._merge_task, paths, opts or {}, dst):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True, "dst": dst}

    def _merge_task(self, paths, opts, dst):
        ffmpeg, ffprobe = self._tools()
        self._edit_cancel.clear()
        infos = [editor.probe(ffprobe, p) for p in paths]
        last = [0.0]

        def progress(frac):
            now = time.monotonic()
            if now - last[0] >= 0.2:
                last[0] = now
                self._emit("mergeProgress", value=frac)

        self._emit("mergeProgress", value=None)
        try:
            res = editor.merge(ffmpeg, paths, infos, dst, opts, progress, self._edit_cancel.is_set)
        except editor.Cancelled:
            self._emit("mergeDone", cancelled=True)
            return
        self._emit("mergeDone", path=res["path"], name=os.path.basename(res["path"]), width=res["width"],
                   height=res["height"], duration=res["duration"], encoder=res["encoder"])
        self._notify("Merge finished", os.path.basename(res["path"]))

    # ---------- smart cut (owner edition) ----------
    def smartcut_state(self):
        if not smartcut:
            return {"ok": False, "error": "Not available in this edition."}
        opts = {**smartcut.defaults(), **(load_settings().get("smartCut") or {})}
        return {"ok": True, "opts": opts, "whisperModels": list(smartcut.WHISPER_MODELS),
                "ollama": smartcut.ollama_models(), "cuda": smartcut.cuda_devices()}

    def smartcut_start(self, path, opts):
        """`path`: a video file, or a YouTube / Douyin link that is downloaded first."""
        if not smartcut:
            return {"ok": False, "error": "Not available in this edition."}
        path = (path or "").strip()
        if re.match(r"^(https?://|www\.|youtu\.be/|v\.douyin\.com/)", path, re.I):
            if detect_kind(path) not in ("youtube", "douyin"):
                return {"ok": False, "error": "Paste a YouTube or Douyin video link."}
        elif not path or not os.path.isfile(path):
            return {"ok": False, "error": "Choose a video first."}
        opts = {**smartcut.defaults(), **(opts or {})}
        opts["count"] = min(max(int(opts["count"]), 1), 20)
        opts["minLen"], opts["maxLen"] = sorted((min(max(float(opts["minLen"]), 5), 180), min(max(float(opts["maxLen"]), 5), 180)))
        opts["style"] = "moments" if opts["style"] == "moments" else "series"
        opts["target"] = min(max(float(opts["target"]), 20), 180)
        if opts["style"] == "moments" and opts["maxLen"] - opts["minLen"] < 5:
            return {"ok": False, "error": "Leave at least 5 seconds between the shortest and the longest clip."}
        if opts["whisper"] not in smartcut.WHISPER_MODELS:
            opts["whisper"] = "small"
        if opts["reframe"] not in ("face", "center", "fit"):
            opts["reframe"] = "face"
        if opts["subPos"] not in smartcut.SUB_POSITIONS:
            opts["subPos"] = "bottom"
        save_setting("smartCut", opts)
        if not self._run(self._smartcut_task, path, opts):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True}

    def _smartcut_download(self, url, progress, log, cancel):
        """Download a YouTube / Douyin video into <download folder>/Smart cut (reused when already there)."""
        folder = os.path.join(load_settings().get("dir") or DEFAULT_DIR, "Smart cut")
        os.makedirs(folder, exist_ok=True)
        if not url.startswith("http"):
            url = "https://" + url
        kind = detect_kind(url)
        progress("Reading the link…", None)
        if kind == "douyin":
            v = douyin.resolve_video(url)
            vid, title = v["id"], v["title"]
        else:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
            if info.get("_type") in ("playlist", "multi_video"):
                raise RuntimeError("This is a playlist or channel link - paste the link of one video.")
            vid, title = info["id"], info.get("title") or info["id"]
        tag = f"[{vid}]"
        done = next((os.path.join(folder, f) for f in os.listdir(folder) if tag in f and f.endswith(".mp4")), None)
        if done:
            log(f"Already downloaded: {os.path.basename(done)}")
            return done
        dst = os.path.join(folder, safe_name(f"{title[:80]} {tag}", fallback=tag) + ".mp4")
        log(f"Downloading “{title[:80]}”")
        last = [0.0]

        def report(frac):
            now = time.monotonic()
            if now - last[0] >= 0.3:
                last[0] = now
                progress(f"Downloading the video… {round(frac * 100)}%", frac)

        if kind == "douyin":
            return douyin.download(v["media"], dst, report, cancel, editor.Cancelled)

        def hook(d):
            if cancel():
                raise yt_dlp.utils.DownloadCancelled("Cancelled by user")
            size = d.get("total_bytes") or d.get("total_bytes_estimate")
            if d.get("status") == "downloading" and size:
                report(min(d["downloaded_bytes"] / size, 1))

        opts = {"format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
                "merge_output_format": "mp4", "outtmpl": {"default": dst[:-4] + ".%(ext)s"}, "noplaylist": True,
                **({"ffmpeg_location": FFMPEG_DIR} if os.path.isfile(os.path.join(FFMPEG_DIR, "ffmpeg.exe")) else {}),
                "quiet": True, "no_warnings": True, "noprogress": True, "progress_hooks": [hook]}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return downloaded_file(ydl.extract_info(url, download=True)) or dst
        except yt_dlp.utils.DownloadCancelled:
            raise editor.Cancelled() from None

    def _smartcut_task(self, path, opts):
        ffmpeg, ffprobe = self._tools()
        self._edit_cancel.clear()
        cancel = self._edit_cancel.is_set
        name = os.path.basename(path) if os.path.isfile(path) else path

        def progress(text, value):
            self._emit("scProgress", text=text, value=value)

        def log(message):
            self._log("info", "smartcut", f"{name}: {message}")
            self._emit("scLog", message=message)

        clips = []
        try:
            if not os.path.isfile(path):  # a link: download it, then cut the file
                try:
                    path = self._smartcut_download(path, progress, log, cancel)
                except (douyin.LoginRequired, yt_dlp.utils.DownloadError) as ex:
                    raise RuntimeError(re.sub(r"^ERROR:\s*", "", str(ex))) from None
                name = os.path.basename(path)
                p_info = editor.probe(ffprobe, path)
                self._emit("scSource", path=path, name=name, duration=p_info["duration"], width=p_info["width"],
                           height=p_info["height"])
            info = editor.probe(ffprobe, path)
            data = smartcut.analyze(ffmpeg, path, info, opts, facebook.DATA_DIR, progress, cancel, log)
            progress("Choosing the best moments…", 0.6)
            if opts["style"] == "series":
                moments, method = smartcut.series(data, info, opts, cancel, log)
                if not moments:
                    raise RuntimeError("Nothing to cut in this video.")
                log(f"Series of {len(moments)} part(s), " + ("blocks chosen with the LLM" if method == "llm" else "heuristics")
                    + ": " + "; ".join(f"Part {m['part']}: {round(m['length'])} s, {smartcut._fmt(m['start'])}-"
                                       f"{smartcut._fmt(m['end'])}" for m in moments))
            else:
                moments, method = smartcut.pick(data, info, opts, cancel, log)
                if not moments:
                    raise RuntimeError("No moment fits the clip length - try a shorter minimum length.")
                for m in moments:
                    m["parts"] = [[m["start"], m["end"]]]
                log(f"Picked {len(moments)} moment(s) with " + ("the LLM" if method == "llm" else "heuristics") + ": "
                    + ", ".join(f"{smartcut._fmt(m['start'])}-{smartcut._fmt(m['end'])}" for m in moments))
            self._emit("scPlan", method=method, moments=moments, language=data["language"])
            progress("Looking for black bars…", 0.61)
            bars = smartcut.detect_bars(path, info["duration"])
            if bars and bars[0] * bars[1] < info["width"] * info["height"] * 0.96:
                log(f"Black bars removed: the picture is {bars[0]}×{bars[1]}")
            else:
                bars = None
            pic_w, pic_h = (bars[0], bars[1]) if bars else (info["width"], info["height"])
            yunet = None
            if opts["reframe"] == "face" and pic_w / pic_h > 9 / 16:
                progress("Getting the face detector…", 0.62)
                yunet = smartcut.ensure_yunet(os.path.join(facebook.DATA_DIR, "models"))
            stem = os.path.splitext(name)[0]
            folder = os.path.join(os.path.dirname(path), safe_name(f"{stem} - smart cut", fallback="smart cut"))
            os.makedirs(folder, exist_ok=True)
            span = 0.37 / len(moments)
            for i, m in enumerate(moments):
                if cancel():
                    raise editor.Cancelled()
                base = 0.62 + i * span
                start, end = m["start"], m["end"]
                keys_list = [[(0.0, 0.5)] for _ in m["parts"]]
                if yunet:
                    progress(f"Clip {i + 1}/{len(moments)}: following faces…", base)
                    for k, (a, b) in enumerate(m["parts"]):
                        samples = smartcut.track_faces(path, a, b, yunet, cancel, bars)
                        cuts = [c - a for c in data["cuts"] if a < c < b]
                        keys_list[k] = smartcut.camera_path(samples, cuts, (pic_h * 9 / 16) / pic_w)
                subs = None
                tmp_subs = os.path.join(folder, f".clip{i + 1}.ass")
                label = f"Part {m['part']}/{m['of']}" if m.get("part") and opts["partLabel"] and m["of"] > 1 else ""
                if (opts["subtitles"] and data["segments"]) or label:
                    segs = data["segments"] if opts["subtitles"] else []
                    if smartcut.write_ass(tmp_subs, segs, m["parts"], data["language"], opts["subPos"], label):
                        subs = tmp_subs
                title = m.get("title") or ""
                if m.get("part"):  # series: "Part 2/3 · <caption>" for TikTok, "Part 2 - <caption>" as the file name
                    stem_name = f"Part {m['part']} - {title}" if title else f"Part {m['part']}"
                    title = f"Part {m['part']}/{m['of']}" + (f" · {title}" if title else "")
                else:
                    stem_name = f"{i + 1:02d} - {title or smartcut._fmt(start).replace(':', '.')}"
                dst = editor.unique_output(folder, safe_name(stem_name, fallback=f"clip {i + 1}", limit=80))
                last = [0.0]

                def on_progress(frac, i=i, base=base):
                    now = time.monotonic()
                    if now - last[0] >= 0.25:
                        last[0] = now
                        progress(f"Clip {i + 1}/{len(moments)}: rendering… {round(frac * 100)}%", base + span * (0.3 + 0.7 * frac))

                try:
                    progress(f"Clip {i + 1}/{len(moments)}: rendering…", base + span * 0.3)
                    smartcut.render(ffmpeg, path, dst, info, m["parts"], keys_list, subs, on_progress, cancel, opts["reframe"], bars)
                finally:
                    if subs:
                        os.remove(subs)
                clip = {"index": i, "path": dst, "name": os.path.basename(dst), "title": title, "reason": m.get("reason", ""),
                        "start": start, "end": end, "score": m.get("score"),
                        "length": round(sum(b - a for a, b in m["parts"]), 1), "shots": len(m["parts"])}
                clips.append(clip)
                self._emit("scClip", **clip)
        except (editor.Cancelled, smartcut.Cancelled):
            self._emit("scDone", clips=clips, cancelled=True)
            return
        self._log("success", "smartcut", f"{name}: {len(clips)} clip(s) made", os.path.dirname(clips[0]["path"]) if clips else "")
        self._emit("scDone", clips=clips, cancelled=False, folder=os.path.dirname(clips[0]["path"]) if clips else "")
        self._notify("Smart cut finished", f"{len(clips)} clips from {name}")

    # ---------- notifications & drag-drop ----------
    def _notify(self, title, message):
        try:
            if self._notifier:
                self._notifier.notify(title, message)
        except Exception:  # noqa: BLE001 - a notification must never break a task
            pass

    def _setup_window(self):
        """Called once the page has loaded: tray notifications + file drag & drop."""
        self._notifier = Notifier(self._window)
        from webview.dom import DOMEventHandler
        doc = self._window.dom.document
        # dragover is cancelled in app.js: a pywebview handler serializes every event to Python (~0.4 s each,
        # fired every ~50 ms while dragging), which froze the window during any drag. Only drop needs the paths.
        doc.events.drop += DOMEventHandler(self._on_drop, prevent_default=True)

    def _on_drop(self, event):
        files = (event.get("dataTransfer") or {}).get("files") or []
        dropped = []
        for f in files:
            path = f.get("pywebviewFullPath")
            kind = DROP_KINDS.get(os.path.splitext(path or "")[1].lower())
            if path and kind:
                dropped.append({"path": path, "name": os.path.basename(path), "kind": kind})
        if dropped:
            self._emit("filesDropped", files=dropped)

    def _detect_encoder(self):
        try:
            label = editor.detect_encoder(self._tools()[0])[1]
        except Exception as ex:  # noqa: BLE001 - shown in the editor panel
            label = f"unavailable ({ex})"
        self._emit("editEncoder", label=label)

    def editor_export(self, path, spec, out_name):
        if not path or not os.path.isfile(path):
            return {"ok": False, "error": "Open a video first."}
        out_name = safe_name((out_name or "").strip(), fallback="edited")
        dst = editor.unique_output(os.path.dirname(path), out_name)
        if not self._run(self._export_task, path, spec, dst):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True, "dst": dst}

    def editor_export_many(self, jobs):
        """Export several edited videos one after another: [{"path", "spec", "outName"}], each with its own edits."""
        jobs = [j for j in (jobs or []) if os.path.isfile(j.get("path") or "")]
        if not jobs:
            return {"ok": False, "error": "No videos to export."}
        if not self._run(self._export_many_task, jobs):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True}

    def _export_many_task(self, jobs):
        ffmpeg, ffprobe = self._tools()
        self._edit_cancel.clear()
        done, failed = 0, []
        for idx, job in enumerate(jobs):
            if self._edit_cancel.is_set():
                self._emit("queueItem", index=idx, state="cancelled", pct=0)
                continue
            self._emit("queueItem", index=idx, state="working", pct=0)
            last = [0.0]

            def progress(frac, idx=idx):
                now = time.monotonic()
                if now - last[0] >= 0.25:
                    last[0] = now
                    self._emit("queueItem", index=idx, state="working", pct=round(frac * 100))
                    self._emit("editProgress", value=(idx + frac) / len(jobs),
                               text=f"Exporting video {idx + 1}/{len(jobs)}… {round(frac * 100)}%")

            path = job["path"]
            try:
                info = editor.probe(ffprobe, path)
                dst = editor.unique_output(os.path.dirname(path), safe_name((job.get("outName") or "").strip(), fallback="edited"))
                res = editor.export(ffmpeg, path, dst, info, job["spec"], progress, self._edit_cancel.is_set)
                done += 1
                self._emit("queueItem", index=idx, state="done", pct=100, output=res["path"], name=os.path.basename(res["path"]))
            except editor.Cancelled:
                self._emit("queueItem", index=idx, state="cancelled", pct=0)
            except Exception as ex:  # noqa: BLE001 - keep going with the other videos
                failed.append(f"{os.path.basename(path)}: {ex}")
                self._emit("queueItem", index=idx, state="failed", pct=0, error=str(ex)[:200])
        self._emit("queueDone", total=len(jobs), ok=done, failed=failed[:20], cancelled=self._edit_cancel.is_set())
        self._notify("Export finished", f"{done}/{len(jobs)} videos exported")

    def editor_cancel(self):
        self._edit_cancel.set()

    def _export_task(self, path, spec, dst):
        ffmpeg, ffprobe = self._tools()
        self._edit_cancel.clear()
        info = editor.probe(ffprobe, path)
        self._emit("editProgress", value=None, text="Preparing encoder…")
        last = [0.0]

        def progress(frac):
            now = time.monotonic()
            if now - last[0] >= 0.2:
                last[0] = now
                self._emit("editProgress", value=frac, text=f"Exporting… {round(frac * 100)}%")

        try:
            res = editor.export(ffmpeg, path, dst, info, spec, progress, self._edit_cancel.is_set)
        except editor.Cancelled:
            self._emit("editCancelled")
            return
        self._emit("editDone", path=res["path"], name=os.path.basename(res["path"]),
                   encoder=res["encoder"], width=res["width"], height=res["height"])
        self._notify("Export finished", os.path.basename(res["path"]))

    def strip_files(self, paths=None):
        """Remove metadata from files in place (no re-encode). Opens a multi-select dialog when no paths."""
        if not paths:
            picked = self._window.create_file_dialog(
                webview.FileDialog.OPEN, allow_multiple=True,
                file_types=("Video files (*.mp4;*.mov;*.mkv;*.webm;*.m4v)", "All files (*.*)"))
            if not picked:
                return {"ok": False, "cancelled": True}
            paths = list(picked)
        paths = [p for p in paths if os.path.isfile(p)]
        if not paths:
            return {"ok": False, "error": "No files to process."}
        if not self._run(self._strip_task, paths):
            return {"ok": False, "error": "Another task is running."}
        return {"ok": True, "count": len(paths)}

    def _strip_task(self, paths):
        ffmpeg = self._tools()[0]
        failed = []
        for n, p in enumerate(paths, 1):
            self._emit("editProgress", value=(n - 1) / len(paths), text=f"Removing metadata {n}/{len(paths)}…")
            try:
                editor.strip_metadata(ffmpeg, p)
            except (OSError, RuntimeError) as ex:
                failed.append(f"{os.path.basename(p)}: {ex}")
        self._emit("stripDone", total=len(paths), ok=len(paths) - len(failed), failed=failed[:20])
        self._notify("Metadata removed", f"{len(paths) - len(failed)}/{len(paths)} files")


class Notifier:
    """Tray balloon (shown by Windows 10/11 as a toast) + taskbar flash - only while the app is in the background."""

    def __init__(self, window):
        self._window = window
        self._icon = None

    def _hwnd(self):
        return int(self._window.native.Handle.ToInt64())

    def in_background(self):
        user32 = ctypes.windll.user32
        hwnd = self._hwnd()
        return bool(user32.IsIconic(hwnd)) or user32.GetForegroundWindow() != hwnd

    def notify(self, title, message):
        if not self.in_background():
            return
        self._flash()
        from System import Action
        self._window.native.Invoke(Action(lambda: self._show(title, message)))

    def _show(self, title, message):  # UI thread
        from System.Drawing import Icon
        from System.Windows.Forms import NotifyIcon, ToolTipIcon
        if self._icon is None:
            self._icon = NotifyIcon()
            self._icon.Icon = Icon(ICON_FILE)
            self._icon.Text = "Grabber"
            self._icon.BalloonTipClicked += lambda *_: self._restore()
            self._icon.Click += lambda *_: self._restore()
        self._icon.Visible = True
        self._icon.ShowBalloonTip(5000, title, message, ToolTipIcon.Info)

    def _restore(self):  # UI thread
        from System.Windows.Forms import FormWindowState
        form = self._window.native
        if form.WindowState == FormWindowState.Minimized:
            form.WindowState = FormWindowState.Normal
        form.Activate()

    def _flash(self):
        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p), ("dwFlags", ctypes.c_uint),
                        ("uCount", ctypes.c_uint), ("dwTimeout", ctypes.c_uint)]
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), self._hwnd(), 0x3 | 0xC, 0, 0)  # FLASHW_ALL | FLASHW_TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))

    def dispose(self):
        if self._icon is not None:
            from System import Action
            icon = self._icon
            self._icon = None

            def hide():
                icon.Visible = False  # otherwise a dead icon stays in the tray until hovered
                icon.Dispose()
            try:
                self._window.native.Invoke(Action(hide))
            except Exception:  # noqa: BLE001 - window already gone
                pass


def main():
    if os.name == "nt":
        # own taskbar group, so Windows shows our icon instead of python.exe's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    api = Api()
    window = webview.create_window(
        "Grabber — Shorts & Reels Downloader", os.path.join(WEB_DIR, "index.html"), js_api=api,
        width=1280, height=860, min_size=(860, 620), background_color="#07071a")
    api._window = window
    window.events.loaded += api._setup_window
    window.events.closing += lambda: api._notifier and api._notifier.dispose()
    webview.start(icon=ICON_FILE)


if __name__ == "__main__":
    main()
