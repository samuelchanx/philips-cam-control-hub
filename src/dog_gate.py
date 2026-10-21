"""Dog-presence checker — macOS Vision via pyobjc (Samuel, 2026-10-21).

Samuel's rule: motion clips are only worth keeping when the dog is actually
in frame — pixel-diff cannot tell IR night noise from the dog (93% of the
first 291 clips had no dog at all). This wraps Apple's VNRecognizeAnimals
Request (the same on-device classifier Photos uses); the frame classified
is dog_final's live JPEG, so both the recorder gate and the telegram filter
see exactly what the camera sees.

Env: DOG_GATE=0 disables gating; DOG_CONF_MIN (default 0.3) tunes
sensitivity; DOG_FRAME overrides the frame path.

Fail-open contract: check_frame RAISES on any error — callers decide. Both
callers (motion_recorder's clip gate, dog_final's telegram) treat failure
as "gate open", so a broken classifier never loses real dog footage.
"""
import os
import time

DOG_CONF_MIN = float(os.environ.get("DOG_CONF_MIN", "0.3"))
DOG_FRAME = os.environ.get("DOG_FRAME", "/tmp/dog/live_latest.jpg")


def gate_enabled():
    return os.environ.get("DOG_GATE", "1") != "0"


def check_frame(path=DOG_FRAME, retries=1, retry_delay_s=1.2):
    """(confidence, species) of the best animal observation, or (0.0, "")
    when the frame has none. One retry by default: the renderer rotates
    live_latest.jpg with two os.replace calls, so a read can land in the
    tiny window where the file is momentarily absent (hit live 2026-10-21
    13:52 → needless fail-open clip). Vision import is lazy — pytest runs
    fine on machines without pyobjc, and callers inject fakes instead."""
    last = None
    for attempt in range(retries + 1):
        try:
            return _classify(path)
        except Exception as e:            # noqa: BLE001 — fail-open contract
            last = e
            if attempt < retries:
                time.sleep(retry_delay_s)
    raise last


def _classify(path):
    import Quartz
    import Vision
    from Foundation import NSURL

    src = Quartz.CGImageSourceCreateWithURL(NSURL.fileURLWithPath_(path), None)
    if src is None:
        raise IOError(f"unreadable image: {path}")
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        raise IOError(f"undecodable image: {path}")
    req = Vision.VNRecognizeAnimalsRequest.alloc().init()
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        img, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(f"Vision failed: {err}")
    best = (0.0, "")
    for obs in (req.results() or []):
        if obs.confidence() > best[0]:
            labels = obs.labels() or []
            best = (obs.confidence(), labels[0].identifier() if labels else "")
    return best
