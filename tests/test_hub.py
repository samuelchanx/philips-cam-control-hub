"""control-hub ticket 01+02 — webapp seam: status, feed actions, guards, log."""
import json
import os
import threading

import fastapi.testclient as tc

import dispatcher
import feedlog
import hub
import motion_recorder

SECRET = "super-secret-tuya-key-value"


class FakeFeeder:
    """Stands in for CloudFeeder; carries a secret to prove it never leaks."""

    def __init__(self, ok=True):
        self.ok = ok
        self.secret = SECRET

    def status(self):
        if not self.ok:
            raise RuntimeError("cloud down")
        return {"online": True, "battery_percentage": 88, "feed_state": "standby",
                "light": False, "slow_feed": False}

    def feed(self, portions):
        self.fed = portions
        return "done"

    def quick_feed(self):
        return True

    def toggle(self, code, on):
        return True


def client(fake, **kw):
    d = kw.get("dispatcher") or dispatcher.Dispatcher(
        fake, log_path=kw.get("log_path", feedlog.LOG_PATH),
        notify=kw.get("notify") or (lambda msg: None))
    return tc.TestClient(hub.create_app(feeder_backend=fake, dispatcher=d,
                        pipeline=FakePipeline()))


def test_status_returns_real_fields_from_backend():
    r = client(FakeFeeder()).get("/api/feeder/status")
    assert r.status_code == 200
    assert r.json() == {"online": True, "battery_percentage": 88,
                        "feed_state": "standby", "light": False, "slow_feed": False}


def test_cloud_failure_is_graceful_json_not_stacktrace():
    r = client(FakeFeeder(ok=False)).get("/api/feeder/status")
    assert r.status_code == 502
    body = r.json()
    assert set(body) == {"error"}
    assert "Traceback" not in r.text


def test_no_secret_in_any_response_or_page():
    c = client(FakeFeeder())
    for path in ("/", "/api/feeder/status", "/api/nope"):
        assert SECRET not in c.get(path).text


def test_dashboard_page_serves_and_mentions_feeder():
    r = client(FakeFeeder()).get("/")
    assert r.status_code == 200
    assert "出糧機" in r.text


def test_cloudfeeder_maps_fields(monkeypatch):
    class FakeCloud:
        def _tuyaplatform(self, path):
            return {"result": {"online": True}}

    monkeypatch.setattr("feeder.cloud", lambda: FakeCloud())
    monkeypatch.setattr("feeder.ENV", {"TUYA_DEVICE_ID": "test-device-id"})
    monkeypatch.setattr("feeder.get_status",
                        lambda c: {"battery_percentage": 7, "feed_state": "done",
                                   "light": True, "slow_feed": False})
    out = hub.CloudFeeder().status()
    assert out == {"online": True, "battery_percentage": 7, "feed_state": "done",
                   "light": True, "slow_feed": False}


def test_default_port_and_env_override():
    assert hub.DEFAULT_PORT == 8790
    assert isinstance(int(hub.load_env().get("CONTROL_HUB_PORT") or 8790), int)


# ---- ticket 02: feed action + guards + history ----

def feed_client(tmp_path, feeder=None, notify=None):
    f = feeder or FakeFeeder()
    notes = []
    d = dispatcher.Dispatcher(f, log_path=str(tmp_path / "feed.jsonl"),
                              notify=notify or notes.append)
    return tc.TestClient(hub.create_app(feeder_backend=f, dispatcher=d,
                                        pipeline=FakePipeline())), f, notes


def test_feed_ok_appends_jsonl_row_and_history(tmp_path):
    c, f, notes = feed_client(tmp_path)
    r = c.post("/api/feeder/feed", json={"portions": 2})
    assert r.status_code == 200
    assert r.json() == {"portions": 2, "feed_state": "done",
                        "source": "cloud", "latency_ms": r.json()["latency_ms"]}
    assert f.fed == 2
    entries = feedlog.recent(10, path=str(tmp_path / "feed.jsonl"))
    assert len(entries) == 1
    row = entries[0]
    assert row["command"] == "feed" and row["portions"] == 2
    assert row["result"] == "ok" and row["source"] == "cloud"
    assert set(row) >= {"ts", "latency_ms"}
    hist = c.get("/api/feeder/history").json()["entries"]
    assert hist == entries


def test_portions_clamped_at_dispatcher(tmp_path):
    c, f, _ = feed_client(tmp_path)
    assert c.post("/api/feeder/feed", json={"portions": 99}).json()["portions"] == 36
    assert c.post("/api/feeder/feed", json={"portions": 0}).json()["portions"] == 1
    assert c.post("/api/feeder/feed", json={"portions": -5}).json()["portions"] == 1
    assert f.fed == 1


def test_non_integer_portions_rejected(tmp_path):
    c, _, _ = feed_client(tmp_path)
    r = c.post("/api/feeder/feed", json={"portions": "abc"})
    assert r.status_code == 400
    assert "error" in r.json()


def test_factory_reset_refused_by_dispatcher(tmp_path):
    c, _, _ = feed_client(tmp_path)
    try:
        dispatcher.Dispatcher(FakeFeeder()).run("factory_reset")
        raise AssertionError("factory_reset must be refused")
    except dispatcher.Rejected:
        pass
    # also refused arriving over HTTP through the toggle path
    assert c.post("/api/feeder/toggle",
                  json={"code": "factory_reset", "value": True}).status_code == 400


def test_unknown_commands_refused(tmp_path):
    c, _, _ = feed_client(tmp_path)
    for cmd in ("delTs", "cloudCard/delete", "reboot", ""):
        try:
            dispatcher.Dispatcher(FakeFeeder()).run(cmd)
            raise AssertionError("command %r must be refused" % cmd)
        except dispatcher.Rejected:
            pass


def test_second_feed_while_busy_gets_409(tmp_path):
    class BlockingFeeder(FakeFeeder):
        started = threading.Event()
        release = threading.Event()

        def feed(self, portions):
            self.started.set()
            self.release.wait(3)
            return "done"

    f = BlockingFeeder()
    d = dispatcher.Dispatcher(f, log_path=str(tmp_path / "feed.jsonl"),
                              notify=lambda m: None)
    app = hub.create_app(feeder_backend=f, dispatcher=d,
                         pipeline=FakePipeline())

    result = {}

    def first():
        result["r"] = tc.TestClient(app).post("/api/feeder/feed", json={"portions": 1})

    t = threading.Thread(target=first)
    t.start()
    assert f.started.wait(2)
    second = tc.TestClient(app).post("/api/feeder/feed", json={"portions": 1})
    assert second.status_code == 409
    f.release.set()
    t.join(5)
    assert result["r"].status_code == 200


def test_feed_failure_logged_and_502(tmp_path):
    class BrokenFeeder(FakeFeeder):
        def feed(self, portions):
            raise RuntimeError("cloud down")

    c, _, notes = feed_client(tmp_path, feeder=BrokenFeeder())
    r = c.post("/api/feeder/feed", json={"portions": 1})
    assert r.status_code == 502
    row = feedlog.recent(5, path=str(tmp_path / "feed.jsonl"))[0]
    assert row["result"] == "failed"
    assert any("失敗" in n for n in notes)


def test_quick_feed_and_toggles_via_dispatcher(tmp_path):
    c, f, _ = feed_client(tmp_path)
    assert c.post("/api/feeder/quick_feed").status_code == 200
    row = feedlog.recent(5, path=str(tmp_path / "feed.jsonl"))[0]
    assert row["command"] == "quick_feed" and row["portions"] is None
    assert c.post("/api/feeder/toggle", json={"code": "light", "value": True}).status_code == 200
    assert c.post("/api/feeder/toggle", json={"code": "slow_feed", "value": True}).status_code == 200


def test_success_feed_sends_telegram(tmp_path):
    c, _, notes = feed_client(tmp_path)
    c.post("/api/feeder/feed", json={"portions": 1})
    assert any("🍖" in n for n in notes)


def test_history_empty_when_no_log(tmp_path):
    c, _, _ = feed_client(tmp_path)
    assert c.get("/api/feeder/history").json()["entries"] == []


# ---- ticket 03: camera endpoints (fake pipeline + fake cloud status) ----

class FakePipeline:
    def __init__(self, state="stopped", frame_path="/nonexistent.jpg",
                 no_frame_retries=0):
        self.state = state
        self.frame_path = frame_path
        self.no_frame_retries = no_frame_retries
        self.started = 0
        self.stopped = 0
        self.touched = 0
        self._error = None
        self._recording = False

    def start(self):
        self.started += 1
        return self.state

    def stop(self):
        self.stopped += 1
        return "stopped"

    def touch(self):
        self.touched += 1

    def set_recording(self, on):
        self._recording = bool(on)

    @property
    def recording(self):
        return self._recording

    def status(self):
        return {"state": self.state, "error": self._error,
                "frame_age_s": 5.0 if self.state == "live" else None,
                "idle_timeout_s": 300,
                "no_frame_retries": self.no_frame_retries}


def cam_client(pipeline=None, camera_status=None, recorder=None):
    return tc.TestClient(hub.create_app(feeder_backend=FakeFeeder(),
                                        pipeline=pipeline,
                                        camera_status=camera_status,
                                        recorder=recorder))


def test_camera_start_stop_routes_to_pipeline():
    fp = FakePipeline()
    c = cam_client(pipeline=fp)
    assert c.post("/api/camera/start").json() == {"state": "stopped"}
    assert c.post("/api/camera/stop").json() == {"state": "stopped"}
    c.get("/api/camera/status")
    assert fp.started == 1 and fp.stopped == 1
    assert fp.touched > 0                        # polling keeps it alive


def test_camera_status_merges_pipeline_and_cloud():
    fp = FakePipeline(state="live")
    c = cam_client(pipeline=fp, camera_status=lambda: {"sdCard": False})
    j = c.get("/api/camera/status").json()
    assert j == {"pipeline": "live", "error": None, "frame_age_s": 5.0,
                 "idle_timeout_s": 300, "online": True, "sdCard": False,
                 "cloud_error": None, "hint": None}


def test_camera_offline_hint_after_no_frame_retries():
    fp = FakePipeline(state="starting", no_frame_retries=2)
    c = cam_client(pipeline=fp, camera_status=lambda: {"sdCard": None})
    j = c.get("/api/camera/status").json()
    assert "離線" in j["hint"]
    assert j["online"] is None                     # starting ≠ online

    fp2 = FakePipeline(state="starting")           # first attempt: no hint yet
    j2 = cam_client(pipeline=fp2).get("/api/camera/status").json()
    assert j2["hint"] is None


# ---- ticket 07: motion recorder ----

def make_recorder(tmp_path):
    return motion_recorder.MotionRecorder(out_dir=str(tmp_path / "recordings"),
                                          dump_path=str(tmp_path / "dump.bin"),
                                          motion_path=str(tmp_path / "m.json"))


def test_recorder_mode_toggles_and_never_starts_pipeline():
    fp = FakePipeline(state="stopped")
    c = cam_client(pipeline=fp)
    j = c.post("/api/recorder/mode", json={"on": True}).json()
    assert j["mode"] is True and fp.started == 0   # never auto-starts
    assert fp.recording is True
    j = c.post("/api/recorder/mode", json={"on": False}).json()
    assert j["mode"] is False


def test_camera_ptz_validation_and_relay_file(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "PTZ_CMD_FILE", str(tmp_path / "ptz_cmd.json"))
    fp = FakePipeline(state="stopped")
    c = cam_client(pipeline=fp)
    assert c.post("/api/camera/ptz",
                  json={"dir": "left"}).status_code == 409     # not live
    fp.state = "live"
    assert c.post("/api/camera/ptz",
                  json={"dir": "sideways"}).status_code == 400
    r = c.post("/api/camera/ptz", json={"dir": "left", "dur": 2.0})
    assert r.json() == {"sent": "left", "dur": 2.0}
    st = json.load(open(tmp_path / "ptz_cmd.json"))
    assert st["dir_byte"] == 3 and st["dur"] == 2.0   # 3 = AVIOCTRL left


# ---- talk-back（安撫喇叭）----

def make_audio(tmp_path, name="calm.wav"):
    import subprocess
    d = tmp_path / "audio"
    d.mkdir(exist_ok=True)
    subprocess.run([motion_recorder.resolve_ffmpeg(), "-y", "-loglevel",
                    "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=1",
                    "-ar", "8000", "-ac", "1", str(d / name)], check=True)
    return d / name


def talk_client(tmp_path, monkeypatch, state="live"):
    monkeypatch.setattr(hub, "TALK_CMD_FILE", str(tmp_path / "talk.json"))
    monkeypatch.setattr(hub, "AUDIO_DIR", str(tmp_path / "audio"))
    return cam_client(pipeline=FakePipeline(state=state))


def test_talk_play_converts_and_relays(tmp_path, monkeypatch):
    make_audio(tmp_path)
    c = talk_client(tmp_path, monkeypatch)
    r = c.post("/api/camera/talk/play", json={"name": "calm.wav"})
    assert r.json() == {"playing": "calm.wav"}
    st = json.load(open(tmp_path / "talk.json"))
    assert st["action"] == "play" and st["file"].endswith(".al")
    assert os.path.getsize(st["file"]) > 0           # real G.711A bytes
    assert c.post("/api/camera/talk/stop").json() == {"stopped": True}
    assert json.load(open(tmp_path / "talk.json"))["action"] == "stop"


def test_talk_play_rejects_bad_input(tmp_path, monkeypatch):
    make_audio(tmp_path)
    c = talk_client(tmp_path, monkeypatch)
    assert c.post("/api/camera/talk/play",
                  json={"name": "../secret.mp3"}).status_code == 400
    assert c.post("/api/camera/talk/play",
                  json={"name": "nope.mp3"}).status_code == 404
    c2 = talk_client(tmp_path, monkeypatch, state="stopped")
    assert c2.post("/api/camera/talk/play",
                   json={"name": "calm.wav"}).status_code == 409


def test_talk_say_relays(tmp_path, monkeypatch):
    c = talk_client(tmp_path, monkeypatch)

    def fake_say(text, aiff):
        open(aiff, "wb").write(b"fake")
        return True

    def fake_convert(src, dst):
        open(dst, "wb").write(b"\x55" * 320)
        return True

    monkeypatch.setattr(hub, "say_to_aiff", fake_say)
    monkeypatch.setattr(hub, "to_g711a", fake_convert)
    r = c.post("/api/camera/talk/say", json={"text": "你好狗仔"})
    assert r.status_code == 200 and r.json() == {"saying": "你好狗仔"}
    st = json.load(open(tmp_path / "talk.json"))
    assert st["action"] == "play" and st["file"].endswith(".al")
    assert c.post("/api/camera/talk/say", json={"text": "  "}).status_code == 400


def test_recorder_status_and_listing(tmp_path):
    rec = make_recorder(tmp_path)
    day = tmp_path / "recordings" / "2026-10-20"
    day.mkdir(parents=True)
    json.dump({"time": "10:00:00", "motion_pct": 5.0, "clip": "100000.mp4",
               "thumb": "100000.jpg", "duration_s": 12.0, "size_kb": 300,
               "ts": 1.0}, open(day / "100000.json", "w"))
    (day / "100000.mp4").write_bytes(b"\x00" * 5000)
    (day / "100000.jpg").write_bytes(b"\xff\xd8x")
    c = cam_client(pipeline=FakePipeline(), recorder=rec)
    st = c.get("/api/recorder/status").json()
    assert st == {"mode": False, "pipeline": "stopped", "days": 1, "clips": 1,
                  "disk_mb": 0.0, "retention_days": 14}
    days = c.get("/api/recorder/days").json()["days"]
    assert days == ["2026-10-20"]
    ev = c.get("/api/recorder/events?day=2026-10-20").json()["events"]
    assert len(ev) == 1 and ev[0]["clip"] == "100000.mp4"


def test_recorder_capture_gated_on_mode_and_live():
    fp = FakePipeline(state="live")                # live view running
    app = hub.create_app(pipeline=fp)              # default recorder wiring
    is_live = app.state.recorder._is_live
    assert is_live() is False                      # mode off → never captures
    tc.TestClient(app).post("/api/recorder/mode", json={"on": True})
    assert is_live() is True                       # mode on + live → capture
    fp.state = "starting"
    assert is_live() is False                      # not producing frames yet


def test_recorder_file_serves_media_and_blocks_traversal(tmp_path):
    rec = make_recorder(tmp_path)
    day = tmp_path / "recordings" / "2026-10-20"
    day.mkdir(parents=True)
    (day / "100000.mp4").write_bytes(b"mp4bytes")
    (day / "100000.jpg").write_bytes(b"jpgbytes")
    (day / "100000.json").write_text("{}")
    c = cam_client(pipeline=FakePipeline(), recorder=rec)
    r = c.get("/api/recorder/file?name=2026-10-20/100000.mp4")
    assert r.status_code == 200 and r.content == b"mp4bytes"
    assert r.headers["content-type"] == "video/mp4"
    assert c.get("/api/recorder/file?name=2026-10-20/100000.jpg").content \
        == b"jpgbytes"
    for bad in ("../../.env", "2026-10-20/../../x.mp4",
                "2026-10-20/100000.json", "2026-10-20/nope.mp4", ""):
        r = c.get("/api/recorder/file", params={"name": bad})
        assert r.status_code in (400, 404)


def test_camera_online_unknown_when_pipeline_off():
    c = cam_client(pipeline=FakePipeline(state="stopped"),
                   camera_status=lambda: {"sdCard": True})
    j = c.get("/api/camera/status").json()
    assert j["online"] is None and j["sdCard"] is True


def test_camera_cloud_failure_is_graceful():
    def broken():
        raise RuntimeError("philips cloud down")

    c = cam_client(pipeline=FakePipeline(), camera_status=broken)
    r = c.get("/api/camera/status")
    assert r.status_code == 200
    j = r.json()
    assert j["sdCard"] is None and "unavailable" in j["cloud_error"]


def test_camera_frame_404_when_missing():
    c = cam_client(pipeline=FakePipeline())
    assert c.get("/api/camera/frame").status_code == 404


def test_camera_frame_served_with_no_store(tmp_path):
    f = tmp_path / "live_latest.jpg"
    f.write_bytes(b"\xff\xd8fake")
    c = cam_client(pipeline=FakePipeline(frame_path=str(f)))
    r = c.get("/api/camera/frame")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert r.content == b"\xff\xd8fake"


def test_camera_status_cache_hits_fetch_once():
    calls = []

    def counting():
        calls.append(1)
        return {"sdCard": True}

    cached = hub.CachedCameraStatus(ttl=300, fetch=counting)
    c = cam_client(pipeline=FakePipeline(), camera_status=cached)
    c.get("/api/camera/status")
    c.get("/api/camera/status")
    assert len(calls) == 1


def test_camera_cloud_failure_not_cached_long():
    calls = []

    def flaky():
        calls.append(1)
        raise RuntimeError("down")

    cached = hub.CachedCameraStatus(ttl=300, fetch=flaky)
    c = cam_client(pipeline=FakePipeline(), camera_status=cached)
    c.get("/api/camera/status")
    c.get("/api/camera/status")
    assert len(calls) == 1                       # short fail-cache: no hammering
    cached._fail_until = 0                       # window passed
    c.get("/api/camera/status")
    assert len(calls) == 2


def test_default_pipeline_off_on_app_build():
    # a fresh app must never auto-start the pipeline (standing decision)
    fp = FakePipeline()
    cam_client(pipeline=fp)
    assert fp.started == 0


# ---- cloud feed history (Tuya device log, ticket-02 extension) ----

def cloud_client(rows=None, fail=False, tmp_path=None):
    def fetch():
        if fail:
            raise RuntimeError("tuya down")
        return rows or []

    f = FakeFeeder()
    d = dispatcher.Dispatcher(f, log_path=str(tmp_path / "feed.jsonl"),
                              notify=lambda m: None)
    return tc.TestClient(hub.create_app(feeder_backend=f, dispatcher=d,
                                        cloud_history=hub.CachedFeedHistory(
                                            ttl=300, fetch=fetch),
                                        pipeline=FakePipeline())), tmp_path


def test_cloud_history_maps_dispense_events_only(tmp_path):
    # feed_report = the device's own per-dispense event (official-app parity,
    # incl. physical-button feeds); everything else is noise
    rows = [
        {"code": "light", "value": "true", "event_time": 1792380000000,
         "event_from": "APP"},
        {"code": "manual_feed", "value": "1", "event_time": 1792380564000,
         "event_from": "cloud2cloud"},          # command — feed_report follows
        {"code": "feed_report", "value": "1", "event_time": 1792380565000,
         "event_from": "1"},
        {"code": "feed_report", "value": "3", "event_time": 1792379900000,
         "event_from": "1"},                    # physical-button feeding
        {"code": "feed_report", "value": "0", "event_time": 1792370000000,
         "event_from": "1"},                    # zero — not a feeding
        {"code": "feed_state", "value": "standby", "event_time": 1792370001000,
         "event_from": "1"},
    ]
    c, tmp = cloud_client(rows, tmp_path=tmp_path)
    r = c.get("/api/feeder/cloud_history")
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert len(entries) == 2                            # only real dispenses
    assert entries[0]["portions"] == 1                  # newest first
    assert entries[0]["source"] == "1"
    assert entries[1]["portions"] == 3


def test_cloud_history_merges_local_feeds_without_dupes(tmp_path):
    # cloud list lags (cache + Tuya propagation): the app's own 20:44 feed
    # must appear immediately, and not duplicate once Tuya catches up
    rows = [
        {"code": "feed_report", "value": "1", "event_time": 1792380565000,
         "event_from": "1"},                    # 11:29 — matches local row
    ]
    c, tmp = cloud_client(rows, tmp_path=tmp_path)
    feedlog.append({"ts": "2026-10-19 20:44:52", "command": "feed",
                    "portions": 5, "result": "ok", "source": "cloud",
                    "latency_ms": 9197}, path=str(tmp_path / "feed.jsonl"))
    feedlog.append({"ts": "2026-10-19 11:29:26", "command": "feed",
                    "portions": 1, "result": "ok", "source": "cloud",
                    "latency_ms": 3613}, path=str(tmp_path / "feed.jsonl"))
    entries = c.get("/api/feeder/cloud_history").json()["entries"]
    assert [(e["ts"], e["portions"]) for e in entries] == [
        ("2026-10-19 20:44", 5),                # local-only row surfaced
        ("2026-10-19 11:29", 1),                # cloud row, no duplicate
    ]


def test_cloud_history_failure_is_graceful(tmp_path):
    c, _ = cloud_client(fail=True, tmp_path=tmp_path)
    r = c.get("/api/feeder/cloud_history")
    assert r.status_code == 502
    assert "unavailable" in r.json()["error"]


def test_cloud_history_cached(tmp_path):
    calls = []

    def counting():
        calls.append(1)
        return [{"code": "feed_report", "value": "2",
                 "event_time": 1792380564534, "event_from": "1"}]

    f = FakeFeeder()
    d = dispatcher.Dispatcher(f, log_path=str(tmp_path / "feed.jsonl"),
                              notify=lambda m: None)
    cached = hub.CachedFeedHistory(ttl=300, fetch=counting)
    c = tc.TestClient(hub.create_app(feeder_backend=f, dispatcher=d,
                                     cloud_history=cached,
                                     pipeline=FakePipeline()))
    c.get("/api/feeder/cloud_history")
    c.get("/api/feeder/cloud_history")
    assert len(calls) == 1
    c.post("/api/feeder/feed", json={"portions": 1})   # app fed -> invalidate
    c.get("/api/feeder/cloud_history")
    assert len(calls) == 2
