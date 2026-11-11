"""Two-way talk: the browser's microphone, played on the camera's speaker.

See docs/BACKEND.md (section 6.4, "Two-way talk") for the protocol and the
pitfalls behind the encoder settings.
"""
import collections, datetime, os, shutil, subprocess, threading, time

from common import DEBUG, LOGDIR, log, set_state


# ---- two-way talk (browser mic -> camera speaker) -------------------------
# During a live view the camera plays any audio the server writes back on the
# same 0x0804 socket: records `05 <seq:u32> <len:u32> <ADTS frame>`, AAC-LC
# 16 kHz mono, one 1024-sample frame (64 ms) per record, seq from 0 per session
# (relay capture of the real cloud, 2026-11-11). No command or setting is
# involved; the rest of the session is the normal live view.
TALK_FRAME_SECS = 1024 / 16000
TALK_RECORD = 0x05
TALK_MAX_BACKLOG = 40          # frames (~2.5 s) queued before we drop the oldest
TALK_IDLE = 3.0                # s without browser audio -> end the talk burst
TALK_PREBUFFER = 5             # frames (~320 ms) to queue before a burst starts playing,
                               # so encoder/network jitter doesn't starve the speaker


class TalkHub:
    """Browser mic -> AAC frames for the live-view session to send.

    webui.py posts raw PCM (s16le mono, at the browser's own sample rate) with a
    talk-session id. One ffmpeg process per talk burst encodes it to ADTS AAC-LC
    16 kHz mono; a reader thread splits the ADTS frames into a queue that
    _liveview_session drains at real-time pace. All socket writes stay in the
    session thread, so the TLS socket is never written from two threads."""
    # -probesize/-analyzeduration: no input probing. Without them the image's
    # ffmpeg (7.1) holds the first ~4.4 s of every talk burst while it probes
    # the pipe, then drops that audio -> long delay and lost words.
    CMD = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
           "-probesize", "32", "-analyzeduration", "0",
           "-f", "s16le", "-ar", "{rate}", "-ac", "1", "-i", "pipe:0",
           # A browser mic is ~10-15 dB quieter than what the Blink app sends
           # (speech RMS ~4-7k, peaks near full scale), too faint on the camera's
           # small speaker. Compress + make-up gain, then limit; low latency.
           "-af", "highpass=f=120,acompressor=threshold=0.01:ratio=6:attack=5:"
                  "release=200:makeup=24,alimiter=limit=0.9:level=disabled",
           # +bitexact: no encoder-name block ("Lavc...") in the stream; the cloud
           # never sends one, and ffmpeg 8 merges it into the first audio frame.
           # (An output option: before -i it would apply to the input instead.)
           "-c:a", "aac", "-b:a", "32k", "-ar", "16000", "-ac", "1", "-flags:a", "+bitexact",
           "-flush_packets", "1", "-f", "adts", "pipe:1"]

    def __init__(self):
        self._lock = threading.Lock()
        self._frames = collections.deque()
        self._proc = None
        self._sid = None
        self._last_feed = 0.0
        self._rx = 0                 # PCM bytes received this burst, and its rate
        self._rate = 16000
        self._last = (None, 0.0)     # (sid, seconds received) of the last burst
        self.dropped = 0             # frames dropped by the backlog cap (diagnostics)
        self.t_feed = self.t_frame = 0.0   # first PCM in / first frame out, this burst
        self.live = False            # set by _liveview_session while streaming
        self._new_burst = False      # a talk press started; its records restart at seq 0
        self._dropping = False       # backlog-cap drops already logged for this press

    def ready(self):
        return bool(shutil.which("ffmpeg"))

    def _start(self, sid, rate):
        cmd = [a.replace("{rate}", str(rate)) for a in self.CMD]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL)
        self._proc, self._sid, self._rx, self._rate = proc, sid, 0, rate
        self.t_feed, self.t_frame = time.time(), 0.0
        self._new_burst, self._dropping = True, False
        # BLINK_DEBUG: keep each press's encoded audio (talk_<time>.aac) so a
        # problem can be replayed exactly via /api/talk?format=adts
        rec = None
        if DEBUG:
            try:
                rec = open(os.path.join(LOGDIR, f"talk_{datetime.datetime.now():%Y%m%d-%H%M%S}.aac"), "wb")
            except OSError:
                pass
        threading.Thread(target=self._reader, args=(proc, rec), daemon=True).start()
        log(f"  talk: started (browser audio {rate} Hz)")

    def _reader(self, proc, rec=None):
        buf = b""
        while True:
            d = proc.stdout.read1(4096)
            if not d:
                break
            buf += d
            while len(buf) >= 7:
                if buf[0] != 0xFF or buf[1] & 0xF0 != 0xF0:      # resync on the 0xFFF syncword
                    i = buf.find(b"\xff", 1)
                    buf = buf[i:] if i > 0 else b""
                    continue
                n = ((buf[3] & 3) << 11) | (buf[4] << 3) | (buf[5] >> 5)
                if n < 7:
                    buf = buf[1:]; continue
                if len(buf) < n:
                    break
                if rec:
                    try: rec.write(buf[:n])
                    except OSError: pass
                with self._lock:
                    if self.live:
                        if not self.t_frame:
                            self.t_frame = time.time()
                            log(f"  talk: first encoded frame {1000 * (self.t_frame - self.t_feed):.0f} ms after first audio")
                        self._frames.append(buf[:n])
                        if len(self._frames) > TALK_MAX_BACKLOG:
                            # keep latency bounded: drop the oldest audio (logged once a press)
                            if not self._dropping:
                                log(f"  talk: falling behind, dropping audio past {TALK_MAX_BACKLOG} queued frames")
                            self._dropping = True
                            while len(self._frames) > TALK_MAX_BACKLOG:
                                self._frames.popleft(); self.dropped += 1
                buf = buf[n:]
        proc.wait()
        if rec:
            rec.close()

    def _stop_locked(self):
        if self._proc:
            try:
                self._proc.stdin.close()      # EOF: ffmpeg flushes its last frames and exits
            except OSError:
                pass                          # ffmpeg already gone
            secs = self._rx / 2 / self._rate
            self._last = (self._sid, secs)
            self._proc, self._sid = None, None
            log(f"  talk: ended ({secs:.1f}s of browser audio received)")
            set_state(talk_rx={"secs": round(secs, 2), "rate": self._rate, "ts": time.time()})

    def feed(self, sid, rate, pcm):
        """Queue browser PCM for the camera. False when no live view is running."""
        with self._lock:
            if not self.live:
                self._stop_locked(); return False
            if self._sid != sid or not self._proc or self._proc.poll() is not None:
                self._stop_locked()
                self._start(sid, rate)
            self._last_feed = time.time()
            self._rx += len(pcm)
            proc = self._proc
        try:
            proc.stdin.write(pcm); proc.stdin.flush()
        except (BrokenPipeError, OSError):
            with self._lock:
                if self._proc is proc:
                    self._proc, self._sid = None, None
            return False
        return True

    def feed_adts(self, data):
        """Queue ready-made ADTS AAC (16 kHz mono) as-is, all of it: plays a
        pre-encoded clip, and lets us test the camera with exact frames
        (e.g. the real cloud's captured talk audio). False if not live."""
        frames, i = [], 0
        while i + 7 <= len(data):
            if data[i] != 0xFF or data[i + 1] & 0xF0 != 0xF0:
                i += 1; continue
            n = ((data[i + 3] & 3) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
            if n < 7 or i + n > len(data):
                break
            frames.append(data[i:i + n]); i += n
        with self._lock:
            if not self.live:
                return False
            self._stop_locked()
            self._frames.clear()
            self._frames.extend(frames)
            self._new_burst = True
        log(f"  talk: queued {len(frames)} pre-encoded frame(s)")
        return True

    def end(self, sid=None):
        """Close a talk burst; returns the seconds of browser audio it received."""
        with self._lock:
            if sid is None or sid == self._sid:
                self._stop_locked()
            return self._last[1] if self._last[0] == sid else 0.0

    def pop(self):
        """Next AAC frame to send, or None. Also ends a burst the browser abandoned."""
        with self._lock:
            if self._proc and time.time() - self._last_feed > TALK_IDLE:
                self._stop_locked()
            return self._frames.popleft() if self._frames else None

    def pending(self):
        return bool(self._frames)

    def qlen(self):
        return len(self._frames)

    def take_new_burst(self):
        """True once per talk press: the session then restarts the record seq at 0."""
        with self._lock:
            nb, self._new_burst = self._new_burst, False
            return nb

    def primed(self):
        """Enough queued to start a burst (or the encoder is done: flush the rest)."""
        with self._lock:
            if self._proc and time.time() - self._last_feed > TALK_IDLE:
                self._stop_locked()                      # browser went away mid-talk
            return len(self._frames) >= TALK_PREBUFFER or (
                bool(self._frames) and (not self._proc or self._proc.poll() is not None))

    def set_live(self, on):
        with self._lock:
            self.live = on
            if not on:
                self._stop_locked()
                self._frames.clear()
TALK = TalkHub()
