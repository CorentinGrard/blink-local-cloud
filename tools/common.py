"""Shared by the server modules: paths, debug flag, logging, the live-state
file read by the dashboard, and the media-request files that drive the camera.

Nothing here talks to the camera; it has no dependencies on the other modules.
"""
import datetime, json, os, threading, time, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# where media, state and logs live (the container mounts its media volume here)
LOGDIR = os.environ.get("BLINK_LOG_DIR") or os.path.join(ROOT, "mitm", "local")
os.makedirs(LOGDIR, exist_ok=True)

# BLINK_DEBUG=1 keeps protocol-debugging captures: raw camera uploads (*_0804.bin),
# the intermediate .ts once the .mp4 exists, every decrypted connection under
# flows/ and every 0x0806 poll under polls/. Off (default): only the playable
# media is kept (.jpg snapshots, .mp4 clips + .thumb.jpg).
DEBUG = os.environ.get("BLINK_DEBUG", "0").strip().lower() in ("1", "true", "yes", "on")

LOG_FILE = os.path.join(LOGDIR, "server.log")   # same lines as stdout, on the media volume
LOG_FILE_MAX = 5 * 1048576                      # rotate to server.log.1 past 5 MiB
_log_lock = threading.Lock()


def log_error(what, exc):
    """Log an error. Network trouble (OSError: resets, timeouts, TLS) is routine
    with this camera and gets one line; anything else is a bug and gets the
    traceback, so it shows up in server.log instead of vanishing."""
    if isinstance(exc, OSError):
        log(f"  {what}: {type(exc).__name__}: {exc}")
    else:
        log(f"  !! {what}: {type(exc).__name__}: {exc}\n"
            + "".join(traceback.format_exception(exc)).rstrip())


def log(msg):
    line = f"[{datetime.datetime.now():%H:%M:%S}] {msg}"
    print(line, flush=True)
    # Also keep it on the media volume, so the log is readable where the
    # container's stdout isn't (e.g. without docker access on the NAS).
    with _log_lock:
        try:
            if os.path.getsize(LOG_FILE) > LOG_FILE_MAX:
                os.replace(LOG_FILE, LOG_FILE + ".1")
        except OSError:
            pass
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass


# ---- live state (state.json) ------------------------------------------------
# live state published for tools/console.py (connection status + last capture)
STATE_FILE = os.path.join(LOGDIR, "state.json")
_state = {"onboarded": False, "req_count": 0}
_state_lock = threading.Lock()
_state_err = []                          # a failed state write was already logged


def _flush_state(data):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(data)
        os.replace(tmp, STATE_FILE)      # atomic: readers never see a partial file
    except OSError as e:
        if not _state_err:               # once: this runs on every request
            _state_err.append(e)
            log(f"(cannot write {STATE_FILE}: {e})")


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


def get_state(*keys):
    """A consistent snapshot of some live-state keys."""
    with _state_lock:
        return {k: _state.get(k) for k in keys}


# ---- media requests ---------------------------------------------------------
# The dashboard (and the motion/time-lapse logic) asks for a snapshot or live view
# by writing MEDIA_REQ; touching MEDIA_STOP ends a running live view. `media` is
# the command logic's view of the current request (see local_server._media_poll).
MEDIA_REQ = os.path.join(LOGDIR, "media_request.json")
MEDIA_STOP = os.path.join(LOGDIR, "media_stop")   # console touches this to end a stream
media = {"active": False}


def clear_media_request():
    try:
        os.remove(MEDIA_REQ)
    except FileNotFoundError:
        pass
    media["active"] = False

