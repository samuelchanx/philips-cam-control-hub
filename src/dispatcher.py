"""Central command dispatcher — the ONLY path from the webapp to device commands.

Policy lives here: command whitelist, factory_reset hard deny, portions clamp
1–36, feed-log rows, Telegram notices, latency measurement, one-feed-at-a-time.
Execution is the backend's job (cloud today, LAN fallback in ticket 04).
"""
import threading
import time

import feedlog

COMMANDS = {"feed", "quick_feed", "light", "slow_feed", "status"}
NEVER_SEND = {"factory_reset"}
MIN_PORTIONS = 1
MAX_PORTIONS = 36


class Rejected(Exception):
    """Command refused by policy (whitelist / factory_reset / bad value)."""


class Busy(Exception):
    """A feed is already in flight."""


class BackendError(Exception):
    """Backend (cloud/LAN) call failed."""


class Dispatcher:

    def __init__(self, backend, source="cloud",
                 log_path=feedlog.LOG_PATH, notify=None):
        self.backend = backend
        self.source = source
        self.log_path = log_path
        self.notify = notify or (lambda msg: None)
        self._feed_lock = threading.Lock()

    def _source(self):
        # backends may switch at runtime (LAN→cloud fallback)
        return getattr(self.backend, "source_name", self.source)

    def run(self, cmd, value=None):
        if cmd in NEVER_SEND or cmd not in COMMANDS:
            raise Rejected("command not allowed: %r" % cmd)
        if cmd == "feed":
            return self._feed(value)
        if cmd == "quick_feed":
            return self._quick_feed()
        if cmd == "status":
            return self._call(lambda: self.backend.status())
        return self._call(lambda: self.backend.toggle(cmd, bool(value)))

    # -- feed actions (logged + telegram) --

    def _feed(self, portions):
        try:
            p = int(portions)
        except (TypeError, ValueError):
            raise Rejected("portions must be an integer")
        p = max(MIN_PORTIONS, min(MAX_PORTIONS, p))
        if not self._feed_lock.acquire(blocking=False):
            raise Busy("feed already in flight")
        try:
            t0 = time.monotonic()
            try:
                state = self.backend.feed(p)
            except Exception:
                self._log("feed", p, "failed", time.monotonic() - t0)
                self.notify("❌ 出糧失敗（%d 份，source=%s）" % (p, self._source()))
                raise BackendError("feed failed")
            latency = time.monotonic() - t0
            self._log("feed", p, "ok", latency)
            self.notify("🍖 出咗 %d 份糧（feed_state=%s, source=%s）" % (p, state, self._source()))
            return {"portions": p, "feed_state": state,
                    "source": self._source(), "latency_ms": int(latency * 1000)}
        finally:
            self._feed_lock.release()

    def _quick_feed(self):
        if not self._feed_lock.acquire(blocking=False):
            raise Busy("feed already in flight")
        try:
            t0 = time.monotonic()
            try:
                self.backend.quick_feed()
            except Exception:
                self._log("quick_feed", None, "failed", time.monotonic() - t0)
                self.notify("❌ quick_feed 失敗（source=%s）" % self._source())
                raise BackendError("quick_feed failed")
            latency = time.monotonic() - t0
            self._log("quick_feed", None, "ok", latency)
            self.notify("🍖 quick_feed 已發送（source=%s）" % self._source())
            return {"command": "quick_feed", "source": self._source(),
                    "latency_ms": int(latency * 1000)}
        finally:
            self._feed_lock.release()

    # -- helpers --

    def _call(self, fn):
        try:
            return fn()
        except Exception:
            raise BackendError("backend call failed")

    def _log(self, command, portions, result, latency_s):
        feedlog.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "command": command,
            "portions": portions,
            "result": result,
            "source": self._source(),
            "latency_ms": int(latency_s * 1000),
        }, path=self.log_path)
