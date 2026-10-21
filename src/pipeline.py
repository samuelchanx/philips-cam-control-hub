"""Camera pipeline manager — dog_final.py + renderer.py as owned subprocesses.

State machine: stopped → starting → live → stopping → error. The pipeline is
OFF on every manager construction (server start) — on-demand only, never 24/7
(standing user decision 2026-10-18). start() mkdirs the dump dir first (known
reboot gotcha: /tmp is wiped) and recovers orphans from a previous server run
via a pidfile, so a restarted server never leaves an unnoticed pipeline.
Idle auto-stop: no camera API touch for idle_timeout_s (default 5 min) stops it.
"""
import os
import signal
import subprocess
import sys
import threading
import time

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DUMP_DIR = "/tmp/philips_apk/p2p"
DUMP_NAME = "ch1_video.bin"
DEFAULT_FRAME_PATH = "/tmp/dog/live_latest.jpg"
DEFAULT_IDLE_S = 300
FRESH_S = 90          # frame younger than this ⇒ camera is producing
STALE_S = 30          # no dump growth this long while live ⇒ stream frozen


class PipelineManager:

    def __init__(self, idle_timeout_s=DEFAULT_IDLE_S, popen=None,
                 dump_dir=DEFAULT_DUMP_DIR, frame_path=DEFAULT_FRAME_PATH,
                 auto_restart=True):
        self.idle_timeout_s = idle_timeout_s
        self._popen = popen or self._real_popen
        self._dump_dir = dump_dir
        self._frame_path = frame_path
        self._pidfile = os.path.join(dump_dir, "hub_pipeline.pids")
        self._state = "stopped"
        self._error = None
        self._last_exit = None
        self._procs = []
        self._last_touch = None
        self._auto_restart = auto_restart
        self._last_fail = None
        self._no_frame_retries = 0
        self._recording = False
        self._lock = threading.RLock()
        self._recover_orphans()

    # -- lifecycle --

    def start(self, _auto=False):
        with self._lock:
            if self._state not in ("stopped", "error"):
                return self._state
            if not _auto:
                self._no_frame_retries = 0   # fresh attempt on a manual start
            os.makedirs(self._dump_dir, exist_ok=True)   # the reboot gotcha
            # fresh dump per session: appended leftovers from a previous run
            # carry stale packet headers that misalign NAL parsing
            try:
                open(os.path.join(self._dump_dir, DUMP_NAME), "wb").close()
            except OSError:
                pass
            logfile = os.path.join(self._dump_dir, "hub_pipeline.log")
            procs = []
            try:
                procs.append(self._popen("dog_final.py", logfile))
                procs.append(self._popen("renderer.py", logfile))
            except Exception:
                for p in procs:                          # don't leak a half-start
                    self._signal(p, signal.SIGTERM)
                raise
            self._procs = procs
            self._write_pids()
            self._state = "starting"
            self._error = None
            self._last_touch = time.time()
            self._started_at = time.monotonic()
        threading.Thread(target=self._watch, daemon=True).start()
        return self._state

    def stop(self):
        with self._lock:
            if self._state == "stopped":
                return self._state
            procs, self._procs = self._procs, []
            self._state = "stopping"
        for p in procs:
            self._signal(p, signal.SIGTERM)
        for p in procs:
            try:
                p.wait(5)
            except Exception:
                self._signal(p, signal.SIGKILL)
                try:
                    p.wait(5)                        # reap, no zombies
                except Exception:
                    pass
        with self._lock:
            self._state = "stopped"
            self._remove_pids()
        return "stopped"

    def touch(self):
        self._last_touch = time.time()

    def set_recording(self, on):
        """Motion-recording mode (ticket 07): while on, the idle auto-stop is
        suspended so clips keep capturing. Never starts the pipeline itself,
        and resets on server restart (plain attribute — standing decision:
        pipeline is on-demand only)."""
        with self._lock:
            self._recording = bool(on)

    @property
    def recording(self):
        with self._lock:
            return self._recording

    @property
    def frame_path(self):
        return self._frame_path

    def status(self):
        with self._lock:
            return {"state": self._state, "error": self._error,
                    "frame_age_s": self.frame_age_s(),
                    "stream_age_s": self.stream_age_s(),
                    "idle_timeout_s": self.idle_timeout_s,
                    "no_frame_retries": self._no_frame_retries}

    def frame_age_s(self):
        try:
            return round(time.time() - os.path.getmtime(self._frame_path), 1)
        except OSError:
            return None

    def stream_age_s(self):
        """Age of the raw P2P dump's last write. The renderer rewrites
        live_latest.jpg every cycle even when decoding a frozen tail, so the
        jpg mtime is NOT a liveness signal — the dump only grows while the
        camera actually sends (found 2026-10-20: a talk play froze the whole
        inbound stream and the page showed a stale frame for 2h)."""
        try:
            return round(time.time() - os.path.getmtime(
                os.path.join(self._dump_dir, DUMP_NAME)), 1)
        except OSError:
            return None

    # -- internals --

    def _real_popen(self, script, logfile):
        logf = open(logfile, "ab")
        env = dict(os.environ)
        # launchd PATH lacks Homebrew — children need ffmpeg
        env["PATH"] = "/opt/homebrew/bin:" + env.get("PATH", "")
        return subprocess.Popen(
            [sys.executable, "-u", os.path.join(SRC_DIR, script)],
            cwd=SRC_DIR, stdout=logf, stderr=subprocess.STDOUT,
            env=env, start_new_session=True)

    def _signal(self, p, sig):
        try:
            os.killpg(os.getpgid(p.pid), sig)   # whole tree incl. ffmpeg kids
            return
        except Exception:
            pass
        try:
            if sig == signal.SIGKILL:
                p.kill()
            else:
                p.terminate()
        except Exception:
            pass

    def _try_auto_restart(self, reason, no_frame=False):
        """Kill any survivors, record the reason, restart if the 60s
        cooldown allows. Returns True when a restart was started."""
        with self._lock:
            self._last_exit = reason
            if no_frame:
                self._no_frame_retries += 1
            print("[pipeline]", reason, "- auto-restart", flush=True)
            cooldown_ok = (self._auto_restart and
                           (self._last_fail is None or
                            time.time() - self._last_fail > 60))
            self._last_fail = time.time()
            procs, self._procs = self._procs, []
            self._remove_pids()
        for p in procs:                      # stuck handshake: procs alive
            self._signal(p, signal.SIGTERM)
            try:
                p.wait(3)
            except Exception:
                self._signal(p, signal.SIGKILL)
        if cooldown_ok:
            with self._lock:
                self._state = "stopped"
            self.start(_auto=True)
            return True
        with self._lock:
            self._state = "error"
            self._error = reason
        return False

    def _watch(self):
        while True:
            time.sleep(2)
            with self._lock:
                if self._state in ("stopped", "stopping"):
                    return
                procs = list(self._procs)
            if (self._state == "error" and self._last_fail
                    and time.time() - self._last_fail > 60):
                if self.recording:
                    # recording mode promised clip continuity — retry after
                    # the cooldown instead of erroring forever (error used to
                    # be a dead end: the 2026-10-20 pytest orphan-kill left
                    # the pipeline errored for hours)
                    with self._lock:
                        self._state = "stopped"
                        self._error = None
                    self.start(_auto=True)
                    return
            alive = [p for p in procs if p.poll() is None]
            if self._state == "starting" and self._frame_fresh():
                with self._lock:
                    self._state = "live"
                    self._no_frame_retries = 0   # producing again: hint must clear
            elif procs and not alive:
                # both children died without a stop() — log exit codes and
                # self-heal so the live view recovers itself
                rcs = [p.poll() for p in procs]
                if self._try_auto_restart("pipeline exited (rc %s)" % rcs):
                    return
            elif (self._state == "live" and self._stream_frozen()):
                # inbound froze (e.g. after a talk play) while children live:
                # the only cure is a fresh session
                if self._try_auto_restart(
                        "no stream data %ds (frozen)" % STALE_S, no_frame=True):
                    return
            elif (self._state == "starting" and self._started_at
                  and time.monotonic() - self._started_at > 90):
                # stuck handshake (e.g. camera unreachable) — retry instead
                # of hanging in "starting" forever
                if self._try_auto_restart("no frame 90s after start",
                                          no_frame=True):
                    return
            if (self._last_touch
                    and time.time() - self._last_touch > self.idle_timeout_s
                    and not self.recording):
                self.stop()
                return

    def _frame_fresh(self):
        age = self.frame_age_s()
        return age is not None and age <= FRESH_S

    def _stream_frozen(self):
        age = self.stream_age_s()
        return age is None or age > STALE_S

    # -- pidfile orphan recovery (server restart must leave nothing running) --

    def _recover_orphans(self):
        try:
            with open(self._pidfile) as f:
                pids = [int(x) for x in f.read().split()]
        except Exception:
            return
        for pid in pids:
            if pid <= 0:
                continue
            try:
                out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                                     capture_output=True, text=True,
                                     timeout=5).stdout
            except Exception:
                continue
            # match the absolute script path our Popen uses — never an editor
            # that merely has the filename open
            if (os.path.join(SRC_DIR, "dog_final.py") in out
                    or os.path.join(SRC_DIR, "renderer.py") in out):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except Exception:
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except Exception:
                        pass
        self._remove_pids()

    def _write_pids(self):
        try:
            with open(self._pidfile, "w") as f:
                f.write(" ".join(str(p.pid) for p in self._procs))
        except Exception:
            pass

    def _remove_pids(self):
        try:
            os.remove(self._pidfile)
        except OSError:
            pass
