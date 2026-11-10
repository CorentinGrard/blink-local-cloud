#!/usr/bin/env python3
"""Blink Mini message classifier — the single source of truth for "what is this
message / action?". Imported by tls_relay.py (live action log), local_server.py,
and analyze_flows.py so they all name messages identically.

A connection carries one request (C->S) and one reply (S->C), so the first
chunk of each direction is enough to name the action (except 0x0806, which is a
lock-step exchange -- see converse_0806). See docs/MESSAGES.md
for the catalog and docs/PROTOCOL.md for field-level detail.
"""
import socket, struct

CS_MAGIC = 0x29
HTTP_METHODS = (b"GET ", b"POST", b"PUT ", b"HEAD", b"DELE", b"OPTI", b"PATC")

# opcode -> short name (CS binary channel, u16 at header offset 0x1c)
OPCODES = {
    0x0806: "command-poll",
    0x0807: "status/bundle",
    0x0804: "media",
}


def is_cs(data: bytes) -> bool:
    return len(data) >= 4 and struct.unpack(">I", data[:4])[0] == CS_MAGIC


def parse_cs_header(data: bytes):
    """Return dict(opcode, serial, owl, token) for a C->S CS request, else None."""
    if not is_cs(data) or len(data) < 30:
        return None
    slen = struct.unpack(">I", data[4:8])[0]
    serial = data[8:8 + slen].decode("latin1", "replace") if 0 < slen <= 32 else ""
    owl = struct.unpack(">I", data[24:28])[0]
    opcode = struct.unpack(">H", data[28:30])[0]
    return {"opcode": opcode, "serial": serial, "owl": owl}


# A 0x0806 command/heartbeat "Kt" block: 00 05 'Kt' | 00 00 00 00 | seq(2 BE) ...
# | at Kt+0x23: tag 0x04 + 4 bytes. '04 00 00 00 01' = idle; '04 <ip>' = media host.
KT = b"\x00\x05Kt"
KT_SEQ = 8        # seq (u16 BE) at Kt+8
KT_FIELD = 0x23   # '04 <ip>' media field at Kt+0x23
IDLE_FIELD = b"\x04\x00\x00\x00\x01"


def _kt_blocks(buf: bytes):
    """Yield (pos, seq, field5) for each Kt block in a 0x0806 reply."""
    i = 0
    while True:
        k = buf.find(KT, i)
        if k < 0:
            break
        seq = struct.unpack(">H", buf[k + KT_SEQ:k + KT_SEQ + 2])[0] if k + KT_SEQ + 2 <= len(buf) else None
        field = buf[k + KT_FIELD:k + KT_FIELD + 5]
        yield k, seq, field
        i = k + 1


def _kt_seq(buf: bytes):
    for _, seq, _ in _kt_blocks(buf):
        return seq
    return None


def set_kt_seq(buf: bytes, seq: int) -> bytes:
    """Return a copy of a Kt-bearing command with every Kt sequence set to `seq`
    (u16 BE at Kt+8). Used to echo the camera's live session seq into a command."""
    b = bytearray(buf)
    for k, _, _ in _kt_blocks(buf):
        struct.pack_into(">H", b, k + KT_SEQ, seq & 0xffff)
    return bytes(b)


# The 0x0806 poll is a LOCK-STEP exchange, not one request + one reply. Measured
# against the real cloud (relay timeline):
#   C>122 header | S<5 ack | C>5+9 | S<5 ack | C>5+182 (fw/MAC/IP/release) | S<5 ack
#   ... cloud keeps the connection open until the camera closes it (~10 s).
# The camera waits for each 5B ack before sending its next message. The "15B idle
# reply" is just three acks; Kt/arm/start replies are those acks + one command
# message. Replying all at once after the header and closing cut the camera off
# after 122B -- the "hollow" poll was simply a truncated one. When the server
# pushes a command, the camera answers in the same poll (e.g. 5B + 61B) and the
# server acks that too.
ACK_0806 = bytes.fromhex("0100000004")


def read_until_idle(conn, idle=0.15, first_wait=1.2, cap=8192):
    """Read one camera message: wait up to `first_wait` s for it to start, then
    until the camera is quiet for `idle` s. Returns (bytes, eof)."""
    data = b""
    conn.settimeout(first_wait)
    try:
        while len(data) < cap:
            more = conn.recv(4096)
            if not more:
                return data, True
            data += more
            conn.settimeout(idle)
    except socket.timeout:
        pass
    except OSError:
        return data, True
    finally:
        conn.settimeout(None)
    return data, False


def converse_0806(conn, first, messages=3, wait=3.0):
    """Run the server side of a 0x0806 poll's opening: ack each camera message as
    soon as it is complete. Each message is one TLS record, or a 5B header record
    followed by a body record (SSLSocket.recv returns one record at a time), so a
    message is complete on any record that isn't 5 bytes -- no idle timing needed.
    A poll is exactly `messages` messages (122B header, 5+9, 5+182); the camera
    then waits for commands. Returns (full poll bytes, acks sent, eof)."""
    data, acks, rec = first, 0, first
    conn.settimeout(wait)
    try:
        while True:
            if len(rec) != 5:
                conn.sendall(ACK_0806); acks += 1
                if acks >= messages:
                    return data, acks, False
            rec = conn.recv(4096)
            if not rec:
                return data, acks, True
            data += rec
    except socket.timeout:
        return data, acks, False
    except OSError:
        return data, acks, True
    finally:
        conn.settimeout(None)


def split_command(payload: bytes, acks: int) -> bytes:
    """From a canned 0x0806 reply (acks + command message [+ trailing ack]) keep
    only the command message: the leading acks were sent per camera message, and
    the trailing ack answers the camera's reply to the command, so it is sent
    when that reply arrives (as the cloud does), not up front."""
    cmd = strip_acks(payload, acks)
    if len(cmd) > len(ACK_0806) and cmd.endswith(ACK_0806):
        cmd = cmd[:-len(ACK_0806)]
    return cmd


def strip_acks(payload: bytes, n: int) -> bytes:
    """Drop up to `n` leading 5B acks (already sent one per camera message) from a
    canned 0x0806 reply, leaving only its command message (if any)."""
    while n and payload.startswith(ACK_0806):
        payload, n = payload[len(ACK_0806):], n - 1
    return payload


def set_media_ip(buf: bytes, ip: str) -> bytes:
    """Return a copy with every Kt field (at Kt+0x23) set to '04 <ip>' — the
    'connect to this media server' directive. See docs/PROTOCOL.md §5."""
    b = bytearray(buf)
    ipb = socket.inet_aton(ip)
    for k, _, _ in _kt_blocks(buf):
        o = k + KT_FIELD
        b[o] = 0x04
        b[o + 1:o + 5] = ipb
    return bytes(b)


def _media_candidates(buf: bytes):
    """Media server IPs offered in a 0x0806 command (the 04-field of each Kt
    block). Idle acks carry 04 00 00 00 01 and yield nothing."""
    ips = []
    for _, _, field in _kt_blocks(buf):
        if len(field) == 5 and field[0] == 0x04 and field != IDLE_FIELD:
            ips.append(socket.inet_ntoa(field[1:5]))
    return ips


# Settings carried in cloud->camera 0x0807 replies. Byte offsets differ by reply
# size: 210B heartbeat vs 174B config-fetch. (Reverse-engineered 2026-10-09 by
# diffing replies across live toggles.)
#   motion detection enabled: boolean 1/0
#   IR emitter strength: level (observed 1,4,7)
# value can be an int offset, or a tuple of offsets for a setting stored in
# several bytes (e.g. "flip video" = flip-H + flip-V, set together).
# "armed": the system-armed flag the Blink app sets when you arm the camera.
# BOTH "armed" (0x36 / 0x2d) AND "motion" (0x73 / 0x6a) must be 1 for the camera
# to actually run motion detection and auto-upload clips -- motion=1 alone does
# nothing. Discovered 2026-11-01 by diffing a real-cloud config captured while the
# app armed the camera (the only byte besides motion + the clock that changed).
SETTINGS_OFFSETS = {
    210: {"retrigger": 0x3c, "sensitivity": 0x35, "armed": 0x36, "early_end": 0x37, "nv": 0x49, "ir": 0x4e, "audio": 0x5e, "audio_stream": 0x61, "motion": 0x73, "flip": (0x5f, 0x60), "clip_len": 0x63, "quality": 0x6c},   # heartbeat
    174: {"retrigger": 0x33, "sensitivity": 0x2c, "armed": 0x2d, "early_end": 0x2e, "nv": 0x40, "ir": 0x45, "audio": 0x55, "audio_stream": 0x58, "motion": 0x6a, "flip": (0x56, 0x57), "clip_len": 0x5a, "quality": 0x63},   # config-fetch
}
NV_NAMES = {0: "off", 1: "on", 2: "auto"}            # night-vision mode
QUALITY_NAMES = {1: "low", 2: "normal", 3: "high"}   # video quality
BOOL_SETTINGS = ("motion", "armed", "flip", "early_end", "audio_stream")   # coerced to 0/1

# Multi-byte integer settings: name -> (start_offset, n_bytes), big-endian.
# "zones" = 5x5 motion-detection grid: bits 0-24 = cells row-major (bit0 = top-
# left, 0 = selected / 1 = not), bit30 = custom-zones-enabled flag.
WIDE_OFFSETS = {210: {"zones": (0x6f, 4)}, 174: {"zones": (0x66, 4)}}

# Single-bit boolean settings sharing a flags byte: name -> (offset, bitmask).
BIT_OFFSETS = {210: {"early_notif": (0x5c, 0x01)}, 174: {"early_notif": (0x53, 0x01)}}


def _offs(v):
    return v if isinstance(v, tuple) else (v,)


def parse_settings(reply: bytes):
    """Return all known settings from a 0x0807 reply (single-byte settings from
    their first offset; wide settings as big-endian ints)."""
    off = SETTINGS_OFFSETS.get(len(reply))
    if not off:
        return None
    s = {k: reply[_offs(v)[0]] for k, v in off.items()}
    for k, (start, n) in WIDE_OFFSETS.get(len(reply), {}).items():
        s[k] = int.from_bytes(reply[start:start + n], "big")
    for k, (o, mask) in BIT_OFFSETS.get(len(reply), {}).items():
        s[k] = 1 if reply[o] & mask else 0
    return s


def zone_grid(mask: int) -> str:
    """One-line 5x5 render of a zones mask (█=selected, ·=not); bit i = cell i,
    0 = selected. Rows separated by '/'."""
    return "/".join("".join("█" if not (mask >> (r * 5 + c)) & 1 else "·"
                            for c in range(5)) for r in range(5))


def fmt_settings(s: dict) -> str:
    parts = []
    if "motion" in s: parts.append(f"motion={'on' if s['motion'] else 'off'}")
    if "ir" in s:     parts.append(f"ir={s['ir']}")
    if "nv" in s:     parts.append(f"nv={NV_NAMES.get(s['nv'], s['nv'])}")
    if "flip" in s:   parts.append(f"flip={'on' if s['flip'] else 'off'}")
    if "early_end" in s: parts.append(f"early_end={'on' if s['early_end'] else 'off'}")
    if "quality" in s: parts.append(f"quality={QUALITY_NAMES.get(s['quality'], s['quality'])}")
    if "clip_len" in s: parts.append(f"clip={s['clip_len']}s")
    if "sensitivity" in s: parts.append(f"sens={s['sensitivity']}")
    if "retrigger" in s: parts.append(f"retrig={s['retrigger']}s")
    if "audio" in s: parts.append(f"audio={s['audio']}")
    if "audio_stream" in s: parts.append(f"audio_stream={'on' if s['audio_stream'] else 'off'}")
    if "zones" in s: parts.append(f"zones=0x{s['zones']:08x}")
    if "early_notif" in s: parts.append(f"early_notif={'on' if s['early_notif'] else 'off'}")
    return " ".join(parts)


# Per-reply volatile fields to ignore when hunting for UNMAPPED setting bytes:
# the 16-byte session-id (0x07-0x16) and the 7-byte timestamp (0x1c-0x22).
VOLATILE_RANGES = {210: [(0x07, 0x16), (0x1c, 0x22)],
                   174: [(0x07, 0x16), (0x1c, 0x22)]}


def _excluded(length):
    excl = set()
    for v in SETTINGS_OFFSETS.get(length, {}).values():      # mapped single-byte settings
        excl.update(_offs(v))
    for start, n in WIDE_OFFSETS.get(length, {}).values():   # mapped wide settings
        excl.update(range(start, start + n))
    for a, b in VOLATILE_RANGES.get(length, []):
        excl.update(range(a, b + 1))
    return excl


def unknown_changes(prev: bytes, cur: bytes):
    """Offsets that changed between two same-length 0x0807 replies, excluding the
    session-id, timestamp, and already-mapped setting bytes. Each entry is a byte
    we do NOT yet understand -> a candidate new setting. Returns [(off, old, new)]."""
    if not prev or len(prev) != len(cur) or len(cur) not in SETTINGS_OFFSETS:
        return []
    L = len(cur)
    excl = _excluded(L)
    bitmap = {}                                   # offset -> union of mapped bits
    for o, mask in BIT_OFFSETS.get(L, {}).values():
        bitmap[o] = bitmap.get(o, 0) | mask
    out = []
    for i in range(L):
        if prev[i] == cur[i] or i in excl:
            continue
        if i in bitmap:                           # only report if an UNmapped bit changed
            if (prev[i] ^ cur[i]) & (~bitmap[i] & 0xff):
                out.append((i, prev[i], cur[i]))
        else:
            out.append((i, prev[i], cur[i]))
    return out


def patch_settings(reply: bytes, **vals):
    """Return a copy of a 0x0807 reply with named setting bytes set. Accepts
    motion/ir/nv (None = leave unchanged). motion is coerced to 0/1."""
    off = SETTINGS_OFFSETS.get(len(reply))
    if not off:
        return reply
    b = bytearray(reply)
    wide = WIDE_OFFSETS.get(len(reply), {})
    bits = BIT_OFFSETS.get(len(reply), {})
    for k, v in vals.items():
        if v is None:
            continue
        if k in wide:
            start, n = wide[k]
            b[start:start + n] = int(v).to_bytes(n, "big")
        elif k in bits:
            o, mask = bits[k]
            b[o] = (b[o] | mask) if v else (b[o] & ~mask & 0xff)
        elif k in off:
            byte = (1 if v else 0) if k in BOOL_SETTINGS else (v & 0xff)
            for o in _offs(off[k]):
                b[o] = byte
    return bytes(b)


def media_kind(data: bytes):
    """Classify a 0x0804 upload: ('snapshot','SNAPSHOT upload (JPEG)') or
    ('liveview','MEDIA upload (liveview H.264)'). The camera signals the mode in
    its header (offset 0x5a: 'RT' = realtime/video) and by payload (JPEG SOI vs
    an MPEG PES video packet)."""
    head = data[:65536]
    if data[0x5a:0x5c] == b"RT":            # explicit mode marker (most reliable)
        return "liveview", "MEDIA upload (liveview H.264)"
    if len(data) > 0x59 and data[0x59] == 0x30:
        return "snapshot", "SNAPSHOT upload (JPEG)"
    if b"\x00\x00\x01\xe0" in head:
        return "liveview", "MEDIA upload (liveview H.264)"
    if b"\xff\xd8\xff" in head:
        return "snapshot", "SNAPSHOT upload (JPEG)"
    return "media", "MEDIA upload (0x0804)"


def describe(direction: str, data: bytes, ctx: dict) -> str:
    """One-line action label. `ctx` is shared per-connection; the C->S call may
    stash the request opcode for the S->C call to use. direction in C->S / S->C."""
    if not data:
        return f"{direction} (empty)"

    # ---- REST / HTTP ----
    if data[:4] in HTTP_METHODS:
        line = data.split(b"\r\n", 1)[0].decode("latin1", "replace")
        ctx["rest"] = line
        return f"REST {line}"
    if data[:5] == b"HTTP/":
        status = data.split(b"\r\n", 1)[0].decode("latin1", "replace")
        body = data.split(b"\r\n\r\n", 1)[-1][:80]
        req = ctx.get("rest", "")
        path = req.split(" ")[1] if " " in req else ""
        return f"REST reply {status}  {path}  {body!r}"

    # ---- CS binary ----
    if direction == "C->S":
        h = parse_cs_header(data)
        if not h:
            return f"C->S ?? {data[:12].hex()}"
        ctx["req_opcode"] = h["opcode"]
        op = h["opcode"]
        name = OPCODES.get(op, f"opcode-0x{op:04x}")
        if op == 0x0807:
            kind = "bundle/config-fetch" if len(data) <= 400 else "heartbeat/status"
            return f"CS 0x0807 {kind}  owl={h['owl']}"
        if op == 0x0806:
            return f"CS 0x0806 command-poll  owl={h['owl']}"
        if op == 0x0804:
            seq = struct.unpack(">H", data[0x76:0x78])[0] if len(data) >= 0x78 else None
            kind, what = media_kind(data)
            s = f"  seq=0x{seq:04x}" if seq is not None else ""
            return f"CS 0x0804 {what}{s}"
        return f"CS 0x{op:04x} {name}  owl={h['owl']}"

    # S->C (reply) — classify by content + size, using request opcode if known
    req = ctx.get("req_opcode")
    if data[:5] == b"\x01\x00\x00\x00\x04":          # 0x0806 reply
        seq = _kt_seq(data)
        ips = _media_candidates(data)
        if len(data) == 15:
            return "CS 0x0806 reply: idle (no command)"
        if ips:
            return f"CS 0x0806 reply: *** START-LIVEVIEW -> media {', '.join(ips)} *** seq=0x{seq:04x}"
        return f"CS 0x0806 reply: config-ack seq=0x{seq:04x}" if seq is not None else "CS 0x0806 reply: command"
    if data[:7] == b"\x00\x29\x00\x00\x00\x00\x10":  # heartbeat/config reply or bundle (have session-id)
        if len(data) > 100000:
            return f"CS 0x0807 reply: BUND bundle ({len(data)}B, re-provisions certs)"
        s = parse_settings(data)
        tag = "heartbeat" if len(data) == 210 else ("config-fetch" if len(data) == 174 else f"0x0807 ({len(data)}B)")
        if s:
            return f"CS 0x0807 reply: {tag} ({fmt_settings(s)})"
        return f"CS 0x0807 reply: {tag}"
    if data[:6] == b"\x00\x29\x00\x00\x00\x00":      # media session reply (no session-id byte)
        return "CS 0x0804 reply: media session config (rate-control)"
    if req == 0x0807 and len(data) > 100000:
        return f"CS 0x0807 reply: BUND bundle ({len(data)}B)"
    return f"S->C reply ({len(data)}B) {data[:10].hex()}"


if __name__ == "__main__":  # quick self-test against captured flows
    import glob, os
    for f in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "..", "mitm", "flows", "*.c2s.bin")))[:0]:
        pass
    print("blink_proto: OPCODES", OPCODES)
