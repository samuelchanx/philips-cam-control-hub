#!/usr/bin/env python3
"""renderer.py — periodically decode the newest clean frame from the raw dump."""
import os, sys, time, struct, re, subprocess

DUMP = "/tmp/philips_apk/p2p/ch1_video.bin"
DOGDIR = "/tmp/dog"
os.makedirs(DOGDIR, exist_ok=True)
last_out = None

while True:
    time.sleep(5)
    try:
        if not os.path.exists(DUMP) or os.path.getsize(DUMP) < 10000:
            continue
        sz_before = os.path.getsize(DUMP)
        # newest frame only — bound work to the dump tail (fast cycles)
        data = open(DUMP, "rb").read()[-3000000:]
        MAGIC = bytes.fromhex("88887676")
        out = bytearray(); i = 0
        while i < len(data):
            if data[i:i+4] == MAGIC and i + 24 <= len(data):
                ln, cmd = struct.unpack("<II", data[i+4:i+12])
                if cmd in (0x10, 0x02, 0x01) and i + 24 + ln <= len(data):
                    i += 24 + ln
                    continue
            out.append(data[i]); i += 1
        stream = bytes(out)
        starts = [m.start() for m in re.finditer(b"\x00\x00\x00\x01", stream)]
        if not starts: continue
        # keep valid NALs
        keep = bytearray(); kept = 0
        for j, s in enumerate(starts):
            nxt = starts[j+1] if j+1 < len(starts) else len(stream)
            if stream[s+4] & 0x1F in (1, 5, 6, 7, 8):
                keep += stream[s:nxt]; kept += 1
        tmp = f"{DOGDIR}/.render.h264"
        open(tmp, "wb").write(bytes(keep))
        # find last SPS+IDR boundary and decode frames after it
        starts2 = [m.start() for m in re.finditer(b"\x00\x00\x00\x01", bytes(keep))]
        # LAST SPS = newest GOP — the first SPS would decode the oldest frame
        sps_list = [s for s in starts2 if keep[s+4] & 0x1F == 7]
        sps = sps_list[-1] if sps_list else None
        if sps is None: continue
        idr = next((s for s in starts2 if s > sps and keep[s+4] & 0x1F == 5), None)
        start = sps
        open(tmp, "wb").write(bytes(keep[start:]))
        # decode last frame
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tmp,
                            "-vf", "scale=640:-2", f"{DOGDIR}/live.jpg"], capture_output=True)
        n = len([f for f in os.listdir(DOGDIR) if f.startswith("live_") and f.endswith(".jpg")])
        # keep numbered history for motion analysis (cap 10)
        if os.path.exists(f"{DOGDIR}/live_latest.jpg"):
            os.replace(f"{DOGDIR}/live_latest.jpg", f"{DOGDIR}/live_{n%10:04d}.jpg")
        if os.path.exists(f"{DOGDIR}/live.jpg"):
            os.replace(f"{DOGDIR}/live.jpg", f"{DOGDIR}/live_latest.jpg")
        print(f"[render] nal={kept} decoded ok, live_latest.jpg updated", flush=True)
    except Exception as e:
        print("[render] err:", e, flush=True)
