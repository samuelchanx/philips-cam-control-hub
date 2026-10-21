#!/usr/bin/env python3
"""demo_server.py — the full dashboard with zero hardware.

Everything is faked at the create_app() seam (the same seam the tests use):
the feeder answers instantly, the camera "streams" a synthetic test frame,
and the replay browser shows generated clips. No hub.py changes, no cloud,
no devices — and the demo's relay files point at a temp dir, so it can never
move a real camera or talk to a real speaker by accident.

    python3 demo/demo_server.py        # → http://127.0.0.1:8799
"""
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
PORT = 8799
TMP = tempfile.mkdtemp(prefix="control-hub-demo-")


def _ffmpeg():
    for p in ("ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if shutil.which(p):
            return p
    raise SystemExit("ffmpeg not found — brew install ffmpeg (or apt)")


def run(*args):
    subprocess.run(args, check=True, capture_output=True)


def gen_frame(path, size="640x726"):
    run(_ffmpeg(), "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}",
        "-frames:v", "1", path)


def gen_clip(day_dir, stem, size="320x363"):
    """A 4s synthetic clip + matching thumbnail, motion_recorder file layout."""
    mp4 = os.path.join(day_dir, stem + ".mp4")
    run(_ffmpeg(), "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=8",
        "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p", mp4)
    run(_ffmpeg(), "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}",
        "-frames:v", "1", os.path.join(day_dir, stem + ".jpg"))
    return mp4


def seed_recordings(rec_dir):
    """Four events today: three dog-confirmed, one raw motion (no badge)."""
    day_dir = os.path.join(rec_dir, dt.date.today().isoformat())
    os.makedirs(day_dir)
    events = [("081200", 6.2, 0.74), ("124100", 11.4, 0.81),
              ("170500", 4.1, 0.66), ("224800", 2.6, None)]
    for stem, pct, conf in events:
        mp4 = gen_clip(day_dir, stem)
        event = {"time": "%s:%s:00" % (stem[:2], stem[2:4]),
                 "ts": time.time() - 3600,
                 "motion_pct": pct, "clip": stem + ".mp4",
                 "thumb": stem + ".jpg", "duration_s": 4.0,
                 "size_kb": os.path.getsize(mp4) // 1024}
        if conf:
            event.update(dog="Dog", dog_conf=conf)
        json.dump(event, open(os.path.join(day_dir, stem + ".json"), "w"))


def seed_feedlog(path):
    rows = [("feed", 2, "ok", "cloud", 3700),
            ("quick_feed", None, "ok", "cloud", 2100),
            ("feed", 1, "ok", "cloud", 3400)]
    now = time.time()
    with open(path, "w") as f:
        for i, (cmd, portions, result, source, ms) in enumerate(rows):
            ts = time.strftime("%Y-%m-%d %H:%M:%S",
                               time.localtime(now - 3600 * (len(rows) - i)))
            f.write(json.dumps({"ts": ts, "command": cmd, "portions": portions,
                                "result": result, "source": source,
                                "latency_ms": ms}) + "\n")


def main():
    # Relay files → tmp BEFORE importing hub (hub binds them at import time).
    import pyp2p_cam
    pyp2p_cam.PTZ_CMD_FILE = os.path.join(TMP, "ptz_cmd.json")
    pyp2p_cam.TALK_CMD_FILE = os.path.join(TMP, "talk_cmd.json")

    import dispatcher as dispatcher_mod
    import motion_recorder
    import uvicorn
    import hub

    frame = os.path.join(TMP, "frame.jpg")
    gen_frame(frame)
    rec_dir = os.path.join(TMP, "recordings")
    seed_recordings(rec_dir)
    feed_log = os.path.join(TMP, "feed.jsonl")
    seed_feedlog(feed_log)

    # a small synthetic audio file so the talk-back picker isn't empty
    # (audio/ is gitignored — this never lands in the repo)
    audio_dir = os.path.join(ROOT, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    demo_tone = os.path.join(audio_dir, "demo-tone.mp3")
    if not os.path.exists(demo_tone):
        run(_ffmpeg(), "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-q:a", "9", demo_tone)

    class DemoFeeder:
        def status(self):
            return {"online": True, "battery_percentage": 88,
                    "feed_state": "standby", "light": True, "slow_feed": False}

        def feed(self, portions):
            time.sleep(1.2)   # keep the real feel: the device takes seconds
            return "done"

        def quick_feed(self):
            time.sleep(0.8)
            return True

        def toggle(self, code, on):
            time.sleep(0.3)
            return True

    class DemoPipeline:
        state = "live"
        _error = None
        recording = True

        def __init__(self, frame_path):
            self.frame_path = frame_path

        def start(self): return self.state
        def stop(self): return "stopped"
        def touch(self): pass
        def set_recording(self, on): pass

        def status(self):
            return {"state": self.state, "error": None, "frame_age_s": 4.0,
                    "idle_timeout_s": 300, "no_frame_retries": 0}

    def seed_cloud_history():
        """Raw Tuya device-log rows — CachedFeedHistory._filter does the shaping."""
        now = dt.datetime.now()
        rows = []
        for days_ago, hm, portions in [(0, "08:51", 1), (0, "07:51", 1),
                                       (1, "21:30", 2), (1, "08:02", 1),
                                       (2, "20:45", 3), (3, "08:00", 1),
                                       (5, "19:12", 2), (8, "08:00", 1)]:
            t = (now - dt.timedelta(days=days_ago)).replace(
                hour=int(hm[:2]), minute=int(hm[3:]))
            rows.append({"code": "feed_report", "value": str(portions),
                         "event_time": int(t.timestamp() * 1000)})
        return rows

    backend = DemoFeeder()
    disp = dispatcher_mod.Dispatcher(backend, log_path=feed_log,
                                     notify=lambda msg: None)
    rec = motion_recorder.MotionRecorder(
        out_dir=rec_dir,
        dump_path=os.path.join(TMP, "none.h264"),
        motion_path=os.path.join(TMP, "none.json"),
        is_live=lambda: True)

    app = hub.create_app(feeder_backend=backend, dispatcher=disp,
                         pipeline=DemoPipeline(frame),
                         camera_status=lambda: {"sdCard": True},
                         cloud_history=hub.CachedFeedHistory(
                             ttl=300, fetch=seed_cloud_history),
                         recorder=rec)
    print("demo dashboard → http://127.0.0.1:%d   (assets in %s)" % (PORT, TMP))
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
