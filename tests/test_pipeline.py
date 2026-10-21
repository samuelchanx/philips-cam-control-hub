"""control-hub ticket 03 — pipeline manager lifecycle (fake subprocess only)."""
import os
import time

import pipeline


class FakePopen:
    _next_pid = 1000

    def __init__(self, script, logfile):
        self.script = script
        self.logfile = logfile
        self.pid = FakePopen._next_pid
        FakePopen._next_pid += 1
        self._returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        return self._returncode or 0

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def kill(self):
        self.killed = True
        self._returncode = 0


def make_manager(tmp_path, idle_timeout_s=300, spawn=None):
    created = []
    popen = spawn or (lambda script, logfile: created.append(FakePopen(script, logfile)) or created[-1])
    frame = tmp_path / "live_latest.jpg"
    mgr = pipeline.PipelineManager(idle_timeout_s=idle_timeout_s, popen=popen,
                                   dump_dir=str(tmp_path / "dump"),
                                   frame_path=str(frame))
    return mgr, created, frame


def test_fresh_manager_is_stopped_and_spawns_nothing(tmp_path):
    mgr, created, _ = make_manager(tmp_path)
    assert mgr.status()["state"] == "stopped"
    assert created == []


def test_start_creates_dump_dir_and_two_procs_starting(tmp_path):
    mgr, created, _ = make_manager(tmp_path)
    assert mgr.start() == "starting"
    assert len(created) == 2
    assert {p.script for p in created} == {"dog_final.py", "renderer.py"}
    assert os.path.isdir(mgr._dump_dir)          # reboot gotcha: dump dir exists
    assert mgr.start() == "starting"             # idempotent while not stopped
    assert len(created) == 2


def test_starting_to_live_on_fresh_frame(tmp_path):
    mgr, created, frame = make_manager(tmp_path)
    mgr.start()
    frame.write_bytes(b"\xff\xd8fakejpeg")
    for _ in range(10):
        if mgr.status()["state"] == "live":
            break
        time.sleep(0.3)
    assert mgr.status()["state"] == "live"
    assert mgr.status()["frame_age_s"] is not None


def test_procs_dying_auto_restarts(tmp_path):
    mgr, created, _ = make_manager(tmp_path)
    mgr.start()
    for p in created:
        p.terminate()                            # simulates crash exit
    for _ in range(20):
        if len(created) >= 4:                    # auto-restart spawned a new pair
            break
        time.sleep(0.3)
    assert len(created) >= 4                     # fresh procs spawned
    assert mgr.status()["state"] in ("starting", "live")
    assert "rc" in (mgr._last_exit or "")              # exit codes recorded
    mgr.stop()


def test_auto_restart_cooldown_then_error(tmp_path):
    mgr, created, _ = make_manager(tmp_path)
    mgr._last_fail = time.time()                 # cooldown just fired
    mgr.start()
    for p in created:
        p.terminate()
    for _ in range(15):
        if mgr.status()["state"] == "error":
            break
        time.sleep(0.3)
    assert mgr.status()["state"] == "error"      # no restart within cooldown
    assert "rc" in mgr.status()["error"]         # exit codes recorded
    mgr.stop()
    assert all(p.terminated for p in created)


def test_stuck_starting_auto_restarts_after_90s(tmp_path):
    # handshake stalls (no frame ever): watcher must retry, not hang forever
    mgr, created, _ = make_manager(tmp_path)
    mgr.start()
    mgr._started_at = time.monotonic() - 91       # backdate: 90s elapsed
    for _ in range(20):
        if len(created) >= 4:
            break
        time.sleep(0.3)
    assert len(created) >= 4                      # restarted with fresh procs
    assert mgr.status()["state"] == "starting"
    assert "no frame" in (mgr._last_exit or "")
    mgr.stop()


def test_no_frame_retries_counted_and_cleared_on_live(tmp_path):
    mgr, created, frame = make_manager(tmp_path)
    mgr.start()
    mgr._started_at = time.monotonic() - 91
    for _ in range(20):
        if len(created) >= 4:
            break
        time.sleep(0.3)
    assert mgr.status()["no_frame_retries"] == 1   # drives the "offline" hint
    frame.write_bytes(b"\xff\xd8fakejpeg")         # camera comes back
    for _ in range(10):
        if mgr.status()["state"] == "live":
            break
        time.sleep(0.3)
    assert mgr.status()["state"] == "live"
    assert mgr.status()["no_frame_retries"] == 0   # hint must not linger
    mgr.stop()


def test_manual_start_resets_no_frame_retries(tmp_path):
    mgr, created, _ = make_manager(tmp_path)
    mgr._no_frame_retries = 5
    mgr.start()
    assert mgr.status()["no_frame_retries"] == 0   # fresh attempt
    mgr.stop()


def test_stuck_starting_cooldown_then_error(tmp_path):
    mgr, created, _ = make_manager(tmp_path)
    mgr._last_fail = time.time()
    mgr.start()
    mgr._started_at = time.monotonic() - 91
    for _ in range(15):
        if mgr.status()["state"] == "error":
            break
        time.sleep(0.3)
    assert mgr.status()["state"] == "error"
    assert "no frame" in mgr.status()["error"]
    mgr.stop()


def test_stop_terminates_both_procs(tmp_path):
    mgr, created, _ = make_manager(tmp_path)
    mgr.start()
    assert mgr.stop() == "stopped"
    assert all(p.terminated for p in created)
    assert mgr.status()["state"] == "stopped"
    assert not os.path.exists(mgr._pidfile)
    assert mgr.stop() == "stopped"               # idempotent


def test_idle_timeout_stops_pipeline(tmp_path):
    mgr, created, _ = make_manager(tmp_path, idle_timeout_s=1)
    mgr.start()
    for _ in range(20):
        if mgr.status()["state"] == "stopped":
            break
        time.sleep(0.5)
    assert mgr.status()["state"] == "stopped"
    assert all(p.terminated for p in created)


def test_recording_mode_blocks_idle_stop(tmp_path):
    # ticket 07: recording mode suspends the idle auto-stop
    mgr, created, _ = make_manager(tmp_path, idle_timeout_s=1)
    assert mgr.recording is False                    # default off (restart-safe)
    mgr.set_recording(True)
    assert mgr.recording is True
    mgr.start()
    for _ in range(10):
        time.sleep(0.5)
    assert mgr.status()["state"] in ("starting", "live")
    mgr.set_recording(False)
    for _ in range(20):
        if mgr.status()["state"] == "stopped":
            break
        time.sleep(0.5)
    assert mgr.status()["state"] == "stopped"        # off → idle-stop resumes


def test_recording_mode_never_starts_pipeline(tmp_path):
    mgr, created, _ = make_manager(tmp_path)
    mgr.set_recording(True)
    assert created == []                             # no auto-start, ever
    assert mgr.status()["state"] == "stopped"


def test_live_frozen_stream_auto_restarts(tmp_path):
    # 2026-10-20: a talk play froze the inbound stream while children stayed
    # alive — the page then showed one stale frame for 2h. The watcher must
    # notice the dump stopped growing and restart the session.
    mgr, created, frame = make_manager(tmp_path)
    mgr.start()
    frame.write_bytes(b"\xff\xd8fakejpeg")
    for _ in range(10):
        if mgr.status()["state"] == "live":
            break
        time.sleep(0.3)
    assert mgr.status()["state"] == "live"
    dump = os.path.join(mgr._dump_dir, "ch1_video.bin")
    old = time.time() - 60
    os.utime(dump, (old, old))                    # inbound silent for 60s
    for _ in range(20):
        if len(created) >= 4:
            break
        time.sleep(0.3)
    assert len(created) >= 4                      # fresh session spawned
    assert "frozen" in (mgr._last_exit or "")
    assert mgr.status()["no_frame_retries"] == 1  # offline hint shows
    mgr.stop()


def test_error_state_allows_manual_start(tmp_path):
    # 2026-10-20: start() refused to run from "error" — the UI start button
    # was a dead end and the pipeline stayed errored for hours
    mgr, created, _ = make_manager(tmp_path)
    mgr._last_fail = time.time()
    mgr.start()
    for p in created:
        p.terminate()
    for _ in range(15):
        if mgr.status()["state"] == "error":
            break
        time.sleep(0.3)
    assert mgr.status()["state"] == "error"
    assert mgr.start() == "starting"             # manual re-arm works
    mgr.stop()


def test_error_with_recording_auto_restarts_after_cooldown(tmp_path):
    # recording mode promised clip continuity — an errored pipeline must
    # retry by itself once the 60s cooldown passes
    mgr, created, _ = make_manager(tmp_path)
    mgr._last_fail = time.time()
    mgr.start()
    for p in created:
        p.terminate()
    for _ in range(15):
        if mgr.status()["state"] == "error":
            break
        time.sleep(0.3)
    assert mgr.status()["state"] == "error"
    mgr.set_recording(True)
    mgr._last_fail = time.time() - 61            # cooldown long elapsed
    for _ in range(20):
        if len(created) >= 4:
            break
        time.sleep(0.3)
    assert len(created) >= 4                     # fresh session spawned
    assert mgr.status()["state"] in ("starting", "live")
    mgr.stop()


def test_touch_keeps_pipeline_alive(tmp_path):
    mgr, created, _ = make_manager(tmp_path, idle_timeout_s=1)
    mgr.start()
    for _ in range(6):
        time.sleep(0.4)
        mgr.touch()
    assert mgr.status()["state"] in ("starting", "live")
    mgr.stop()


def test_frame_age_from_mtime(tmp_path):
    mgr, _, frame = make_manager(tmp_path)
    assert mgr.frame_age_s() is None             # no frame yet
    frame.write_bytes(b"x")
    age = mgr.frame_age_s()
    assert age is not None and age < 5


def test_orphan_pidfile_kills_stale_pipeline(tmp_path):
    # A dummy process whose argv contains the ABSOLUTE script path (as our
    # Popen children have) + a "previous run" pidfile: manager init must
    # terminate it (server restart leaves nothing running).
    import subprocess as sp
    import sys as _sys
    dummy = sp.Popen([_sys.executable, "-c",
                      "import time; time.sleep(60)",
                      pipeline.SRC_DIR + "/dog_final.py"],
                     start_new_session=True)
    pidfile = tmp_path / "dump" / "hub_pipeline.pids"
    pidfile.parent.mkdir()
    pidfile.write_text(str(dummy.pid))
    pipeline.PipelineManager(dump_dir=str(tmp_path / "dump"),
                             frame_path=str(tmp_path / "f.jpg"))
    for _ in range(20):
        if dummy.poll() is not None:
            break
        time.sleep(0.3)
    assert dummy.poll() is not None              # orphan was killed
    assert not pidfile.exists()                  # pidfile cleared


def test_orphan_pidfile_spares_unrelated_process(tmp_path):
    # A process NOT matching dog_final/renderer must be left alone.
    import subprocess as sp
    import sys as _sys
    dummy = sp.Popen([_sys.executable, "-c",
                      "import time; time.sleep(60)", "unrelated_job.py"],
                     start_new_session=True)
    pidfile = tmp_path / "dump" / "hub_pipeline.pids"
    pidfile.parent.mkdir()
    pidfile.write_text(str(dummy.pid))
    try:
        pipeline.PipelineManager(dump_dir=str(tmp_path / "dump"),
                                 frame_path=str(tmp_path / "f.jpg"))
        time.sleep(1)
        assert dummy.poll() is None              # still alive
    finally:
        dummy.kill()
        dummy.wait(5)
