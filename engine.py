"""Keeps yt-dlp updatable without pip (also inside the packaged .exe).

Newer releases are downloaded from PyPI as a wheel, unpacked into the data dir and put first
on sys.path. `activate()` must run before the first `import yt_dlp`.
"""
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile

from facebook import DATA_DIR

ENGINE_DIR = os.path.join(DATA_DIR, "engine")
CURRENT_FILE = os.path.join(ENGINE_DIR, "current.txt")
PYPI_URL = "https://pypi.org/pypi/yt-dlp/json"


def parse_version(v):
    return tuple(int(p) for p in str(v).split(".") if p.isdigit())


def _installed_dir():
    try:
        with open(CURRENT_FILE, encoding="utf-8") as f:
            path = os.path.join(ENGINE_DIR, f.read().strip())
    except OSError:
        return None
    return path if os.path.isdir(os.path.join(path, "yt_dlp")) else None


def activate():
    """Prefer a yt-dlp unpacked by `install()` over the bundled/pip one."""
    path = _installed_dir()
    if path and path not in sys.path:
        sys.path.insert(0, path)


def latest_version():
    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": "grabber"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    return data["info"]["version"], data["urls"]


def install(version, urls):
    wheel = next((u for u in urls if u["packagetype"] == "bdist_wheel" and u["filename"].endswith("-py3-none-any.whl")), None)
    if not wheel:
        raise RuntimeError(f"No pure-Python wheel found for yt-dlp {version}")
    req = urllib.request.Request(wheel["url"], headers={"User-Agent": "grabber"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = resp.read()

    target = os.path.join(ENGINE_DIR, version)
    tmp = target + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(tmp)
    if not os.path.isdir(os.path.join(tmp, "yt_dlp")):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("Downloaded wheel does not contain yt_dlp")
    shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp, target)
    with open(CURRENT_FILE, "w", encoding="utf-8") as f:
        f.write(version)

    # drop older unpacked versions; the running one may still be locked, so ignore failures
    for name in os.listdir(ENGINE_DIR):
        path = os.path.join(ENGINE_DIR, name)
        if name != version and os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
