"""Combo probe: monthFlg ym/did variants + getMsgWithSign shapes."""
import json
import datetime as dt
from cam_backup import (load_env, login, find_camera, api,
                        aes_ecb_b64, url_key, CLOUD)

PUSH = "https://push.philipsiot.com"

env = load_env()
tk = login(env)
cam = find_camera(tk)
did = cam.get("did")
uid_enc = None   # optional: AES-b64 of your uid (printed at login) — not needed here
did_aes = aes_ecb_b64(url_key(), did)

ym_dash = "2026-09"
ym_compact = "202609"

print("=== access/monthFlg variants ===")
for ym in (ym_dash, ym_compact):
    for label, d in (("plain", did), ("aes", did_aes)):
        r = api(CLOUD + "/v3/access/monthFlg", {"did": d, "ym": ym}, tk=tk)
        print(f"ym={ym} did={label}: {json.dumps(r, ensure_ascii=False)[:220]}")

print("=== getMsgWithSign variants ===")
for label, d in (("plain", did), ("aes", did_aes)):
    body = {"did": d, "msgDate": ym_dash}
    r = api(PUSH + "/v3/icp/getMsgWithSign", body, tk=tk)
    print(f"did={label}: {json.dumps(r, ensure_ascii=False)[:300]}")

print("=== getMsgWithSign msgDate=202610 (this month) ===")
r = api(PUSH + "/v3/icp/getMsgWithSign", {"did": did, "msgDate": "202610"}, tk=tk)
print(json.dumps(r, ensure_ascii=False)[:300])
