#!/usr/bin/env python3
"""Watch mitm/flows/ and auto-extract 0x0804 media the relay captures.

tls_relay.py only drops raw per-connection streams into mitm/flows/ (see its
module docstring). This watcher turns the media ones into viewable files.

A 0x0804 upload is a sequence of outer transport chunks, each a 9-byte header
`<type:1> <u32 counter> <u32 length>` + <length> payload bytes. Two chunk types
carry two interleaved streams (reassemble by concatenating each type's payloads):
  * type 0x04 (len 1340)  -> a JPEG  (the snapshot, or a liveview poster thumbnail)
  * type 0x00 (len 1316)  -> an MPEG-TS stream, H.264 High/1080p + AAC audio
    (1316 = 7*188; each 0x47 is a TS sync byte). ffmpeg demuxes it natively.
So:
  * liveview  -> <base>.ts (+ <base>.mp4 via ffmpeg -c copy; video+audio)
                 and <base>.thumb.jpg from the type-4 stream
  * snapshot  -> <base>.jpg

A flow is a media upload when its CS opcode (bytes 28:30 of the camera->server
stream) is 0x0804. We process a .c2s.bin once it is *stable* (size unchanged for
STABLE_SECS, i.e. the connection closed) and its output does not already exist,
so it is safe to run alongside the relay and to re-run over a backlog.

  .venv/bin/python tools/media_watch.py            # watch forever
  .venv/bin/python tools/media_watch.py --once      # process backlog and exit
  .venv/bin/python tools/media_watch.py --dir mitm/flows --out mitm/flows/extracted
"""
import argparse, os, shutil, struct, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
SOI = b"\xff\xd8\xff"                    # JPEG start of image
STABLE_SECS = 3.0                        # size must hold this long = conn closed
POLL_SECS = 2.0

GRN, CYN, YEL, DIM, RST = "\033[32m", "\033[36m", "\033[33m", "\033[2m", "\033[0m"


def is_media(c2s_path):
    """Return the CS opcode if this is a readable CS stream, else None."""
    try:
        with open(c2s_path, "rb") as f:
            head = f.read(30)
    except OSError:
        return None
    if len(head) < 30 or struct.unpack(">I", head[:4])[0] != 0x29:
        return None
    return struct.unpack(">H", head[28:30])[0]


def split_streams(data):
    """Walk the outer transport chunks and return {chunk_type: payload_bytes}.
    Each chunk is `<type:1> <u32 counter> <u32 length>` + <length> payload; the
    first header sits 9 bytes before the first JPEG SOI. Returns {} if not found."""
    soi = data.find(SOI)
    if soi < 9:
        return {}
    pos = soi - 9
    out = {}
    while pos + 9 <= len(data):
        t = data[pos]
        L = int.from_bytes(data[pos + 5:pos + 9], "big")
        if L == 0 or L > 4096 or pos + 9 + L > len(data):
            break
        out.setdefault(t, bytearray()).extend(data[pos + 9:pos + 9 + L])
        pos += 9 + L
    return {t: bytes(b) for t, b in out.items()}


def _carve_jpeg(buf):
    s = buf.find(SOI)
    e = buf.rfind(b"\xff\xd9")
    return buf[s:e + 2] if (s != -1 and e > s) else None


def extract(c2s_path, out_dir):
    """Extract one 0x0804 media stream. Returns a short status string or None."""
    base = os.path.splitext(os.path.splitext(os.path.basename(c2s_path))[0])[0]
    data = open(c2s_path, "rb").read()
    os.makedirs(out_dir, exist_ok=True)
    streams = split_streams(data)
    ts = streams.get(0x00, b"")           # MPEG-TS: H.264 + AAC
    jpeg = streams.get(0x04, b"")         # JPEG: snapshot, or liveview thumbnail

    if ts[:1] == b"\x47" and len(ts) >= 188:          # ---- liveview (MPEG-TS) ----
        tsf = os.path.join(out_dir, base + ".ts")
        mp4 = os.path.join(out_dir, base + ".mp4")
        if os.path.exists(mp4) or os.path.exists(tsf):
            return None
        open(tsf, "wb").write(ts)
        msg = f"liveview {base}.ts  ({len(ts) // 188} TS packets, {len(ts)} B)"
        thumb = _carve_jpeg(jpeg)
        if thumb:
            open(os.path.join(out_dir, base + ".thumb.jpg"), "wb").write(thumb)
        if shutil.which("ffmpeg"):        # TS -> MP4, copy video+audio, no re-encode
            r = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", tsf,
                 "-c", "copy", "-movflags", "+faststart", mp4],
                capture_output=True)
            if r.returncode == 0 and os.path.getsize(mp4) > 0:
                msg += f"  ->  {base}.mp4 (H.264+AAC)"
        return msg

    clean = _carve_jpeg(jpeg) or _carve_jpeg(data)    # ---- snapshot (JPEG) ----
    if clean:
        jpg = os.path.join(out_dir, base + ".jpg")
        if os.path.exists(jpg):
            return None
        open(jpg, "wb").write(clean)
        how = "de-chunked" if jpeg else "raw-carve"
        return f"snapshot {base}.jpg  ({len(clean)} B, {how})"

    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=os.path.join(ROOT, "mitm", "flows"),
                    help="flows directory to watch (default: mitm/flows)")
    ap.add_argument("--out", default=None,
                    help="output dir (default: <dir>/extracted)")
    ap.add_argument("--once", action="store_true",
                    help="process the current backlog and exit (no watching)")
    args = ap.parse_args()
    flows = args.dir
    out_dir = args.out or os.path.join(flows, "extracted")

    print(f"{GRN}◉ media watcher{RST}{DIM}  dir={os.path.relpath(flows, ROOT)}  "
          f"out={os.path.relpath(out_dir, ROOT)}  "
          f"{'(once)' if args.once else 'watching 0x0804 uploads'}{RST}")

    seen = {}        # path -> (size, first_seen_ts); stability tracking
    done = set()     # paths already extracted (or skipped)
    while True:
        try:
            files = [os.path.join(flows, f) for f in os.listdir(flows)
                     if f.endswith(".c2s.bin")]
        except OSError:
            files = []
        now = time.time()
        for p in files:
            if p in done:
                continue
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            if not args.once:                       # watch mode: wait for stability
                prev = seen.get(p)
                if prev is None or prev[0] != sz:
                    seen[p] = (sz, now)             # (re)start the stability clock
                    continue
                if (now - prev[1]) < STABLE_SECS:
                    continue
            # ready: --once backlog (already stable) or size held STABLE_SECS
            if is_media(p) == 0x0804:
                try:
                    status = extract(p, out_dir)
                except Exception as e:
                    status = None
                    print(f"{YEL}  ! {os.path.basename(p)}: {e}{RST}")
                if status:
                    print(f"{CYN}▶ {status}{RST}")
            done.add(p)
        if args.once:
            break
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
