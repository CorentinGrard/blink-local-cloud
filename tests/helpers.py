"""Shared test setup: an isolated log dir, the tools/ import path, test audio and
a fake camera for the live-view (0x0804) socket.

Import this before any server module: it points BLINK_LOG_DIR at a temp dir so
tests never touch real media/state files.
"""
import os, shutil, socket, struct, subprocess, sys, tempfile, threading, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
LOGDIR = tempfile.mkdtemp(prefix="blink-test-")
os.environ["BLINK_LOG_DIR"] = LOGDIR
os.environ.setdefault("BLINK_RETENTION_DAYS", "0")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

HAVE_FFMPEG = bool(shutil.which("ffmpeg"))
HAVE_NODE = bool(shutil.which("node"))


def speech_like_pcm(secs, rate=48000):
    """s16le mono PCM that exercises the encoder like a voice: a warbling tone
    over pink noise, with a short silent gap in the middle."""
    half = secs / 2
    expr = "0.3*sin(2*PI*(220+80*sin(2*PI*3*t))*t)"
    out = subprocess.run(
        ["ffmpeg", "-v", "error",
         "-f", "lavfi", "-i", f"aevalsrc={expr}:s={rate}:d={half}",
         "-f", "lavfi", "-i", f"anullsrc=r={rate}:cl=mono:d=0.5",
         "-f", "lavfi", "-i", f"anoisesrc=r={rate}:a=0.05:c=pink:d={max(0.2, half - 0.5)}",   # d=0 = endless
         "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1",
         "-ac", "1", "-f", "s16le", "-"], capture_output=True, check=True)
    return out.stdout


def adts_frames(data):
    """Split an ADTS stream into frames."""
    frames, i = [], 0
    while i + 7 <= len(data):
        n = ((data[i + 3] & 3) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        frames.append(data[i:i + n]); i += n
    return frames


def encode_adts(pcm, rate=48000):
    """Encode PCM to ADTS AAC-LC 16 kHz mono (a pre-encoded clip)."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "-",
         "-c:a", "aac", "-b:a", "32k", "-ar", "16000", "-ac", "1", "-f", "adts", "-"],
        input=pcm, capture_output=True, check=True).stdout


class FakeCamera:
    """The camera's end of a live-view 0x0804 connection: sends the 51-byte
    handshake, then a dummy MPEG-TS chunk every 30 ms (like ~30 msgs/s of
    video), and records every downlink record the server writes, with its
    arrival time. `server_sock` is handed to the live-view session."""

    def __init__(self, handshake=51):
        self.cam, self.server_sock = socket.socketpair()
        self.handshake = handshake
        self.records = []          # (arrival time, type, seq, payload)
        self.raw = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        self.cam.sendall(b"\x00" * self.handshake)
        self.cam.settimeout(0.01)
        buf = b""
        chunk = b"\x00" + struct.pack(">II", 0, 188) + b"\x47" + b"\x00" * 187
        stopped = False                  # like the camera: quiet after the server's stop
        while not self._stop.is_set():
            if not stopped:
                try:
                    self.cam.sendall(chunk)
                except OSError:
                    break
            try:
                d = self.cam.recv(65536)
                if not d:
                    break
                self.raw += d
                buf += d
                now = time.time()
                while len(buf) >= 9:
                    t = buf[0]
                    seq, n = struct.unpack(">II", buf[1:9])
                    if len(buf) < 9 + n:
                        break
                    self.records.append((now, t, seq, buf[9:9 + n]))
                    stopped = stopped or t in (0x07, 0x08)
                    buf = buf[9 + n:]
            except socket.timeout:
                pass
            except OSError:
                break
            time.sleep(0.03)

    def audio(self):
        return [r for r in self.records if r[1] == 0x05]
