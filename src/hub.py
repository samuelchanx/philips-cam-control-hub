"""control-hub — one self-hosted dashboard for the feeder + camera.

Run (from src/):
    python3 hub.py            # binds localhost + Tailscale IP, default port 8790

Seams: the FastAPI app is built by create_app(feeder_backend=...); tests inject
a fake feeder backend and drive the HTTP API — no real cloud in tests.
Secrets: read from a local env file only (CAM_ENV_FILE, default
~/.philips-cam.env) and never rendered into responses, pages, or logs.
"""
import json
import os
import socket
import subprocess
import threading
import time

import uvicorn
from fastapi import Body, FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response

import cam_backup
import dispatcher as dispatcher_mod
import feedlog
import feeder
import motion_recorder
import pipeline as pipeline_mod
from pyp2p_cam import PTZ_CMD_FILE, TALK_CMD_FILE, telegram

ENV_PATH = os.environ.get("CAM_ENV_FILE", "~/.philips-cam.env")
DEFAULT_PORT = 8790
AUDIO_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "audio")
AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".aiff", ".aif", ".flac", ".ogg")


def to_g711a(src, dst):
    """Any audio → raw G.711 8kHz mono (the camera speaker's format);
    TALK_MULAW=1 in .env switches A-law → μ-law if the speaker plays static."""
    fmt = "mulaw" if load_env().get("TALK_MULAW") else "alaw"
    r = subprocess.run(
        [motion_recorder.resolve_ffmpeg(), "-y", "-loglevel", "error",
         "-i", src, "-ar", "8000", "-ac", "1", "-f", fmt, dst],
        capture_output=True, timeout=300)
    return r.returncode == 0 and os.path.exists(dst) \
        and os.path.getsize(dst) > 0


def say_to_aiff(text, aiff_path):
    """macOS `say` → AIFF; TALK_VOICE env picks a voice, defaulting to the
    zh_HK one (Sinji) when present."""
    voice = load_env().get("TALK_VOICE")
    if not voice:
        try:
            listing = subprocess.run(["say", "-v", "?"], capture_output=True,
                                     text=True, timeout=10).stdout
        except Exception:
            listing = ""
        voice = "Sinji" if "zh_HK" in listing else None
    cmd = ["say", "-o", aiff_path]
    if voice:
        cmd += ["-v", voice]
    r = subprocess.run(cmd + [text], capture_output=True, timeout=120)
    return r.returncode == 0 and os.path.exists(aiff_path)


def load_env():
    env = {}
    try:
        for ln in open(os.path.expanduser(ENV_PATH)):
            ln = ln.strip()
            if ln and "=" in ln and not ln.startswith("#"):
                k, v = ln.split("=", 1)
                env[k] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return env


def tailscale_ip():
    """Local address on the Tailnet (100.64/10), or None when offline."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("100.100.100.100", 53))  # MagicDNS — no packet sent
        addr = s.getsockname()[0]
        s.close()
        return addr if addr.startswith("100.") else None
    except Exception:
        return None


def env_int(env, key, default):
    try:
        return int(env.get(key) or default)
    except (TypeError, ValueError):
        return default


class CloudFeeder:
    """Real feeder backend — thin adapter over feeder.py (cloud path)."""

    def status(self):
        c = feeder.cloud()
        dev = c._tuyaplatform("devices/%s" % feeder.ENV["TUYA_DEVICE_ID"])["result"]
        st = feeder.get_status(c)
        return {
            "online": bool(dev.get("online")),
            "battery_percentage": st.get("battery_percentage"),
            "feed_state": st.get("feed_state"),
            "light": st.get("light"),
            "slow_feed": st.get("slow_feed"),
        }

    def feed(self, portions):
        return feeder.feed(portions)

    def quick_feed(self):
        return feeder.send_dp("quick_feed", True)

    def toggle(self, code, on):
        return feeder.send_dp(code, on)


class CachedCameraStatus:
    """≥5-min cache over cam_backup.device_status — don't hammer Philips cloud.

    Successes are cached for ttl; failures for a short window so a dead cloud
    isn't retried on every page poll but recovers fast.
    """

    FAIL_TTL = 60

    def __init__(self, ttl=300, fetch=None):
        self.ttl = ttl
        self._fetch = fetch or cam_backup.device_status
        self._lock = threading.Lock()
        self._value = None
        self._ok_until = 0.0
        self._fail_until = 0.0

    def __call__(self):
        now = time.time()
        with self._lock:
            if self._value is not None and now < self._ok_until:
                return dict(self._value)
            if now < self._fail_until:
                return None
        try:
            v = self._fetch()
        except Exception:
            with self._lock:
                self._fail_until = time.time() + min(self.FAIL_TTL, self.ttl)
            return None
        with self._lock:
            self._value = dict(v)
            self._ok_until = time.time() + self.ttl
        return dict(v)


class CachedFeedHistory:
    """≥5-min cache over the Tuya device log — dispense events of the last
    30 days (manual_feed / quick_feed), newest first. Don't hammer the cloud."""

    FAIL_TTL = 60
    DAYS = 30

    def __init__(self, ttl=300, fetch=None):
        self.ttl = ttl
        self._fetch = fetch or self._fetch_cloud
        self._lock = threading.Lock()
        self._value = None
        self._ok_until = 0.0
        self._fail_until = 0.0

    def _fetch_cloud(self):
        end = int(time.time() * 1000)
        start = end - self.DAYS * 24 * 3600 * 1000
        r = feeder.cloud().getdevicelog(
            deviceid=feeder.ENV["TUYA_DEVICE_ID"], start=start, end=end,
            size=100, max_fetches=10)
        if not isinstance(r, dict) or r.get("success") is False:
            raise RuntimeError("tuya log fetch failed")
        return (r.get("result") or {}).get("logs") or []

    @staticmethod
    def _filter(logs):
        """raw device-log entries → dispense rows.

        feed_report is the device's own "dispensed N portions" event — it
        fires for EVERY feeding (webapp, official app, cloud CLI, and the
        machine's physical button), so it matches the official app's
        Feed Record exactly. manual_feed/quick_feed command events are
        redundant (each dispense reports its own feed_report).
        """
        rows = []
        for lg in logs or []:
            if lg.get("code") != "feed_report":
                continue
            try:
                portions = int(str(lg.get("value")))
            except (TypeError, ValueError):
                continue
            if portions <= 0:
                continue
            ts = time.strftime("%Y-%m-%d %H:%M",
                               time.localtime((lg.get("event_time") or 0) / 1000))
            rows.append({
                "_t": lg.get("event_time") or 0,
                "ts": ts,
                "portions": portions,
                "source": lg.get("event_from") or "device",
            })
        rows.sort(key=lambda r: r["_t"], reverse=True)   # newest first
        for r in rows:
            del r["_t"]
        return rows

    def __call__(self):
        now = time.time()
        with self._lock:
            if self._value is not None and now < self._ok_until:
                return list(self._value)
            if now < self._fail_until:
                return None
        try:
            v = self._filter(self._fetch())
        except Exception:
            with self._lock:
                self._fail_until = time.time() + min(self.FAIL_TTL, self.ttl)
            return None
        with self._lock:
            self._value = list(v)
            self._ok_until = time.time() + self.ttl
        return list(self._value)


    def invalidate(self):
        with self._lock:
            self._value = None
            self._ok_until = 0.0
            self._fail_until = 0.0


PAGE = """<!doctype html>
<html lang="zh-Hant-HK">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>control-hub</title>
<style>
  :root {
    color-scheme: dark light;
    --bg: #0f172a; --card: #1b2336; --fg: #f8fafc; --muted: #94a3b8;
    --border: #334155; --accent: #22c55e; --accent-fg: #0f172a;
    --primary: #2563eb; --primary-fg: #ffffff; --danger: #ef4444;
    --danger-fg: #ffffff; --press: rgba(255,255,255,.10);
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f1f5f9; --card: #ffffff; --fg: #0f172a; --muted: #64748b;
      --border: #e2e8f0; --accent: #16a34a; --accent-fg: #ffffff;
      --press: rgba(0,0,0,.07);
    }
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, "PingFang HK", sans-serif;
         background: var(--bg); color: var(--fg); margin: 0;
         font-size: 16px; line-height: 1.5; }
  main { max-width: 34rem; margin: 0 auto; padding: 1rem 1rem 3rem; }
  section { background: var(--card); border: 1px solid var(--border);
            border-radius: 14px; padding: 1rem 1.2rem; margin-bottom: .9rem; }
  h1 { font-size: 1.15rem; margin: .2rem 0 .9rem; }
  h2 { font-size: .95rem; margin: 0 0 .7rem; display: flex;
       align-items: center; gap: .5rem; flex-wrap: wrap; }
  h2 .sub { color: var(--muted); font-size: .8rem; font-weight: 400; }
  h2 + h2, .stack-gap { margin-top: 1.1rem; }
  .row { display: flex; justify-content: space-between; align-items: center;
         padding: .32rem 0; gap: .5rem; }
  .row .k { color: var(--muted); }
  .muted { color: var(--muted); }
  .err { color: var(--danger); }
  .ok { color: var(--accent); }
  .note { color: var(--muted); font-size: .8rem; margin: .5rem 0 0; }
  .pill { padding: .1rem .6rem; border-radius: 999px; font-size: .78rem;
          background: var(--press); color: var(--muted); }
  .pill.live { background: var(--accent); color: var(--accent-fg); }
  .pill.starting { background: #f59e0b; color: #0f172a; }
  .pill.error { background: var(--danger); color: var(--danger-fg); }

  button { font-size: .95rem; padding: .45rem .9rem; border-radius: 10px;
           border: 1px solid var(--border); background: transparent;
           color: var(--fg); min-height: 44px; cursor: pointer;
           touch-action: manipulation;
           transition: transform .06s ease, background .15s ease,
                       border-color .15s ease, opacity .15s ease; }
  button:hover { background: var(--press); }
  button:active { transform: scale(.95); background: var(--press); }
  button:disabled { opacity: .45; cursor: default; transform: none; }
  button:focus-visible, input:focus-visible, select:focus-visible {
    outline: 2px solid var(--primary); outline-offset: 2px; }
  .btn-primary { background: var(--primary); color: var(--primary-fg);
                 border-color: transparent; }
  .btn-primary:hover { background: var(--primary); filter: brightness(1.12); }
  .btn-danger-tint { color: var(--danger); border-color: var(--danger); }
  .btn-block { display: block; width: 100%; }
  .btn-row { display: flex; gap: .5rem; flex-wrap: wrap; margin-top: .4rem; }
  .spin { display: inline-block; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .stepper { display: flex; gap: 10px; align-items: center; margin: .55rem 0; }
  .stepper button { width: 54px; font-size: 1.25rem; }
  #portions { font-size: 1.4rem; min-width: 3.2rem; text-align: center; }
  #feed-btn { min-height: 54px; font-size: 1.1rem; margin: .4rem 0; }
  .switch { display: flex; justify-content: space-between; align-items: center;
            padding: .45rem 0; min-height: 44px; gap: .5rem; }
  .switch input, .switch select { width: 26px; height: 26px; }

  #history, #cloud-history, #rec-events { list-style: none; padding: 0;
    margin: .5rem 0 0; font-size: .88rem; }
  #history li, #cloud-history li, #rec-events li {
    display: flex; justify-content: space-between; gap: .5rem;
    padding: .38rem 0; border-top: 1px solid var(--border);
    align-items: center; flex-wrap: wrap; }
  #rec-events li.clickable { cursor: pointer; }
  #rec-events li.clickable:active { background: var(--press); }
  #rec-events img { border-radius: 6px; display: block; }
  #result { min-height: 1.4rem; margin: .4rem 0; font-size: .92rem; }

  .ptz-pad { display: grid; grid-template-columns: repeat(3, 72px);
             gap: 8px; justify-content: center; margin: .6rem 0 .2rem; }
  .ptz-pad button { min-height: 54px; font-size: 1.05rem; }
  .ptz-pad .pad-e { visibility: hidden; }

  #cam-frame, #rec-player { max-width: 100%; border-radius: 10px;
                            border: 1px solid var(--border); margin-top: .5rem; }
  #say-text { font-size: 1rem; padding: .5rem .7rem; border-radius: 10px;
              border: 1px solid var(--border); background: var(--bg);
              color: var(--fg); min-width: 0; flex: 1 1 12rem; }
  #talk-file { font-size: .95rem; padding: .45rem .5rem; border-radius: 10px;
               border: 1px solid var(--border); background: var(--bg);
               color: var(--fg); max-width: 11rem; flex: 1 1 auto; }
  .chip { padding: .3rem .8rem; min-height: 38px; border-radius: 999px;
          font-size: .85rem; margin: .2rem .3rem .2rem 0; }
  .chip.selected { background: var(--accent); color: var(--accent-fg);
                   border-color: transparent; }
  #toast { position: fixed; left: 50%; bottom: 26px;
           transform: translateX(-50%) translateY(16px); opacity: 0;
           background: var(--card); color: var(--fg);
           border: 1px solid var(--border); border-radius: 10px;
           padding: .55rem 1rem; font-size: .9rem; max-width: min(92vw, 30rem);
           box-shadow: 0 4px 16px rgb(0 0 0 / .35);
           transition: opacity .18s ease, transform .18s ease;
           pointer-events: none; z-index: 9; }
  #toast.show { opacity: 1; transform: translateX(-50%); }
  #toast.bad { border-color: var(--danger); color: var(--danger); }
  @media (prefers-reduced-motion: reduce) {
    button, #toast, .spin { transition: none; animation: none; }
  }
</style>
</head>
<body>
<main>
  <h1>🐕 control-hub</h1>

  <section id="feeder">
    <h2>出糧機 <button id="refresh" type="button" aria-label="重新整理">↻</button></h2>
    <div class="row"><span class="k">在線</span><b id="online">—</b></div>
    <div class="row"><span class="k">電量</span><b id="battery">—</b></div>
    <div class="row"><span class="k">狀態</span><b id="feed_state">—</b></div>

    <div class="stepper">
      <button id="minus" type="button" aria-label="減一份">−</button>
      <b id="portions">1</b>
      <button id="plus" type="button" aria-label="加一份">＋</button>
    </div>
    <button id="feed-btn" type="button" class="btn-primary btn-block">🍖 出糧</button>
    <button id="quick-btn" type="button" class="btn-block">quick_feed（機身預設份數）</button>
    <p id="result" role="alert"></p>

    <div class="switch"><span>夜燈</span>
      <input type="checkbox" id="light" aria-label="夜燈"></div>
    <div class="switch"><span>慢餵</span>
      <input type="checkbox" id="slow_feed" aria-label="慢餵"></div>
  </section>

  <section id="history-card">
    <h2>出糧紀錄 <span class="sub">（呢個 app）</span></h2>
    <ul id="history"></ul>
    <h2 class="stack-gap">雲端出糧歷史 <span class="sub">（30 日）</span>
      <button id="cloudhist-refresh" type="button" aria-label="重新整理雲端紀錄">↻</button></h2>
    <ul id="cloud-history"></ul>
  </section>

  <section id="camera">
    <h2>摄像头 <span id="cam-state" class="pill">stopped</span></h2>
    <div class="btn-row">
      <button id="cam-start" type="button" class="btn-primary">▶ 開 live view</button>
      <button id="cam-stop" type="button">⏹ 停</button>
    </div>
    <img id="cam-frame" alt="camera live frame" style="display:none">
    <p class="note" id="cam-meta"></p>
    <div class="ptz-pad">
      <span class="pad-e"></span>
      <button id="ptz-up" type="button" aria-label="鏡頭向上">▲</button>
      <span class="pad-e"></span>
      <button id="ptz-left" type="button" aria-label="鏡頭向左">◀</button>
      <button id="ptz-stop" type="button" aria-label="鏡頭停">⏹</button>
      <button id="ptz-right" type="button" aria-label="鏡頭向右">▶</button>
      <span class="pad-e"></span>
      <button id="ptz-down" type="button" aria-label="鏡頭向下">▼</button>
      <span class="pad-e"></span>
    </div>
    <p class="note">轉鏡頭：撳一下 ＝ 1 秒。on-demand：開咗先有畫面，5 分鐘冇人睇自動停。</p>
    <div class="row"><span class="k">雲端：在線</span><b id="cam-online">—</b></div>
    <div class="row"><span class="k">雲端：SD 卡</span><b id="cam-sd">—</b></div>
  </section>

  <section id="talk">
    <h2>安撫喇叭 <span class="sub">對講機出聲</span></h2>
    <div class="btn-row">
      <select id="talk-file"></select>
      <button id="talk-play" type="button">🔊 播</button>
      <button id="talk-stop" type="button">⏹ 停</button>
    </div>
    <div class="btn-row">
      <input id="say-text" placeholder="打字俾佢聽（TTS）">
      <button id="say-btn" type="button" class="btn-primary">📣 講</button>
    </div>
    <p class="note">音檔放入 audio/（mp3／m4a 都得，自動轉格式）。留意音量。</p>
  </section>

  <section id="recorder">
    <h2>回放 <span class="sub" id="rec-meta"></span></h2>
    <div class="switch"><span>錄影模式</span>
      <input type="checkbox" id="rec-mode" aria-label="錄影模式"></div>
    <p class="note" id="rec-note">開咗之後：有郁動自動剪片存底（pipeline 唔會自動停；重啟 server 後要重新開）。要行錄影，先開 live view。</p>
    <div id="rec-days"></div>
    <ul id="rec-events"></ul>
    <video id="rec-player" controls style="display:none"></video>
  </section>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script>
const $ = id => document.getElementById(id);
let busy = false;
let toastTimer = null;

function toast(msg, ok = true) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'show' + (ok ? '' : ' bad');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = ''; }, 2600);
}

// press feedback + label swap while a POST is in flight; restores after
async function withBusy(btn, busyLabel, fn) {
  if (btn.disabled) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
  if (busyLabel) btn.textContent = busyLabel;
  try { return await fn(); }
  finally {
    btn.disabled = false;
    btn.textContent = old;
    btn.removeAttribute('aria-busy');
  }
}

function flashSpin(btn) {
  const old = btn.innerHTML;
  btn.innerHTML = '<span class="spin">↻</span>';
  setTimeout(() => { btn.innerHTML = old; }, 700);
}

function setBusy(on) {
  busy = on;
  for (const id of ['feed-btn', 'quick-btn', 'minus', 'plus', 'light', 'slow_feed'])
    $(id).disabled = on;
  if (on) $('feed-btn').textContent = '⏳ 出糧中…';
  else $('feed-btn').textContent = '🍖 出糧';
}

function showResult(text, ok) {
  $('result').textContent = text;
  $('result').className = ok ? 'ok' : 'err';
}

async function refreshStatus() {
  try {
    const r = await fetch('/api/feeder/status');
    const j = await r.json();
    if (!r.ok) throw new Error(j.error || 'error');
    $('online').textContent = j.online ? '✅' : '❌';
    $('battery').textContent = j.battery_percentage == null ? '—' : j.battery_percentage + '%';
    $('feed_state').textContent = j.feed_state ?? '—';
    if (!busy) {
      $('light').checked = !!j.light;
      $('slow_feed').checked = !!j.slow_feed;
    }
  } catch (e) { /* status line stays at last known values */ }
}

async function renderHistory() {
  try {
    const j = await (await fetch('/api/feeder/history')).json();
    $('history').innerHTML = (j.entries || []).map(e => {
      const what = e.command === 'quick_feed' ? 'quick_feed'
                 : (e.portions ?? '?') + ' 份';
      const mark = e.result === 'ok' ? '✅' : '❌';
      return `<li><span>${e.ts}</span><span>${mark} ${what}</span>` +
             `<span class="muted">${e.source} ${e.latency_ms}ms</span></li>`;
    }).join('') || '<li class="muted">暫時冇紀錄</li>';
  } catch (e) { /* leave list as-is */ }
}

async function post(url, body) {
  const r = await fetch(url, {method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify(body || {})});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || 'error ' + r.status);
  return j;
}

$('minus').onclick = () => { const v = $('portions'); v.textContent = Math.max(1, +v.textContent - 1); };
$('plus').onclick = () => { const v = $('portions'); v.textContent = Math.min(36, +v.textContent + 1); };

$('feed-btn').onclick = async () => {
  if (busy) return;
  setBusy(true); showResult('出緊糧…', true);
  try {
    const j = await post('/api/feeder/feed', {portions: +$('portions').textContent});
    showResult(`✅ 出咗 ${j.portions} 份糧（feed_state=${j.feed_state}，${j.latency_ms}ms）`, true);
  } catch (e) {
    showResult(e.message.includes('in flight') ? '⏳ 上一單出糧仲未完成' : '❌ 出糧失敗：' + e.message, false);
  } finally { setBusy(false); refreshStatus(); renderHistory(); }
};

$('quick-btn').onclick = async () => {
  if (busy) return;
  setBusy(true); showResult('quick_feed 發送中…', true);
  try {
    const j = await post('/api/feeder/quick_feed');
    showResult(`✅ quick_feed 已發送（${j.latency_ms}ms）`, true);
  } catch (e) { showResult('❌ quick_feed 失敗：' + e.message, false); }
  finally { setBusy(false); refreshStatus(); renderHistory(); }
};

for (const code of ['light', 'slow_feed']) {
  $(code).onchange = async e => {
    try { await post('/api/feeder/toggle', {code, value: e.target.checked});
          toast(code === 'light' ? '夜燈已切換' : '慢餵已切換'); }
    catch (err) { e.target.checked = !e.target.checked;
                  showResult('❌ 切換失敗：' + err.message, false); }
  };
}

$('refresh').onclick = () => { flashSpin($('refresh')); refreshStatus(); };
refreshStatus(); renderHistory();
setInterval(refreshStatus, 30000);

async function renderCloudHistory() {
  try {
    const r = await fetch('/api/feeder/cloud_history');
    const j = await r.json();
    $('cloud-history').innerHTML = !r.ok
      ? '<li class="muted err">雲端讀唔到：' + (j.error || '') + '</li>'
    : (j.entries || []).map(e =>
        `<li><span>${e.ts}</span><span>🍖 出糧 ${e.portions} 份</span>` +
        `<span class="muted">${e.source}</span></li>`).join('')
      || '<li class="muted">30 日內冇出糧紀錄</li>';
  } catch (e) { /* leave list as-is */ }
}
$('cloudhist-refresh').onclick = () => { flashSpin($('cloudhist-refresh')); renderCloudHistory(); };
renderCloudHistory();

// ---- camera ----
let frameTimer = null;

function camStateBadge(state, err) {
  const el = $('cam-state');
  el.textContent = state + (err ? '：' + err : '');
  el.className = 'pill ' + (state === 'live' ? 'live'
                : state === 'error' ? 'error'
                : state === 'starting' ? 'starting' : '');
}

function camOnlineMark(v) { return v === true ? '✅' : v === false ? '⚫ 離線' : '—（未知）'; }

async function camPollOnce() {
  try {
    const j = await (await fetch('/api/camera/status')).json();
    camStateBadge(j.pipeline, j.error);
    $('cam-online').textContent = camOnlineMark(j.online);
    $('cam-sd').textContent = j.sdCard == null ? '—（雲端讀唔到）'
      : (j.sdCard ? '💾 已插入' : '未插入');
    if (j.pipeline === 'live' || j.pipeline === 'starting') {
      const img = $('cam-frame');
      img.style.display = '';
      img.src = '/api/camera/frame?t=' + Date.now();   // cache-busted poll
      $('cam-meta').textContent = j.hint ? '⚠ ' + j.hint
        : j.frame_age_s == null ? '等緊第一格畫面…'
        : '畫面係 ' + Math.round(j.frame_age_s) + ' 秒前';
      return true;
    }
    $('cam-frame').style.display = 'none';
    $('cam-meta').textContent = j.pipeline === 'error' ? 'pipeline 出咗錯，睇 server log' : '';
  } catch (e) { /* keep last frame */ }
  return false;
}

async function camLoop() {
  if (await camPollOnce()) {
    if (!frameTimer) frameTimer = setInterval(camLoop, 3000);
  } else if (frameTimer) {
    clearInterval(frameTimer); frameTimer = null;
  }
}

$('cam-start').onclick = () => withBusy($('cam-start'), '⏳ 開緊…', async () => {
  camStateBadge('starting');                      // optimistic: react at once
  try {
    const j = await (await fetch('/api/camera/start', {method: 'POST'})).json();
    camStateBadge(j.state);
    if (j.state === 'starting') toast('開緊 live view…（大約 15 秒）');
    else if (j.state === 'live') toast('✅ live view 開咗');
    else if (j.state === 'error') toast('❌ 開唔到：' + (j.error || ''), false);
  } catch (e) { camStateBadge('error', e.message); toast('❌ ' + e.message, false); }
  camLoop();
});

$('cam-stop').onclick = () => withBusy($('cam-stop'), '⏳ 停緊…', async () => {
  try { await fetch('/api/camera/stop', {method: 'POST'}); toast('已停止 live view'); }
  catch (e) { toast('❌ ' + e.message, false); }
  camLoop();
});

// PTZ — relayed through the live session (ptz_cmd.json → dog_final cmd 4097)
for (const [id, dir] of [['ptz-left', 'left'], ['ptz-right', 'right'],
                         ['ptz-up', 'up'], ['ptz-down', 'down'],
                         ['ptz-stop', 'stop']]) {
  $(id).onclick = async e => {
    const b = e.currentTarget;
    b.style.borderColor = 'var(--accent)';
    setTimeout(() => { b.style.borderColor = ''; }, 350);
    try { await post('/api/camera/ptz', {dir, dur: 1.0}); }
    catch (err) { toast('⚠ 轉鏡頭：' + err.message, false); }
  };
}

camLoop();
setInterval(camLoop, 30000);   // also refreshes cloud status (server caches ≥5 min)

// ---- talk-back：安撫喇叭（live session 直出 G.711A）----
async function talkRefresh() {
  try {
    const j = await (await fetch('/api/camera/talk/files')).json();
    const sel = $('talk-file'), cur = sel.value;
    sel.innerHTML = (j.files || []).map(n => `<option>${n}</option>`).join('')
      || '<option value="">（audio/ 冇檔案）</option>';
    if (cur && (j.files || []).includes(cur)) sel.value = cur;
  } catch (e) { /* keep as-is */ }
}

$('talk-play').onclick = () => {
  if (!$('talk-file').value) { toast('audio/ 冇檔案揀', false); return; }
  withBusy($('talk-play'), '⏳ 播緊…', async () => {
    try { await post('/api/camera/talk/play', {name: $('talk-file').value});
          toast('🔊 播緊 ' + $('talk-file').value); }
    catch (e) { toast('⚠ 喇叭：' + e.message, false); }
  });
};
$('talk-stop').onclick = () => withBusy($('talk-stop'), '⏳ …', async () => {
  try { await post('/api/camera/talk/stop', {}); toast('已停'); }
  catch (e) { toast('⚠ ' + e.message, false); }
});
$('say-btn').onclick = () => {
  const text = $('say-text').value.trim();
  if (!text) { toast('打啲字先', false); return; }
  withBusy($('say-btn'), '⏳ 講緊…', async () => {
    try { await post('/api/camera/talk/say', {text}); $('say-text').value = '';
          toast('📣 講緊：' + text); }
    catch (e) { toast('⚠ 講嘢：' + e.message, false); }
  });
};
talkRefresh();
setInterval(talkRefresh, 30000);

// ---- ticket 07: motion recorder / playback ----
let recDay = null;

function recEventUrl(day, name) {
  return '/api/recorder/file?name=' + encodeURIComponent(day + '/' + name);
}

async function loadRecEvents(day) {
  recDay = day;
  for (const b of document.querySelectorAll('#rec-days button'))
    b.classList.toggle('selected', b.dataset.day === day);
  try {
    const j = await (await fetch('/api/recorder/events?day=' + day)).json();
    $('rec-events').innerHTML = (j.events || []).map(e => {
      const thumb = e.thumb ? `<img src="${recEventUrl(day, e.thumb)}" alt="" width="96">` : '';
      const open = e.clip ? ` onclick="playRec('${day}','${e.clip}')"` : '';
      const cls = e.clip ? ' class="clickable"' : '';
      return `<li${open}${cls}>` +
        `${thumb}<span>${e.time}</span>` +
        `<span>郁動 ${e.motion_pct}%</span>` +
        (e.dog_conf != null ? `<span title="Vision 認狗 ${e.dog ? e.dog : ''} ${e.dog_conf}">🐶 ${e.dog_conf}</span>` : '') +
        `<span class="muted">${e.duration_s}s · ${e.size_kb}KB</span></li>`;
    }).join('') || '<li class="muted">呢一日冇錄影</li>';
  } catch (e) { /* leave list as-is */ }
}

window.playRec = (day, clip) => {
  const v = $('rec-player');
  v.src = recEventUrl(day, clip) + '&t=' + Date.now();
  v.style.display = '';
  v.play().catch(() => {});   // autoplay rejection is fine — controls exist
};

let recBusyUntil = 0;         // don't let the poll flip the toggle mid-POST

async function recRefresh() {
  try {
    const j = await (await fetch('/api/recorder/status')).json();
    if (Date.now() > recBusyUntil) $('rec-mode').checked = !!j.mode;
    $('rec-meta').textContent = j.clips + ' 條片 · ' + j.days + ' 日 · ' +
      j.disk_mb + 'MB（留 ' + j.retention_days + ' 日）';
    const dj = await (await fetch('/api/recorder/days')).json();
    const days = dj.days || [];
    $('rec-days').innerHTML = days.map(d =>
      `<button type="button" class="chip" data-day="${d}">${d}</button>`).join('');
    for (const b of document.querySelectorAll('#rec-days button'))
      b.onclick = () => loadRecEvents(b.dataset.day);
    if (!$('rec-events').innerHTML || (recDay && !days.includes(recDay)))
      loadRecEvents(days[0] || '');
    else for (const b of document.querySelectorAll('#rec-days button'))
      if (b.dataset.day === recDay) b.classList.add('selected');
  } catch (e) { /* keep last state */ }
}

$('rec-mode').onchange = async e => {
  recBusyUntil = Date.now() + 5000;
  try {
    await post('/api/recorder/mode', {on: e.target.checked});
    toast(e.target.checked ? '🎬 錄影模式開咗' : '錄影模式關咗');
  }
  catch (err) { e.target.checked = !e.target.checked;
                toast('❌ 切換失敗：' + err.message, false); }
  recRefresh();
};

recRefresh();
setInterval(recRefresh, 30000);
</script>
</body>
</html>
"""


def make_feeder_backend():
    """FEEDER_LAN on (default off) → LAN first with automatic cloud fallback."""
    if load_env().get("FEEDER_LAN", "").strip().lower() in ("1", "true", "on", "yes"):
        try:
            from feeder_lan import AutoFeeder, LanFeeder
            print("[hub] FEEDER_LAN on — lan first, cloud fallback")
            return AutoFeeder(LanFeeder(), CloudFeeder())
        except Exception as e:
            print("[hub] FEEDER_LAN on but LAN init failed (%s) — cloud only"
                  % type(e).__name__)
    return CloudFeeder()


def create_app(feeder_backend=None, dispatcher=None,
               pipeline=None, camera_status=None, cloud_history=None,
               recorder=None):
    app = FastAPI(title="control-hub")
    backend = feeder_backend if feeder_backend is not None else make_feeder_backend()
    disp = dispatcher if dispatcher is not None else dispatcher_mod.Dispatcher(
        backend, notify=telegram)
    cam = pipeline if pipeline is not None else pipeline_mod.PipelineManager()
    cam_cloud = camera_status if camera_status is not None \
        else CachedCameraStatus()
    feed_hist = cloud_history if cloud_history is not None \
        else CachedFeedHistory()
    env = load_env()
    # capture only while the user's recording mode is on AND their live-view
    # session is actually running (standing on-demand rule, ticket 07)
    rec = recorder if recorder is not None else motion_recorder.MotionRecorder(
        out_dir=env.get("RECORDINGS_DIR") or motion_recorder.DEFAULT_OUT,
        is_live=lambda: cam.recording and cam.status()["state"] == "live")
    retention_days = env_int(env, "RETENTION_DAYS", 14)
    app.state.recorder = rec

    @app.get("/")
    def index():
        return HTMLResponse(PAGE)

    def run_cmd(cmd, value=None):
        try:
            return disp.run(cmd, value)
        except dispatcher_mod.Busy as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        except dispatcher_mod.Rejected as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        except dispatcher_mod.BackendError as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.get("/api/feeder/status")
    def feeder_status():
        return run_cmd("status")

    @app.post("/api/feeder/feed")
    def feeder_feed(payload: dict = Body(default={})):
        result = run_cmd("feed", (payload or {}).get("portions"))
        if not isinstance(result, JSONResponse):
            feed_hist.invalidate()   # cloud list refetches on next view
        return result

    @app.post("/api/feeder/quick_feed")
    def feeder_quick_feed():
        result = run_cmd("quick_feed")
        if not isinstance(result, JSONResponse):
            feed_hist.invalidate()
        return result

    @app.post("/api/feeder/toggle")
    def feeder_toggle(payload: dict = Body(default={})):
        p = payload or {}
        return run_cmd(p.get("code"), p.get("value"))

    @app.get("/api/feeder/history")
    def feeder_history():
        return {"entries": feedlog.recent(20, path=disp.log_path)}

    @app.get("/api/feeder/cloud_history")
    def feeder_cloud_history():
        try:
            cloud = feed_hist()
        except Exception:
            cloud = None
        if cloud is None:
            return JSONResponse({"error": "tuya cloud history unavailable"},
                                status_code=502)
        # merge in this app's own feeds (the Tuya log can lag) — dedupe on
        # minute + portions against cloud rows, newest first
        entries = [dict(e, source=e.get("source") or "cloud") for e in cloud]
        for row in feedlog.recent(30, path=disp.log_path):
            if row.get("result") != "ok" or row.get("portions") is None:
                continue
            minute = row["ts"][:16]
            if any(e["ts"] == minute and e["portions"] == row["portions"]
                   for e in entries):
                continue
            entries.append({"ts": minute, "portions": row["portions"],
                            "source": "app"})
        entries.sort(key=lambda e: e["ts"], reverse=True)
        return {"entries": entries[:30]}

    # ---- camera (ticket 03) ----

    @app.post("/api/camera/start")
    def camera_start():
        return {"state": cam.start()}

    @app.post("/api/camera/stop")
    def camera_stop():
        return {"state": cam.stop()}

    @app.get("/api/camera/status")
    def camera_status():
        cam.touch()
        st = cam.status()
        state = st["state"]
        if state == "live" and st["frame_age_s"] is not None \
                and st["frame_age_s"] <= pipeline_mod.FRESH_S:
            online = True    # pipeline producing fresh frames = camera reachable
        elif state == "live":
            online = False   # running but frames stale
        else:
            online = None    # pipeline off — cloud has no online field
        try:
            cloud = cam_cloud()
        except Exception:
            cloud = None
        hint = None
        if state == "starting" and st["no_frame_retries"]:
            # already retried a full 90s cycle without a single frame —
            # the camera itself is most likely unreachable
            hint = "鏡頭一直冇反應，可能離線（檢查電源／Wi-Fi）— 自動重試緊"
        return {
            "pipeline": state,
            "error": st["error"],
            "frame_age_s": st["frame_age_s"],
            "idle_timeout_s": st["idle_timeout_s"],
            "hint": hint,
            "online": online,
            "sdCard": None if cloud is None else cloud.get("sdCard"),
            "cloud_error": None if cloud is not None else "camera cloud unavailable",
        }

    @app.get("/api/camera/frame")
    def camera_frame():
        cam.touch()
        try:
            with open(cam.frame_path, "rb") as f:
                data = f.read()
        except OSError:
            return JSONResponse({"error": "no frame yet"}, status_code=404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    PTZ_DIRS = {"stop": 0, "up": 1, "down": 2, "left": 3, "left_up": 4,
                "left_down": 5, "right": 6, "right_up": 7, "right_down": 8}

    @app.post("/api/camera/ptz")
    def camera_ptz(payload: dict = Body(default={})):
        # relay to the live session via ptz_cmd.json (dog_final sends cmd 4097)
        if cam.status()["state"] != "live":
            return JSONResponse({"error": "live view not running"},
                                status_code=409)
        p = payload or {}
        d = p.get("dir")
        if d not in PTZ_DIRS:
            return JSONResponse(
                {"error": "dir must be one of: " + ", ".join(PTZ_DIRS)},
                status_code=400)
        try:
            dur = min(abs(float(p.get("dur") or 1.0)), 5.0)
        except (TypeError, ValueError):
            dur = 1.0
        json.dump({"dir_byte": PTZ_DIRS[d], "dur": dur, "ts": time.time()},
                  open(PTZ_CMD_FILE, "w"))
        return {"sent": d, "dur": dur}

    def talk_cmd(action, file=None):
        json.dump(dict(action=action, file=file, ts=time.time()),
                  open(TALK_CMD_FILE, "w"))

    def g711_cache_path(name):
        cache = os.path.join(AUDIO_DIR, ".cache")
        os.makedirs(cache, exist_ok=True)
        return os.path.join(cache, os.path.splitext(name)[0] + ".al")

    @app.get("/api/camera/talk/files")
    def talk_files():
        try:
            names = sorted(n for n in os.listdir(AUDIO_DIR)
                           if n.lower().endswith(AUDIO_EXTS)
                           and not n.startswith("."))
        except OSError:
            names = []
        return {"files": names}

    @app.post("/api/camera/talk/play")
    def talk_play(payload: dict = Body(default={})):
        if cam.status()["state"] != "live":
            return JSONResponse({"error": "live view not running"},
                                status_code=409)
        name = (payload or {}).get("name") or ""
        if os.path.basename(name) != name or not name.lower().endswith(AUDIO_EXTS):
            return JSONResponse({"error": "bad name"}, status_code=400)
        src = os.path.join(AUDIO_DIR, name)
        if not os.path.isfile(src):
            return JSONResponse({"error": "no such file"}, status_code=404)
        dst = g711_cache_path(name)
        if not (os.path.exists(dst)
                and os.path.getmtime(dst) >= os.path.getmtime(src)):
            if not to_g711a(src, dst):
                return JSONResponse({"error": "convert failed"},
                                    status_code=500)
        talk_cmd("play", dst)
        return {"playing": name}

    @app.post("/api/camera/talk/stop")
    def talk_stop():
        talk_cmd("stop")
        return {"stopped": True}

    @app.post("/api/camera/talk/say")
    def talk_say(payload: dict = Body(default={})):
        if cam.status()["state"] != "live":
            return JSONResponse({"error": "live view not running"},
                                status_code=409)
        text = ((payload or {}).get("text") or "").strip()[:200]
        if not text:
            return JSONResponse({"error": "empty text"}, status_code=400)
        import hashlib
        cache = os.path.join(AUDIO_DIR, ".cache")
        os.makedirs(cache, exist_ok=True)
        aiff = os.path.join(cache, hashlib.md5(text.encode()).hexdigest()
                            + ".aiff")
        dst = aiff[:-5] + ".al"
        if not os.path.exists(dst):
            if not say_to_aiff(text, aiff):
                return JSONResponse({"error": "say failed"}, status_code=500)
            if not to_g711a(aiff, dst):
                return JSONResponse({"error": "convert failed"},
                                    status_code=500)
        talk_cmd("play", dst)
        return {"saying": text}

    # ---- ticket 07: motion-clip recorder ----

    @app.post("/api/recorder/mode")
    def recorder_mode(payload: dict = Body(default={})):
        # never starts the pipeline — recording only captures while the
        # user's own live-view session is running (standing on-demand rule)
        cam.set_recording(bool((payload or {}).get("on")))
        return recorder_state()

    def recorder_state():
        days = rec.days()
        clips = sum(len(rec.events(d)) for d in days)
        return {"mode": cam.recording, "pipeline": cam.status()["state"],
                "days": len(days), "clips": clips,
                "disk_mb": rec.disk_usage_mb(),
                "retention_days": retention_days}

    @app.get("/api/recorder/status")
    def recorder_status():
        return recorder_state()

    @app.get("/api/recorder/days")
    def recorder_days():
        return {"days": rec.days()}

    @app.get("/api/recorder/events")
    def recorder_events(day: str = ""):
        return {"day": day, "events": rec.events(day)}

    @app.get("/api/recorder/file")
    def recorder_file(name: str = ""):
        got = rec.read_file(name)
        if got is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        content, media = got
        return Response(content=content, media_type=media,
                        headers={"Cache-Control": "no-store"})

    return app


def main():
    port = int(load_env().get("CONTROL_HUB_PORT") or DEFAULT_PORT)
    hosts = ["127.0.0.1"]
    ts = tailscale_ip()
    if ts:
        hosts.append(ts)
    app = create_app()

    def retention_loop():
        days = env_int(load_env(), "RETENTION_DAYS", 14)
        while True:
            time.sleep(3600)
            try:
                app.state.recorder.clean_retention(days)
            except Exception as e:
                print("[rec] retention err:", e, flush=True)

    threading.Thread(target=retention_loop, daemon=True).start()
    # ticket 07: capture loop — polls motion + cuts clips while recording
    threading.Thread(target=app.state.recorder.run_forever, daemon=True).start()
    server = uvicorn.Server(uvicorn.Config(app, log_level="info"))
    sockets = []
    for h in hosts:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((h, port))
        s.listen(5)
        sockets.append(s)
        print("[hub] listening on http://%s:%d" % (h, port))
    server.run(sockets=sockets)


if __name__ == "__main__":
    main()
