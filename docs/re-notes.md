# Reverse-engineering notes

Distilled findings from decoding the stack. Everything here was recovered from
packet captures of my own camera, the vendor's own Android app (jadx), and
careful probing of my own devices. Numbers are real measurements, kept honest
including the negative results.

## 1. The P2P protocol ("f1" family, ThroughTek PPCS-derived)

### Session flow

```
client                          relay :32100                   camera
  │  f1 00 (hello)                 │                             │
  │ ◀──────── f1 01 ───────────────│                             │
  │  f1 20 (search, DID + fixed 36B body)                        │
  │ ◀── f1 21 / f1 40 (candidates) ──────────────────────────────│
  │  f1 41 (punch, DID)                                          │
  │ ◀── f1 42 (READY) ───────────────────────────────────────────│
  │  f1 E0 (alive) / f1 E1 ◀──── keepalive loop                  │
  │  f1 D0 (data) / f1 D1 (ack) ◀─ the actual stream             │
```

- Frame format: `f1 <type> <len_be16> <body>` — lengths are **big-endian**
  (the vendor layer below is little-endian; that asymmetry cost real time).
- DID wire packing: `ASCII(brand) + 4 zero bytes + uint24be(number) + ASCII(suffix)`,
  e.g. `CFMSA-000000-XXXXX` → `CFMSA` + `00000000` + `000000` + `XXXXX`.
- Relays: fixed vendor UDP relays on port 32100 (addresses discoverable in the
  app's traffic; they answer `search` with candidates even when the camera's
  LAN IP is unknown).
- **Diagnostic trick:** the relay answering `search` tells you the camera's
  *relay session* is still alive even when the device is otherwise unreachable
  — that's how "camera P2P service hung, needs power cycle" was distinguished
  from "router/Wi-Fi dead" (a LAN-reachable feeder proved the network fine).

### Data channels (DRW) — and the ack-echo rule

```
data:  f1 d0 <len_be16> <chan[2]> <idx_be16> <payload>
ack:   f1 d1 0006 <chan[2]> 0001 <idx_be16>     ← chan must ECHO the inbound packet's channel
```

Channels observed: `d1 00` = vendor command/response, `d1 01` = talk audio out,
`d1 02` = video/audio in.

**The bug that posed as "corruption" for a year:** early code acked everything
on `d1 00`. The camera never saw video acks and retransmitted every packet
~19×, so the decode saw a perfect GOP head drowned in duplicate garbage —
"first frame perfect, then progressive mush". Measured A/B:

| ack mode | video packets seen | distinct indexes |
|---|---|---|
| hardcoded `d1 00` | 1817 | 121 |
| echo inbound channel | 1337 | 1337 |

Two-line fix. Full GOP: **94 frames, 0 decode errors** (~25 fps, 1920×2176,
GOP ≈ 10s).

### Per-packet media header (32 bytes)

Inside every `d1 02` payload, before the H.264 NAL:

| offset | size | meaning |
|---|---|---|
| 0–1 | 2 | frame marker: `18 05` = IDR start, `f1 0f` = P-frame start, `f2 0f` = audio |
| 2–3 | 2 | magic `86 19` |
| 4–7 | 4 | u32 LE byte offset within the frame |
| 8–11 | 4 | u32 LE epoch seconds |
| 12–15 | 4 | frame type (1 = IDR, 2 = P) |
| 16–19 | 4 | frame sequence number |
| 20–27 | 8 | constants (2, 3) |
| 28–31 | 4 | `00 00 xx 00` |

NAL units start at offset 32. Audio frames are **G.711, 320 bytes / 40 ms**
(silence is a `55…d5` pattern) — they must be excluded from the video stream or
every P-frame corrupts.

Method lessons: continuation packets have no boundaries, so the capture tool
(`src/gopcap.py`) stores each payload length-preserved with its DRW index;
dedupe by index before decoding. Beware false-positive start codes inside
header fields (a LE u32 `01 00 00 00` reads as an H.264 start code at a shifted
offset — a "17-byte header" theory was an artifact of sampling 4665 identical
retransmits).

### Vendor command layer (inside `d1 00` payloads)

```
header: 76 76 88 88 | len_le32 | cmd_le32 | 00 00 00 00   + payload
```

| cmd | meaning | notes |
|---|---|---|
| `0x10` | login | 72 B payload: per-device P2P pwd at offset 0, owner flag at 0x40 |
| `0x01` | live start (createBuffer) | `res, streamType, p2` LE ints; camera checks nResult |
| `4097` | PTZ | `IOTYPE_USER_IPCAM_PTZ_COMMAND`: 0 stop, 1 up, 2 down, 3 left, 4 left-up, 5 left-down, 6 right, 7 right-up, 8 right-down |
| 6 / 7 | speaker buffer create / stop | create takes `<0><codec><frame-bytes><0*4>`; codec 82 = G711A (µ-law 81 as fallback probe) |

Talk-back frames go out on channel `d1 01` as
`<audio-flag><len><pts><codec>` (LE ints) + raw G.711, paced at 40 ms/frame.

Hard-won session rules:

- Send vendor login + live-start **once per session**. An old "keepalive" that
  re-sent them every 5 s made the camera restart the whole stream each time —
  P-frames never chained (and a "LIVE OK" notification fired on every restart →
  alert spam + rate limiting).
- The renderer must decode from the **last** SPS in the dump (newest GOP), not
  the first one — picking the first shows the oldest frame forever ("frozen"
  clock in the overlay was the tell).

## 2. The vendor cloud API

Hosts: `user.philipsiot.com` (`/v3/...` login + device), `cloud-storage.philipsiot.com`
(`/v3/access/...` recordings). Requests carry a static header block copied from
the app plus `ts` and a `tk` session token from `loginUp`
(password AES-128-ECB'd with a **static app key** — a vendor constant extracted
from the APK, identical for every install; not shipped in this repo — the cloud
client reads `PHILIPS_URL_KEY`, and extraction is part of the APK-mining
workflow above).

Findings (all server-verified):

- **Single-session auth:** every `loginUp` kicks the previous token — including
  the phone app's. One login per probe, reuse the `tk`.
- **No "online now" field exists.** Verified by walking the entire
  `queryDevPage` payload, mining the APK dex for `/v3/*` endpoints, and probing
  candidate endpoints (`getImproveCtrlNew` → 10003, `getWarnDevs` → HTTP error).
  The hub derives `online` from pipeline frame freshness instead of faking it.
- **No cloud recording plan:** the month-flag endpoint (a **GET** with query
  params — every POST attempt returned 10003) answers server error **13005**
  "no purchased plan / cloud storage off". The `did` used in queries is an
  AES-ECB blob of the UID under the static URL key.
- The push/message store holds **0 messages for 12 months** — so the "old
  videos" the app once showed were expiring alert messages, not recordings.
- Hard safety rule in all code: **never** call `/delTs` or `cloudCard/delete`
  (destructive).

## 3. The Tuya feeder (cloud + LAN)

- Cloud: `manual_feed` takes precise portions 1–36 (clamped and whitelisted in
  the dispatcher).
- Local datapoint map, verified by safe probing:

| dp | meaning | notes |
|---|---|---|
| 2 | quick_feed (bool) | dispenses the machine preset |
| 3 | feed_report | history events |
| 4 | feed_state | standby/done |
| 13 | battery | reads 0 on mains power |
| 17 | light (bool) | night light |
| 23 | slow_feed (bool) | |
| 24 | **likely factory_reset** | **never write 1** — tripwired in code |
| 101 | power mode | |

- Probe method: write **0 first** to booleans (a legal no-op even if the dp is
  destructive), then probe integers — the full 1–40 sweep produced zero
  accidental dispenses.
- Negative result: `manual_feed` has **no local datapoint**, so precise-portion
  feeding over LAN is physically impossible on this hardware; only the preset
  `quick_feed` works locally (its portion count is still unverified —
  deliberately not field-tested on the dog without asking).

## 4. The dog gate (numbers)

- Baseline audit: 291 motion clips → only **21 (7%)** contained the dog. False
  positives: 3 a.m. IR noise in an empty room, auto-exposure jumps in daylight.
  Burn rate 1.4 GB / 2 days → ~20 GB / 30 days before the gate.
- Gate: pixel-diff ≥2% on two consecutive 20 s polls → one Apple Vision
  animal classification of the freshest frame → open the capture window only if
  confidence ≥ 0.3. Re-checks throttled to 10 s per episode; every decision
  appended to an audit log.
- Validation: all 288 historical thumbnails re-scored with a throwaway Swift
  prototype against the tuned threshold — the 21 true positives separate
  cleanly, zero dogs in the rejected tail.
- **Fail-open contract:** any classifier error → record the clip untagged. A
  broken gate must never lose real footage; it just loses the badge.
- macOS 26 SDK gotcha: `VNRecognizedAnimalsObservation` no longer exists in the
  Swift *or* ObjC headers. The working path is
  `VNRecognizeAnimalsRequest.results()` → generic `VNRecognizedObjectObservation`
  labels → `VNClassificationObservation.identifier` ("Dog"/"Cat").

## 5. Operational lessons (each one earned)

- **Liveness = dump growth, never image mtime.** A frozen renderer happily
  rewrites byte-identical JPEGs forever.
- **A test suite can kill production.** Test code constructing a second pipeline
  manager on the default dump directory triggered real orphan recovery →
  SIGTERM'd the live pipeline (caught red-handed via exit code −15). Rule: every
  app-construction site takes injected fakes.
- **launchd's PATH has no Homebrew** — every subprocess must resolve ffmpeg via
  an explicit path augmentation, or the whole chain silently breaks only when
  running as a service.
- **Motion ground truth needs self-consistent frames.** Comparing directory
  images of different ages produced a fake 87.9% "motion" during a stream
  freeze and 22 false alerts; the analyser now compares its own snapshots, so a
  frozen stream reads as diff 0 by construction.
- Error states must not be dead ends: `error → start` is legal, and with
  recording mode on, an error auto-retries after a cooldown (recording
  continuity is a promise).

## 6. Open problems

- Talk-back playback freezes the whole inbound stream ~15–20 s later; root cause
  unknown (suspect: speaker-stop ineffective → camera holds the talk buffer and
  eventually tears the session). A dump-growth watchdog auto-heals it today; a
  µ-law codec probe (`TALK_MULAW=1`) is queued.
- The hub doesn't know when a talk clip finishes playing (fire-and-forget).
- LAN `quick_feed` preset portion count unverified on hardware.
- The camera's SD-card clip protocol is known but has no browser UI yet.
