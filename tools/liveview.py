"""The camera's media session (0x0804): live view, snapshots and motion clips.

The camera opens one TLS connection per upload. For a live view, the session is
lock-step (see liveview_session); the same socket carries talk audio back to the
camera's speaker. The finished upload is de-chunked into .jpg / .ts / .mp4.
"""
import datetime, os, queue, shutil, socket, struct, subprocess, threading, time

import blink_proto as bp
import media_watch
from common import DEBUG, LOGDIR, MEDIA_STOP, ROOT, clear_media_request, log, log_error, set_state
from notify import notify_motion, notify_motion_snapshot
from talk import TALK, TALK_FRAME_SECS, TALK_RECORD


class LiveHub:
    """Fan-out of the live, de-chunked MPEG-TS to browser subscribers (webui.py /live.ts)."""
    def __init__(self):
        self._subs = set()
        self._lock = threading.Lock()
    def subscribe(self):
        q = queue.Queue(maxsize=1024)
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
            except queue.Full:
                pass          # slow/stalled browser: drop rather than block the camera
LIVE_HUB = LiveHub()


def to_mp4(ts_path):
    """Remux a liveview MPEG-TS (H.264 + AAC) into .mp4 without re-encoding --
    ffmpeg -c copy, else GStreamer (tsdemux -> mp4mux, video only). Neither decodes, so the
    missing native H.264 decoder doesn't matter. Returns the mp4 path or None."""
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
        except (OSError, subprocess.TimeoutExpired) as e:
            log(f"  ({cmd[0]} remux failed: {e})"); continue
        if r.returncode == 0 and os.path.exists(mp4) and os.path.getsize(mp4) > 0:
            return mp4
        log(f"  ({cmd[0]} remux failed: {r.stderr.decode(errors='replace').strip()[-300:]})")
    return None


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
        except OSError as e:
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
            except OSError:
                pass                                # player already gone
            self.proc = None


LIVE_MAX_SECS = 600.0                            # safety cap for an open-ended live view


def liveview_session(conn, fh, idle, live=False, secs=None):
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
    # 2) stream: read, send a receiver report every second, and play any talk
    #    audio from the browser (TALK) on the camera's speaker
    t_end = time.time() + secs
    next_report = time.time() + 1.0
    last_data = time.time()
    talk_seq, talk_next, talk_sent = 0, 0.0, 0
    # talk diagnostics, logged once a second while audio flows
    dg = {"t": time.time(), "sent": 0, "qmax": 0, "gap": 0.0, "loop": 0.0, "drop": TALK.dropped,
          "last_send": 0.0, "last_iter": time.time(), "restarts": 0}
    TALK.set_live(True)
    conn.settimeout(0.2)
    try:
        while time.time() < t_end:
            # Talk: send AAC frames at real-time pace (64 ms each), up to ~2 frames
            # ahead so our loop granularity never starves the camera's buffer.
            now = time.time()
            dg["loop"] = max(dg["loop"], now - dg["last_iter"]); dg["last_iter"] = now
            dg["qmax"] = max(dg["qmax"], TALK.qlen())
            idle_gap = talk_next < now - 0.2
            if idle_gap and talk_sent:
                log(f"  talk: played {talk_sent} frame(s) ({talk_sent * TALK_FRAME_SECS:.1f}s)")
                set_state(talk_played={"frames": talk_sent, "secs": round(talk_sent * TALK_FRAME_SECS, 2),
                                       "ts": time.time()})
                talk_sent = 0
            if idle_gap and TALK.primed():          # (re)start a burst once pre-buffered
                talk_next = now
                dg["restarts"] += 1
                # Each press is its own talk burst with seq from 0, like the cloud's
                # (0..73). Continuing the count across presses, the camera played
                # only the first press of a live view.
                if TALK.take_new_burst() and talk_seq:
                    log(f"  talk: new press, seq {talk_seq} -> 0")
                    talk_seq = 0
            while talk_next >= now - 0.2 and talk_next <= now + 2 * TALK_FRAME_SECS:
                fr = TALK.pop()
                if fr is None:
                    break
                conn.sendall(bytes([TALK_RECORD]) + struct.pack(">II", talk_seq, len(fr)) + fr)
                talk_seq += 1; talk_sent += 1; talk_next += TALK_FRAME_SECS
                t_s = time.time()
                if dg["last_send"]:
                    dg["gap"] = max(dg["gap"], t_s - dg["last_send"])
                dg["last_send"] = t_s; dg["sent"] += 1
            if now - dg["t"] >= 1.0:
                if DEBUG and (dg["sent"] or dg["qmax"] or TALK.dropped != dg["drop"]):
                    log(f"  talk/s: sent={dg['sent']} seq={talk_seq} q_max={dg['qmax']} "
                        f"max_gap={1000 * dg['gap']:.0f}ms loop_max={1000 * dg['loop']:.0f}ms "
                        f"dropped={TALK.dropped - dg['drop']} restarts={dg['restarts']}")
                dg.update(t=now, sent=0, qmax=0, gap=0.0, loop=0.0, drop=TALK.dropped, restarts=0)
            conn.settimeout(0.02 if TALK.pending() else 0.2)
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
        except OSError:
            pass                                    # quiet (timeout) or closed: done
        log("  liveview: sent stop")
    finally:
        TALK.set_live(False)
        if player:
            player.close()
        web.close()
        try:
            os.remove(MEDIA_STOP)
        except OSError:
            pass
        set_state(streaming=None)


def capture_media(peer, first, conn, kind="media", opts=None, motion=False):
    """Drain a 0x0804 upload to disk, then de-chunk it (media_watch.split_streams):
    snapshot -> .jpg, liveview -> .ts (H.264+AAC) + .thumb.jpg."""
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # motion clips are tagged in the filename so the gallery can tell them apart
    raw = os.path.join(LOGDIR, f"{ts}_{peer[0]}_{'motion' if motion else kind}_0804.bin")
    fh = open(raw, "wb"); fh.write(first)
    # An upload starting means the request was honoured: clear it NOW, or every
    # poll during the upload sends another ARM (camera red-LEDs mid-upload).
    clear_media_request()
    # The camera keeps the connection open after its last byte, so end on a short
    # idle gap (liveview streams continuously) rather than a 30s timeout -- the
    # console's 50s wait used to expire before we published last_media.
    idle = float(os.environ.get("BLINK_MEDIA_IDLE", "5"))
    try:
        if kind == "liveview":
            liveview_session(conn, fh, idle, **(opts or {}))
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
    except Exception as e:      # keep what arrived; report why the session ended
        log_error(f"{kind} session ended", e)
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
            mp4 = to_mp4(out)
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
    except Exception as e:      # a bad upload must not lose the raw capture
        log_error("decode failed", e)
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
    set_state(last_media={"ts": time.time(), "kind": kind,
                          "raw": (os.path.relpath(raw, ROOT) if raw else None),
                          "out": (os.path.relpath(out, ROOT) if out else None),
                          "abs": (out or raw),
                          "frames": frames, "bytes": len(data), "motion": bool(motion)})
    clear_media_request()        # streaming started -> request satisfied
