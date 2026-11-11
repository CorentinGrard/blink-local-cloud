"""Motion notifications through ntfy (https://ntfy.sh or a self-hosted server)."""
import datetime, os, threading, urllib.request

from common import log


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
        with open(NTFY_TOPIC_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""                        # no topic file: notifications off

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
        except OSError as e:                 # URLError/HTTPError are OSErrors
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
            with open(jpg_path, "rb") as f:
                body = f.read()
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
        except OSError as e:
            log(f"  (ntfy snapshot notify failed: {e})")

    threading.Thread(target=_send, daemon=True).start()
