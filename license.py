"""Machine-locked license keys.

The buyer sends the Machine ID shown by the app; the seller signs it with tools/keygen.py (private key kept
outside this repo) and sends back the key. A key only unlocks the machine it was made for, so it cannot be shared.

Key = base32( version(1) | machine hash(8) | issued day(2) | expiry day(2, 0 = lifetime) | Ed25519 signature(64) ),
days counted from 2026-01-01.
"""
import base64
import ctypes
import datetime
import hashlib
import os
import re

import ed25519

PUBLIC_KEY = bytes.fromhex("ea31c3f423acf9ec94941ffc568585a320ef9634346fc8165b2456f6e68f9aeb")
VERSION = 1
EPOCH = datetime.date(2026, 1, 1)
PAYLOAD_LEN = 13


def _machine_guid():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, "MachineGuid")[0]
    except OSError:
        return ""


def _volume_serial():
    serial = ctypes.c_uint32()
    drive = os.environ.get("SystemDrive", "C:") + "\\"
    ok = ctypes.windll.kernel32.GetVolumeInformationW(ctypes.c_wchar_p(drive), None, 0, ctypes.byref(serial),
                                                      None, None, None, 0)
    return f"{serial.value:08X}" if ok else ""


def machine_hash():
    """8 stable bytes for this Windows install (MachineGuid + system drive serial)."""
    return hashlib.sha256(f"grabber|{_machine_guid()}|{_volume_serial()}".encode()).digest()[:8]


def machine_id(raw=None):
    h = (raw or machine_hash()).hex().upper()
    return "-".join(h[i:i + 4] for i in range(0, 16, 4))


def parse_machine_id(text):
    h = re.sub(r"[^0-9A-Fa-f]", "", text or "")
    if len(h) != 16:
        raise ValueError("A Machine ID has 16 hex characters, e.g. 8F3A-21C9-77DE-0B41")
    return bytes.fromhex(h)


def _day(d):
    return (d - EPOCH).days


def make_key(secret, machine, days_valid=0, issued=None):
    issued = issued or datetime.date.today()
    expiry = _day(issued) + days_valid if days_valid else 0
    payload = bytes([VERSION]) + machine + _day(issued).to_bytes(2, "big") + expiry.to_bytes(2, "big")
    raw = base64.b32encode(payload + ed25519.sign(secret, payload)).decode().rstrip("=")
    return "-".join(raw[i:i + 6] for i in range(0, len(raw), 6))


def check(key, machine=None):
    """{"ok", "reason", "issued", "expires"} for `key` on this machine (or `machine`)."""
    raw = re.sub(r"[^A-Za-z2-7]", "", key or "").upper()
    try:
        data = base64.b32decode(raw + "=" * (-len(raw) % 8))
    except ValueError:
        return {"ok": False, "reason": "This is not a valid license key."}
    if len(data) != PAYLOAD_LEN + 64 or data[0] != VERSION:
        return {"ok": False, "reason": "This is not a valid license key."}
    payload, signature = data[:PAYLOAD_LEN], data[PAYLOAD_LEN:]
    if not ed25519.verify(PUBLIC_KEY, payload, signature):
        return {"ok": False, "reason": "This license key is not genuine."}
    if payload[1:9] != (machine or machine_hash()):
        return {"ok": False, "reason": "This key was made for another computer. Send your Machine ID to get a key for this one."}
    issued = EPOCH + datetime.timedelta(days=int.from_bytes(payload[9:11], "big"))
    exp_day = int.from_bytes(payload[11:13], "big")
    expires = EPOCH + datetime.timedelta(days=exp_day) if exp_day else None
    if expires and datetime.date.today() > expires:
        return {"ok": False, "reason": f"This license expired on {expires:%Y-%m-%d}."}
    return {"ok": True, "reason": "", "issued": issued.isoformat(), "expires": expires.isoformat() if expires else ""}


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        return {"ok": False, "reason": "", "key": ""}
    return {**check(key), "key": key}


def save(path, key):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key.strip() + "\n")
