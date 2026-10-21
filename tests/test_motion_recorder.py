"""ticket 07 — motion recorder mechanics (tmp dirs, fake encode, no threads)."""
import json
import os
import shutil
import struct
import time
from datetime import datetime

import pytest

import motion_recorder as mr


def make_rec(tmp_path, is_live=True, **kw):
    dump = tmp_path / "ch1_video.bin"
    mpath = tmp_path / "motion_state.json"
    out = tmp_path / "recordings"
    rec = mr.MotionRecorder(out_dir=str(out), dump_path=str(dump),
                            motion_path=str(mpath),
                            is_live=lambda: is_live,
                            dog_check=kw.pop("dog_check", None), **kw)
    return rec, dump, mpath, out


def set_motion(mpath, level, ts=None):
    json.dump({"level": level, "ts": ts if ts is not None else time.time()},
              open(mpath, "w"))


def fake_encode(rec, ok=True, thumb=True):
    calls = []

    def _enc(h264, mp4, thumb_p):
        calls.append((h264, mp4, thumb_p))
        if ok:
            open(mp4, "wb").write(b"\x00\x00\x00\x18ftypmp42" + b"x" * 2000)
        if ok and thumb:
            open(thumb_p, "wb").write(b"\xff\xd8thumb")
        return (ok, ok and thumb)

    rec._encode = _enc
    return calls


def gop_parts():
    sps = b"\x00\x00\x00\x01\x67" + b"S" * 200
    pps = b"\x00\x00\x00\x01\x68" + b"Q" * 20
    idr = b"\x00\x00\x00\x01\x65" + b"I" * 1500
    p = b"\x00\x00\x00\x01\x41" + b"P" * 900
    ctrl = (bytes.fromhex("88887676") + struct.pack("<II", 20, 0x10)
            + b"\x00" * 12 + b"C" * 20)
    return ctrl, p, sps, pps, idr


def dump_with_gops():
    """Raw-dump shape: 24B-header control frames interleaved with SPS/PPS/
    IDR/P NALs (header = magic 4 + ln/cmd 8 + 12 more, payload follows)."""
    ctrl, p, sps, pps, idr = gop_parts()
    return ctrl + p + sps + pps + ctrl + idr + p + ctrl


def test_strip_control_removes_vendor_frames():
    data = dump_with_gops()
    clean = mr.strip_control(data)
    assert bytes.fromhex("88887676") not in clean
    assert b"\x00\x00\x00\x01\x67" in clean and b"\x00\x00\x00\x01\x65" in clean


def test_clean_h264_starts_at_first_sps():
    clean = mr.clean_h264(mr.strip_control(dump_with_gops()))
    assert clean.startswith(b"\x00\x00\x00\x01\x67")   # SPS first, full GOPs
    assert b"C" not in clean                            # control bytes gone


def test_clean_h264_keeps_final_partial_nal():
    # the newest frames sit at the dump tail — the last NAL must survive
    data = b"\x00\x00\x00\x01\x67" + b"S" * 50
    clean = mr.clean_h264(data)
    assert clean == data


def test_motion_trigger_records_clip(tmp_path):
    rec, dump, mpath, out = make_rec(tmp_path)
    calls = fake_encode(rec)
    ctrl, p, sps, pps, idr = gop_parts()
    dump.write_bytes(ctrl + p)                     # pre-trigger bytes only
    set_motion(mpath, 0.0)
    rec.poll_once(1000.0)                          # warm-up size sample
    with open(dump, "ab") as f:                    # GOP lands before trigger
        f.write(sps + pps + ctrl + idr + p + ctrl)
    t0 = 1020.0
    set_motion(mpath, 5.0)
    assert rec.poll_once(t0) == "idle (confirming)"   # 1st above poll
    assert rec.poll_once(t0 + 1) == "capturing"       # 2nd confirms → record
    set_motion(mpath, 0.1)
    assert rec.poll_once(t0 + 12) == "idle"        # post_s=10 elapsed
    assert len(calls) == 1
    day = datetime.fromtimestamp(t0).strftime("%Y-%m-%d")
    day_dir = out / day
    stem = [n for n in os.listdir(day_dir) if n.endswith(".mp4")]
    assert len(stem) == 1 and (day_dir / stem[0]).stat().st_size > 1000
    event = json.load(open(day_dir / stem[0].replace(".mp4", ".json")))
    assert event["motion_pct"] == 5.0 and event["clip"] == stem[0]
    assert event["thumb"] == stem[0].replace(".mp4", ".jpg")
    assert "dog_conf" not in event                 # gate off → old json shape
    assert not list(day_dir.glob(".*h264"))        # temp cleaned up


def test_no_clip_when_pipeline_off(tmp_path):
    rec, dump, mpath, out = make_rec(tmp_path, is_live=False)
    fake_encode(rec)
    dump.write_bytes(dump_with_gops())
    set_motion(mpath, 9.0)
    assert "pipeline off" in rec.poll_once(1000.0)
    set_motion(mpath, 0.0)
    rec.poll_once(1015.0)
    assert not out.exists() or not list(out.rglob("*.mp4"))


def test_stale_motion_never_triggers(tmp_path):
    rec, dump, mpath, out = make_rec(tmp_path)
    fake_encode(rec)
    dump.write_bytes(dump_with_gops())
    set_motion(mpath, 9.0, ts=time.time() - 999)
    assert rec.poll_once(time.time()) == "idle"
    assert not out.exists() or not list(out.rglob("*.mp4"))


def test_pre_roll_uses_size_history(tmp_path):
    rec, dump, mpath, out = make_rec(tmp_path, pre_s=5)
    dump.write_bytes(b"a" * 100)
    rec.poll_once(10.0)                                # sample: (10, 100)
    dump.write_bytes(b"a" * 200)
    rec.poll_once(12.0)                                # sample: (12, 200)
    set_motion(mpath, 4.0)
    rec.poll_once(14.0)                                # above poll #1
    rec.poll_once(15.0)                                # #2 → trigger
    assert rec._clip["start"] == 100                   # size at t≥10 → (10,100)


def test_max_clip_length_closes_while_motion_continues(tmp_path):
    rec, dump, mpath, out = make_rec(tmp_path, max_s=30)
    calls = fake_encode(rec)
    ctrl, p, sps, pps, idr = gop_parts()
    dump.write_bytes(ctrl + p)
    set_motion(mpath, 0.0)
    rec.poll_once(90.0)                                # warm-up sample
    with open(dump, "ab") as f:
        f.write(sps + pps + ctrl + idr + p + ctrl)
    set_motion(mpath, 6.0)
    rec.poll_once(100.0)                               # confirm #1
    rec.poll_once(101.0)                               # #2 → clip opens t0=101
    set_motion(mpath, 6.0)
    rec.poll_once(132.0)                               # still above, but max_s
    assert len(calls) == 1


def test_tiny_cut_skips_encoding(tmp_path):
    rec, dump, mpath, out = make_rec(tmp_path)
    calls = fake_encode(rec)
    dump.write_bytes(b"\x00" * 100)                    # < MIN_H264_BYTES
    set_motion(mpath, 5.0)
    rec.poll_once(10.0)
    rec.poll_once(11.0)
    set_motion(mpath, 0.0)
    rec.poll_once(26.0)
    assert calls == []                                 # garbage cut, no encode


def test_days_and_events_listing(tmp_path):
    rec, _, _, out = make_rec(tmp_path)
    d1 = out / "2026-10-19"
    d2 = out / "2026-10-20"
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    json.dump({"time": "10:00:00", "clip": "100000.mp4"},
              open(d1 / "100000.json", "w"))
    json.dump({"time": "11:00:00", "clip": "110000.mp4"},
              open(d2 / "110000.json", "w"))
    (d2 / "110000.mp4").write_bytes(b"x")
    assert rec.days() == ["2026-10-20", "2026-10-19"]
    ev = rec.events("2026-10-20")
    assert len(ev) == 1 and ev[0]["day"] == "2026-10-20"
    assert rec.events("../etc") == [] and rec.events("nope") == []


def test_retention_removes_only_old_days(tmp_path):
    rec, _, _, out = make_rec(tmp_path)
    old = out / "2026-08-01"
    new = out / "2026-10-20"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "1.mp4").write_bytes(b"x")
    (new / "2.mp4").write_bytes(b"x")
    os.utime(old / "1.mp4", (1, 1))
    now = time.time()
    removed = rec.clean_retention(30, now=now)
    assert removed == 1 and not old.exists() and (new / "2.mp4").exists()


def arm_motion(rec, dump, mpath, t):
    """Pre-trigger bytes + warm-up size sample, then two above-threshold
    polls (the confirm window) — same dump shape as the trigger test so
    the finalize cut is non-empty. Returns (poll1, poll2) verdicts."""
    ctrl, p, sps, pps, idr = gop_parts()
    dump.write_bytes(ctrl + p)
    set_motion(mpath, 0.0)
    rec.poll_once(t - 1.0)
    with open(dump, "ab") as f:
        f.write(sps + pps + ctrl + idr + p + ctrl)
    set_motion(mpath, 5.0)
    return rec.poll_once(t), rec.poll_once(t + 1.0)


def test_dog_gate_blocks_clip_when_no_dog(tmp_path):
    rec, dump, mpath, out = make_rec(tmp_path,
                                     dog_check=lambda p: (0.0, ""))
    fake_encode(rec)
    r1, r2 = arm_motion(rec, dump, mpath, 100.0)
    assert r1 == "idle (confirming)"
    assert "no dog" in r2
    assert not list(out.rglob("*.mp4"))                # nothing recorded
    log = [json.loads(l) for l in
           open(out / "dog_gate_log.jsonl").read().splitlines()]
    assert log[-1]["action"] == "reject" and log[-1]["conf"] == 0.0


def test_dog_gate_records_and_tags_event(tmp_path):
    rec, dump, mpath, out = make_rec(tmp_path,
                                     dog_check=lambda p: (0.73, "Dog"))
    calls = fake_encode(rec)
    _, r2 = arm_motion(rec, dump, mpath, 100.0)
    assert r2 == "capturing"
    set_motion(mpath, 0.1)
    rec.poll_once(115.0)
    assert len(calls) == 1
    day_dir = out / datetime.fromtimestamp(100.0).strftime("%Y-%m-%d")
    event = json.load(open(day_dir / [n for n in os.listdir(day_dir)
                                      if n.endswith(".json")][0]))
    assert event["dog_conf"] == 0.73 and event["dog"] == "Dog"
    log = [json.loads(l) for l in
           open(out / "dog_gate_log.jsonl").read().splitlines()]
    assert log[-1]["action"] == "record"


def test_dog_gate_fail_open_on_checker_error(tmp_path):
    def broken(_p):
        raise RuntimeError("Vision exploded")
    rec, dump, mpath, out = make_rec(tmp_path, dog_check=broken)
    calls = fake_encode(rec)
    _, r2 = arm_motion(rec, dump, mpath, 100.0)
    assert r2 == "capturing"                           # fails OPEN
    set_motion(mpath, 0.1)
    rec.poll_once(115.0)
    assert len(calls) == 1                             # clip still recorded
    day_dir = out / datetime.fromtimestamp(100.0).strftime("%Y-%m-%d")
    event = json.load(open(day_dir / [n for n in os.listdir(day_dir)
                                      if n.endswith(".json")][0]))
    assert "dog_conf" not in event                     # untagged, not blocked
    log = [json.loads(l) for l in
           open(out / "dog_gate_log.jsonl").read().splitlines()]
    assert log[-1]["action"] == "fail-open" and log[-1]["conf"] is None


def test_dog_gate_throttles_rechecks(tmp_path):
    calls = []
    def checker(_p):
        calls.append(1)
        return 0.0, ""
    rec, dump, mpath, out = make_rec(tmp_path, dog_check=checker)
    dump.write_bytes(dump_with_gops())
    set_motion(mpath, 0.0)
    rec.poll_once(99.0)
    set_motion(mpath, 5.0)
    rec.poll_once(100.0)                               # confirm #1
    rec.poll_once(101.0)                               # classify #1
    for t in (102.0, 108.0):                           # inside 10s throttle
        rec.poll_once(t)
    assert "no dog" in rec.poll_once(111.5)            # → classify #2
    assert len(calls) == 2


def test_dog_gate_env_off(monkeypatch, tmp_path):
    monkeypatch.setenv("DOG_GATE", "0")
    rec = mr.MotionRecorder(out_dir=str(tmp_path / "r"),
                            dump_path=str(tmp_path / "d.bin"),
                            motion_path=str(tmp_path / "m.json"),
                            is_live=lambda: True)      # default (UNSET) path
    assert rec._dog_check is None                      # gate disabled


def test_dog_gate_default_on(monkeypatch, tmp_path):
    monkeypatch.delenv("DOG_GATE", raising=False)
    rec = mr.MotionRecorder(out_dir=str(tmp_path / "r"),
                            dump_path=str(tmp_path / "d.bin"),
                            motion_path=str(tmp_path / "m.json"),
                            is_live=lambda: True)
    assert rec._dog_check is mr.dog_gate.check_frame   # real Vision checker


@pytest.mark.skipif(shutil.which("ffmpeg") is None,
                    reason="real ffmpeg integration")
def test_real_ffmpeg_encode_produces_playable_mp4(tmp_path):
    import subprocess
    src = tmp_path / "in.h264"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
                    "-c:v", "libx264", "-f", "h264", str(src)], check=True)
    rec, _, _, out = make_rec(tmp_path)
    ok, thumb_ok = rec._encode(str(src), str(tmp_path / "o.mp4"),
                               str(tmp_path / "o.jpg"))
    assert ok and thumb_ok
    probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                            "-show_entries", "stream=codec_name",
                            "-of", "csv=p=0", str(tmp_path / "o.mp4")],
                           capture_output=True, text=True)
    assert probe.stdout.strip() == "h264"
