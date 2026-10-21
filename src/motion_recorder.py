"""Motion-clip recorder — ticket 07 (self-hosted「Historical video playback」).

dog_final.py writes the analysed motion level to motion_state.json; this
watcher polls it, and when the level crosses the threshold it cuts a clip out
of the raw P2P dump (pre-roll + motion + post-roll), strips the vendor control
frames the same way renderer.py does, snaps the cut to the first SPS in the
window, and encodes MP4 + thumbnail with ffmpeg into recordings/YYYY-MM-DD/.

Pure file watching: no P2P, no cloud. Clips are only cut while the camera
pipeline is live (is_live callable) — a stale dump must never look like
footage. Recording mode itself (keep-alive, UI) lives in pipeline.PipelineManager
and hub.py; this class is the capture + listing + retention mechanics.

Env knobs (read by hub.py): RECORDINGS_DIR, RETENTION_DAYS (default 14).
Dog gate (2026-10-21): a clip window only opens when dog_gate.check_frame
sees the dog in dog_final's live frame — two consecutive above-threshold
polls first (single-sample noise filter), then one Vision classification.
DOG_GATE=0 disables the gate; any classifier error fails OPEN so a broken
gate never loses real dog footage. Decisions land in dog_gate_log.jsonl at
the root of the recordings dir (retention only sweeps date dirs).
"""
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time
from collections import deque
from datetime import datetime

import dog_gate

UNSET = object()         # __init__ sentinel: "not passed" (None = gate off)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DUMP = "/tmp/philips_apk/p2p/ch1_video.bin"
DEFAULT_MOTION = "/tmp/philips_apk/p2p/motion_state.json"
DEFAULT_OUT = os.path.join(REPO_ROOT, "recordings")
DEFAULT_FRAME = dog_gate.DOG_FRAME
GATE_LOG_NAME = "dog_gate_log.jsonl"
GATE_RECHECK_S = 10      # min gap between classifications within one episode
MAGIC = bytes.fromhex("88887676")
CONTROL_CMDS = (0x10, 0x02, 0x01)
NAL_TYPES = (1, 5, 6, 7, 8)
MOTION_STALE_S = 60      # older level readings count as "no motion"
MIN_H264_BYTES = 1000    # below this the cut is garbage — skip encoding


def resolve_ffmpeg():
    """Absolute ffmpeg path — under launchd the hub's PATH lacks Homebrew
    (same gotcha as pipeline._real_popen, but this thread runs in-hub)."""
    p = shutil.which("ffmpeg")
    if p:
        return p
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.exists(cand):
            return cand
    return "ffmpeg"


def strip_control(data):
    """Remove 24B vendor control frames (renderer.py's walk, but the scan
    runs in C via finditer — clips can span a few MB)."""
    cuts = []
    pos = 0
    for m in re.finditer(re.escape(MAGIC), data):
        i = m.start()
        if i < pos or i + 24 > len(data):
            continue
        ln, cmd = struct.unpack("<II", data[i+4:i+12])
        if cmd in CONTROL_CMDS and i + 24 + ln <= len(data):
            cuts.append((i, i + 24 + ln))
            pos = i + 24 + ln
    if not cuts:
        return data
    out = bytearray()
    last = 0
    for a, b in cuts:
        out += data[last:a]
        last = b
    out += data[last:]
    return bytes(out)


def clean_h264(data):
    """Keep NAL types 1/5/6/7/8 and start at the first SPS in the window so
    the clip decodes from a clean GOP (first, not last — we want the earliest
    clean picture, renderer.py wants the newest)."""
    starts = [m.start() for m in re.finditer(b"\x00\x00\x00\x01", data)]
    keep = bytearray()
    for j, s in enumerate(starts):
        nxt = starts[j+1] if j+1 < len(starts) else len(data)
        if s + 4 >= len(data):
            break
        complete = j + 1 < len(starts) or nxt - s > 4
        if data[s+4] & 0x1F in NAL_TYPES and complete:
            keep += data[s:nxt]
    if not keep:
        return b""
    ks = [m.start() for m in re.finditer(b"\x00\x00\x00\x01", bytes(keep))]
    sps = next((s for s in ks if keep[s+4] & 0x1F == 7), None)
    return bytes(keep[sps:]) if sps is not None else bytes(keep)


class MotionRecorder:

    def __init__(self, out_dir=DEFAULT_OUT, dump_path=DEFAULT_DUMP,
                 motion_path=DEFAULT_MOTION, threshold_pct=2.0,
                 pre_s=30, post_s=10, max_s=120, ffmpeg=None,
                 is_live=None, dog_check=UNSET, confirm_polls=2,
                 frame_path=DEFAULT_FRAME):
        # pre_s=30 on purpose: dog_final analyses motion every 20s, so a 5s
        # pre-roll would miss most of the action before the trigger fires
        self._out_dir = out_dir
        self._dump_path = dump_path
        self._motion_path = motion_path
        self._threshold = threshold_pct
        self._pre_s = pre_s
        self._post_s = post_s
        self._max_s = max_s
        self._ffmpeg = ffmpeg or resolve_ffmpeg()
        self._is_live = is_live or (lambda: True)
        self._dog_check = self._resolve_gate(dog_check)
        self._confirm_polls = max(1, int(confirm_polls))
        self._frame_path = frame_path
        self._samples = deque(maxlen=256)   # (t, dump size)
        self._clip = None                   # active capture window
        self._streak = 0                    # consecutive above-threshold polls
        self._last_gate_try = 0.0           # classification throttle per episode
        self._io_lock = threading.Lock()    # capture vs retention on out_dir

    def _resolve_gate(self, dog_check):
        """UNSET → env-driven default (off via DOG_GATE=0, else real Vision);
        None → explicitly off; callable → injected (tests / hub overrides)."""
        if dog_check is not UNSET:
            return dog_check
        if not dog_gate.gate_enabled():
            return None
        return dog_gate.check_frame

    # -- capture loop (one cycle per call; hub runs it in a thread) --

    def poll_once(self, now=None):
        now = time.time() if now is None else now
        if not self._is_live():
            self._clip = None                  # stale tail must not look like footage
            self._streak = 0
            return "idle (pipeline off)"
        try:
            self._samples.append((now, os.path.getsize(self._dump_path)))
        except OSError:
            pass
        level = self._read_motion(now)
        above = level is not None and level >= self._threshold
        self._streak = self._streak + 1 if above else 0
        if above and self._clip is None:
            if self._streak < self._confirm_polls:
                return "idle (confirming)"     # single-sample noise filter
            ok, verdict = self._gate_open(now, level)
            if not ok:
                return verdict
            dog_conf, species = verdict
            start = self._size_at(now - self._pre_s)
            if start is None:
                try:                        # no history yet — start now
                    start = os.path.getsize(self._dump_path)
                except OSError:
                    return "idle"
            self._clip = {"start": start, "t0": now,
                          "peak": level, "last_above": now,
                          "dog_conf": dog_conf, "dog": species}
        elif above and self._clip is not None:
            self._clip["peak"] = max(self._clip["peak"], level)
            self._clip["last_above"] = now
        if self._clip is not None and (
                now - self._clip["last_above"] >= self._post_s
                or now - self._clip["t0"] >= self._max_s):
            self._finalize(now)
        return "capturing" if self._clip else "idle"

    def run_forever(self, poll_s=1.0):
        while True:
            time.sleep(poll_s)
            try:
                self.poll_once()
            except Exception as e:
                print("[rec] err:", e, flush=True)

    # -- listing / retention (hub endpoints) --

    @property
    def out_dir(self):
        return self._out_dir

    def days(self):
        try:
            return sorted((n for n in os.listdir(self._out_dir)
                           if re.fullmatch(r"\d{4}-\d{2}-\d{2}", n)
                           and os.path.isdir(os.path.join(self._out_dir, n))),
                          reverse=True)
        except OSError:
            return []

    def events(self, day):
        """Events of one YYYY-MM-DD, newest first."""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
            return []
        out = []
        try:
            names = sorted(os.listdir(os.path.join(self._out_dir, day)),
                           reverse=True)
        except OSError:
            return []
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                e = json.load(open(os.path.join(self._out_dir, day, n)))
            except Exception:
                continue
            e["day"] = day
            out.append(e)
        return out

    def disk_usage_mb(self):
        total = 0
        for root, _, files in os.walk(self._out_dir):
            for n in files:
                try:
                    total += os.path.getsize(os.path.join(root, n))
                except OSError:
                    pass
        return round(total / 1e6, 1)

    def clean_retention(self, keep_days, now=None):
        """Delete event files older than keep_days; returns removed count."""
        now = time.time() if now is None else now
        cutoff = now - keep_days * 86400
        removed = 0
        with self._io_lock:                # never sweep under a live capture
            for day in self.days():
                d = os.path.join(self._out_dir, day)
                try:
                    names = os.listdir(d)
                except OSError:
                    continue
                for n in names:
                    p = os.path.join(d, n)
                    try:
                        if os.path.getmtime(p) < cutoff:
                            os.remove(p)
                            removed += 1
                    except OSError:
                        pass
                if not os.listdir(d):
                    shutil.rmtree(d, ignore_errors=True)
        return removed

    # -- internals --

    def _gate_open(self, now, level):
        """(open, verdict) — run the dog checker once per episode-slice.
        (None, None) verdict = gate off or fail-open (record untagged).
        Any checker error fails OPEN: a broken gate must never lose real
        dog footage, it only loses the 🐶 badge."""
        if self._dog_check is None:
            return True, (None, None)
        if now - self._last_gate_try < GATE_RECHECK_S:
            return False, "rejected (no dog, recheck pending)"
        self._last_gate_try = now
        try:
            conf, species = self._dog_check(self._frame_path)
        except Exception as e:
            print(f"[rec] dog gate error ({e}) — fail open", flush=True)
            self._gate_log(now, level, None, "", "fail-open")
            return True, (None, None)
        has_dog = conf >= dog_gate.DOG_CONF_MIN
        self._gate_log(now, level, conf, species,
                       "record" if has_dog else "reject")
        if not has_dog:
            return False, f"rejected (no dog, conf {conf:.2f})"
        return True, (round(conf, 2), species)

    def _gate_log(self, now, level, conf, species, action):
        """One jsonl line per decision at the recordings root — the audit
        trail Samuel tunes DOG_CONF_MIN against. Retention never touches it
        (only YYYY-MM-DD dirs are swept)."""
        try:
            os.makedirs(self._out_dir, exist_ok=True)
            with open(os.path.join(self._out_dir, GATE_LOG_NAME), "a") as f:
                f.write(json.dumps({
                    "ts": round(now, 1), "level": round(level, 2),
                    "conf": None if conf is None else round(conf, 3),
                    "species": species, "action": action}) + "\n")
        except OSError:
            pass

    def _read_motion(self, now):
        """Motion level in %, or None when missing/stale."""
        try:
            st = json.load(open(self._motion_path))
        except Exception:
            return None
        if now - st.get("ts", 0) > MOTION_STALE_S:
            return None
        try:
            return float(st["level"])
        except (KeyError, TypeError, ValueError):
            return None

    def _size_at(self, t):
        """Dump size at time t from the sample history (None if unknown)."""
        for ts, size in self._samples:
            if ts >= t:
                return size
        return None

    def _finalize(self, now):
        clip = self._clip
        self._clip = None
        with self._io_lock:                    # keep retention out of the write
            try:
                end = os.path.getsize(self._dump_path)
                start = min(clip["start"], end)
                raw = open(self._dump_path, "rb").read()[start:end]
            except OSError:
                return
            h264 = clean_h264(strip_control(raw))
            if len(h264) < MIN_H264_BYTES:
                return
            t0 = datetime.fromtimestamp(clip["t0"])
            day_dir = os.path.join(self._out_dir, t0.strftime("%Y-%m-%d"))
            os.makedirs(day_dir, exist_ok=True)
            stem = t0.strftime("%H%M%S")
            tmp = os.path.join(day_dir, "." + stem + ".h264")
            open(tmp, "wb").write(h264)
            mp4 = os.path.join(day_dir, stem + ".mp4")
            thumb = os.path.join(day_dir, stem + ".jpg")
            try:
                ok, thumb_ok = self._encode(tmp, mp4, thumb)
            except Exception:                  # ffmpeg missing/killed — no junk
                ok, thumb_ok = False, False
            os.remove(tmp)
            if not ok:
                for p in (mp4, thumb):
                    if os.path.exists(p):
                        os.remove(p)
                return
            event = {"time": t0.strftime("%H:%M:%S"), "ts": clip["t0"],
                     "motion_pct": round(clip["peak"], 1),
                     "clip": stem + ".mp4",
                     "thumb": (stem + ".jpg") if thumb_ok else None,
                     "duration_s": round(now - clip["t0"], 1),
                     "size_kb": (os.path.getsize(mp4) // 1024
                                 if os.path.exists(mp4) else 0)}
            if clip.get("dog_conf") is not None:   # 🐶 badge in 回放 UI
                event["dog_conf"] = clip["dog_conf"]
                event["dog"] = clip.get("dog") or "?"
            json.dump(event, open(os.path.join(day_dir, stem + ".json"), "w"))
            print("[rec] clip saved:", day_dir, stem, flush=True)

    def read_file(self, name):
        """(bytes, media_type) for a clip/thumb inside the recordings tree,
        or None. The only path-safe way in — hub endpoints stay thin."""
        base = os.path.realpath(self._out_dir)
        p = os.path.realpath(os.path.join(base, name or ""))
        if not p.startswith(base + os.sep) \
                or not os.path.isfile(p) \
                or not p.endswith((".mp4", ".jpg")):
            return None
        media = "video/mp4" if p.endswith(".mp4") else "image/jpeg"
        try:
            return open(p, "rb").read(), media
        except OSError:
            return None

    def _encode(self, h264_path, mp4_path, thumb_path):
        """h264 → MP4 (stream copy, re-encode fallback) + thumbnail.
        Returns (clip_ok, thumb_ok)."""
        r = subprocess.run(
            [self._ffmpeg, "-y", "-loglevel", "error", "-f", "h264",
             "-i", h264_path, "-c", "copy", mp4_path],
            capture_output=True, timeout=120)
        if not self._mp4_ok(mp4_path):
            r = subprocess.run(
                [self._ffmpeg, "-y", "-loglevel", "error", "-f", "h264",
                 "-i", h264_path, "-c:v", "libx264", "-preset", "veryfast",
                 "-pix_fmt", "yuv420p", mp4_path],
                capture_output=True, timeout=300)
        if r.returncode != 0 or not self._mp4_ok(mp4_path):
            return False, False
        t = subprocess.run(
            [self._ffmpeg, "-y", "-loglevel", "error", "-i", mp4_path,
             "-frames:v", "1", "-vf", "scale=320:-2", thumb_path],
            capture_output=True, timeout=60)
        return True, (t.returncode == 0 and os.path.exists(thumb_path))

    def _mp4_ok(self, mp4_path):
        return os.path.exists(mp4_path) \
            and os.path.getsize(mp4_path) >= MIN_H264_BYTES
