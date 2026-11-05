"""Owner edition: keep the app's data in a private GitHub repo so two PCs can share it.

The data files are packed into one blob, encrypted with AES-GCM using a key derived from the
user's passphrase (scrypt), and stored as a single file in the repo. GitHub only ever sees the
encrypted bytes; every upload is a commit, so older copies stay in the repo history.
"""
import base64
import datetime
import hashlib
import json
import os
import re
import socket
import urllib.error
import urllib.request
import zlib

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FILES = ["saved.json", "downloaded.json", "accounts.json", "tiktok_posts.json", "research_cache.json",
         "edit_presets.json"]
REMOTE_PATH = "grabber-sync.bin"
MAGIC = b"GRB1"
API = "https://api.github.com"


class SyncError(Exception):
    pass


def _key(passphrase, salt):
    return hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2 ** 15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)


def read_local(data_dir):
    files = {}
    for name in FILES:
        try:
            with open(os.path.join(data_dir, name), encoding="utf-8") as f:
                files[name] = f.read()
        except FileNotFoundError:
            pass
    return files


def digest(files):
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode("utf-8")).hexdigest()


def pack(files, passphrase):
    meta = {"host": socket.gethostname(), "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")}
    plain = zlib.compress(json.dumps({"meta": meta, "files": files}, ensure_ascii=False).encode("utf-8"), 9)
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_key(passphrase, salt)).encrypt(nonce, plain, MAGIC), meta


def unpack(blob, passphrase):
    if blob[:4] != MAGIC or len(blob) < 4 + 16 + 12 + 16:
        raise SyncError("The file in the repo is not Grabber sync data.")
    salt, nonce = blob[4:20], blob[20:32]
    try:
        plain = AESGCM(_key(passphrase, salt)).decrypt(nonce, blob[32:], MAGIC)
    except InvalidTag:
        raise SyncError("Wrong passphrase — use the same passphrase on both PCs.") from None
    return json.loads(zlib.decompress(plain).decode("utf-8"))


def write_local(data_dir, files):
    """Replace the local files with `files`; the previous copy of each is kept as <name>.before-sync."""
    os.makedirs(data_dir, exist_ok=True)
    for name, text in files.items():
        if name not in FILES:
            continue
        path = os.path.join(data_dir, name)
        with open(path + ".tmp", "w", encoding="utf-8", newline="") as f:
            f.write(text)
        if os.path.isfile(path):
            os.replace(path, path + ".before-sync")
        os.replace(path + ".tmp", path)


class GitHub:
    def __init__(self, repo, token):
        repo = (repo or "").strip().removeprefix("https://github.com/").strip("/").removesuffix(".git")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise SyncError("Repo must look like owner/name, e.g. myname/grabber-data.")
        if not (token or "").strip():
            raise SyncError("Paste a GitHub token.")
        self.repo, self.token = repo, token.strip()

    def _call(self, method, path, body=None, accept="application/vnd.github+json"):
        req = urllib.request.Request(f"{API}/repos/{self.repo}{path}", method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": accept,
                                              "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Grabber"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as ex:
            if ex.code == 401:
                raise SyncError("GitHub rejected the token (expired or wrong).") from None
            if ex.code in (404, 409, 422):
                return ex.code, ex.read()
            if ex.code == 403:
                raise SyncError("The token has no access to this repo — give it Contents: Read and write.") from None
            raise SyncError(f"GitHub error {ex.code}.") from None
        except OSError as ex:
            raise SyncError(f"Cannot reach GitHub: {ex}") from None

    def check(self):
        code, raw = self._call("GET", "")
        if code == 404:
            raise SyncError(f"Repo {self.repo} not found, or the token cannot see it.")
        if not json.loads(raw).get("private"):
            raise SyncError(f"{self.repo} is public. Use a private repo — the data must not be public.")

    def download(self):
        """(sha, blob) of the sync file, or (None, None) when nothing was uploaded yet."""
        code, raw = self._call("GET", f"/contents/{REMOTE_PATH}")
        if code == 404:
            self.check()
            return None, None
        sha = json.loads(raw)["sha"]
        _code, blob = self._call("GET", f"/contents/{REMOTE_PATH}", accept="application/vnd.github.raw")
        return sha, blob

    def upload(self, blob, sha, message):
        body = {"message": message, "content": base64.b64encode(blob).decode("ascii")}
        if sha:
            body["sha"] = sha
        code, raw = self._call("PUT", f"/contents/{REMOTE_PATH}", body)
        if code in (409, 422):
            raise SyncError("The cloud copy changed while uploading. Try again.")
        if code == 404:
            self.check()
            raise SyncError("The token cannot write to this repo — give it Contents: Read and write.")
        return json.loads(raw)["content"]["sha"]
