"""talk-back wire layout — SPEAKER_START/STOP + AVFrame audio (jadx-derived)."""
import struct

import pyp2p_cam as p


def test_speaker_start_layout():
    b = p.speaker_start()
    magic, ln, cmd, z1, z2, z3 = struct.unpack("<IIIIII", b[:24])
    assert magic == p.VENDOR_MAGIC and cmd == 6 and ln == 16
    a, codec, frame = struct.unpack("<iii", b[24:36])
    assert (a, codec, frame) == (0, 82, 320)      # G711A, 320B frames
    assert b[36:] == b"\x00" * 4


def test_speaker_stop_layout():
    b = p.speaker_stop()
    magic, ln, cmd = struct.unpack("<III", b[:12])
    assert magic == p.VENDOR_MAGIC and cmd == 7 and ln == 4
    assert b[24:] == b"\x00" * 4


def test_talk_frame_layout():
    f = p.talk_frame(b"\x55" * 320)
    flag, ln, pts, codec = struct.unpack("<iiii", f[:16])
    assert flag == p.TALK_AUDIO_FLAG == 428216306
    assert ln == 320 and pts == 0 and codec == 82
    assert f[16:] == b"\x55" * 320
