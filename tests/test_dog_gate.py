"""dog_gate — retry-then-fail-open contract (no pyobjc needed: _classify
is monkeypatched; Vision import stays lazy inside it)."""
import pytest

import dog_gate


def test_check_frame_retries_then_succeeds(monkeypatch):
    calls = []
    def flaky(path):
        calls.append(path)
        if len(calls) == 1:
            raise IOError("unreadable image: mid-rotate")
        return (0.7, "Dog")
    monkeypatch.setattr(dog_gate, "_classify", flaky)
    monkeypatch.setattr(dog_gate.time, "sleep", lambda s: None)
    assert dog_gate.check_frame("/tmp/f.jpg") == (0.7, "Dog")
    assert calls == ["/tmp/f.jpg", "/tmp/f.jpg"]      # same path, twice


def test_check_frame_raises_after_retry_exhausted(monkeypatch):
    calls = []
    def always_broken(path):
        calls.append(1)
        raise IOError("still unreadable")
    monkeypatch.setattr(dog_gate, "_classify", always_broken)
    monkeypatch.setattr(dog_gate.time, "sleep", lambda s: None)
    with pytest.raises(IOError):
        dog_gate.check_frame("/tmp/f.jpg")
    assert len(calls) == 2                            # 1 try + 1 retry


def test_check_frame_no_retry_on_success(monkeypatch):
    calls = []
    def fine(path):
        calls.append(1)
        return (0.0, "")
    monkeypatch.setattr(dog_gate, "_classify", fine)
    assert dog_gate.check_frame("/tmp/f.jpg") == (0.0, "")
    assert len(calls) == 1
