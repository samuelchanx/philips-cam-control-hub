"""Dog feeder control — Oneisall PFD-001 Pro (IPF-W100) via Tuya cloud.

Usage (from src/):
    python3 feeder.py            # 出 1 份糧
    python3 feeder.py 2          # 出 2 份
    python3 feeder.py status     # 只睇狀態,唔出糧

Credentials: TUYA_* keys in the env file (CAM_ENV_FILE, default
~/.philips-cam.env). Sends the same Telegram notice as the camera events.
"""
import json
import os
import sys
import time

import tinytuya

from pyp2p_cam import telegram

ENV_PATH = os.environ.get("CAM_ENV_FILE", "~/.philips-cam.env")
MAX_PORTIONS = 36            # Tuya function values: min 1 max 36 step 1
NEVER_SEND = ("factory_reset",)   # hard guard — this brick/erase the device


def _env():
    env = dict(os.environ)
    try:
        for ln in open(os.path.expanduser(ENV_PATH)):
            ln = ln.strip()
            if ln and "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                env.setdefault(k, v)
    except Exception:
        pass
    return env


ENV = _env()


def cloud():
    return tinytuya.Cloud(
        apiKey=ENV["TUYA_API_KEY"],
        apiSecret=ENV["TUYA_API_SECRET"],
        apiRegion=ENV.get("TUYA_API_REGION", "us"),
    )


def get_status(c):
    st = c._tuyaplatform("devices/%s/status" % ENV["TUYA_DEVICE_ID"])
    if not st.get("success"):
        raise RuntimeError("status failed: %s" % st)
    return {s["code"]: s["value"] for s in st["result"]}


def feed(portions=1, wait_done=True):
    portions = max(1, min(MAX_PORTIONS, int(portions)))
    c = cloud()
    for code in NEVER_SEND:
        assert code not in json.dumps({"code": "manual_feed"})  # paranoia guard
    r = c.sendcommand(ENV["TUYA_DEVICE_ID"],
                      {"commands": [{"code": "manual_feed", "value": portions}]})
    if not r.get("success"):
        raise RuntimeError("feed command rejected: %s" % r)
    state = "?"
    if wait_done:
        for _ in range(8):
            time.sleep(2)
            state = get_status(c).get("feed_state", "?")
            if state in ("done", "standby"):
                break
    return state


def send_dp(code, value):
    """Send one guarded DP command (light / quick_feed / slow_feed…)."""
    for c in NEVER_SEND:
        assert c != code  # paranoia guard
    c = cloud()
    r = c.sendcommand(ENV["TUYA_DEVICE_ID"],
                      {"commands": [{"code": code, "value": value}]})
    if not r.get("success"):
        raise RuntimeError("command %s rejected: %s" % (code, r))
    return True


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "1"
    if arg == "status":
        c = cloud()
        dev = c._tuyaplatform("devices/%s" % ENV["TUYA_DEVICE_ID"])["result"]
        st = get_status(c)
        print("online=%s battery=%s%% feed_state=%s" % (
            dev.get("online"), st.get("battery_percentage"), st.get("feed_state")))
        return
    portions = int(arg)
    print("出緊 %d 份糧…" % portions)
    state = feed(portions)
    msg = "🍖 出咗 %d 份糧（feed_state=%s）" % (portions, state)
    print(msg)
    telegram(msg)


if __name__ == "__main__":
    main()
