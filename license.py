"""Machine-locked license keys.

The buyer sends the Machine ID shown by the app; the seller signs it with tools/keygen.py (private key kept
outside this repo) and sends back the key. A key only unlocks the machine it was made for, so it cannot be shared.

Key v1 = base32( version(1) | machine hash(8) | issued day(2) | expiry day(2, 0 = lifetime) | Ed25519 signature(64) ),
days counted from 2026-01-01.
Key v2 (short test keys) = same, but issued / expiry are UTC minutes(4 each) counted from 2026-01-01.
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
VERSION_MINUTES = 2
EPOCH = datetime.date(2026, 1, 1)
EPOCH_UTC = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
PAYLOAD_LEN = {VERSION: 13, VERSION_MINUTES: 17}


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


def _minute(t):
    return int((t - EPOCH_UTC).total_seconds() // 60)


def make_key(secret, machine, days_valid=0, issued=None, minutes_valid=0):
    """`minutes_valid` makes a v2 key that expires to the minute (for tests); otherwise a v1 key in days."""
    if minutes_valid:
        start = _minute(datetime.datetime.now(datetime.timezone.utc))
        payload = (bytes([VERSION_MINUTES]) + machine + start.to_bytes(4, "big")
                   + (start + minutes_valid).to_bytes(4, "big"))
    else:
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
    size = PAYLOAD_LEN.get(data[0]) if data else None
    if not size or len(data) != size + 64:
        return {"ok": False, "reason": "This is not a valid license key."}
    payload, signature = data[:size], data[size:]
    if not ed25519.verify(PUBLIC_KEY, payload, signature):
        return {"ok": False, "reason": "This license key is not genuine."}
    if payload[1:9] != (machine or machine_hash()):
        return {"ok": False, "reason": "This key was made for another computer. Send your Machine ID to get a key for this one."}
    if payload[0] == VERSION_MINUTES:
        issued = EPOCH_UTC + datetime.timedelta(minutes=int.from_bytes(payload[9:13], "big"))
        expires = EPOCH_UTC + datetime.timedelta(minutes=int.from_bytes(payload[13:17], "big"))
        local = expires.astimezone()
        if datetime.datetime.now(datetime.timezone.utc) >= expires:
            return {"ok": False, "reason": f"This license expired at {local:%Y-%m-%d %H:%M}."}
        return {"ok": True, "reason": "", "issued": issued.astimezone().strftime("%Y-%m-%d %H:%M"),
                "expires": local.strftime("%Y-%m-%d %H:%M")}
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
