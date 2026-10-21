"""control-hub ticket 04 — LAN feeder control, cloud fallback, guards on LAN."""
import dispatcher
import feeder_lan
from feeder_lan import AutoFeeder, LanFeeder, LanNotSupported


class FakeTinytuyaDev:
    def __init__(self, ack_dps=None, fail=False):
        self.ack_dps = ack_dps or {}
        self.fail = fail
        self.sets = []

    def set_socketTimeout(self, s):
        pass

    def status(self):
        if self.fail:
            raise OSError("lan unreachable")
        return {"dps": {"3": 1, "4": "standby", "13": 0, "17": True, "23": False}}

    def set_value(self, dp, value):
        self.sets.append((dp, value))
        if self.fail:
            return None
        if str(dp) in self.ack_dps:
            return {"dps": {str(dp): value}}
        return None


def make_lan(ack_dps=None, fail=False, monkeypatch=None):
    dev = FakeTinytuyaDev(ack_dps=ack_dps, fail=fail)
    if monkeypatch:
        monkeypatch.setattr(feeder_lan.tinytuya, "Device",
                            lambda *a, **k: dev)
    lan = LanFeeder(device_id="id", local_key="key", address="1.2.3.4")
    return lan, dev


def test_lan_status_maps_dps(monkeypatch):
    lan, _ = make_lan(monkeypatch=monkeypatch)
    st = lan.status()
    assert st == {"online": True, "battery_percentage": 0, "feed_state": "standby",
                  "light": True, "slow_feed": False}


def test_lan_feed_not_supported(monkeypatch):
    lan, _ = make_lan(monkeypatch=monkeypatch)
    try:
        lan.feed(1)
        raise AssertionError("feed must not be supported over LAN")
    except LanNotSupported:
        pass


def test_lan_toggle_uses_verified_dps(monkeypatch):
    lan, dev = make_lan(ack_dps={"17": True, "23": False},
                        monkeypatch=monkeypatch)
    lan.toggle("light", True)
    lan.toggle("slow_feed", False)
    assert dev.sets == [(17, True), (23, False)]


def test_lan_quick_feed_disabled_until_trigger_identified(monkeypatch):
    lan, dev = make_lan(monkeypatch=monkeypatch)
    try:
        lan.quick_feed()
        raise AssertionError("LAN dispense must stay disabled")
    except LanNotSupported:
        pass
    assert dev.sets == []                       # nothing written


def test_lan_never_writes_factory_reset_dp(monkeypatch):
    lan, dev = make_lan(monkeypatch=monkeypatch)
    for code in ("factory_reset", "delTs", "unknown_dp"):
        try:
            lan.toggle(code, True)
            raise AssertionError("%r must be refused" % code)
        except LanNotSupported:
            pass
    assert dev.sets == []                       # nothing reached the device


def test_lan_set_without_ack_raises(monkeypatch):
    lan, _ = make_lan(ack_dps={}, monkeypatch=monkeypatch)   # nothing acks
    try:
        lan.toggle("light", True)
        raise AssertionError("no-ack must raise")
    except RuntimeError:
        pass


def test_autofeeder_lan_success_reports_lan():
    class Ok:
        source_name = "lan"

        def feed(self, p):
            return "done"

    class MustNotCall:
        def feed(self, p):
            raise AssertionError("cloud must not be called when LAN works")

    auto = AutoFeeder(Ok(), MustNotCall())
    assert auto.feed(1) == "done"
    assert auto.source_name == "lan"


def test_autofeeder_falls_back_to_cloud_on_any_lan_failure():
    class Dead:
        source_name = "lan"

        def feed(self, p):
            raise OSError("lan down")

        def status(self):
            raise OSError("lan down")

    class Cloud:
        def __init__(self):
            self.fed = []

        def feed(self, p):
            self.fed.append(p)
            return "done"

        def status(self):
            return {"online": True}

    cloud = Cloud()
    auto = AutoFeeder(Dead(), cloud)
    assert auto.feed(1) == "done"               # user sees success either way
    assert cloud.fed == [1]
    assert auto.source_name == "cloud"
    auto.status()
    assert auto.source_name == "cloud"


def test_dispatcher_logs_source_from_autofeeder(tmp_path):
    class LanOk:
        source_name = "lan"

        def feed(self, p):
            return "done"

    rows = []
    d = dispatcher.Dispatcher(LanOk(), log_path=str(tmp_path / "f.jsonl"))
    out = d.run("feed", 2)
    assert out["source"] == "lan"
    import feedlog
    assert feedlog.recent(1, path=str(tmp_path / "f.jsonl"))[0]["source"] == "lan"


def test_dispatcher_guards_apply_on_lan_path(tmp_path):
    class LanBackend:
        source_name = "lan"

        def feed(self, p):
            return "done"

    d = dispatcher.Dispatcher(LanBackend(), log_path=str(tmp_path / "f.jsonl"))
    try:
        d.run("factory_reset")
        raise AssertionError("factory_reset refused on the LAN path too")
    except dispatcher.Rejected:
        pass
    out = d.run("feed", 99)                      # clamp unchanged
    assert out["portions"] == 36 and out["source"] == "lan"


def test_quick_feed_while_feed_busy_gets_409(tmp_path):
    import threading as th

    class SlowFeed:
        source_name = "cloud"
        started = th.Event()
        release = th.Event()

        def feed(self, p):
            self.started.set()
            self.release.wait(3)
            return "done"

        def quick_feed(self):
            raise AssertionError("quick_feed must be refused while busy")

    b = SlowFeed()
    d = dispatcher.Dispatcher(b, log_path=str(tmp_path / "f.jsonl"))

    out = {}

    def bg():
        out["r"] = d.run("feed", 1)

    t = th.Thread(target=bg)
    t.start()
    assert b.started.wait(2)
    try:
        d.run("quick_feed")
        raise AssertionError("quick_feed during a feed must be rejected")
    except dispatcher.Busy:
        pass
    b.release.set()
    t.join(5)
    assert out["r"]["feed_state"] == "done"


def test_autofeeder_failure_row_attributes_cloud_not_stale_lan(tmp_path):
    class LanDead:
        source_name = "lan"

        def feed(self, p):
            raise OSError("lan down")

    class CloudDead:
        def feed(self, p):
            raise RuntimeError("cloud down too")

    auto = feeder_lan.AutoFeeder(LanDead(), CloudDead())
    d = dispatcher.Dispatcher(auto, log_path=str(tmp_path / "f.jsonl"))
    try:
        d.run("feed", 1)
        raise AssertionError("double failure must surface as BackendError")
    except dispatcher.BackendError:
        pass
    import feedlog
    row = feedlog.recent(1, path=str(tmp_path / "f.jsonl"))[0]
    assert row["result"] == "failed" and row["source"] == "cloud"


def test_flag_off_is_cloud_only(monkeypatch):
    import hub
    monkeypatch.setattr(hub, "load_env", lambda: {})
    assert isinstance(hub.make_feeder_backend(), hub.CloudFeeder)


def test_flag_on_builds_autofeeder(monkeypatch):
    import hub

    class FakeLan:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr(hub, "load_env", lambda: {"FEEDER_LAN": "on"})
    monkeypatch.setattr("feeder_lan.LanFeeder", FakeLan)
    out = hub.make_feeder_backend()
    assert isinstance(out, feeder_lan.AutoFeeder)


def test_flag_on_without_local_key_falls_back_to_cloud(monkeypatch):
    import hub
    monkeypatch.setattr(hub, "load_env", lambda: {"FEEDER_LAN": "on"})
    monkeypatch.setattr(feeder_lan, "feeder",
                        type("F", (), {"ENV": {}}))   # no TUYA_LOCAL_KEY
    out = hub.make_feeder_backend()
    assert isinstance(out, hub.CloudFeeder)
