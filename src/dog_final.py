#!/usr/bin/env python3
"""
dog_final.py — pure-Python CCTV -> dog behaviour pipeline.

P2P (pyp2p_cam.P2PCam) -> H.264 -> ffmpeg -> rolling segments + periodic JPEGs
-> motion analysis (frame diff) + audio analysis (astats RMS)
-> Telegram events (movement / barking / session status).
"""
import os, sys, time, threading, subprocess, struct, re, json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pyp2p_cam import (P2PCam, VENDOR_MAGIC, PTZ_CMD_FILE, TALK_CMD_FILE,
                       TALK_FRAME_BYTES, telegram)

try:
    import dog_gate              # same dir; Vision via pyobjc (lazy import)
except ImportError as _e:        # e.g. stripped env — telegram stays ungated
    print(f"[an] dog_gate unavailable ({_e}) — telegram unmuted", flush=True)
    dog_gate = None

DOGDIR = "/tmp/dog"
os.makedirs(DOGDIR, exist_ok=True)
DUR = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 3600 * 6

state = {"video_bytes": 0, "frames": 0, "motion_events": 0, "bark_events": 0}
cam = None          # the live P2PCam — ptz_loop sends through this session
lock = threading.Lock()

def strip_control(payload):
    """Remove 24B vendor control frames (login/live responses) from a DRW payload;
    keep raw NAL data."""
    out = bytearray()
    i = 0
    while i < len(payload):
        if payload[i:i+4] == bytes.fromhex("88887676") and i + 24 <= len(payload):
            ln, cmd = struct.unpack("<II", payload[i+4:i+12])
            if cmd in (0x10, 0x02, 0x01) and i + 24 + ln <= len(payload):
                i += 24 + ln
                continue
        out.append(payload[i])
        i += 1
    return bytes(out)

nal_state = {"buf": b"", "starts": [], "pos": 0}

def nal_filter(chunk):
    """Concatenate, then emit only valid NAL units (types 1/5/6/7/8) as an
    Annex-B stream. Removes vendor media headers that corrupt decoding."""
    st = nal_state
    st["buf"] += chunk
    stream = st["buf"]
    starts = [m.start() for m in re.finditer(b"\x00\x00\x00\x01", stream)]
    if not starts:
        # keep tail for boundary-spanning NALs
        st["buf"] = stream[-3:]
        return b""
    out = bytearray()
    for j, s in enumerate(starts):
        nxt = starts[j+1] if j+1 < len(starts) else len(stream)
        if s + 4 > len(stream): break
        if stream[s+4] & 0x1F in (1, 5, 6, 7, 8):
            # keep only complete NALs (not the last one — may be incomplete)
            if j+1 < len(starts):
                out += stream[s:nxt]
            else:
                out += stream[s:nxt] if nxt - s > 4 else b""
    st["buf"] = b""
    return bytes(out)

def cam_thread(ffmpeg_proc, fflock):
    global cam
    c = P2PCam()
    cam = c
    dump = open("/tmp/philips_apk/p2p/ch1_video.bin", "ab")
    def on_payload(payload):
        dump.write(payload); dump.flush()
        state["video_bytes"] += len(payload)
    class W:
        def write(self, b):
            on_payload(b)
        def flush(self): pass
        def close(self): pass
    c.run(dur=DUR, out=W())

def start_ffmpeg():
    class _Null:
        def write(self, b): pass
        def flush(self): pass
        def poll(self): return 0
        def close(self): pass
    return _Null()

def analysis_loop():
    last_tg_motion = 0.0
    last_tg_bark = 0.0
    while True:
        time.sleep(20)
        try:
            frames = sorted(f for f in os.listdir(DOGDIR) if f.startswith("live_") and f.endswith(".jpg"))
            segs = []
            with lock:
                log = f"[an] video={state['video_bytes']//1024}KB liveframes={len(frames)}"
            print(log, flush=True)
            # motion = the renderer's newest frame vs OUR OWN previous
            # snapshot. Comparing directory files instead meant an hours-old
            # live_0009.jpg sat next to live_latest.jpg forever — when the
            # stream froze on 2026-10-20 that pair reported a constant fake
            # 87.9% (22 motion telegrams). A frozen renderer rewrites the
            # SAME bytes, so identical snapshots simply diff to 0.
            cur_p, prev_p = f"{DOGDIR}/live_latest.jpg", f"{DOGDIR}/.motion_prev.jpg"
            try:
                cur = open(cur_p, "rb").read()
            except OSError:
                cur = None
            try:
                prev = open(prev_p, "rb").read()
            except OSError:
                prev = None
            if cur and prev and cur != prev:
                a = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", prev_p,
                                    "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
                b = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", cur_p,
                                    "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True).stdout
                if len(a) == len(b) and len(a) > 0:
                    n = len(a); step = max(1, n // 40000)
                    diff = sum(1 for i in range(0, n, step) if abs(a[i]-b[i]) > 25) * step / n
                    print(f"[an] motion {diff*100:.1f}%", flush=True)
                    # ticket 07: the recorder watches this file, not stdout
                    try:
                        json.dump({"level": diff*100, "ts": time.time()},
                                  open("/tmp/philips_apk/p2p/motion_state.json", "w"))
                    except OSError:
                        pass
                    # honest snapshot-diff scale: idle IR noise ≈0.1-1.5%,
                    # real movement ≫5% — 0.05 was the old broken-pair scale
                    if diff > 5 and time.time() - last_tg_motion > 300:
                        state["motion_events"] += 1
                        # 2026-10-21: telegram is dog-gated like the recorder
                        # (the 93%-no-dog audit) — raw motion_events keeps
                        # counting regardless. Gate error → send as before.
                        send, note = True, ""
                        if dog_gate is not None and dog_gate.gate_enabled():
                            try:
                                conf, species = dog_gate.check_frame()
                                if conf >= dog_gate.DOG_CONF_MIN:
                                    note = f"，狗仔確認 {species.lower()} {conf:.0f}%"
                                else:
                                    send = False
                                    print(f"[an] motion {diff*100:.0f}% — "
                                          f"冇狗 (conf {conf:.2f})，唔發 telegram",
                                          flush=True)
                            except Exception as e:
                                print(f"[an] dog gate error ({e}) — fail open",
                                      flush=True)
                        if send:
                            telegram(f"🐕 狗仔郁緊（畫面變化 {diff*100:.0f}%{note}）")
                            last_tg_motion = time.time()
            if cur:
                try:
                    open(prev_p, "wb").write(cur)
                except OSError:
                    pass
            if segs:
                r = subprocess.run(["ffmpeg", "-loglevel", "info", "-i", f"{DOGDIR}/{segs[-1]}",
                                    "-map", "0:a?", "-af", "astats=metadata=1:reset=1",
                                    "-f", "null", "-"], capture_output=True, text=True, timeout=30)
                vals = [float(x.split("RMS level dB:")[1].strip())
                        for x in r.stderr.splitlines() if "RMS level dB:" in x
                        and _isnum(x.split("RMS level dB:")[1].strip())]
                if vals:
                    peak = max(vals)
                    print(f"[an] audio RMS peak {peak:.0f} dB", flush=True)
                    if peak > -32 and time.time() - last_tg_bark > 300:
                        state["bark_events"] += 1
                        telegram(f"🔊 大聲（RMS {peak:.0f} dB）— 可能吠緊")
                        last_tg_bark = time.time()
        except Exception as e:
            print("[an] err:", e, flush=True)

def _isnum(x):
    try: float(x); return True
    except Exception: return False

def ptz_loop():
    """hub writes ptz_cmd.json {dir_byte, dur, ts}; we pulse it on the live
    session (cmd 4097, repeated ~4x/s then STOP) — ticket: PTZ control."""
    last_ts = 0.0
    while True:
        time.sleep(0.15)
        try:
            st = json.load(open(PTZ_CMD_FILE))
        except Exception:
            continue
        ts = st.get("ts", 0)
        if ts <= last_ts or cam is None or not cam.session_up.is_set():
            continue
        last_ts = ts
        try:
            direction, dur = int(st["dir_byte"]), min(float(st.get("dur", 1.0)), 5.0)
        except (KeyError, TypeError, ValueError):
            continue
        print(f"[ptz] dir={direction} dur={dur}s", flush=True)
        end = time.time() + dur
        while time.time() < end:
            cam.ptz(direction)
            time.sleep(0.25)
        cam.ptz(0)

_stop_play = threading.Event()

def _play_file(path):
    """Stream a raw G.711A file (8kHz, 320B frames) to the speaker in
    real time — hub pre-converts with ffmpeg."""
    if cam is None or not cam.session_up.is_set():
        print("[talk] no session", flush=True)
        return
    _stop_play.clear()
    try:
        data = open(path, "rb").read()
    except OSError as e:
        print("[talk]", e, flush=True)
        return
    print(f"[talk] playing {os.path.basename(path)} ({len(data)//1024}KB)", flush=True)
    try:
        cam.speaker_start()
        time.sleep(0.1)
        next_t = time.time()
        for off in range(0, len(data), TALK_FRAME_BYTES):
            if _stop_play.is_set() or not cam.session_up.is_set():
                break
            cam.send_talk(data[off:off + TALK_FRAME_BYTES])
            next_t += 0.04                       # 320B = 40ms @ 8kHz
            delay = next_t - time.time()
            if delay > 0:
                time.sleep(delay)
    except Exception as e:
        print("[talk] err:", e, flush=True)
    finally:
        try:
            cam.speaker_stop()
        except Exception:
            pass
        print("[talk] done", flush=True)

def talk_loop():
    """hub writes talk_cmd.json {action: play|stop, file, ts} — same relay
    pattern as ptz_cmd.json."""
    last_ts = 0.0
    while True:
        time.sleep(0.1)
        try:
            st = json.load(open(TALK_CMD_FILE))
        except Exception:
            continue
        ts = st.get("ts", 0)
        if ts <= last_ts:
            continue
        last_ts = ts
        if st.get("action") == "play":
            f = st.get("file")
            if f and os.path.isfile(f):
                _stop_play.set()                 # cancel whatever is playing
                time.sleep(0.15)
                threading.Thread(target=_play_file, args=(f,), daemon=True).start()
        elif st.get("action") == "stop":
            _stop_play.set()

def status_loop():
    t0 = time.time()
    while True:
        time.sleep(300)
        print(f"[st] up {int(time.time()-t0)}s video={state['video_bytes']//1024}KB "
              f"motion_events={state['motion_events']} bark_events={state['bark_events']}", flush=True)

if __name__ == "__main__":
    telegram("🐾 dog_final pipeline start（純 Python）")
    ff = start_ffmpeg()
    fflock = threading.Lock()
    threading.Thread(target=analysis_loop, daemon=True).start()
    threading.Thread(target=ptz_loop, daemon=True).start()
    threading.Thread(target=talk_loop, daemon=True).start()
    threading.Thread(target=status_loop, daemon=True).start()
    cam_thread(ff, fflock)
