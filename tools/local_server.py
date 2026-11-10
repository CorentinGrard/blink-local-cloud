#!/usr/bin/env python3
"""Local-only server for the Blink Mini — replaces Blink's cloud.

The camera trusts our cert (flash-patched, certs/v2) and reaches us because the
mitm AP redirects its :443 to this server on :8080. We answer the REST control
API and speak the CS binary command channel well enough to keep the camera
onboarded with no internet — and we can PUSH commands to it to drive it (e.g. to
make it stream), capturing whatever it does, fully decrypted.

Run (with the mitm AP up and the relay stopped — this takes :8080):
  .venv/bin/python tools/local_server.py

Pushing a command to the camera: drop a raw .bin file into tools/queue/. The
next command-poll (0x0806) long-poll will return its bytes instead of the empty
ack, then the file is moved to tools/queue/sent/. This is how we experiment with
liveview/snapshot commands and watch the camera react.
"""
import os, re, ssl, socket, struct, time, threading, datetime, glob, json
import urllib.request
import blink_proto as bp
import media_watch

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHAIN = os.path.join(ROOT, "certs/v2/chain-dup.pem")
KEY   = os.path.join(ROOT, "certs/v2/my-ca2-key.pem")
TPL   = os.path.join(HERE, "templates")
QUEUE = os.path.join(HERE, "queue"); os.makedirs(os.path.join(QUEUE, "sent"), exist_ok=True)
LOGDIR = os.path.join(ROOT, "mitm", "local"); os.makedirs(LOGDIR, exist_ok=True)

LISTEN = ("0.0.0.0", int(os.environ.get("BLINK_PORT", "8080")))
# BLINK_DEBUG=1 keeps protocol-debugging captures: raw camera uploads (*_0804.bin),
# the intermediate .ts once the .mp4 exists, every decrypted connection under
# flows/ and every 0x0806 poll under polls/. Off (default): only the playable
# media is kept (.jpg snapshots, .mp4 clips + .thumb.jpg).
DEBUG = os.environ.get("BLINK_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")
HEARTBEAT = open(os.path.join(TPL, "heartbeat_0807.bin"), "rb").read()
BUNDLE    = open(os.path.join(TPL, "bundle.bin"), "rb").read()
MEDIA_REPLY = open(os.path.join(TPL, "media_reply_0804.bin"), "rb").read()          # liveview session config
# Real cloud liveview opening (28B header + 9B timestamp + 145B session config),
# captured 2026-10-12 via the relay; see _capture_media for the rest of the exchange.
MEDIA_REPLY_LIVE = open(os.path.join(TPL, "media_reply_live_0804.bin"), "rb").read()
MEDIA_REPLY_SNAP = open(os.path.join(TPL, "media_reply_snapshot_0804.bin"), "rb").read()  # snapshot session config
CONFIG    = open(os.path.join(TPL, "config_0807.bin"), "rb").read()  # 174B post-onboarding config-fetch reply
START_TPL = open(os.path.join(TPL, "start_liveview_0806.bin"), "rb").read()  # 0x0806 reply w/ Kt + '04 <ip>'
ARM_TPL   = open(os.path.join(TPL, "arm_0806.bin"), "rb").read()             # 0x0806 reply w/ Kt + idle field

# camera settings we push in 0x0807 replies; editable live via tools/set_setting.py
SETTINGS_FILE = os.path.join(LOGDIR, "settings.json")
_bundle_served = {"done": False}

# live state published for tools/console.py (connection status + last capture)
STATE_FILE = os.path.join(LOGDIR, "state.json")
_state = {"onboarded": False, "req_count": 0}
_state_lock = threading.Lock()


# --- rolling media retention -------------------------------------------------
# Keep only the last BLINK_RETENTION_DAYS days of captured media/logs under
# LOGDIR; the small live-state files are always kept. Set BLINK_RETENTION_DAYS=0
# (or negative) to keep everything forever.
RETENTION_DAYS = float(os.environ.get("BLINK_RETENTION_DAYS", "7") or "0")
_RETAIN_KEEP = {"settings.json", "state.json", "kt_seq.json",
                "media_request.json", "media_stop"}

# interval (time-lapse) snapshots: take a snapshot every N seconds while the
# camera is online. Disabled by default (0). The LIVE value is the settings.json
# key "snapshot_interval" (set from the dashboard); this env var is only the
# initial default when that key is absent. See _timelapse_janitor.
SNAPSHOT_INTERVAL_DEFAULT = float(os.environ.get("BLINK_SNAPSHOT_INTERVAL", "0") or "0")

def _prune_old_media():
    """Delete files under LOGDIR older than RETENTION_DAYS (persistent state kept)."""
    if RETENTION_DAYS <= 0:
        return
    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = freed = 0
    for dirpath, _dirs, filenames in os.walk(LOGDIR, topdown=False):
        for name in filenames:
            if name in _RETAIN_KEEP or name.endswith(".tmp"):
                continue
            fp = os.path.join(dirpath, name)
            try:
                st = os.stat(fp)
                if st.st_mtime < cutoff:
                    os.remove(fp)
                    removed += 1
                    freed += st.st_size
            except OSError:
                pass
        if dirpath != LOGDIR:            # drop now-empty subdirectories
            try: os.rmdir(dirpath)
            except OSError: pass
    if removed:
        log(f"retention: removed {removed} file(s) older than {RETENTION_DAYS:g}d "
            f"({freed/1048576:.1f} MiB freed)")

def _retention_janitor():
    while True:                          # prune at startup, then hourly
        try: _prune_old_media()
        except Exception as e: log(f"(retention error: {e})")
        time.sleep(3600)


class LiveHub:
    """Fan-out of the live, de-chunked MPEG-TS to browser subscribers (webui.py /live.ts)."""
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()
    def subscribe(self):
        import queue as _q
        q = _q.Queue(maxsize=1024)
        with self._lock:
            self._subs.add(q)
        return q
    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)
    def publish(self, data):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(data)
            except Exception:
                pass          # slow/stalled browser: drop rather than block the camera
LIVE_HUB = LiveHub()


def _flush_state(data):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, STATE_FILE)      # atomic: readers never see a partial file
    except Exception:
        pass


def bump_seen(ip, opcode=None, action=None):
    """Record that the camera just talked to us (drives console's connected/stale)."""
    with _state_lock:
        _state["ip"] = ip
        _state["last_seen"] = time.time()
        _state["last_seen_str"] = datetime.datetime.now().strftime("%H:%M:%S")
        if opcode is not None:
            _state["last_opcode"] = f"0x{opcode:04x}"
        if action is not None:
            _state["last_action"] = action
        _state["req_count"] = _state.get("req_count", 0) + 1
        data = json.dumps(_state)
    _flush_state(data)


def set_state(**kw):
    with _state_lock:
        _state.update(kw)
        data = json.dumps(_state)
    _flush_state(data)


def _to_mp4(ts_path):
    """Remux a liveview MPEG-TS (H.264 + AAC) into .mp4 without re-encoding --
    ffmpeg -c copy, else GStreamer (tsdemux -> mp4mux, video only). Neither decodes, so the
    missing native H.264 decoder doesn't matter. Returns the mp4 path or None."""
    import shutil, subprocess
    mp4 = os.path.splitext(ts_path)[0] + ".mp4"
    cmds = []
    if shutil.which("ffmpeg"):
        cmds.append(["ffmpeg", "-y", "-loglevel", "error", "-i", ts_path,
                     "-c", "copy", "-movflags", "+faststart", mp4])
    if shutil.which("gst-launch-1.0"):
        # video only: the camera's AAC won't negotiate into mp4mux here
        cmds.append(["gst-launch-1.0", "-q", "-e", "filesrc", f"location={ts_path}", "!",
                     "tsdemux", "!", "queue", "!", "h264parse", "!",
                     "mp4mux", "faststart=true", "!", "filesink", f"location={mp4}"])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=120)
            if r.returncode == 0 and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
                return mp4
        except Exception:
            pass
    return None


_settings_cache = {}


def get_settings():
    # Defaults = the settings inside the real-cloud heartbeat template (the camera's
    # actual settings when it was captured), so with an untouched settings.json the
    # heartbeat we send is byte-identical to the cloud's (bar the clock).
    s = dict(bp.parse_settings(HEARTBEAT))
    override = {}
    try:
        with open(SETTINGS_FILE) as f:
            override = json.load(f)
        _settings_cache["last_good"] = override
    except FileNotFoundError:
        pass
    except Exception:
        # unreadable/corrupt file: keep the last good overrides rather than
        # pushing template defaults (which would e.g. disarm an armed camera)
        override = _settings_cache.get("last_good", {})
    s.update(override)
    # "like stock": the firmware runs motion detection only when BOTH the motion
    # byte and the armed byte are set (verified 2026-11-01 against a real-cloud
    # armed config). Enabling motion therefore also arms the camera, unless
    # settings.json pins 'armed' explicitly.
    if "armed" not in override:
        s["armed"] = 1 if s.get("motion") else 0
    # server-side automation knob (not a camera setting; patch_settings ignores
    # unknown keys, so this never touches the heartbeat/config bytes)
    s.setdefault("snapshot_interval", SNAPSHOT_INTERVAL_DEFAULT)
    return s
NOTHING_0806 = bytes.fromhex("010000000401000000040100000004")
# 86B Kt-echo reply: when the camera's 0x0806 poll carries a Kt block (media-session
# keepalive, 389B poll), the real cloud echoes the Kt sequence (bytes 29-30) instead
# of the flat 15B idle ack. A flat reply breaks the session and the camera degrades
# to a 122B hollow poll. (Isolated 2026-10-12 via the relay BLINK_FAKE diagnostic.)
_KT_ECHO_TPL = bytes.fromhex(
    "01000000040100000004010000000400000000000700054b7400000000"
    "021e0000000000000000000000000000000000000000000000000004000000010000"
    "0000000000000000000000000000000000000100000004")


def kt_echo(seq):
    """Build the 86B Kt-echo 0x0806 reply echoing the camera's poll Kt sequence."""
    return _KT_ECHO_TPL[:29] + struct.pack(">H", seq) + _KT_ECHO_TPL[31:]
CS_HOST = "ccs-e007.immedia-semi.com"   # DNS points this at us too
# The real cloud answers POST /ip_address with the camera's PUBLIC IP; the camera
# uses it as its server-reflexive media candidate. Returning its own LAN IP (what
# we used to do) leaves it with no usable candidate, so its media subsystem never
# comes up (hollow 122B polls, never media-ready). Hand it a public-looking IP so
# the media controller activates. The transport still goes wherever the 0x0806
# '04 <ip>' command points (= us). Overridable via BLINK_PUBLIC_IP.
PUBLIC_IP = os.environ.get("BLINK_PUBLIC_IP", "84.102.210.103")
# where the camera is told to upload media (it dials this; the router DNATs it to
# us). Same address used for liveview and motion-clip grants.
MEDIA_IP = os.environ.get("BLINK_MEDIA_IP", "10.10.10.1")
# motion auto-record: when armed, the camera opens a media session on its own (a
# motion event) and waits for the cloud to grant an upload target. We grant it to
# MEDIA_IP so it uploads the clip to us. Cooldown avoids re-granting the same clip.
_motion = {"last_grant": 0.0}
MOTION_COOLDOWN = float(os.environ.get("BLINK_MOTION_COOLDOWN", "35"))  # s between motion-clip grants
# ntfy push on motion. The topic is a secret (anyone with it can read/publish), so
# it's never baked into the image/compose: the server reads it from a file
# (BLINK_NTFY_TOPIC_FILE, mounted read-only) or BLINK_NTFY_TOPIC. No topic => off.
NTFY_URL = os.environ.get("BLINK_NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC_FILE = os.environ.get("BLINK_NTFY_TOPIC_FILE", "/app/ntfy-topic")

def _ntfy_topic():
    t = os.environ.get("BLINK_NTFY_TOPIC", "").strip()
    if t:
        return t
    try:
        return open(NTFY_TOPIC_FILE).read().strip()
    except Exception:
        return ""

def notify_motion():
    """Fire-and-forget ntfy notification that motion was detected."""
    topic = _ntfy_topic()
    if not topic:
        return

    def _send():
        try:
            req = urllib.request.Request(
                f"{NTFY_URL}/{topic}",
                data="Motion detected by the Blink camera".encode(),
                # NB: urllib encodes headers as latin-1, so keep header values
                # ASCII (an em-dash etc. raises and the POST never goes out).
                headers={"Title": "Blink: motion detected",
                         "Priority": "high",
                         "Tags": "rotating_light"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            log(f"  (ntfy notify failed: {e})")

    threading.Thread(target=_send, daemon=True).start()

# The motion push is sent once the clip has been uploaded, with its snapshot
# attached (ntfy shows image attachments inline). Tapping it opens BLINK_NTFY_CLICK
# (default: the dashboard). No snapshot => falls back to the text-only notify_motion.
NTFY_CLICK = os.environ.get("BLINK_NTFY_CLICK", "https://blink.lan").strip()
# Base URL the "Watch clip" button points at (the dashboard's /clips/ route).
CLIP_BASE_URL = os.environ.get("BLINK_CLIP_BASE_URL", "https://blink.lan").rstrip("/")

def notify_motion_snapshot(jpg_path, when=None, clip_path=None):
    """Fire-and-forget ntfy motion notification with the clip's thumbnail attached."""
    topic = _ntfy_topic()
    if not topic:
        return
    when = when or datetime.datetime.now().strftime("%H:%M:%S")

    def _send():
        try:
            body = open(jpg_path, "rb").read()
            # The body IS the attachment, so the text goes in the Message header
            # (ASCII only, see notify_motion).
            headers = {"Title": "Blink: motion detected",
                       "Message": f"Motion at {when}",
                       "Filename": "motion.jpg",
                       "Priority": "high",
                       "Tags": "rotating_light"}
            if NTFY_CLICK:
                headers["Click"] = NTFY_CLICK
            if clip_path and CLIP_BASE_URL:
                # ntfy action button; opens the MP4 in the phone's browser/player
                clip_url = f"{CLIP_BASE_URL}/clips/{os.path.basename(clip_path)}"
                headers["Actions"] = f"view, Watch clip, {clip_url}"
            req = urllib.request.Request(f"{NTFY_URL}/{topic}", data=body,
                                         headers=headers, method="PUT")
            urllib.request.urlopen(req, timeout=20).read()
        except Exception as e:
            log(f"  (ntfy snapshot notify failed: {e})")

    threading.Thread(target=_send, daemon=True).start()
_LATENCY = float(os.environ.get("BLINK_LATENCY", "0") or "0")   # artificial reply RTT (s)
# 0x0806 is a long-poll: after the per-message acks the real cloud keeps it open
# until the camera closes it (~10s); commands can be pushed meanwhile.
POLL_HOLD = float(os.environ.get("BLINK_POLL_HOLD", "15.0"))  # max 0x0806 long-poll hold (s); camera closes at ~10s

# ---- media (liveview/snapshot) commands ---------------------------------
# A media command is the 0x0806 reply's command message (after the 3 acks): a Kt
# block whose TYPE byte (reply offset 20, just before '00 05 Kt') picks the action:
#   0x07 + media field '04 00000001'  -> SNAPSHOT: camera uploads a JPEG (0x0804)
#                                        to the normal ccs host (SNAP_TPL)
#   0x05 + media field '04 <ip>'      -> LIVEVIEW: camera opens a 0x0804 stream
#                                        to <ip>:443 (START_TPL, IP patched in)
# The camera answers in-poll by echoing the Kt (e.g. '01 00000000' + '07 00 05 Kt
# ... <seq>'). There is no separate "arm" step: the old arm->trigger dance was a
# workaround for the truncated (hollow) polls -- the "arm" template IS the
# snapshot command, which is why liveview requests came back as JPEGs.
# Driven by mitm/local/media_request.json (console.py writes {"mode","ip","ts"});
# the request clears when the 0x0804 upload starts.
MEDIA_REQ = os.path.join(LOGDIR, "media_request.json")
MEDIA_REQ_TTL = 60.0        # a request older than this is abandoned
MEDIA_MAX_SENDS = 12        # command sends per request (in-poll retries count too)
POLL_RETRIES = 5            # immediate resends after a "03" refusal, per poll (2 seen needed)
SNAP_TPL = ARM_TPL
_media = {"active": False}


# Command Kt seq: the cloud numbers commands with one ever-increasing counter
# (0x0229..0x022e on 2026-10-12, persisted on the camera). We used to restart at
# 0x01f3 for every request; reusing old seqs is suspected of making the camera
# answer with a '03 ... 1a 00 05 Kt 21 <seq>' refusal and not start the media.
# Keep our own counter, continuing above the highest seq the cloud used.
SEQ_FILE = os.path.join(LOGDIR, "kt_seq.json")
SEQ_START = 0x0230


def _next_seq():
    try:
        seq = int(json.load(open(SEQ_FILE))["next"])
    except Exception:
        seq = SEQ_START
    try:
        json.dump({"next": (seq + 1) & 0xffff}, open(SEQ_FILE, "w"))
    except Exception:
        pass
    return seq


def _media_poll(poll_len, poll_seq):
    """Decide the 0x0806 reply when a media request is active. Returns the command
    bytes to send, or None if there is no active request (fall back to queue/idle)."""
    try:
        req = json.load(open(MEDIA_REQ))
    except Exception:
        req = None
    now = time.time()
    if not req or now - req.get("ts", 0) > MEDIA_REQ_TTL:
        if _media.get("active"):
            log("  (media request expired/cleared)")
        _media["active"] = False
        return None
    if not _media.get("active") or _media.get("ts") != req.get("ts"):
        _media.update({"active": True, "ts": req.get("ts"),
                       "ip": req.get("ip", "192.168.200.1"),
                       "mode": req.get("mode", "liveview"), "sends": 0,
                       "live": bool(req.get("live")), "secs": req.get("secs"),
                       "motion": bool(req.get("motion"))})
        log(f"  >> MEDIA REQUEST active: mode={_media['mode']} ip={_media['ip']}")
    if _media["sends"] >= MEDIA_MAX_SENDS:
        return None
    _media["sends"] += 1
    _media["last_mode"] = _media["mode"]      # survives the request being cleared
    _media["last_opts"] = {"live": _media.get("live"), "secs": _media.get("secs")}
    _media["last_motion"] = _media.get("motion", False)   # -> ntfy push after capture
    seq = _next_seq()
    if _media["mode"] == "snapshot":
        log(f"  >> SNAPSHOT command #{_media['sends']} seq 0x{seq:04x} (poll {poll_len}B)")
        return bp.set_kt_seq(SNAP_TPL, seq)
    log(f"  >> LIVEVIEW command #{_media['sends']} seq 0x{seq:04x} -> stream to "
        f"{_media['ip']}:443 (poll {poll_len}B)")
    return bp.set_media_ip(bp.set_kt_seq(START_TPL, seq), _media["ip"])


def _clear_media_request():
    try:
        os.remove(MEDIA_REQ)
    except Exception:
        pass
    _media["active"] = False


def ts_patch(hb):
    """Refresh the timestamp (offset 28: YYYY MM DD HH MM SS) to 'now'."""
    hb = bytearray(hb)
    n = datetime.datetime.now(datetime.timezone.utc)
    hb[28:35] = struct.pack(">H", n.year) + bytes([n.month, n.day, n.hour, n.minute, n.second])
    return bytes(hb)


def server_ctx():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=CHAIN, keyfile=KEY)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try: ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError: pass
    return ctx
SRV = server_ctx()


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------- REST ----------------
# The real cloud (nginx + Phusion Passenger) sends a specific header set and
# Connection: keep-alive. We mirror it byte-for-byte so the camera receives an
# identical REST response to the real cloud -- this is the one pre-first-poll
# exchange that could gate the camera coming up "active" vs "hollow" (see
# docs/PROTOCOL.md §5.1). Observed order on /owl/status:
#   Date, Content-Type, Content-Length, Connection, Client-Verified, Server,
#   Status, X-Blink-Trace-Id, X-Content-Type-Options, X-Powered-By
def _trace_id():
    import secrets
    return "1-%08x-%024x" % (int(time.time()), secrets.randbits(96))


def http_response(status="200 OK", body=b"", ctype="application/json",
                  keep_alive=True, account_id=None,
                  x_frame_options=False, x_xss_protection=False):
    # Header set + ORDER mirror the real cloud (nginx + Phusion) exactly, per
    # endpoint, so the camera receives byte-identical REST replies (Date and
    # X-Blink-Trace-Id are the only legitimately-dynamic fields). Observed order:
    #   Date, Content-Type, Content-Length, Connection, [Account-Id,]
    #   Client-Verified, Server, Status, X-Blink-Trace-Id, X-Content-Type-Options,
    #   [X-Frame-Options,] X-Powered-By, [X-Xss-Protection]
    if isinstance(body, str): body = body.encode()
    date = datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    h = [f"HTTP/1.1 {status}",
         f"Date: {date}",
         f"Content-Type: {ctype}",
         f"Content-Length: {len(body)}",
         f"Connection: {'keep-alive' if keep_alive else 'close'}"]
    if account_id:
        h.append(f"Account-Id: {account_id}")
    h += ["Client-Verified: 0",
          "Server: nginx + Phusion Passenger(R)",
          f"Status: {status}",
          f"X-Blink-Trace-Id: {_trace_id()}",
          "X-Content-Type-Options: nosniff"]
    if x_frame_options:
        h.append("X-Frame-Options: SAMEORIGIN")
    h.append("X-Powered-By: Phusion Passenger(R)")
    if x_xss_protection:
        h.append("X-Xss-Protection: 1; mode=block")
    return ("\r\n".join(h) + "\r\n\r\n").encode() + body


def handle_rest(conn, first, peer):
    # Honor HTTP keep-alive: serve requests on this connection until the camera
    # closes it (it usually opens a fresh connection per call, but the real cloud
    # advertises keep-alive, so we do too and frame requests by Content-Length).
    buf = first
    conn.settimeout(10.0)
    try:
        while True:
            while b"\r\n\r\n" not in buf:
                more = conn.recv(4096)
                if not more: return
                buf += more
            head, _, buf = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            line = lines[0].decode("latin1")
            clen = 0
            for h in lines[1:]:
                if h.lower().startswith(b"content-length:"):
                    try: clen = int(h.split(b":", 1)[1].strip())
                    except ValueError: clen = 0
            while len(buf) < clen:                      # consume request body
                more = conn.recv(4096)
                if not more: break
                buf += more
            reqbody = buf[:clen]; buf = buf[clen:]
            log(f"REST {peer[0]}  {line}")
            try: method, path, _ = line.split(" ", 2)
            except ValueError: method, path = "GET", "/"
            p = path.split("?", 1)[0]
            bump_seen(peer[0], action=f"REST {p}")
            if p == "/api/v1/owl/status":
                resp = http_response(body=json.dumps({"act": "cs", "host": CS_HOST, "wait": 5},
                                                      separators=(",", ":")))
            elif p.endswith("/ip_address"):
                log(f"  ip_address -> {PUBLIC_IP} (public-looking reflexive candidate; camera LAN={peer[0]})")
                resp = http_response(body=PUBLIC_IP, ctype="text/html;charset=utf-8",
                                     account_id="311179", x_frame_options=True,
                                     x_xss_protection=True)
            elif p.endswith("/onboarded"):
                resp = http_response(body=json.dumps({"message": "Onboard Status Updated"},
                                                      separators=(",", ":")),
                                     account_id="311179")
            else:
                log(f"  !! unknown REST path {p} -- returning 200 {{}} (capture & implement)")
                with open(os.path.join(LOGDIR, "unknown_rest.log"), "ab") as f:
                    f.write(line.encode() + b"\n" + reqbody[:2048] + b"\n====\n")
                resp = http_response(body="{}")
            conn.sendall(resp)
    except socket.timeout:
        return
    except (ConnectionResetError, BrokenPipeError, OSError):
        return


# ---------------- CS binary ----------------
def next_queued():
    files = sorted(glob.glob(os.path.join(QUEUE, "*.bin")))
    if not files: return None
    f = files[0]
    data = open(f, "rb").read()
    os.rename(f, os.path.join(QUEUE, "sent", os.path.basename(f)))
    return os.path.basename(f), data


def _read_request(conn, first):
    """Read a whole 0x0807 / 0x0804 request without waiting for silence. Byte 0x78
    of the 366B first record says whether more follows: 0x0e = heartbeat (445B,
    the camera sends 79B more in 4 records ~80ms later); 0x02 = config/bundle fetch
    or media upload header (complete at 366B; an upload waits for our reply).
    Unknown layouts fall back to a 250ms idle gap."""
    data = first
    want = None
    if len(first) > 0x78:
        want = {0x0e: 445, 0x02: len(first)}.get(first[0x78])
    if want is None:
        more, _ = bp.read_until_idle(conn, idle=0.25, first_wait=0.25)
        return data + more
    conn.settimeout(2.0)
    try:
        while len(data) < want:
            more = conn.recv(4096)
            if not more:
                break
            data += more
    except (socket.timeout, OSError):
        pass
    finally:
        conn.settimeout(None)
    return data


def handle_cs(conn, first, peer):
    data = first
    n0 = len(first)
    acks, eof = 0, False
    if len(first) >= 30 and struct.unpack(">H", first[28:30])[0] == 0x0806:
        # lock-step poll: ack each camera message (see bp.converse_0806)
        data, acks, eof = bp.converse_0806(conn, first)
    else:
        data = _read_request(conn, first)
    if len(data) < 30:
        return
    opcode = struct.unpack(">H", data[28:30])[0]
    action = bp.describe('C->S', data, {})
    grew = f" (first recv {n0}B -> {len(data)}B)" if len(data) != n0 else ""
    log(f"CS   {peer[0]}  {action}  req={len(data)}B{grew}")
    bump_seen(peer[0], opcode, action)
    if DEBUG and opcode == 0x0806:   # save the raw poll so we can diff it vs the real cloud
        try:
            pd = os.path.join(LOGDIR, "polls"); os.makedirs(pd, exist_ok=True)
            open(os.path.join(pd, f"{datetime.datetime.now():%H%M%S}_{len(data)}B_0806.bin"), "wb").write(data)
        except Exception:
            pass

    if opcode == 0x0806:                      # command poll (long-poll)
        poll_seq = bp._kt_seq(data)           # camera's live media-session seq, if any
        media_ready = poll_seq is not None
        set_state(poll_size=len(data),
                  poll_seq=(f"0x{poll_seq:04x}" if media_ready else None),
                  media_ready=media_ready)
        if media_ready:
            log(f"  camera poll carries a media Kt (seq 0x{poll_seq:04x}) -- media-ready")
        # EXPERIMENT (BLINK_EMPTY_HOLLOW=1): the ONE time the real cloud saw a 122B
        # hollow poll (no identity/candidate block) it replied with 0 bytes, and the
        # camera recovered to a full 323B media-active poll next time. Our 15B ack may
        # be why the camera stays hollow. If enabled, reply empty to a hollow poll to
        # try to force re-init. Opt-in so it can't silently break the onboard loop.
        hollow = (poll_seq is None) and len(data) < 200
        if os.environ.get("BLINK_EMPTY_HOLLOW") == "1" and hollow and not _media.get("active"):
            try:
                if not os.path.exists(MEDIA_REQ):
                    log(f"  hollow poll ({len(data)}B) -> replying EMPTY (0B) to force re-init [experiment]")
                    return
            except Exception:
                return
        # The acks were sent per camera message by converse_0806. Now hold the
        # poll open like the real cloud (until the camera closes it, ~10s) and push
        # a command the moment there is one -- the cloud pushes mid-poll too, so a
        # media request doesn't wait for the next poll. The camera answers a
        # command in the same poll; ack each of its messages (any non-5B record).
        def next_command():
            p = _media_poll(len(data), poll_seq)          # console media request
            if p is None:
                q = next_queued()                           # manual .bin in tools/queue/
                if q:
                    name, p = q
                    if media_ready:
                        p = bp.set_kt_seq(p, poll_seq)
                    log(f"  >> PUSHING queued command {name} ({len(p)}B) to camera")
            if p is None and poll_seq is not None:
                p = kt_echo(poll_seq)
            return p
        pushed, retries = False, 0
        hdr = b""
        conn.settimeout(0.1)
        deadline = time.time() + POLL_HOLD
        try:
            while not eof and time.time() < deadline:
                if not pushed:
                    payload = next_command()
                    if payload is not None:
                        cmd = bp.split_command(payload, acks)
                        if cmd:
                            conn.sendall(cmd)
                        pushed = True
                try:
                    rec = conn.recv(4096)
                except socket.timeout:
                    continue
                if not rec:
                    break
                if len(rec) == 5:
                    hdr = rec                        # message header; body follows
                    continue
                conn.sendall(bp.ACK_0806)            # ack every camera message
                # the header belongs to this message only; a later single-record
                # message must not inherit it (a stale '03' looked like a refusal)
                refused, hdr = hdr[:1] == b"\x03", b""
                if refused:
                    # '03' + '1a 00 05 Kt 21 <seq> ...' = the camera REFUSED the
                    # command. The real cloud acks it and at once resends the command
                    # with the next seq in the same poll, which is accepted (cloud
                    # 16:16:08, 16:26:11, 16:40:49). Retrying only on later polls
                    # kept the camera refusing until it was power-cycled.
                    if retries < POLL_RETRIES:
                        retries += 1; pushed = False
                        log(f"  camera refused the command ({rec[:16].hex()}); "
                            f"retrying now (#{retries})")
                    else:
                        log(f"  camera refused the command ({rec[:16].hex()}); giving up this poll")
                else:
                    log(f"  camera answered in-poll: {len(rec)}B {rec[:24].hex()}")
                    # Armed motion event: when it detects motion the camera
                    # spontaneously opens a media session (a mid-poll Kt record).
                    # Grant a clip upload to our media IP via the normal media
                    # request path so the camera uploads the motion clip.
                    if (bp.KT in rec and not _media.get("active")
                            and get_settings().get("armed")
                            and time.time() - _motion["last_grant"] > MOTION_COOLDOWN):
                        _motion["last_grant"] = time.time()
                        secs = int(get_settings().get("clip_len", 30))
                        try:
                            json.dump({"mode": "liveview", "ip": MEDIA_IP, "live": True,
                                       "secs": secs, "ts": time.time(), "motion": True},
                                      open(MEDIA_REQ, "w"))
                            log(f"  >> MOTION detected -> requesting {secs}s clip upload to {MEDIA_IP}")
                            # ntfy push is sent after the clip upload, with its snapshot
                            pushed = False     # re-run next_command this poll to push START
                        except Exception as e:
                            log(f"  (motion request write failed: {e})")
        except OSError:
            pass
        conn.settimeout(None)
    elif opcode == 0x0807:                    # heartbeat (445) OR bundle/config fetch (366)
        st = get_settings()
        if len(data) <= 400:                  # 366-byte body = bundle (onboarding) then config
            if not _bundle_served["done"]:
                _bundle_served["done"] = True
                set_state(onboarded=True)
                log(f"  >> serving BUNDLE ({len(BUNDLE)}B, our certs) [onboarding]")
                conn.sendall(BUNDLE)
            else:
                cfg = bp.patch_settings(ts_patch(CONFIG), **st)
                log(f"  >> serving CONFIG ({len(cfg)}B, {bp.fmt_settings(st)})")
                conn.sendall(cfg)
                set_state(settings_pushed=time.time())   # web UI: "applied to camera"
        else:                                 # 445-byte body = heartbeat
            # The heartbeat reply carries the camera's settings (offsets in
            # bp.SETTINGS_OFFSETS[210]); the cloud's is constant while the settings
            # don't change. An earlier "patch_settings corrupts byte 0x4e" was really
            # a stale settings.json (ir=1) overwriting the camera's real ir=7.
            hb = bp.patch_settings(ts_patch(HEARTBEAT), **get_settings())
            conn.sendall(hb)
            set_state(settings_pushed=time.time())       # web UI: "applied to camera"
    elif opcode == 0x0804:                    # MEDIA upload: liveview H.264 OR snapshot JPEG
        # The header alone doesn't reliably tell snapshot from liveview, but we
        # know which command we sent; fall back to the header guess otherwise.
        kind = _media.pop("last_mode", None) or bp.media_kind(data)[0]
        opts = _media.pop("last_opts", None) or {}
        motion = _media.pop("last_motion", False)
        if kind == "liveview":
            reply = MEDIA_REPLY_LIVE
        elif kind == "snapshot":
            # the 191B template = 182B opening + '08' stop; the stop goes out only
            # once the JPEG has arrived (see _capture_media), or the camera quits
            reply = MEDIA_REPLY_SNAP[:182]
        else:
            reply = MEDIA_REPLY
        log(f"  >> MEDIA upload ({kind}); replying session config ({len(reply)}B), capturing")
        conn.sendall(ts_patch(reply))
        _capture_media(peer, data, conn, kind, opts, motion=motion)
    else:
        log(f"  !! unknown opcode 0x{opcode:04x}; logging raw, holding open")
        _dump(peer, "cs_unknown", data, conn)


# Liveview stream control, as the real cloud does it (relay capture 2026-10-12):
#   S> opening (MEDIA_REPLY_LIVE) | C> 51B handshake | S> LIVE_GO -> stream starts
#   S> LIVE_REPORT every 1s while streaming (a receiver report; the cloud's counters
#      drift a little, we replay a typical one) | S> LIVE_STOP_1, LIVE_STOP_2 -> end
LIVE_GO = bytes.fromhex("0d0000000100000000")
LIVE_REPORT = bytes.fromhex("12000003e8000000180000001f0000003f0010000000000012"
                            "0000001f00000000")
LIVE_STOP_1 = bytes.fromhex("070000000000000000")
LIVE_STOP_2 = bytes.fromhex("080000000000000000")
LIVE_HANDSHAKE = 51        # camera's liveview handshake: 7 + 9 + 9 + 26 bytes
MEDIA_END = bytes.fromhex("010000000000000000")   # camera: end of a snapshot upload
LIVE_SECS = float(os.environ.get("BLINK_LIVE_SECS", "20"))   # default clip length


class _PcrFixer:
    """Make the camera's MPEG-TS playable in real time by a timestamp-driven player.
    The camera sends a PCR (the TS clock reference) only every 2 s; the spec wants
    <= 100 ms. GStreamer's tsdemux then mis-maps time and jumps timestamps back
    ~1.5 s once per 2 s keyframe group -> the window freezes periodically.
    (The PES timestamps themselves are fine: one per frame, real time.) Fix: put a
    PCR-only packet (adaptation field, no payload, so continuity counters are
    untouched) before every video PES with a PTS, at PTS - delay, and rewrite the
    camera's own PCRs to the same relation. `delay` is the playback cushion; the
    camera's own relation is 1.5 s."""
    def __init__(self, delay, pid=0x100):
        self.pid, self.off, self.rem, self.pts = pid, int(delay * 90000), b"", None

    def _pcr(self):
        base = max(0, self.pts - self.off) & ((1 << 33) - 1)
        return bytes([(base >> 25) & 0xff, (base >> 17) & 0xff, (base >> 9) & 0xff,
                      (base >> 1) & 0xff, ((base & 1) << 7) | 0x7e, 0x00])

    def feed(self, data):
        buf = self.rem + data
        n = len(buf) // 188
        out = bytearray()
        for i in range(n):
            p = bytearray(buf[i * 188:(i + 1) * 188])
            pid = ((p[1] & 0x1f) << 8) | p[2]
            afc = (p[3] >> 4) & 3
            pos = 4 + (1 + p[4] if afc in (2, 3) else 0)
            if pid == self.pid and p[1] & 0x40 and afc in (1, 3) \
                    and p[pos:pos + 3] == b"\x00\x00\x01" and p[pos + 7] & 0x80:
                q = p[pos + 9:pos + 14]
                self.pts = (((q[0] >> 1) & 7) << 30 | q[1] << 22 | (q[2] >> 1) << 15
                            | q[3] << 7 | q[4] >> 1)
                af = bytes([183, 0x10]) + self._pcr()
                out += bytes([0x47, (pid >> 8) & 0x1f, pid & 0xff, 0x20]) + af \
                    + b"\xff" * (184 - len(af))
            if afc in (2, 3) and p[4] >= 7 and p[5] & 0x10 and self.pts is not None:
                p[6:12] = self._pcr()                        # camera's own PCR
            out += p
        self.rem = buf[n * 188:]
        return bytes(out)


class _WebLiveFeeder:
    """De-chunk the camera's 0x0804 stream (9B <type><u32 ctr><u32 len> headers) on
    the fly and publish the RAW type-0x00 MPEG-TS payload to LIVE_HUB for browser
    mpegts.js. No PCR fix-up: mpegts.js times off the PES PTS (fine as-is), and the
    PCR-only packets _PcrFixer injects break mpegts.js continuity tracking -> black."""
    def __init__(self):
        self.buf = b""
    def feed(self, data):
        self.buf += data
        out = bytearray()
        while len(self.buf) >= 9:
            t, n = self.buf[0], int.from_bytes(self.buf[5:9], "big")
            if n > 65536:
                self.buf = b""; break         # lost framing; resync on next upload
            if len(self.buf) < 9 + n:
                break
            if t == 0x00:
                out += self.buf[9:9 + n]
            self.buf = self.buf[9 + n:]
        if out:
            LIVE_HUB.publish(bytes(out))
    def close(self):
        pass


class _LivePlayer:
    """Show a liveview as it arrives: de-chunk the 0x0804 stream on the fly (9B
    headers <type><u32 ctr><u32 len>, see media_watch) and pipe the type-0x00
    MPEG-TS payload into a GStreamer window (GPU H.264 decode + AAC audio).
    Playback follows the stream's timestamps (sync=true) BLINK_LIVE_DELAY seconds
    behind live (default 1.5 s, the camera's own PCR offset), which absorbs the
    camera's keyframe bursts; _PcrFixer makes those timestamps usable."""
    _Q = ["queue", "max-size-time=5000000000", "max-size-buffers=0", "max-size-bytes=0"]
    CMD = ["gst-launch-1.0", "-q", "fdsrc", "fd=0", "!", "tsdemux", "name=d",
           "d.", "!", *_Q, "!", "h264parse", "!", "nvh264dec", "!", "videoconvert", "!",
           "autovideosink", "sync=true",
           "d.", "!", *_Q, "!", "aacparse", "!", "avdec_aac", "!", "audioconvert", "!",
           "audioresample", "!", "autoaudiosink", "sync=true"]

    def __init__(self):
        import shutil, subprocess
        self.buf = b""
        self.proc = None
        self.pcr = _PcrFixer(float(os.environ.get("BLINK_LIVE_DELAY", "1.5")))
        if not shutil.which("gst-launch-1.0"):
            log("  (live view: gst-launch-1.0 not found; recording only)"); return
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            log("  (live view: no display in local_server's environment; recording only)"); return
        cmd = os.environ.get("BLINK_LIVE_PLAYER")
        cmd = cmd.split() if cmd else self.CMD
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log("  live view: player window opening")
        except Exception as e:
            log(f"  (live view: player failed to start: {e})")

    def feed(self, data):
        if not self.proc:
            return
        self.buf += data
        out = bytearray()
        while len(self.buf) >= 9:
            t, n = self.buf[0], int.from_bytes(self.buf[5:9], "big")
            if n > 65536:                       # lost framing: stop feeding, keep recording
                log("  (live view: lost chunk framing; player stopped)"); self.close(); return
            if len(self.buf) < 9 + n:
                break
            if t == 0x00:
                out += self.buf[9:9 + n]
            self.buf = self.buf[9 + n:]
        out = self.pcr.feed(bytes(out)) if out else b""
        if out:
            try:
                self.proc.stdin.write(out); self.proc.stdin.flush()
            except (BrokenPipeError, OSError):
                log("  (live view: player closed; still recording)"); self.proc = None

    def close(self):
        if self.proc:
            try:
                self.proc.stdin.close()             # EOS: the player finishes and exits
            except Exception:
                pass
            self.proc = None


MEDIA_STOP = os.path.join(LOGDIR, "media_stop")   # console touches this to end a stream
LIVE_MAX_SECS = 600.0                            # safety cap for an open-ended live view


def _liveview_session(conn, fh, idle, live=False, secs=None):
    """Run the server side of a liveview 0x0804 stream, writing camera bytes to fh.
    Streams for `secs` (default BLINK_LIVE_SECS), or until mitm/local/media_stop
    appears; with live=True the video is also shown as it arrives."""
    # 1) the camera's handshake (7+9+9+26 = 51B), then the go-ahead
    hs, eof = b"", False
    conn.settimeout(3.0)
    try:
        while len(hs) < LIVE_HANDSHAKE:
            d = conn.recv(4096)
            if not d:
                eof = True; break
            hs += d
    except socket.timeout:
        pass
    conn.settimeout(None)
    fh.write(hs)
    if eof:
        log("  !! camera closed the liveview stream before the handshake"); return
    try:
        os.remove(MEDIA_STOP)                       # a stale stop must not end us at once
    except OSError:
        pass
    secs = min(float(secs or LIVE_SECS), LIVE_MAX_SECS)
    player = _LivePlayer() if live else None
    web = _WebLiveFeeder()                        # always publish to the browser hub
    if len(hs) > LIVE_HANDSHAKE:
        if player:
            player.feed(hs[LIVE_HANDSHAKE:])      # stream bytes that came with it
        web.feed(hs[LIVE_HANDSHAKE:])
    conn.sendall(LIVE_GO)
    log(f"  liveview: handshake {len(hs)}B -> go; streaming up to {secs:.0f}s"
        f"{' (live view; stop from the console)' if live else ''}")
    set_state(streaming={"since": time.time(), "live": bool(live), "secs": secs})
    # 2) stream: read, and send a receiver report every second
    t_end = time.time() + secs
    next_report = time.time() + 1.0
    last_data = time.time()
    conn.settimeout(0.2)
    try:
        while time.time() < t_end:
            if os.path.exists(MEDIA_STOP):
                log("  liveview: stop requested")
                break
            try:
                d = conn.recv(65536)
                if not d:
                    log("  liveview: camera ended the stream"); return
                fh.write(d); last_data = time.time()
                if player:
                    player.feed(d)
                web.feed(d)
            except socket.timeout:
                if time.time() - last_data > idle:
                    log(f"  !! liveview: no data for {idle:.0f}s, giving up"); return
            if time.time() >= next_report:
                conn.sendall(LIVE_REPORT); next_report += 1.0
        # 3) stop, drain what's in flight, close
        conn.sendall(LIVE_STOP_1); conn.sendall(LIVE_STOP_2)
        conn.settimeout(0.3)
        try:
            while True:
                d = conn.recv(65536)
                if not d: break
                fh.write(d)
                if player:
                    player.feed(d)
                web.feed(d)
        except Exception:
            pass
        log("  liveview: sent stop")
    finally:
        if player:
            player.close()
        web.close()
        try:
            os.remove(MEDIA_STOP)
        except OSError:
            pass
        set_state(streaming=None)


def _capture_media(peer, first, conn, kind="media", opts=None, motion=False):
    """Drain a 0x0804 upload to disk, then de-chunk it (media_watch.split_streams):
    snapshot -> .jpg, liveview -> .ts (H.264+AAC) + .thumb.jpg."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # motion clips are tagged in the filename so the gallery can tell them apart
    raw = os.path.join(LOGDIR, f"{ts}_{peer[0]}_{'motion' if motion else kind}_0804.bin")
    fh = open(raw, "wb"); fh.write(first)
    # An upload starting means the request was honoured: clear it NOW, or every
    # poll during the upload sends another ARM (camera red-LEDs mid-upload).
    _clear_media_request()
    # The camera keeps the connection open after its last byte, so end on a short
    # idle gap (liveview streams continuously) rather than a 30s timeout -- the
    # console's 50s wait used to expire before we published last_media.
    idle = float(os.environ.get("BLINK_MEDIA_IDLE", "5"))
    try:
        if kind == "liveview":
            _liveview_session(conn, fh, idle, **(opts or {}))
        else:
            # A snapshot ends with a 9B end marker (01 00000000 00000000) after the
            # JPEG + a 152B type-02 trailer; the camera then waits for the cloud's
            # '08' stop. Send it right away instead of waiting out an idle timeout.
            conn.settimeout(idle)
            tail = b""
            while True:
                d = conn.recv(65536)
                if not d: break
                fh.write(d)
                tail = (tail + d)[-9:]
                if tail == MEDIA_END:
                    conn.sendall(LIVE_STOP_2)
                    log("  snapshot complete -> sent stop")
                    break
    except Exception:
        pass
    finally:
        fh.close()
    data = open(raw, "rb").read()
    log(f"  upload saved: {os.path.relpath(raw, ROOT)} ({len(data)} bytes)")
    out = None; frames = None; thumb_path = None
    # Classify on the COMPLETE upload: the first recv (~366B) usually ends before
    # the JPEG SOI / TS payload starts, so the early guess is often just "media".
    kind = bp.media_kind(data)[0]
    try:
        # The upload is chunked (<type:1><u32 ctr><u32 len> + payload, see
        # media_watch.py): type 0x04 = JPEG, type 0x00 = MPEG-TS. Carving a JPEG
        # straight out of the raw stream leaves the 9B chunk headers inside it
        # (image corrupt below the first chunk), so de-chunk first.
        streams = media_watch.split_streams(data)
        tsb, jpg = streams.get(0x00, b""), streams.get(0x04, b"")
        if tsb[:1] == b"\x47" and len(tsb) >= 188:       # liveview: H.264+AAC in TS
            kind, frames = "liveview", len(tsb) // 188
            out = raw[:-4] + ".ts"
            open(out, "wb").write(tsb)
            log(f"  saved liveview MPEG-TS: {os.path.relpath(out, ROOT)} ({frames} TS packets)  "
                f"view: gst-launch-1.0 filesrc location={out} ! tsdemux ! h264parse ! "
                f"nvh264dec ! videoconvert ! autovideosink")
            thumb = media_watch._carve_jpeg(jpg)
            if thumb:
                thumb_path = raw[:-4] + ".thumb.jpg"
                open(thumb_path, "wb").write(thumb)
            mp4 = _to_mp4(out)
            if mp4:
                out = mp4
                log(f"  remuxed MP4 (H.264+AAC, no re-encode): {os.path.relpath(mp4, ROOT)}  file://{mp4}")
            else:
                log("  (MP4 remux failed: no ffmpeg/gst-launch-1.0? kept the .ts)")
        else:
            clean = media_watch._carve_jpeg(jpg) or media_watch._carve_jpeg(data)
            if clean:
                kind = "snapshot"
                out = raw[:-4] + ".jpg"
                open(out, "wb").write(clean)
                log(f"  saved JPEG snapshot: {os.path.relpath(out, ROOT)} ({len(clean)} bytes, "
                    f"{'de-chunked' if jpg else 'raw-carve'})  file://{out}")
            else:
                log(f"  (no JPEG or MPEG-TS found in upload; streams={ {hex(k): len(v) for k, v in streams.items()} })")
    except Exception as e:
        log(f"  (decode skipped: {e})")
    if not DEBUG and out:
        # keep only the playable result; the raw upload (and the .ts once the
        # .mp4 exists) are protocol-debugging leftovers -- set BLINK_DEBUG=1 to keep
        drop = [raw]
        if out.endswith(".mp4"):
            drop.append(out[:-4] + ".ts")
        for f in drop:
            try: os.remove(f)
            except OSError: pass
        raw = None
    if motion:   # single ntfy push: with the snapshot if we got one, else text-only
        when = datetime.datetime.strptime(ts, "%Y%m%d-%H%M%S").strftime("%H:%M:%S")
        if thumb_path:
            notify_motion_snapshot(thumb_path, when,
                                   out if out and out.endswith(".mp4") else None)
        else:
            notify_motion()
    try:
        set_state(last_media={"ts": time.time(), "kind": kind,
                              "raw": (os.path.relpath(raw, ROOT) if raw else None),
                              "out": (os.path.relpath(out, ROOT) if out else None),
                              "abs": (out or raw),
                              "frames": frames, "bytes": len(data), "motion": bool(motion)})
    except Exception:
        pass
    _clear_media_request()        # streaming started -> request satisfied


def _dump(peer, kind, first, conn):
    """Log an unrecognized / media stream fully; keep reading."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    fn = os.path.join(LOGDIR, f"{ts}_{peer[0]}_{peer[1]}_{kind}.bin")
    fh = open(fn, "wb"); fh.write(first); fh.flush()
    log(f"  capturing -> {os.path.relpath(fn, ROOT)}")
    conn.settimeout(30)
    try:
        while True:
            d = conn.recv(65536)
            if not d: break
            fh.write(d); fh.flush()
    except Exception:
        pass
    finally:
        fh.close()


class TeeConn:
    """Wraps the TLS socket and records every decrypted byte both ways, so we can
    dump the complete request+reply exchange to mitm/local/flows/ (same format as
    the relay's mitm/flows/) and diff local vs real-cloud byte-for-byte."""
    def __init__(self, sock):
        self._s = sock
        self.c2s = bytearray()   # camera -> us
        self.s2c = bytearray()   # us -> camera
    def recv(self, n):
        d = self._s.recv(n)
        if DEBUG: self.c2s += d
        return d
    def sendall(self, d):
        # BLINK_LATENCY=<seconds> adds artificial reply latency to mimic the real
        # cloud's RTT (it is ~tens of ms away; local_server answers in microseconds).
        # Test whether the camera refuses to go "online" against an implausibly-fast
        # server. Applied once per connection (on the first reply) to approximate RTT.
        if _LATENCY and not getattr(self, "_delayed", False):
            self._delayed = True
            time.sleep(_LATENCY)
        if DEBUG: self.s2c += d
        return self._s.sendall(d)
    def __getattr__(self, name):
        return getattr(self._s, name)


def _save_flow(peer, c2s, s2c):
    try:
        fd = os.path.join(LOGDIR, "flows"); os.makedirs(fd, exist_ok=True)
        # tag: REST path or CS opcode, for readable filenames
        if c2s[:4] in (b"GET ", b"POST", b"PUT ", b"HEAD", b"DELE"):
            tag = "REST_" + c2s.split(b"\r\n", 1)[0].decode("latin1").split(" ")[1].split("?")[0].strip("/").replace("/", "-")[:40]
        elif len(c2s) >= 30 and struct.unpack(">I", bytes(c2s[:4]))[0] == 0x29:
            tag = f"CS{struct.unpack('>H', bytes(c2s[28:30]))[0]:04x}_req{len(c2s)}"
        else:
            tag = "unknown"
        base = os.path.join(fd, f"{datetime.datetime.now():%H%M%S-%f}_{tag}")
        open(base + ".c2s.bin", "wb").write(bytes(c2s))
        open(base + ".s2c.bin", "wb").write(bytes(s2c))
    except Exception:
        pass


def handle(raw, peer):
    # Bound the TLS handshake so abandoned/half-open connections (the camera
    # retry-storms when it can't come online) can't block a handler thread
    # forever and leak FDs/threads -- that starved accept() and caused a RST
    # storm, which in turn kept the camera from establishing a stable session.
    try:
        raw.settimeout(15)
    except Exception:
        pass
    try:
        conn = TeeConn(SRV.wrap_socket(raw, server_side=True))
    except Exception as e:
        log(f"TLS failed from {peer[0]}: {e}"); raw.close(); return
    try:
        conn.settimeout(20)
        first = conn.recv(65536)
        conn.settimeout(None)
        if not first:
            return
        if first[:4] in (b"GET ", b"POST", b"PUT ", b"HEAD", b"DELE"):
            handle_rest(conn, first, peer)
        elif len(first) >= 4 and struct.unpack(">I", first[:4])[0] == 0x29:
            handle_cs(conn, first, peer)
        else:
            log(f"?? {peer[0]} unrecognized first bytes {first[:16].hex()} -- capturing")
            _dump(peer, "unknown", first, conn)
    except Exception as e:
        log(f"handler error {peer[0]}: {e}")
    finally:
        if conn.c2s:
            _save_flow(peer, conn.c2s, conn.s2c)
        try: conn.close()
        except Exception: pass


# ---- interval (time-lapse) snapshots ------------------------------------
# Request a snapshot every `snapshot_interval` seconds (settings.json; 0 = off,
# default BLINK_SNAPSHOT_INTERVAL). Reuses the normal snapshot path by writing
# MEDIA_REQ, and skips a tick while a media session/request is already in flight
# or the camera hasn't been seen recently, so it never fights motion/live view.
_timelapse = {"last": 0.0, "iv": None}


def _timelapse_janitor():
    while True:
        try:
            iv = float(get_settings().get("snapshot_interval") or 0)
            if iv != _timelapse["iv"]:                 # enabled/changed (or startup)
                _timelapse["iv"] = iv
                if iv > 0:
                    _timelapse["last"] = time.time() - iv   # take the first one promptly
                log(f"time-lapse: {('a snapshot every %gs' % iv) if iv > 0 else 'disabled'}")
            now = time.time()
            if iv > 0 and now - _timelapse["last"] >= iv:
                with _state_lock:
                    seen = _state.get("last_seen", 0)
                    streaming = _state.get("streaming")
                if (now - seen < 30 and not streaming and not _media.get("active")
                        and not os.path.exists(MEDIA_REQ)):
                    with open(MEDIA_REQ, "w") as f:
                        json.dump({"mode": "snapshot", "ts": now, "timelapse": True}, f)
                    _timelapse["last"] = now
                    log(f"  >> TIME-LAPSE snapshot (every {iv:g}s)")
        except Exception as e:
            log(f"(time-lapse error: {e})")
        time.sleep(5)


def main():
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ls.bind(LISTEN); ls.listen(256)   # generous backlog; the camera opens bursts of connections
    log(f"LOCAL SERVER on {LISTEN[0]}:{LISTEN[1]}  (REST + CS + bundle)  cert={os.path.relpath(CHAIN,ROOT)}")
    log(f"push commands by dropping .bin files into {os.path.relpath(QUEUE,ROOT)}/")
    try:
        import webui
        webui.start(LIVE_HUB, LOGDIR, MEDIA_REQ, MEDIA_STOP,
                    os.environ.get("BLINK_MEDIA_IP", "10.10.10.1"),
                    int(os.environ.get("BLINK_WEB_PORT", "8090")),
                    get_settings=get_settings, settings_file=SETTINGS_FILE)
    except Exception as e:
        log(f"(webui failed to start: {e})")
    log(f"debug captures: {'ON (raw uploads, .ts, flows/, polls/ kept)' if DEBUG else 'off'}  [BLINK_DEBUG]")
    if RETENTION_DAYS > 0:
        threading.Thread(target=_retention_janitor, daemon=True).start()
        log(f"retention: keeping last {RETENTION_DAYS:g} day(s) of media under "
            f"{os.path.relpath(LOGDIR, ROOT)}")
    else:
        log("retention: disabled (BLINK_RETENTION_DAYS<=0) — keeping media forever")
    threading.Thread(target=_timelapse_janitor, daemon=True).start()
    while True:
        raw, peer = ls.accept()
        threading.Thread(target=handle, args=(raw, peer), daemon=True).start()


if __name__ == "__main__":
    main()
