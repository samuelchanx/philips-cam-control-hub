"""Philips/千鳥 camera — cloud + SD-card recording backup (from the RE'd API).

Usage (from src/):
    python3 cam_backup.py probe            # login + dump device list + last 3 months monthFlg (raw JSON)
    python3 cam_backup.py backup           # download all cloud clips -> backup/YYYY-MM/
    python3 cam_backup.py backup --sd      # also pull SD-card clip list (downTs for SD clips TBD from list shape)

Credentials: PHILIPS_CAM_ACCOUNT / PHILIPS_CAM_PASSWORD — set as env vars or in
the env file (CAM_ENV_FILE, default ~/.philips-cam.env).

NEVER call /delTs or cloudCard/delete.
"""
import base64
import datetime as dt
import json
import os
import sys
import time
import uuid

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ENV_PATH = os.environ.get("CAM_ENV_FILE", "~/.philips-cam.env")
OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backup")

def url_key() -> bytes:
    """Static vendor app key — identical in every app install, extracted from
    the APK. Not shipped in this repo: set PHILIPS_URL_KEY to use the cloud
    client. Encrypts login pwd + did/pwd blobs both ways."""
    key = os.environ.get("PHILIPS_URL_KEY", "").strip()
    if not key:
        raise RuntimeError("PHILIPS_URL_KEY not set — extract the static app "
                           "key from your own APK (method: docs/re-notes.md §2)")
    return key.encode()
USER = "https://user.philipsiot.com"
CLOUD = "https://cloud-storage.philipsiot.com"
CAMERA_UID = os.environ.get("CAMERA_UID", "CFMSA-000000-XXXXX")  # P2P UID form

BASE_HEADERS = {
    "platform": "ANDROID", "os": "1", "version": "1.5.0",
    "appkey": "com.tphp.philips.iot", "channel": "google", "env": "pro",
    "appId": "4", "sysver": "33", "netType": "wifi", "operator": "unknown",
    "fonType": "google_sdk_gphone64_arm64", "timeZone": "Asia/Hong_Kong",
    "lang": "en", "user-agent": "okhttp/4.9.3", "content-type": "application/json; charset=UTF-8",
}


def load_env():
    env = dict(os.environ)
    try:
        for ln in open(os.path.expanduser(ENV_PATH)):
            ln = ln.strip()
            if ln and "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                env.setdefault(k, v.strip().strip('"').strip("'"))
    except Exception:
        pass
    return env


def aes_ecb_b64(key, data):
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    raw = data.encode()
    pad = 16 - len(raw) % 16
    raw += bytes([pad]) * pad
    return base64.b64encode(enc.update(raw) + enc.finalize()).decode()


def aes_ecb_dec_b64(key, b64):
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    raw = dec.update(base64.b64decode(b64)) + dec.finalize()
    return raw[:-raw[-1]].decode(errors="replace")


def api(url, body, tk=None, timeout=30):
    h = dict(BASE_HEADERS)
    h["ts"] = str(int(time.time() * 1000))
    if tk:
        h["tk"] = tk
    r = requests.post(url, data=json.dumps(body), headers=h, timeout=timeout)
    r.raise_for_status()
    return r.json()


def login(env):
    acct = env["PHILIPS_CAM_ACCOUNT"]
    pwd = env["PHILIPS_CAM_PASSWORD"]
    phone_uuid = env.get("PHILIPS_CAM_PHONE_UUID") or uuid.uuid4().hex
    resp = api(USER + "/v3/user/loginUp",
               {"account": acct, "pwd": aes_ecb_b64(url_key(), pwd),
                "phoneUuid": phone_uuid})
    data = resp.get("data") or {}
    tk = data.get("tk")
    if not tk:
        raise SystemExit("login failed: %s" % json.dumps(resp, ensure_ascii=False)[:400])
    print("[+] logged in, uid=%s" % (data.get("uid") or data.get("id") or "?"))
    return tk


def find_camera(tk):
    resp = api(USER + "/v3/device/suit/queryDevPage",
               {"page": 1, "pageSize": 100}, tk=tk)
    entries = resp.get("data") or []
    if isinstance(entries, dict):
        entries = entries.get("list") or entries.get("data") or []
    for e in entries:
        s = json.dumps(e, ensure_ascii=False)
        if e.get("devName") == "Living Room" or "CFMSA" in s:
            print("[+] camera found: %s devType=%s did=%s" % (
                e.get("devName"), e.get("devType"), (e.get("did") or "")[:12] + "…"))
            return e
    print("[-] camera not in device list; entries seen:")
    for e in entries:
        print("   -", json.dumps(e, ensure_ascii=False)[:200])
    return None


def device_status(env=None):
    """queryDevPage -> {"sdCard": bool} for the living-room camera (quiet).

    NOTE (verified 2026-10-19, full-payload walk + dex endpoint mining): this
    cloud has NO online-now field anywhere — hub.py derives `online` from
    pipeline frame freshness instead. Never call /delTs or cloudCard/delete.
    """
    env = env or load_env()
    try:
        tk = login(env)
    except SystemExit:
        raise RuntimeError("philips cloud login failed")
    resp = api(USER + "/v3/device/suit/queryDevPage", {"page": 1, "pageSize": 100}, tk=tk)
    entries = resp.get("data") or []
    if isinstance(entries, dict):
        entries = entries.get("list") or entries.get("data") or []
    for e in entries:
        s = json.dumps(e, ensure_ascii=False)
        if e.get("devName") == "Living Room" or "CFMSA" in s:
            return {"sdCard": str(e.get("sdCard")) == "1"}
    raise RuntimeError("camera not found in device list")


def months_back(n):
    today = dt.date.today()
    out = []
    for i in range(n):
        ym = today.replace(day=1) - dt.timedelta(days=30 * i + today.day - 1)
        out.append(ym.strftime("%Y-%m"))
    return out


def probe(tk, cam):
    did = cam.get("did") or cam.get("id")
    for ym in months_back(3):
        r = api(CLOUD + "/v3/access/monthFlg", {"did": did, "ym": ym}, tk=tk)
        print(f"== monthFlg {ym}: {json.dumps(r, ensure_ascii=False)[:600]}")
    r = api(CLOUD + "/v3/cloudCard/deviceSdCard", {"did": did}, tk=tk)
    print("== SD card list:", json.dumps(r, ensure_ascii=False)[:800])


def main():
    env = load_env()
    if "PHILIPS_CAM_ACCOUNT" not in env or "PHILIPS_CAM_PASSWORD" not in env:
        raise SystemExit("set PHILIPS_CAM_ACCOUNT / PHILIPS_CAM_PASSWORD in your env file first")
    tk = login(env)
    cam = find_camera(tk)
    if not cam:
        raise SystemExit(1)
    if sys.argv[1] == "probe":
        probe(tk, cam)


if __name__ == "__main__":
    main()
