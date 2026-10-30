"""Seller-side license key generation (owner edition and tools/keygen.py). Never part of the commercial build.

The private signing key lives in %USERPROFILE%\\.grabber-keygen\\private.key - back it up: without it no new keys
can be made for apps already sold, and anyone holding it can make keys.
"""
import csv
import datetime
import os
import secrets

import ed25519
import license

HOME = os.path.join(os.path.expanduser("~"), ".grabber-keygen")
PRIVATE = os.path.join(HOME, "private.key")
ISSUED = os.path.join(HOME, "issued.csv")


class KeygenError(Exception):
    pass


def load_secret():
    try:
        with open(PRIVATE, encoding="utf-8") as f:
            return bytes.fromhex(f.read().strip())
    except (OSError, ValueError):
        raise KeygenError(f"No signing key at {PRIVATE}.") from None


def status():
    """{"hasKey", "matches", "path", "publicKey"} - matches = keys made now are accepted by this app's license.py."""
    try:
        pub = ed25519.public_key(load_secret())
    except KeygenError:
        return {"hasKey": False, "matches": False, "path": PRIVATE, "publicKey": ""}
    return {"hasKey": True, "matches": pub == license.PUBLIC_KEY, "path": PRIVATE, "publicKey": pub.hex()}


def init():
    """Create the signing key once; returns its public key (to embed in license.PUBLIC_KEY)."""
    if not os.path.exists(PRIVATE):
        os.makedirs(HOME, exist_ok=True)
        with open(PRIVATE, "w", encoding="utf-8") as f:
            f.write(secrets.token_bytes(32).hex() + "\n")
    return ed25519.public_key(load_secret()).hex()


def issue(machine_text, days=0, note=""):
    secret = load_secret()
    if ed25519.public_key(secret) != license.PUBLIC_KEY:
        raise KeygenError("This signing key does not match PUBLIC_KEY in license.py — the app would reject its keys.")
    try:
        machine = license.parse_machine_id(machine_text)
    except ValueError as ex:
        raise KeygenError(str(ex)) from None
    key = license.make_key(secret, machine, int(days or 0))
    if not license.check(key, machine)["ok"]:
        raise KeygenError("The new key did not verify.")
    new = not os.path.exists(ISSUED)
    with open(ISSUED, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "machine_id", "days", "note", "key"])
        w.writerow([datetime.date.today().isoformat(), license.machine_id(machine), days or "lifetime", note, key])
    return key


def issued():
    """Every key made so far, newest first."""
    try:
        with open(ISSUED, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))[::-1]
    except OSError:
        return []
