"""LAN-direct feeder control (tinytuya local) — ticket 04, behind FEEDER_LAN.

Verified local dp map (2026-10-19, safe probes — 0 screens Booleans, 1 only
probes Integers, never touched the likely-factory_reset Boolean):
    dp2  quick_feed  (Boolean, acks)
    dp3  feed_report dp4  feed_state  dp13 battery_percentage  (status)
    dp14 unknown Integer, read-only (write ignored)
    dp17 light       (Boolean, acks)
    dp23 slow_feed   (Boolean, per cloud mapping)
    dp24 Boolean that acks 0 — likely factory_reset: NEVER write 1
    dp101 power mode (string status)
manual_feed has NO writable local dp on this firmware (probed dps 1–40:
only dp2/17/23+24 ack) → feed(portions) raises LanNotSupported and the
AutoFeeder wrapper falls back to cloud for precise portions.
"""
import tinytuya

import feeder

LAN_TIMEOUT_S = 4
NEVER_WRITE_DPS = (24,)          # likely factory_reset — hard local guard


class LanNotSupported(Exception):
    """This command has no LAN path on this firmware."""


class LanFeeder:

    source_name = "lan"
    DPS = {"quick_feed": 2, "light": 17, "slow_feed": 23}

    def __init__(self, device_id=None, local_key=None, address=None):
        self.device_id = device_id or feeder.ENV["TUYA_DEVICE_ID"]
        self.local_key = local_key or feeder.ENV.get("TUYA_LOCAL_KEY")
        self.address = address or feeder.ENV.get("TUYA_DEVICE_IP") or None
        if not self.local_key:
            raise RuntimeError("TUYA_LOCAL_KEY missing — set it in your env file")

    def _dev(self):
        d = tinytuya.Device(self.device_id, address=self.address,
                            local_key=self.local_key)
        d.set_socketTimeout(LAN_TIMEOUT_S)
        return d

    def status(self):
        dps = (self._dev().status() or {}).get("dps") or {}
        return {"online": True,          # it answered over LAN
                "battery_percentage": dps.get("13"),
                "feed_state": dps.get("4"),
                "light": dps.get("17"),
                "slow_feed": dps.get("23")}

    def feed(self, portions):
        raise LanNotSupported("manual_feed has no writable local dp on this firmware")

    def quick_feed(self):
        # DISABLED 2026-10-19: a value-1 write during the dp sweep hit an
        # unidentified trigger dp (5–16 range) and dispensed the 8-portion
        # preset unintentionally (device feed_report=8). Until the real
        # trigger dp is identified, LAN dispensing stays off — guessing more
        # writes is not worth the risk.
        raise LanNotSupported("LAN dispense disabled: trigger dp unidentified")

    def toggle(self, code, on):
        if code not in self.DPS:
            raise LanNotSupported("no LAN dp for %r" % code)
        self._set(code, bool(on))

    def _set(self, code, value):
        dp = self.DPS[code]
        if dp in NEVER_WRITE_DPS:
            raise LanNotSupported("dp %d is hard-denied locally" % dp)
        r = self._dev().set_value(dp, value)
        ack = bool(r and (r.get("dps") or (r.get("data") or {}).get("dps")))
        if not ack:
            raise RuntimeError("lan set %s (dp%d) got no ack" % (code, dp))
        return True


class AutoFeeder:
    """FEEDER_LAN on: try LAN first, fall back to cloud on ANY failure or
    unsupported command. source_name reflects what actually served the last
    call (lan on success, cloud on fallback) — the dispatcher logs it."""

    def __init__(self, lan, cloud):
        self.lan = lan
        self.cloud = cloud
        self.last_source = None

    @property
    def source_name(self):
        return self.last_source or "cloud"

    def _try(self, name, *args):
        try:
            out = getattr(self.lan, name)(*args)
            self.last_source = "lan"
            return out
        except Exception:
            self.last_source = "cloud"   # even if cloud also fails, name the route
            return getattr(self.cloud, name)(*args)

    def status(self):
        return self._try("status")

    def feed(self, portions):
        return self._try("feed", portions)

    def quick_feed(self):
        return self._try("quick_feed")

    def toggle(self, code, on):
        return self._try("toggle", code, on)
