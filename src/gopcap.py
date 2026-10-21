#!/usr/bin/env python3
"""gopcap.py — capture DRW video payloads WITH boundaries (len-prefixed).

Experiments 2026-10-19: video arrives on DRW channel d1 02 (vendor frames on
d1 00); the stock ack_drw hardcodes d1 00, so the camera never sees video
acks and retransmits every packet ~19x. ACK_CHAN_ECHO=1 echoes each D0's own
channel bytes in the ack — duplication collapses if that's the fix.

Output: /tmp/philips_apk/p2p/gop_capture.bin  (<u32 len><u16 idx><payload>...)
"""
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pyp2p_cam

pyp2p_cam.telegram = lambda msg: None    # testing — don't spam the group

from pyp2p_cam import P2PCam

OUT = "/tmp/philips_apk/p2p/gop_capture.bin"
DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 40
ACK_CHAN_ECHO = os.environ.get("ACK_CHAN_ECHO", "0") == "1"

os.makedirs(os.path.dirname(OUT), exist_ok=True)
fh = open(OUT, "wb")


class Cam(P2PCam):
    last_chan = bytes.fromhex("d100")
    idx_seen = []

    def ack_drw(self, index, addr, chan=bytes.fromhex("d100")):
        if ACK_CHAN_ECHO:
            chan = Cam.last_chan          # echo the inbound D0's channel
        pkt = bytes([0xF1, 0xD1]) + struct.pack(">H", 6) + chan + \
            struct.pack(">H", 1) + struct.pack(">H", index)
        self.sendf(pkt, addr)


class BoundaryWriter:
    n = 0

    def write(self, b):
        idx = Cam.idx_seen[-1] if Cam.idx_seen else 0
        fh.write(struct.pack("<I", len(b) + 2))
        fh.write(struct.pack(">H", idx))
        fh.write(b)
        self.n += 1
        return len(b)

    def flush(self):
        fh.flush()

    def close(self):
        fh.close()


class SockSpy:
    def __init__(self, s):
        self._s = s

    def __getattr__(self, name):
        return getattr(self._s, name)

    def recvfrom(self, size):
        p, a = self._s.recvfrom(size)
        if len(p) > 8 and p[1] == 0xD0:
            Cam.last_chan = bytes(p[4:6])       # d1 XX
            Cam.idx_seen.append(struct.unpack(">H", p[6:8])[0])
        return p, a


c = Cam()
c.sock = SockSpy(c.sock)
t0 = time.time()
c.run(dur=DUR, out=BoundaryWriter())
idx = Cam.idx_seen
print("[cap] mode=%s D0=%d distinct_idx=%d min=%d max=%d" %
      ("echo" if ACK_CHAN_ECHO else "stock", len(idx), len(set(idx)),
       min(idx) if idx else -1, max(idx) if idx else -1))
print("[cap] elapsed=%.0fs file=%d bytes" % (time.time() - t0, fh.tell()))
