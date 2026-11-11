#!/usr/bin/env python3
"""Web UI for the Blink local cloud (local_server.py).

HTTP server (default :8090) in a background thread: shows the camera, takes
snapshots, plays the liveview in-browser (mpegts.js, served locally), edits
camera settings and browses/deletes the saved media (/gallery). Commands use the
same files local_server already watches (media_request.json / media_stop /
settings.json), so nothing here imports local_server (no import cycle). Live
MPEG-TS is fanned out from LiveHub.

The pages themselves live in tools/web/ (HTML, CSS and JS); _build() fills their
__PLACEHOLDER__ tokens (shared header/CSS/JS, icons, settings) at startup.
"""
import os, re, json, time, glob, shutil, subprocess, threading, queue as _queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
WEB = os.path.join(HERE, "web")      # page templates, styles and scripts (see _build)


def _asset(name):
    """A dashboard source file from tools/web/, read once at import."""
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


RETENTION_DAYS = float(os.environ.get("BLINK_RETENTION_DAYS", "7") or "0")

# User-facing settings: key -> (label, control, options). Written as ints to
# settings.json; local_server pushes them to the camera on its next heartbeat.
# NB: motion detection is driven by the Arm/Disarm switch (it sets both the
# "armed" and "motion" bytes together — the firmware needs both to record), so it
# is not a settings row here.
SETTINGS_UI = [
    ("nv",           "Night vision",     "select", {0: "Off", 1: "On", 2: "Auto"}),
    ("ir",           "Infrared light",   "select", {1: "Low", 4: "Medium", 7: "High"}),
    ("quality",      "Video quality",    "select", {1: "Low", 2: "Normal", 3: "High"}),
    ("flip",         "Flip image",       "toggle", None),
    ("audio_stream", "Record audio",     "toggle", None),
    ("clip_len",     "Clip length (s)",  "range",  {"min": 5, "max": 30}),
    ("sensitivity",  "Motion sensitivity", "range", {"min": 2, "max": 100}),
    ("retrigger",    "Re-arm delay (s)", "range",  {"min": 10, "max": 60}),
]

# Media files that belong to one capture share a base name; deleting an item
# removes all of these siblings.
SIBLING_EXTS = (".jpg", ".mp4", ".ts", ".thumb.jpg", ".bin")
NAME_RE = re.compile(r"[\w-][\w.-]*")
TALK_MAX_BODY = 1 << 20     # one /api/talk POST (~100 ms of PCM; generous cap)

# Shared by both pages: styles, JS helpers, the confirm dialog + toast area.
CSS = _asset("base.css")
COMMON_JS = _asset("common.js")
CONFIRM_HTML = _asset("confirm.html")


def header(active):
    def a(href, label, key):
        return f'<a href="{href}"{" class=on aria-current=page" if key == active else ""}>{label}</a>'
    return ("<header><div class=hbar><div class=brand><span class=dot id=dot></span><span>Blink</span></div>"
            f"<nav>{a('/', 'Camera', 'cam')}{a('/gallery', 'Gallery', 'gal')}</nav>"
            "<div class=hright>__HRIGHT__</div></div></header>")


# Each page = its markup + its script, joined before the placeholders are filled.
PAGE = _asset("camera.html").replace("__CAMERA_JS__", _asset("camera.js"))

ARM_HTML = ('<span class=pill><span id=status>…</span></span>'
            '<label class=armbox id=armbox title="Arm: record a clip when motion is detected">'
            '<span class=lbl id=armlbl>…</span><span class=sw><input type=checkbox id=arm aria-label="Arm motion recording">'
            '<span class=sl></span></span></label>')

GALLERY = _asset("gallery.html").replace("__GALLERY_JS__", _asset("gallery.js"))

GALLERY_HRIGHT = '<span class=pill><span id=status>…</span></span>'


ICONS = {
    "play":  '<path d="M7 4v16l13-8z" fill="currentColor" stroke="none"/>',
    "stop":  '<rect x="6" y="6" width="12" height="12" rx="1.5" fill="currentColor" stroke="none"/>',
    "cam":   '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/>',
    "mute":  '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M17 9.5l4 5M21 9.5l-4 5"/>',
    "sound": '<path d="M4 9h4l5-4v14l-5-4H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/>',
    "mic":   '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>',
    "fs":    '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
    "dl":    '<path d="M12 4v11m-5-5 5 5 5-5M5 20h14"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    "x":     '<path d="M6 6l12 12M18 6 6 18"/>',
}


# Browser-tab icon, served at /favicon.svg (and /favicon.ico). The same camera
# glyph the UI uses (ICONS["cam"]) in the brand accent on the app's dark
# background, so it stays legible down to 16px.
FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">'
    '<rect width="24" height="24" rx="5" fill="#0e1013"/>'
    '<g fill="none" stroke="#4da3ff" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M4 8h3l2-3h6l2 3h3v11H4z"/><circle cx="12" cy="13" r="3.5"/></g></svg>'
).encode()


def _build(tpl, active, hright, **extra):
    """Fill a page template: __I_<icon>__, __HEADER__ (nav + `hright`), __CSS__,
    __CONFIRM__, __COMMON_JS__, then any __<KEY>__ given in `extra`."""
    for k, v in ICONS.items():
        tpl = tpl.replace(f"__I_{k}__", f'<svg class=ic viewBox="0 0 24 24" aria-hidden=true>{v}</svg>')
    s = (tpl.replace("__HEADER__", header(active).replace("__HRIGHT__", hright))
            .replace("__CSS__", CSS).replace("__CONFIRM__", CONFIRM_HTML)
            .replace("__COMMON_JS__", COMMON_JS))
    for k, v in extra.items():
        s = s.replace(f"__{k}__", v)
    return s


def start(hub, logdir, media_req, media_stop, media_ip, port=8090,
          get_settings=None, settings_file=None, talk=None):
    page = _build(PAGE, "cam", ARM_HTML, SETTINGS=json.dumps(
        [{"key": k, "label": lbl, "control": c, "options": o} for k, lbl, c, o in SETTINGS_UI]))
    gallery = _build(GALLERY, "gal", GALLERY_HRIGHT,
                     RETENTION=json.dumps(RETENTION_DAYS if RETENTION_DAYS > 0 else 0))

    def newest(pat):
        # skip liveview thumbnails (<base>.thumb.jpg) -- they are not snapshots
        fs = [f for f in glob.glob(os.path.join(logdir, pat)) if not f.endswith(".thumb.jpg")]
        fs.sort(key=os.path.getmtime, reverse=True)
        return fs[0] if fs else None

    def latest_snapshot():
        f = newest("*.jpg")
        if not f: return None
        try: return {"name": os.path.basename(f), "ts": os.path.getmtime(f)}
        except OSError: return None

    # --- clip durations: ffprobe in a background thread, cached by (name, mtime)
    durations = {}
    probe_q = _queue.Queue()
    queued = set()
    ffprobe = shutil.which("ffprobe")

    def _prober():
        while True:
            key = probe_q.get()
            name, _mt = key
            d = None
            try:
                r = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                                    "-of", "default=nw=1:nk=1", os.path.join(logdir, name)],
                                   capture_output=True, text=True, timeout=15)
                d = round(float(r.stdout.strip()), 1)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                d = 0          # unknown; don't retry
            durations[key] = d
            queued.discard(key)

    if ffprobe:
        threading.Thread(target=_prober, daemon=True).start()

    def duration(name, mtime):
        key = (name, mtime)
        if key in durations:
            return durations[key] or None
        if ffprobe and key not in queued:
            queued.add(key); probe_q.put(key)
        return None

    def list_media():
        """All saved snapshots + clips in logdir, newest first. A liveview capture
        yields <base>.ts (+ <base>.mp4 if remuxed) and <base>.thumb.jpg; the mp4
        represents the clip when present (plays natively), else the .ts. Motion
        clips are named *_motion_* by local_server."""
        items = []
        for f in glob.glob(os.path.join(logdir, "*")):
            n = os.path.basename(f)
            if n.endswith(".thumb.jpg"):
                continue
            try: st = os.stat(f)
            except OSError: continue
            if n.endswith(".jpg"):
                items.append({"kind": "snapshot", "name": n, "thumb": n, "motion": False,
                              "ts": st.st_mtime, "bytes": st.st_size, "duration": None})
            elif n.endswith((".ts", ".mp4")):
                base = n.rsplit(".", 1)[0]
                if n.endswith(".ts") and os.path.exists(os.path.join(logdir, base + ".mp4")):
                    continue
                thumb = base + ".thumb.jpg"
                items.append({"kind": "clip", "name": n, "motion": "_motion_" in n,
                              "thumb": thumb if os.path.exists(os.path.join(logdir, thumb)) else None,
                              "ts": st.st_mtime, "bytes": st.st_size,
                              "duration": duration(n, st.st_mtime)})
        for it in items:   # filename prefix = capture time; more accurate than mtime
            m = re.match(r"(\d{8}-\d{6})_", it["name"])
            if m:
                try: it["ts"] = time.mktime(time.strptime(m.group(1), "%Y%m%d-%H%M%S"))
                except (ValueError, OverflowError): pass      # keep the mtime
        items.sort(key=lambda it: it["ts"], reverse=True)
        return items

    def delete_media(names):
        """Delete each named item plus its sibling files (same capture base)."""
        deleted = []
        real_dir = os.path.realpath(logdir)
        for n in names if isinstance(names, list) else []:
            if not isinstance(n, str) or not NAME_RE.fullmatch(n):
                continue
            if n.endswith(".thumb.jpg"): base = n[:-len(".thumb.jpg")]
            elif n.endswith((".jpg", ".mp4", ".ts")): base = n.rsplit(".", 1)[0]
            else: continue
            removed = False
            for ext in SIBLING_EXTS:
                p = os.path.join(logdir, base + ext)
                if os.path.dirname(os.path.realpath(p)) != real_dir:
                    continue
                try:
                    os.remove(p); removed = True
                except OSError:
                    pass
            if removed:
                deleted.append(n)
        return deleted

    settings_lock = threading.Lock()

    def update_settings(upd):
        """Merge `upd` into settings.json. Locked so concurrent saves don't drop
        each other's changes; written to a temp file and renamed so the server's
        heartbeat never reads a half-written file."""
        with settings_lock:
            try:
                with open(settings_file) as f:
                    cur = dict(json.load(f))
            except FileNotFoundError:
                cur = {}
            except (OSError, ValueError, TypeError) as e:
                print(f"[webui] {settings_file} unreadable ({e}); rewriting it", flush=True)
                cur = {}
            cur.update(upd)
            os.makedirs(os.path.dirname(settings_file), exist_ok=True)
            tmp = settings_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cur, f)
            os.replace(tmp, settings_file)

    def write_request(obj):
        """Ask local_server for a snapshot/live view. Atomic (temp + rename) so
        its 0x0806 poll never reads a half-written request."""
        tmp = media_req + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f)
        os.replace(tmp, media_req)

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def log_message(self, *a): pass

        def _send(self, code, ctype, body, cache=False):
            if isinstance(body, str): body = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if not cache: self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try: self.wfile.write(body)
            except OSError: pass             # the browser went away

        def _json(self, obj, code=200): self._send(code, "application/json", json.dumps(obj))

        def _file(self, path, ctype):
            try:
                with open(path, "rb") as f:
                    b = f.read()
            except OSError:
                return self.send_error(404)
            self._send(200, ctype, b)

        def _clip(self, name):
            """Serve a captured clip/snapshot from logdir, with HTTP Range support
            (iOS/Safari won't play an MP4 without it). Bare filenames only."""
            if not re.fullmatch(r"[\w.-]+\.(mp4|jpg|ts)", name) or name.startswith("."):
                return self.send_error(404)
            path = os.path.join(logdir, name)
            try: size = os.path.getsize(path)
            except OSError: return self.send_error(404)
            start, end = 0, size - 1
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", "").strip())
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2): end = min(int(m.group(2)), size - 1)
                else:                                  # suffix range: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0"); self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", {"mp4": "video/mp4", "ts": "video/mp2t"}.get(
                name.rsplit(".", 1)[-1], "image/jpeg"))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.send_header("Cache-Control", "private, max-age=86400")
            self.end_headers()
            if self.command == "HEAD":
                return
            try:
                with open(path, "rb") as f:
                    f.seek(start); left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(65536, left))
                        if not chunk: break
                        self.wfile.write(chunk); left -= len(chunk)
            except OSError:
                pass             # the player stopped reading (seek, close)

        def do_HEAD(self):
            p = self.path.split("?", 1)[0]
            if p.startswith("/clips/"): self._clip(p[len("/clips/"):])
            else: self.send_error(404)

        def _body(self):
            try:
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, OSError):
                return {}
            return body if isinstance(body, dict) else {}

        def _query(self):
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv: k, v = kv.split("=", 1); q[k] = v
            return q

        def do_GET(self):
            p = self.path.split("?", 1)[0]
            if p in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", page)
            elif p == "/gallery":
                self._send(200, "text/html; charset=utf-8", gallery)
            elif p == "/static/mpegts.js":
                self._file(os.path.join(STATIC, "mpegts.js"), "application/javascript")
            elif p in ("/favicon.svg", "/favicon.ico"):
                self._send(200, "image/svg+xml", FAVICON_SVG, cache=True)
            elif p == "/api/state":
                try:
                    with open(os.path.join(logdir, "state.json")) as f:
                        st = json.load(f)
                except (OSError, ValueError):
                    st = {}
                st["latest_snapshot"] = latest_snapshot()
                st["now"] = time.time()
                try:
                    cur = get_settings() if get_settings else {}
                    st["armed"] = int(bool(cur.get("armed")))
                    st["snapshot_interval"] = int(float(cur.get("snapshot_interval") or 0))
                except (ValueError, TypeError): pass
                self._json(st)
            elif p == "/api/settings":
                self._json({"current": get_settings() if get_settings else {}})
            elif p == "/api/media":
                items = list_media()
                try: limit = int(self._query().get("limit", 0))
                except ValueError: limit = 0
                self._json({"items": items[:limit] if limit > 0 else items})
            elif p == "/live.ts":
                self._live()
            elif p.startswith("/clips/"):
                self._clip(p[len("/clips/"):])
            else:
                self.send_error(404)

        def _live(self):
            q = hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                idle = 0
                while True:
                    try: data = q.get(timeout=1.0)
                    except _queue.Empty:
                        idle += 1
                        if idle > 90: break
                        continue
                    idle = 0
                    self.wfile.write(data); self.wfile.flush()
            except OSError:
                pass             # viewer disconnected
            finally:
                hub.unsubscribe(q)

        def _same_origin(self):
            """CSRF guard for every POST. Require a JSON body type: a cross-site
            page can only send one after a CORS preflight, which we never grant.
            Also reject an Origin / Sec-Fetch-Site that says the request came
            from another site."""
            ctype = self.headers.get("Content-Type", "")
            if "application/json" not in ctype and "application/octet-stream" not in ctype:
                return False   # both need a CORS preflight cross-site; we never grant one
            if self.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "null":
                host = origin.split("://", 1)[-1]
                if host != self.headers.get("Host", ""):
                    return False
            return True

        def do_POST(self):
            p = self.path.split("?", 1)[0]
            q = self._query()
            if not self._same_origin():
                return self.send_error(403)
            if p == "/api/snapshot":
                write_request({"mode": "snapshot", "ts": time.time()})
                self._json({"msg": "ok"})
            elif p == "/api/liveview":
                try: secs = max(5, min(600, int(q.get("secs", 60))))
                except ValueError: secs = 60
                write_request({"mode": "liveview", "ip": media_ip, "live": True,
                               "secs": secs, "ts": time.time()})
                self._json({"msg": "ok"})
            elif p == "/api/talk":
                # Two-way talk: a body of raw s16le mono PCM at ?rate=, for talk
                # burst ?sid=; ?end=1 closes the burst. Played on the camera's
                # speaker by the running live view (409 when none is running).
                try: n = int(self.headers.get("Content-Length", 0))
                except ValueError: n = 0
                if not 0 <= n <= TALK_MAX_BODY:
                    return self.send_error(413)
                pcm = self.rfile.read(n) if n else b""
                sid = q.get("sid", "")
                if not talk or not re.fullmatch(r"[A-Za-z0-9]{1,32}", sid):
                    return self._json({"msg": "bad request"}, 400)
                if q.get("end"):
                    return self._json({"ok": True, "rx_secs": talk.end(sid)})
                if q.get("format") == "adts":         # pre-encoded AAC, queued as-is
                    ok = talk.feed_adts(pcm)
                    return self._json({"ok": ok}, 200 if ok else 409)
                if not talk.ready():
                    return self._json({"msg": "ffmpeg not installed"}, 503)
                try: rate = int(q.get("rate", 0))
                except ValueError: rate = 0
                if not 8000 <= rate <= 96000:
                    return self._json({"msg": "bad rate"}, 400)
                ok = talk.feed(sid, rate, pcm[:len(pcm) & ~1])
                self._json({"ok": ok}, 200 if ok else 409)
            elif p == "/api/stop":
                open(media_stop, "w").close()
                try: os.remove(media_req)
                except FileNotFoundError: pass
                self._json({"msg": "ok"})
            elif p == "/api/delete":
                self._json({"deleted": delete_media(self._body().get("names"))})
            elif p == "/api/arm":
                # Arm/disarm the camera. The firmware records on motion only when
                # BOTH the armed and motion bytes are set, so arming sets both =1
                # and disarming both =0. Pushed to the camera on its next heartbeat.
                if not settings_file: return self.send_error(400)
                armed = 1 if self._body().get("armed") else 0
                try:
                    update_settings({"armed": armed, "motion": armed})
                    self._json({"armed": armed, "ts": time.time()})
                except OSError as e:
                    self._json({"msg": str(e)}, 500)
            elif p == "/api/timelapse":
                # Interval (time-lapse) snapshots: store the interval in seconds
                # (0 = off) in settings.json; local_server's scheduler reads it live.
                if not settings_file: return self.send_error(400)
                try: secs = max(0, int(self._body().get("seconds", 0)))
                except (ValueError, TypeError): secs = 0
                try:
                    update_settings({"snapshot_interval": secs})
                    self._json({"snapshot_interval": secs, "ts": time.time()})
                except OSError as e:
                    self._json({"msg": str(e)}, 500)
            elif p == "/api/settings":
                if not settings_file: return self.send_error(400)
                want, upd = self._body(), {}
                for k, _lbl, _c, _o in SETTINGS_UI:
                    if k in want:
                        try: upd[k] = int(want[k])
                        except (ValueError, TypeError): pass     # ignore junk values
                try:
                    update_settings(upd)
                    self._json({"msg": "saved", "ts": time.time()})
                except OSError as e:
                    self._json({"msg": str(e)}, 500)
            else:
                self.send_error(404)

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[webui] http://0.0.0.0:{port}  (media_ip={media_ip})", flush=True)
    return srv
