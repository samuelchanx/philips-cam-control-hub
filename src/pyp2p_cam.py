#!/usr/bin/env python3
"""
pyp2p_cam.py — pure-Python ThroughTek PPCS(cs2p2p) camera client.

Implements the f1-family P2P wire protocol (reverse-engineered from wire logs
of the working Android lib + elastic/camera-hacks) and the qianniao vendor
session layer, entirely in Python. No Android, no emulation.

Usage: python3 pyp2p_cam.py [--did CFMSA-...] [--dur 3600] [--dump FILE]
"""
import socket, struct, sys, time, threading, argparse, os, subprocess

ENV_PATH = os.environ.get("CAM_ENV_FILE", "~/.philips-cam.env")
_env = {}
try:
    for _ln in open(os.path.expanduser(ENV_PATH)):
        _ln = _ln.strip()
        if "=" in _ln and not _ln.startswith("#"):
            _k, _v = _ln.split("=", 1); _env[_k] = _v
except Exception:
    pass

def _env_key(key: str, default: str = "") -> str:
    """Prefer real env vars, then the env file, then a default."""
    return os.environ.get(key) or _env.get(key) or default

def telegram(msg):
    tok, chat = _env.get("TELEGRAM_BOT_TOKEN", ""), _env.get("TELEGRAM_CHAT_ID", "")
    if not tok or not chat:
        print("[tg]", msg)
        return
    try:
        subprocess.run(["curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{tok}/sendMessage",
            "-d", f"chat_id={chat}", "-d", f"text={msg}"], timeout=10)
    except Exception:
        pass

# ---------------- config ----------------
# Your camera's P2P UID (from the vendor cloud device list / app; see README).
# The placeholder parses fine but connects to nothing.
DID_STR = _env_key("CAMERA_UID", "CFMSA-000000-XXXXX")
RELAYS = ["47.89.232.167", "123.56.74.245", "8.134.120.63"]  # group for CFMS/CFCS/NFES
RELAY_PORT = 32100
VENDOR_MAGIC = 0x76768888
PTZ_CMD_FILE = "/tmp/philips_apk/p2p/ptz_cmd.json"   # hub → dog_final relay
TALK_CMD_FILE = "/tmp/philips_apk/p2p/talk_cmd.json"  # same pattern, audio

# talk-back (jadx 2026-10-20: P2pSDK.startTalkAudio + AudioFrame/AVFrame)
TALK_AUDIO_FLAG = 428216306        # AVFrame.P2PKIT_AUDIO_FRAME_FLAG
TALK_CODEC_G711A = 82              # AVFrame.P2PKIT_CODEC_AUDIO_G711A
TALK_FRAME_BYTES = 320             # 40ms of 8kHz G.711 — camera's packet size
# the app speaks G711A; if the speaker plays static, set TALK_MULAW=1 in .env
TALK_CODEC = 81 if _env.get("TALK_MULAW", "").strip().lower() in (
    "1", "true", "on") else TALK_CODEC_G711A

def parse_did(s: str):
    """'CFMSA-000000-XXXXX' -> wire packing:
    ASCII(brand) + 4 zero bytes + uint24(number) + ASCII(suffix)"""
    brand, num, suffix = s.split("-")
    return brand.encode() + b"\x00\x00\x00\x00" + int(num).to_bytes(3, "big") + suffix.encode()

DID_BIN = parse_did(DID_STR)

# ---------------- f1 frame helpers ----------------
def f1(t: int, body: bytes = b"") -> bytes:
    return bytes([0xF1, t]) + struct.pack(">H", len(body)) + body

def f1_hello():       return f1(0x00)
def f1_search(body):  return f1(0x20, body)
def f1_punch(did):    return f1(0x41, did + b"\x00" * 3)
def f1_alive():       return f1(0xE0)
def f1_alive_ack():   return f1(0xE1)
def f1_close():       return f1(0xF0)

def parse(p: bytes):
    if len(p) < 4 or p[0] != 0xF1:
        return None, b""
    t = p[1]
    ln = struct.unpack(">H", p[2:4])[0]
    return t, p[4:4+ln]

# ---------------- vendor framing (channel 0 commands) ----------------
def vend_cmd(cmd: int, payload: bytes) -> bytes:
    return struct.pack("<IIIIII", VENDOR_MAGIC, len(payload), cmd, 0, 0, 0) + payload

def vendor_login():
    b = bytearray(0x48)
    pwd = _env_key("CAMERA_P2P_PWD").encode()  # per-device P2P password (vendor cloud)
    b[0:len(pwd)] = pwd
    b[0x40] = 0x00  # owner (0x3F only for shared devices)
    struct.pack_into("<I", b, 0x44, 0)
    return vend_cmd(0x10, bytes(b))

def vendor_live():
    b = bytearray(0x54)
    b[16] = 0xE0
    return vend_cmd(0x01, bytes(b))

# ---------------- talk-back (speaker) ----------------
def speaker_start() -> bytes:
    """cmd 6 — IOCTRL_SPEAKER createBuffer: <0><codec 82 G711A><frame 320><0*4>"""
    return vend_cmd(6, struct.pack("<iii", 0, TALK_CODEC,
                                   TALK_FRAME_BYTES) + b"\x00" * 4)

def speaker_stop() -> bytes:
    """cmd 7 — createStopBuffer"""
    return vend_cmd(7, b"\x00" * 4)

def talk_frame(g711: bytes) -> bytes:
    """AudioFrame.getDate: <flag><len><pts=0><codec> u32 LE each + payload,
    written to TalkChannel (PPCS channel 1 → wire chan d1 01)."""
    return struct.pack("<iii", TALK_AUDIO_FLAG, len(g711), 0) \
        + struct.pack("<i", TALK_CODEC) + g711

# ---------------- client ----------------
class P2PCam:
    def __init__(self, verbose=False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.5)
        self.local_port = self.sock.getsockname()[1]
        self.local_ip = self._local_ip()
        self.verbose = verbose
        self.session_up = threading.Event()
        self.got_device = threading.Event()
        self.device_addr = None      # (ip, port) of camera candidate
        self.drw_in = []             # inbound vendor payloads (bytes)
        self.drw_lock = threading.Lock()
        self.drw_event = threading.Event()
        self.send_index = 0
        self.ack_needed = []         # inbound drw indexes to ack
        self.stop = False
        self.combo = 0
        self.relay_set = [(r, RELAY_PORT) for r in RELAYS]

    def _local_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    def v(self, *a):
        if self.verbose: print("[v]", *a, flush=True)

    # ---- wire ----
    def sendf(self, data, addr):
        self.sock.sendto(data, addr)
        self.v(">>", addr, data.hex()[:60])

    def build_search(self):
        # exact 36-byte body shape observed from the working lib session
        body = DID_BIN + bytes.fromhex("0000000002983a020011ac0000000000000000")
        return f1_search(body)

    # ---- DRW + vendor ----
    def ack_drw(self, index: int, addr, chan: bytes = b"\xd1\x00"):
        # chan must echo the inbound D0's own channel bytes — video rides
        # d1 02 (vendor frames d1 00); a mismatched ack is ignored by the
        # camera and every packet gets retransmitted ~19x (found 2026-10-19)
        pkt = bytes([0xF1, 0xD1]) + struct.pack(">H", 6) + chan + struct.pack(">H", 1) + struct.pack(">H", index)
        self.sendf(pkt, addr)

    def drw_send(self, payload: bytes, addr, chan: bytes = b"\xd1\x00"):
        idx = self.send_index & 0xFFFF
        self.send_index += 1
        pkt = bytes([0xF1, 0xD0]) + struct.pack(">H", len(payload) + 4) + chan + struct.pack(">H", idx) + payload
        self.sendf(pkt, addr)

    def vendor_login(self):
        b = bytearray(0x48)
        pwd = _env_key("CAMERA_P2P_PWD").encode()
        b[0:len(pwd)] = pwd
        b[0x40] = 0x00
        struct.pack_into("<I", b, 0x44, 0)
        return vend_cmd(0x10, bytes(b))

    def vendor_live(self, res=1, stype=2, p2=0):
        # 20-byte createBuffer(res, streamType, p2) — nResult checked by camera
        b = struct.pack("<iii", res, stype, p2) + b"\x00" * 8
        return vend_cmd(0x01, b)

    def ptz(self, direction: int):
        """AVIOCTRL PTZ (CMD.java IOTYPE_USER_IPCAM_PTZ_COMMAND, cmd 4097,
        same 24B vendor header as login/live — jadx 2026-10-20):
        0=stop 1=up 2=down 3=left 4=left-up 5=left-down 6=right
        7=right-up 8=right-down. Pulse then send 0 (stop)."""
        payload = bytes([direction, 8, 0, 0, 0, 0, 0, 0])
        self.drw_send(vend_cmd(4097, payload), self.data_path)

    def speaker_start(self):
        self.drw_send(speaker_start(), self.data_path)

    def speaker_stop(self):
        self.drw_send(speaker_stop(), self.data_path)

    def send_talk(self, g711: bytes):
        """One G.711A audio frame to the talk channel (d1 01)."""
        self.drw_send(talk_frame(g711), self.data_path, chan=b"\xd1\x01")

    # ---- unified state machine ----
    def run(self, dur=600, out=None):
        t_end = time.time() + dur
        # hello
        for addr in self.relay_set:
            self.sendf(f1_hello(), addr)
        print("[+] hello sent to relays", flush=True)

        candidates = set()
        last_search = 0.0
        last_punch = 0.0
        last_alive = 0.0
        live_sent = False
        live_ok_sent = False
        if out is None:
            videofile = None
        elif hasattr(out, "write"):
            videofile = out
        else:
            videofile = open(out, "wb")
        video_started = False

        while time.time() < t_end:
            now = time.time()
            # periodic actions
            if not self.got_device.is_set() and now - last_search >= 2:
                pkt = self.build_search()
                for addr in self.relay_set:
                    self.sendf(pkt, addr)
                last_search = now
            if self.got_device.is_set() and not self.session_up.is_set() and now - last_punch >= 1:
                pkt = f1_punch(DID_BIN)
                for addr in list(self.relay_set) + list(candidates):
                    try: self.sendf(pkt, addr)
                    except Exception: pass
                last_punch = now
            if self.session_up.is_set() and now - last_alive >= 3:
                try: self.sendf(f1_alive(), self.data_path)
                except Exception: pass
                last_alive = now
            if self.session_up.is_set() and not live_sent:
                # send login+LIVE_START ONCE per session. A 5s re-send timer
                # used to live here: it restarted the camera stream every 5s
                # (fresh GOP head each decode + a "LIVE OK" telegram each
                # time = the spam), found 2026-10-19.
                live_sent = True
                res, st, p2v = 1, 2, 0  # locked: STREAM_HIGH + SIO_TYPE_ALL (verified working)
                self.drw_send(vendor_login(), self.data_path)
                time.sleep(0.3)
                self.drw_send(self.vendor_live(res, st, p2v), self.data_path)

            # receive
            try:
                self.sock.settimeout(0.3)
                p, a = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except ConnectionResetError:
                continue
            t, body = parse(p)
            if t is None: continue

            if t == 0x01:
                self.v("hello-ack", a)
            elif t == 0x21:
                if not self.got_device.is_set():
                    print("[+] relay found the device", flush=True)
                self.got_device.set()
            elif t == 0x40 and len(body) >= 8:
                port = struct.unpack("<H", body[2:4])[0]
                ip = socket.inet_ntoa(body[4:8][::-1])
                cand = (ip, port)
                if cand not in candidates:
                    print("[+] camera candidate:", cand, flush=True)
                candidates.add(cand)
                self.device_addr = cand
                self.got_device.set()
            elif t == 0x42:
                if not self.session_up.is_set():
                    print("[+] P2P READY from", a, flush=True)
                    self.data_path = a
                    self.session_up.set()
                    telegram("📹 P2P session established")
            elif t == 0xE0:
                self.sendf(f1_alive_ack(), a)
            elif t == 0xD0 and len(body) >= 4:
                idx = struct.unpack(">H", body[2:4])[0]
                payload = body[4:]
                self.ack_drw(idx, a, chan=body[:2])
                if not video_started:
                    video_started = True
                    telegram("🎬 receiving video stream")
                if videofile:
                    videofile.write(payload); videofile.flush()
                if len(payload) >= 24 and struct.unpack("<I", payload[:4])[0] == VENDOR_MAGIC:
                    ln, cmd = struct.unpack("<II", payload[4:12])
                    ret = payload[24+4] if len(payload) >= 29 else -1
                    print(f"[<] vendor cmd={cmd:#x} len={ln}", flush=True)
                    if cmd == 0x10 and ret == 0:
                        print("[+] LOGIN OK", flush=True)
                    if cmd in (0x06, 0x07):
                        # speaker start/stop RESP — the result code tells us
                        # whether the camera accepted the talk buffer. Layout
                        # varies per cmd (0x02 keeps nResult in header bytes
                        # 20-24, 0x10 puts ret at body+4) so dump 20-40.
                        print(f"[<] speaker cmd={cmd:#x} resp={payload[20:40].hex()}",
                              flush=True)
                    if cmd == 0x02 and len(payload) >= 0x18:
                        nres, w, h = struct.unpack("<iii", payload[0x14:0x20])
                        print(f"[!] LIVE_RESP nResult={nres} {w}x{h}", flush=True)
                        # the camera sends periodic 0x02 frames — notify once
                        # per session or the group gets spammed (429s)
                        if nres == 0 and not live_ok_sent:
                            live_ok_sent = True
                            telegram(f"🎬 LIVE OK {w}x{h}")
            elif self.verbose:
                self.v("pkt", hex(t), a)

        if videofile: videofile.close()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dur", type=int, default=600)
    ap.add_argument("--dump", default="/tmp/philips_apk/p2p/py_video.h264")
    ap.add_argument("-v", action="store_true")
    a = ap.parse_args()
    c = P2PCam(verbose=a.v)
    c.run(dur=a.dur, out=a.dump)
