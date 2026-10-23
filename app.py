"""Shorts / Reels downloader - pywebview desktop app (HTML/CSS UI) on top of yt-dlp + Playwright."""
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor

import webview

import douyin
import editor
import engine
import facebook

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
    return None


def normalize_url(kind, url):
    if kind == "youtube":
        return normalize_shorts_url(url)
    if kind == "douyin":
        return douyin.normalize_user_url(url)
    return facebook.normalize_reels_url(url)


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
    return f"https://www.facebook.com/reel/{video_id}"


def downloaded_file(info):
    downloads = (info or {}).get("requested_downloads") or [{}]
    return downloads[0].get("filepath") or ""


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


PLATFORM_NAMES = {"youtube": "YouTube", "facebook": "Facebook", "douyin": "Douyin"}


class Api:
    """Methods exposed to the web UI. Long tasks run in threads and report back through events."""

    def __init__(self):
        self._window = None
        self._busy = False
        self._lock = threading.Lock()
        self._lists = {"youtube": [], "facebook": [], "douyin": []}
        self._sources = {"youtube": "", "facebook": "", "douyin": ""}
        self._gen = {"youtube": 0, "facebook": 0, "douyin": 0}
        self._caption_requested = set()
        self._meta_pool = ThreadPoolExecutor(max_workers=3)
        self._cancel = threading.Event()
        self._names_lock = threading.Lock()
        self._reserved_names = set()
        self._latest_engine = None
        self._media = editor.MediaServer()
        self._edit_cancel = threading.Event()
        self._notifier = None
        self._fonts = None

    # ---------- plumbing ----------
    def _emit(self, type_, **data):
        payload = json.dumps({"type": type_, **data}, ensure_ascii=False)
        try:
            self._window.evaluate_js(f"window.onPy({payload})")
        except Exception:  # noqa: BLE001 - window may be closing
            pass

    def _run(self, target, *args):
        """Start a background task unless another one is running."""
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        self._emit("busy", busy=True)

        def wrapper():
            try:
                target(*args)
            except Exception as ex:  # noqa: BLE001 - surface any error to the UI
                self._emit("error", message=str(ex))
            finally:
                with self._lock:
                    self._busy = False
                self._emit("busy", busy=False)

        threading.Thread(target=wrapper, daemon=True).start()
        return True

    # ---------- state ----------
    def init(self):
        threading.Thread(target=self._check_engine, daemon=True).start()
        return {
            "saved": load_saved(),
            "fbLoggedIn": facebook.is_logged_in(),
            "dyLoggedIn": douyin.is_logged_in(),
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
        worker = {"youtube": fetch_youtube, "facebook": fetch_facebook, "douyin": fetch_douyin}[kind]
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
        done_ids = load_downloaded().get(kind, {})
        videos = [v for v in fetched if v["id"] not in done_ids]
        hidden = len(fetched) - len(videos)
        self._gen[kind] += 1
        self._lists[kind] = videos
        self._sources[kind] = source
        limited = bool(extra and extra[0])
        if kind == "douyin":
            self._emit("dyLogin", loggedIn=douyin.is_logged_in())
        self._emit("list", kind=kind, gen=self._gen[kind], url=url, videos=videos, hidden=hidden,
                   source=source, folder=safe_name(source, fallback=kind), limited=limited)
        note = f" ({hidden} already downloaded, hidden)" if hidden else ""
        self._emit("status", text=f"Found {len(videos)} new videos{note}", progress=0)
        self._notify("Fetch finished", f"{len(videos)} new videos from {source or url}")

    def load_captions(self, kind, indices):
        """Facebook grids show no captions, so they are resolved lazily for visible cards."""
        if kind != "facebook":
            return
        gen = self._gen[kind]
        videos = self._lists[kind]
        for idx in indices:
            key = (gen, idx)
            if key in self._caption_requested or idx >= len(videos) or videos[idx]["title"]:
                continue
            self._caption_requested.add(key)
            self._meta_pool.submit(self._caption_job, gen, idx, videos[idx])

    def _caption_job(self, gen, idx, video):
        try:
            with facebook.cookie_copy() as cookies, yt_dlp.YoutubeDL(
                    {"cookiefile": cookies, "quiet": True, "no_warnings": True}) as ydl:
                info = ydl.extract_info(video["url"], download=False)
        except Exception:  # noqa: BLE001 - keep the placeholder title
            return
        if gen != self._gen["facebook"]:
            return
        video["title"] = reel_caption(info, video["id"])
        self._emit("caption", kind="facebook", gen=gen, index=idx, title=video["title"])

    # ---------- facebook login ----------
    def fb_login(self):
        return self._run(self._login_task)

    def _login_task(self):
        self._emit("status", text="Log in to Facebook in the opened browser window…", progress=None)
        ok = facebook.login()
        self._emit("fbLogin", loggedIn=facebook.is_logged_in())
        self._emit("status", text="Facebook connected" if ok else "Facebook login cancelled", progress=0)

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
            return {"ok": False, "error": "Only YouTube, Facebook and Douyin links are supported."}
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
        if path and os.path.isfile(path):
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])

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
        doc.events.dragover += DOMEventHandler(lambda e: None, prevent_default=True)
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
