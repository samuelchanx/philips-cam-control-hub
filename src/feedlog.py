"""Append-only feed-action log (JSONL).

One row per food-dispensing action:
    {"ts": ..., "command": ..., "portions": ..., "result": ...,
     "source": "cloud"|"lan", "latency_ms": ...}
"""
import json
import os
import threading

LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "feed_log.jsonl")

_lock = threading.Lock()


def append(row, path=LOG_PATH):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def recent(n=20, path=LOG_PATH):
    """Newest-first last n rows; corrupt lines are skipped, never rewritten."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        if len(out) >= n:
            break
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out
