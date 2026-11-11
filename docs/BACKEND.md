# Backend reference — blink-local-cloud

A complete, low-level description of the server for anyone **reimplementing it**
(in another language, or from scratch). It covers how the process runs, the exact
wire protocol it speaks to the camera, every action it can perform, and every
file and environment knob it uses.

Scope: a single **Blink Mini** camera whose flash has been patched to trust a
local CA. There is no account system, no multi-camera support, and no cloud.

- [1. What the backend is](#1-what-the-backend-is)
- [2. Process model & startup](#2-process-model--startup)
- [3. Network placement (DNS, DNAT, TLS)](#3-network-placement-dns-dnat-tls)
- [4. Connection dispatch](#4-connection-dispatch)
- [5. REST control API (camera-facing)](#5-rest-control-api-camera-facing)
- [6. CS binary channel](#6-cs-binary-channel)
  - [6.1 Common header](#61-common-header)
  - [6.2 `0x0806` command poll](#62-0x0806-command-poll)
  - [6.3 `0x0807` heartbeat / config / bundle](#63-0x0807-heartbeat--config--bundle)
  - [6.4 `0x0804` media upload](#64-0x0804-media-upload)
- [7. Media pipeline](#7-media-pipeline)
- [8. Actions the backend can perform](#8-actions-the-backend-can-perform)
- [9. Settings model](#9-settings-model)
- [10. State & IPC files](#10-state--ipc-files)
- [11. Web dashboard & HTTP API](#11-web-dashboard--http-api)
- [12. Notifications (ntfy)](#12-notifications-ntfy)
- [13. Retention](#13-retention)
- [14. Templates](#14-templates)
- [15. Environment variables](#15-environment-variables)
- [16. Reimplementation checklist](#16-reimplementation-checklist)

---

## 1. What the backend is

The camera normally talks only to Amazon's Blink cloud over TLS. We stand in for
that cloud. Two processes, in one Python program:

| Component | File | Listens | Audience |
|-----------|------|---------|----------|
| **Camera server** | `tools/local_server.py` | `:8080` TLS | the camera |
| **Dashboard** | `tools/webui.py` | `:8090` HTTP | your browser/phone (via Traefik) |
| Protocol codec | `tools/blink_proto.py` | — | library |
| Media de-chunker | `tools/media_watch.py` | — | library (+ standalone watcher) |

`local_server.py` is the entry point; it imports `webui` and starts it in a
thread. `webui` never imports `local_server` — the two communicate only through
files in the log directory (§10), so there is no import cycle and either could be
reimplemented alone.

The camera speaks two protocols, both over TLS/443 (which the router redirects to
our `:8080`):

1. a small **REST/HTTP** control API (§5), and
2. a custom **binary "CS" channel** (§6) carrying one opcode per TLS connection.

Everything the camera can be made to do — snapshot, live view, arm/disarm, apply
settings, auto-record on motion — is driven through those two channels (§8).

---

## 2. Process model & startup

`main()` (`local_server.py`):

1. Opens a blocking TCP listener on `0.0.0.0:$BLINK_PORT` (default **8080**),
   `SO_REUSEADDR`, `listen(256)` (the camera opens connections in bursts).
2. Starts the web UI in a daemon thread (`webui.start(...)`, §11).
3. Starts the retention janitor thread if `BLINK_RETENTION_DAYS > 0` (§13).
4. Accept loop: every accepted socket is handed to a **new daemon thread**
   running `handle(raw, peer)`. There is no thread pool or connection cap beyond
   the listen backlog; threads are short-lived (one TLS connection each).

`handle()`:

- Sets a **15 s timeout** on the raw socket so a stalled TLS handshake can't pin
  a thread forever (the camera retry-storms when it can't come online; leaked
  threads once starved `accept()`).
- Wraps the socket in a server-side `SSLContext` (§3) through a `TeeConn`
  (records every decrypted byte each way, for `BLINK_DEBUG` flow dumps).
- Reads the first chunk (up to 64 KiB, 20 s timeout) and dispatches by its first
  bytes (§4).
- On close, if `BLINK_DEBUG`, writes the full decrypted exchange to
  `mitm/local/flows/`.
- Every log line also goes to `mitm/local/server.log` (rotated to
  `server.log.1` past 5 MiB), so the log is readable from the media volume
  without access to the container's stdout.

There is **no persistent per-camera state in memory** beyond a few module globals
(onboarding-done flag, current media request, last motion-grant time, live-state
cache). All durable state is on disk (§10). The process is safe to restart at any
time; the camera simply reconnects on its next poll.

---

## 3. Network placement (DNS, DNAT, TLS)

For the camera to reach us instead of Amazon:

- **DNS**: the camera (flash-patched) builds its names under `blink.lan`
  (`rest-hw-e007.blink.lan`, `is-cam-e007.blink.lan`), and we hand it
  `ccs-e007.blink.lan` in `owl/status` (`$BLINK_CS_HOST`). `*.blink.lan` must
  resolve to a host the camera can reach (router/our host). In the reference
  deployment this is an AdGuard rewrite on the OpenWrt router. An unpatched
  camera dials `*.immedia-semi.com` instead.
- **DNAT**: the camera dials port **443**; the router rewrites the camera's
  `:443` to this server's `:8080`. We serve *every* role (REST, CS, bundle,
  media) on that one port — dispatch is by content, not port.
- **TLS trust**: the camera has been flash-patched so its pinned CA bank trusts
  our CA (`certs/v2/`). We present `certs/v2/chain-dup.pem` with the private key
  `certs/v2/my-ca2-key.pem` (not in git; supply your own).

TLS context (`server_ctx()`), mirroring what the camera's stack accepts:

- `PROTOCOL_TLS_SERVER`, **minimum TLS 1.2**.
- Ciphers forced to `DEFAULT:@SECLEVEL=0` — the camera negotiates the legacy
  **`TLS_RSA_WITH_AES_128_CBC_SHA256`**, which modern OpenSSL refuses at the
  default security level. An RSA (not EC) cert is required.
- SNI is always `immedia-semi.com`; one cert covers every role.

The **media host IP is not DNS-resolved**. When we grant a live view we hand the
camera a raw IP in the command (§6.2); it opens TLS straight to `<ip>:443` with
SNI `immedia-semi.com`, so our patched cert still applies. We point that IP at
`$BLINK_MEDIA_IP` (the router), which DNATs it back to us.

---

## 4. Connection dispatch

After the TLS handshake, `handle()` reads the first plaintext bytes and routes:

| First bytes | Handler | Meaning |
|-------------|---------|---------|
| `GET `/`POST`/`PUT `/`HEAD`/`DELE` | `handle_rest` | HTTP/REST (§5) |
| `u32 == 0x00000029` | `handle_cs` | CS binary (§6) |
| anything else | `_dump` | unknown; capture raw to disk, keep reading |

`0x29` (decimal 41) is the CS magic at offset 0. REST and CS never collide
because an HTTP request never starts with those four bytes.

---

## 5. REST control API (camera-facing)

`handle_rest()` frames requests by `Content-Length` and honours HTTP keep-alive
(the real cloud advertises it, so we do too), serving multiple requests per
connection until the camera closes it.

**Header fidelity matters.** The real cloud is nginx + Phusion Passenger with a
specific header *set and order*; the camera may gate coming "online" on a
byte-identical reply. `http_response()` reproduces this order exactly:

```
HTTP/1.1 <status>
Date: <RFC1123 GMT>
Content-Type: <ctype>
Content-Length: <n>
Connection: keep-alive            (or close)
[Account-Id: <id>]                (only where the cloud sends it)
Client-Verified: 0
Server: nginx + Phusion Passenger(R)
Status: <status>
X-Blink-Trace-Id: 1-<hex8>-<hex24>   (only legitimately-dynamic field besides Date)
X-Content-Type-Options: nosniff
[X-Frame-Options: SAMEORIGIN]
X-Powered-By: Phusion Passenger(R)
[X-Xss-Protection: 1; mode=block]
```

Endpoints implemented:

| Method & path | Response body | Notes |
|---|---|---|
| `GET /api/v1/owl/status` | `{"act":"cs","host":"ccs-e007.blink.lan","wait":5}` | tells the camera which CS host to poll and the poll interval (s). `host` points back at us via DNS. |
| `POST …/ip_address` | `84.102.210.103` (text/html) | the camera's **public-looking reflexive candidate**. Must *not* be the camera's own LAN IP, or its media subsystem never activates (it ends up with no usable candidate → hollow polls). Overridable via `BLINK_PUBLIC_IP`. Sent with `Account-Id` + `X-Frame-Options` + `X-Xss-Protection`. |
| `POST …/onboarded` | `{"message":"Onboard Status Updated"}` | sent with `Account-Id`. |
| anything else | `{}` (200) | logged to `mitm/local/unknown_rest.log` with the request line + body so new endpoints can be implemented. |

> Reimplementation note: `act:"cs"` + `wait` is the entire bootstrap. The camera
> then moves to the CS channel and stays there, polling every ~`wait` seconds.

---

## 6. CS binary channel

TLS/443, custom binary. **One opcode per TLS connection**; the camera opens a
fresh connection for each poll / heartbeat / upload. Not every exchange is one
request + one reply — `0x0806` and the `0x0804` live stream are **lock-step**.

`handle_cs()` reads the header, extracts the opcode (`u16` at offset `0x1c`), logs
a one-line description (`blink_proto.describe`), updates live state, and branches.

### 6.1 Common header

Client → server, big-endian:

```
offset  size  field
0       u32   magic = 0x00000029
4       u32   serial_len (= 16)
8       N     serial              e.g. "G8T1940001730C5E"
24      u32   owl_id              device id
28      u16   opcode              0x0806 / 0x0807 / 0x0804
30      u32   token_len (= 22)
34      N     device token
56      ...   opcode-specific body (MAC, LAN IP, fw tag, ICE candidates, …)
```

Opcodes:

| opcode | name | direction of bulk data |
|--------|------|------------------------|
| `0x0806` | command poll | camera polls; server may push a command |
| `0x0807` | status/heartbeat, config fetch, bundle fetch | camera → server, server replies |
| `0x0804` | media upload | camera → server (JPEG / MPEG-TS) |

**Reading a request without idle-waiting.** Byte `0x78` of the first 366-byte
record tells us how much follows: `0x0e` = heartbeat (445 B total; 79 B arrive
~80 ms later in 4 records), `0x02` = config/bundle fetch or media-upload header
(complete at 366 B; an upload then waits for our reply). Unknown layouts fall
back to a 250 ms idle gap. See `_read_request()`.

### 6.2 `0x0806` command poll

This is the heart of the protocol and the single thing that historically broke
local operation. **It is a lock-step long-poll, not request/reply.**

**The opening (`blink_proto.converse_0806`).** A normal poll is three camera
messages; the camera **waits for a 5-byte ack after each one** before sending the
next:

```
C> 122 B   header
S<   5 B   ack = 01 00 00 00 04
C>  14 B   04 00000001 | 07 b2 01 ...        (idle-candidate record)
S<   5 B   ack
C> 187 B   05 00000000 | "9.96", MAC, LAN IP, "release:…"
S<   5 B   ack
   ... connection held open (long-poll) until the camera closes it (~10 s) ...
```

Message boundaries == TLS records: `SSLSocket.recv()` returns one record at a
time, and a message is either one record or a 5-byte header record followed by a
body record. So a message is **complete on any record that isn't 5 bytes** — no
idle timing needed. Ack each one with `01 00 00 00 04`; stop after 3.

> **Failure mode to avoid.** If the server waits for the "whole request" before
> replying, the camera is still waiting for the *first* ack, so nothing more
> arrives: the server only ever sees the first 122 bytes (the infamous "hollow
> poll") and the camera never activates its media subsystem. Reply-all-at-once +
> close produces a red LED on the camera's next (failed) write. The fix is
> strictly: **ack per message, then hold the connection open.**

**Holding the poll & pushing commands.** After the opening acks, `handle_cs`
keeps the connection open for up to `POLL_HOLD` (default **15 s**; the camera
usually closes at ~10 s) with a 0.1 s recv timeout, and pushes a command the
moment one exists (`next_command()`), exactly as the cloud pushes mid-poll. Each
poll, in priority order:

1. **Active media request** (`_media_poll`, from `media_request.json`, §10): a
   snapshot or live-view command.
2. **Manual queued command** — the oldest `*.bin` in `tools/queue/` (§8,
   escape hatch), moved to `tools/queue/sent/` after sending. Its Kt seq is
   patched to the poll's seq when the poll is media-ready.
3. **Kt echo** — if the poll itself carried a media-session `Kt` block (the
   camera is mid-session), echo its sequence back (`kt_echo`). A flat idle reply
   here would break the session and degrade the camera to hollow polls.
4. Otherwise nothing (the three acks already sent *are* the idle reply).

**Command message shape (a `Kt` block).** A command rides after the acks as one
more server message:

```
00 00 00 00 00 | <type> 00 05 "Kt" | 00 00 00 00 | <seq:u16> | ...zeros... | 04 <field:4> | ...
```

- **type byte** (just before `00 05 Kt`) selects the action:
  `0x07` = **snapshot** (also the keepalive/echo type), `0x05` = **start live view**.
- `Kt` marker is the ASCII bytes `00 05 4b 74`. Helpers locate every `Kt` block
  in a buffer (`_kt_blocks`): **seq** is a `u16` at `Kt+8` (`KT_SEQ`), the
  **media field** is 5 bytes at `Kt+0x23` (`KT_FIELD`).
- **field**: `04 00 00 00 01` = idle / no media host; `04 <ip0..ip3>` = "connect
  to this media server IP" (`set_media_ip`). A command may carry several blocks,
  each a candidate IP; the camera picks a reachable one. We send a single block
  pointing at `$BLINK_MEDIA_IP`.

**Sequence numbers.** Commands are numbered by one ever-increasing `u16` counter.
We persist ours in `kt_seq.json` starting at `SEQ_START = 0x0230` (above the
highest the real cloud was seen using), via `_next_seq()`. Reusing old sequence
numbers is suspected of triggering refusals.

**In-poll answers & refusal/retry.** The camera answers a command *inside the
same poll*. The server acks every answer message (any record that isn't 5 bytes).
If an answer's 5-byte header begins with `0x03`, the camera **refused** the
command (`03 ... 1a 00 05 Kt 21 <seq> ...`). The cloud's proven behaviour — and
ours — is to **resend immediately with the next seq, in the same poll**, up to
`POLL_RETRIES` (**5**) times. Retrying only on a later poll keeps the camera
refusing until a power cycle.

**Motion auto-record** is detected here too: see §8.

`BLINK_EMPTY_HOLLOW=1` is an opt-in experiment to reply 0 bytes to a hollow poll
to force re-init; off by default.

### 6.3 `0x0807` heartbeat / config / bundle

Branch by request size:

- **445-byte body = heartbeat.** Reply a **210-byte** config blob carrying the
  camera's settings. We start from the captured `heartbeat_0807.bin` template,
  patch the clock (`ts_patch`, §below) and overlay current settings
  (`patch_settings`, §9), then send it. The cloud's heartbeat reply is constant
  except the clock, so a patched replay is byte-faithful.
- **≤400-byte body (366) = onboarding bundle / config fetch:**
  - **First time** (`_bundle_served` false): serve the ~956 KB **`BUND` bundle**
    (`bundle.bin`) carrying *our* cert bank, and mark the camera onboarded. The
    bundle re-provisions the camera's cert store; because ours is already baked
    in, no in-flight rewrite is needed. There is no integrity hash over the cert
    bank.
  - **Afterwards**: serve the **174-byte** config-fetch reply (`config_0807.bin`),
    clock-patched and settings-overlaid.

`ts_patch()` writes the current UTC time at reply offset `0x1c`:
`year:u16 | month | day | hour | minute | second` (7 bytes, big-endian year).

### 6.4 `0x0804` media upload

The camera opens a new TLS connection and uploads media. `handle_cs` replies the
right **session-config opening** for what we asked for and then drains the upload
(`_capture_media`):

| We requested | Opening reply sent | Template |
|---|---|---|
| live view | 182 B opening + 9 B ts + 145 B session config | `media_reply_live_0804.bin` |
| snapshot | first **182 B** of the snapshot template (stop sent later) | `media_reply_snapshot_0804.bin` |
| (unknown) | generic | `media_reply_0804.bin` |

We know snapshot-vs-liveview from *which command we pushed* (`_media["last_mode"]`),
because the first 366 upload bytes don't reliably distinguish them;
`blink_proto.media_kind()` is the fallback (header `RT` marker at `0x5a`, byte
`0x30` at `0x59`, or payload sniff for a JPEG SOI vs an MPEG PES).

**Transport framing (the upload payload).** After the CS header the payload is a
sequence of **outer chunks**:

```
<type:1> <counter:u32> <length:u32> <payload: length bytes>
```

Two interleaved streams, demultiplexed by `type`:

- **`type 0x04`** (len 1340) → a **JPEG**. Snapshot = the still; live view = a
  poster thumbnail.
- **`type 0x00`** (len 1316 = 7×188) → a standard **MPEG-TS** stream: H.264
  **High/1080p ~15 fps** + **AAC-LC 16 kHz**. Each 188-byte TS packet starts with
  sync `0x47`. Feed straight to any TS demuxer; `ffmpeg -c copy` remuxes to MP4
  with no re-encode.

Reassemble each stream by concatenating its chunks' payloads in order
(`media_watch.split_streams`). The first chunk header sits 9 bytes before the
first JPEG SOI.

**Snapshot exchange:** camera sends a 7-byte opener, JPEG chunks, a 152-byte
`type 0x02` trailer, then a **9-byte end marker `01 00000000 00000000`** and
waits. Send the stop `08 00000000 00000000` on seeing the marker (don't wait out
a timeout).

**Live-view exchange (`_liveview_session`), lock-step:**

```
S< 182 B  session-config opening (ts-patched)
C>  51 B  handshake (7 + 9 + 9 + 26 bytes)    [LIVE_HANDSHAKE]
S<   9 B  go       = 0d 00000001 00000000     [LIVE_GO]  -> stream starts
C> ...    chunked thumbnail + MPEG-TS, ~30 msgs/s
S<  ~24 B receiver report every 1 s           [LIVE_REPORT]  (replayed; counters drift)
S<   9 B  stop 1   = 07 00000000 00000000      [LIVE_STOP_1]
S<   9 B  stop 2   = 08 00000000 00000000      [LIVE_STOP_2]
```

Streams for `secs` (request value, else `BLINK_LIVE_SECS`=20, capped at
`LIVE_MAX_SECS`=600) or until `media_stop` appears (§10). Sending the go *before*
the handshake makes the camera quit after ~0.5 s — order matters.

While streaming, bytes are (a) written to the raw capture file, (b) published to
the browser live hub (`_WebLiveFeeder` → `LiveHub`, §7/§11), and (c) optionally
piped to a local GStreamer window if `live=True` and a display exists
(`_LivePlayer`, with `_PcrFixer`).

**PCR quirk.** The camera emits a PCR only every 2 s (spec wants ≤100 ms), at
PTS−1.5 s. Files are fine (ffmpeg ignores PCR), and `mpegts.js` in the browser
times off PES PTS so it's fine too — but GStreamer's `tsdemux` on a live pipe
mis-maps time and freezes periodically. `_PcrFixer` injects a PCR-only packet
before each video PES at PTS−`BLINK_LIVE_DELAY` (default 1.5 s) for the local
GStreamer window only; it is **not** applied to the browser feed (it breaks
`mpegts.js` continuity → black screen).

**Two-way talk (server → camera speaker).** The live-view socket is
bidirectional: while streaming, the camera plays any audio we write back on it.
There is no extra command, setting, or handshake — the real cloud's talk
downlink (relay capture, 2026-11-11) is exactly the live-view sequence above
(182 B opening, go, a receiver report per second, the two stops) with audio
records interleaved:

```
05 <seq:u32> <len:u32> <ADTS frame: len bytes>
```

- AAC-LC, **16 kHz, mono**, ADTS (`FF F1 60 40 …`, MPEG-4, no CRC), one
  1024-sample frame (**64 ms**) per record; `len` = the frame's ADTS length.
- `seq` starts at **0 for each talk press** and increments per record. If it
  keeps counting across presses in one live view, the camera plays only the
  first press and ignores the rest (verified on the camera, 2026-11-11).
- Send in real time (one frame per 64 ms), a couple of frames ahead at most.
  Gaps are tolerated: underruns of up to 750 ms didn't break playback.

Our implementation (`TalkHub` → `_liveview_session`): the dashboard posts mic PCM
to `/api/talk` (§11). One ffmpeg process per talk press encodes it
(`highpass + acompressor + alimiter`, then `-c:a aac -b:a 32k -ar 16000 -ac 1
-f adts`), a reader thread splits the ADTS frames into a queue, and the
live-view loop sends them, pre-buffering `TALK_PREBUFFER` (5) frames before each
press to absorb jitter. Only the session thread writes to the TLS socket. Audio
posted while no live view is running is refused (409). `?format=adts` posts
ready-made ADTS frames instead (queued as-is), e.g. to replay a clip.

Pitfalls found the hard way:

- **ffmpeg input probing.** Without `-probesize 32 -analyzeduration 0`, the
  image's ffmpeg (7.1) holds the first ~4.4 s of a raw PCM pipe while probing,
  then drops it: talk starts ~5 s late and short phrases vanish.
- **Loudness.** Browser mic speech is 10–15 dB quieter than what the Blink app
  sends (RMS ~4–7k, peaks near full scale), too faint on the camera's speaker;
  hence the compressor and make-up gain.
- **A wedged camera.** After a run of failed sessions the camera reached a state
  where *any* talk audio made it stop its live-view upload within seconds (no
  error, the stream just stops), while live view without talk still worked.
  Power-cycling the camera cleared it. Rule this out before debugging talk.
- **Debugging talk.** With `BLINK_DEBUG=1` the server saves each press's encoded
  audio as `talk_<time>.aac` (replay it with `?format=adts`) and logs per-second
  talk stats (frames sent, queue depth, gaps, drops). The camera mixes its
  speaker output into its own upload, so the recorded live-view audio shows what
  was played (its echo canceller removes steady tones after ~2 s).

---

## 7. Media pipeline

`_capture_media(peer, first, conn, kind, opts, motion)`:

1. Opens `mitm/local/<ts>_<ip>_<kind|motion>_0804.bin` and writes the first
   chunk. **Clears the media request immediately** (an upload starting means the
   request was honoured; otherwise every poll during the upload re-sends the
   command and the camera red-LEDs mid-upload).
2. Drains the upload: live view via `_liveview_session`; snapshot by reading
   until the 9-byte end marker (then sends the stop). Idle cutoff
   `BLINK_MEDIA_IDLE` (default 5 s).
3. Re-classifies on the **complete** upload (`media_kind`) and de-chunks
   (`split_streams`):
   - **live view** (TS present): write `<base>.ts`, carve a `<base>.thumb.jpg`
     from the type-4 stream, remux to `<base>.mp4` (`_to_mp4`: `ffmpeg -c copy`,
     GStreamer video-only fallback). No decode happens, so a missing H.264 High
     decoder doesn't matter.
   - **snapshot** (JPEG): carve SOI…EOI → `<base>.jpg`.
4. Unless `BLINK_DEBUG`, delete the raw `.bin` (and the `.ts` once the `.mp4`
   exists) — keep only the playable result.
5. If this was a **motion** capture, fire one ntfy push (§12): with the thumbnail
   + a "Watch clip" action if available, else text-only.
6. Publish `last_media` into `state.json`.

`LiveHub` is a simple pub/sub: `/live.ts` subscribers each get a bounded queue;
`publish()` drops to slow subscribers rather than blocking the camera thread.

`media_watch.py` can also run **standalone** to extract media from relay flow
captures (`--once` for a backlog, else a watch loop keyed on file-size stability).
Inside the server only its `split_streams` / `_carve_jpeg` helpers are used.

---

## 8. Actions the backend can perform

Everything below is ultimately a CS command pushed in an `0x0806` poll, or a
settings byte pushed in an `0x0807` reply. The web UI and any external caller
trigger them by writing the IPC files in §10 — there is no in-process RPC.

| Action | Trigger (file written) | What the server does |
|---|---|---|
| **Snapshot** | `media_request.json` `{"mode":"snapshot"}` | push type-`0x07` command; camera uploads a JPEG → `.jpg`. |
| **Live view / record clip** | `media_request.json` `{"mode":"liveview","ip","live","secs"}` | push type-`0x05` command with `04 <ip>`; run the live exchange for `secs`; save `.ts`+`.mp4`+`.thumb.jpg`; fan out to the browser hub. `live:true` also opens a local GStreamer window. |
| **Stop live view** | `media_stop` (touch) | `_liveview_session` ends the stream and sends the stops. |
| **Arm / disarm** | `settings.json` `armed` + `motion` both 0/1 | pushed on the next heartbeat. The firmware records on motion **only when both bytes are set** (§9). |
| **Change settings** | `settings.json` keys (§9) | overlaid onto the next `0x0807` reply. |
| **Time-lapse snapshots** | `settings.json` `snapshot_interval` (seconds; 0 = off) | a background thread requests a snapshot every N seconds while the camera is online. See below. |
| **Motion auto-record** | *(automatic)* | see below. |
| **Arbitrary raw command** | drop a `*.bin` into `tools/queue/` | sent verbatim on the next poll (seq patched if the poll is media-ready), then moved to `queue/sent/`. Escape hatch for protocol experiments. |

**Motion auto-record (automatic).** While the camera is **armed**, on a motion
event it spontaneously opens a media session (a mid-poll `Kt` record). When
`handle_cs` sees a `Kt` in an in-poll answer and no media request is active and
`armed` is set and the last grant was > `MOTION_COOLDOWN` (default 35 s) ago, it
writes a `media_request.json` for a `clip_len`-second clip to `$BLINK_MEDIA_IP`
(`motion:true`) and re-runs `next_command()` in the same poll to push START. The
resulting clip is saved tagged `_motion_` and triggers an ntfy push.

Limits: a media request is abandoned after `MEDIA_REQ_TTL` (60 s); at most
`MEDIA_MAX_SENDS` (12) command sends per request.

**Time-lapse snapshots (automatic).** `_timelapse_janitor` (a background thread)
reads `snapshot_interval` (seconds) from `settings.json` live — `0` = off
(default; the env default is `BLINK_SNAPSHOT_INTERVAL`). When it is > 0 and that
many seconds have elapsed, it writes a normal snapshot `media_request.json`
(tagged `"timelapse": true`), so the result is an ordinary `.jpg`. It skips a
tick while a live view is streaming, a media request is already in flight, or the
camera hasn't been seen in the last 30 s, so it never fights motion recording or
a manual snapshot/live view. `snapshot_interval` is a **server-only** setting: it
lives in `settings.json` but is not a camera byte, so `patch_settings` ignores it
and it never touches the heartbeat/config (§9).

---

## 9. Settings model

Settings live in the `0x0807` reply bytes. Offsets differ by reply size (210-byte
heartbeat vs 174-byte config-fetch). `blink_proto` holds the maps; `get_settings`
merges the template defaults with `settings.json` overrides.

Single-byte settings (`SETTINGS_OFFSETS`):

| key | 210 off | 174 off | values |
|---|---|---|---|
| `retrigger` (re-arm delay s) | `0x3c` | `0x33` | int |
| `sensitivity` | `0x35` | `0x2c` | int |
| `armed` | `0x36` | `0x2d` | 0/1 |
| `early_end` | `0x37` | `0x2e` | 0/1 |
| `nv` (night vision) | `0x49` | `0x40` | 0 off / 1 on / 2 auto |
| `ir` (IR strength) | `0x4e` | `0x45` | 1 / 4 / 7 |
| `audio` | `0x5e` | `0x55` | int |
| `audio_stream` | `0x61` | `0x58` | 0/1 |
| `motion` | `0x73` | `0x6a` | 0/1 |
| `flip` (H+V, set together) | `0x5f,0x60` | `0x56,0x57` | 0/1 |
| `clip_len` (s) | `0x63` | `0x5a` | int |
| `quality` | `0x6c` | `0x63` | 1 low / 2 normal / 3 high |

Wide (big-endian) settings (`WIDE_OFFSETS`): `zones` — the 5×5 motion grid,
4 bytes at `0x6f` (210) / `0x66` (174). Bits 0–24 = cells row-major (0 =
selected), bit 30 = custom-zones enabled.

Single-bit settings (`BIT_OFFSETS`): `early_notif` — bit `0x01` of byte `0x5c`
(210) / `0x53` (174).

**Armed ⇔ motion coupling.** The firmware runs motion detection only when **both**
the `armed` byte and the `motion` byte are 1; `motion=1` alone does nothing.
`get_settings()` therefore sets `armed = motion` unless `settings.json` pins
`armed` explicitly, and the dashboard's Arm switch writes both bytes together.

`patch_settings(reply, **vals)` writes named settings into a reply copy (booleans
coerced to 0/1, wide values to big-endian, bit values masked). `parse_settings`
reads them back; `unknown_changes` diffs two same-length replies ignoring mapped
offsets + the volatile session-id (`0x07–0x16`) and timestamp (`0x1c–0x22`) to
hunt for still-unmapped setting bytes.

Defaults come from the heartbeat template, so an untouched `settings.json`
produces a reply byte-identical to the camera's own captured config (bar the
clock).

---

## 10. State & IPC files

All under the log dir `mitm/local/` (`$LOGDIR`; container-mounted to the NAS). The
camera server and the web UI coordinate purely through these — the contract any
reimplementation must keep.

| File | Writer → reader | Purpose |
|---|---|---|
| `media_request.json` | UI / motion → server | `{"mode":"snapshot"|"liveview","ip","live","secs","motion","ts"}`. Picked up on the next poll; cleared when the upload starts. TTL 60 s. |
| `media_stop` | UI → server | presence = stop the current live view. Removed at session start/end. |
| `settings.json` | UI → server | setting overrides (keys from §9, plus `armed`/`motion`, plus the server-only `snapshot_interval`). Overlaid on every `0x0807` reply (unknown keys ignored). |
| `kt_seq.json` | server | `{"next": <u16>}` — persisted command sequence counter. |
| `state.json` | server → UI | live status snapshot (written atomically via temp+rename). Fields: `ip`, `last_seen`(+`_str`), `last_opcode`, `last_action`, `req_count`, `onboarded`, `poll_size`, `poll_seq`, `media_ready`, `streaming` (`{since,live,secs}` or null), `settings_pushed`, `last_media`. |
| `unknown_rest.log` | server | appended: unimplemented REST request lines + bodies. |
| `tools/queue/*.bin` → `tools/queue/sent/` | you → server | manual raw CS commands (§8). |

Captured media also lands in `$LOGDIR`: `*.jpg` (snapshots), `*.ts`/`*.mp4` +
`*.thumb.jpg` (clips), filenames prefixed `YYYYMMDD-HHMMSS_<ip>_<kind>`; motion
clips contain `_motion_`. With `BLINK_DEBUG=1`, raw `*_0804.bin`, intermediate
`.ts`, and `flows/` + `polls/` captures are kept too.

`$LOGDIR` is `<repo>/mitm/local` by default; in the container it is bind-mounted
from `/mnt/applications/blinkcloud/media` (compose maps it to `/app/mitm/local`).

---

## 11. Web dashboard & HTTP API

`webui.start(hub, logdir, media_req, media_stop, media_ip, port, get_settings,
settings_file, talk)` runs a `ThreadingHTTPServer` (HTTP/1.1) on `0.0.0.0:$BLINK_WEB_PORT`
(default **8090**). Traefik fronts it at `https://blink.lan` (LAN-only).

The UI is two server-rendered pages (Camera, Gallery) built from inline
HTML/CSS/JS templates (no build step, no external CDN — `mpegts.js` is vendored
and served from `/static/`). The client polls `/api/state` every 2 s and renders
status, snapshot, live countdown, settings and recent activity from it.

Routes:

| Method | Path | Purpose |
|---|---|---|
| GET | `/`, `/index.html` | Camera page |
| GET | `/gallery` | Gallery page |
| GET | `/static/mpegts.js` | vendored player |
| GET | `/api/state` | `state.json` + `latest_snapshot` + `now` + `armed` |
| GET | `/api/settings` | `{"current": <get_settings()>}` |
| GET | `/api/media[?limit=N]` | all saved items (newest first): `{kind,name,thumb,motion,ts,bytes,duration}` |
| GET | `/live.ts` | live MPEG-TS stream (subscribes to `LiveHub`; `Connection: close`; ends after 90 s idle) |
| GET/HEAD | `/clips/<name>` | serve a saved `.mp4`/`.jpg`/`.ts` with **HTTP Range** support (iOS/Safari need it); bare filenames only, validated against the log dir |
| POST | `/api/snapshot` | write `media_request.json` (snapshot) |
| POST | `/api/liveview?secs=N` | write `media_request.json` (liveview, `secs` clamped 5–600) |
| POST | `/api/stop` | touch `media_stop`, remove `media_request.json` |
| POST | `/api/talk?sid=S&rate=R` | two-way talk: body = raw s16le mono PCM at `R` Hz (8000–96000) for talk burst `S`; `&end=1` closes the burst. `Content-Type: application/octet-stream`. **409** when no live view is running, **503** without ffmpeg (§6.4) |
| POST | `/api/arm` | set `armed`+`motion` in `settings.json` (JSON body `{"armed":0/1}`) |
| POST | `/api/timelapse` | set the time-lapse interval in `settings.json` (JSON body `{"seconds":N}`, `0` = off) |
| POST | `/api/settings` | merge posted setting keys into `settings.json` (ints only, whitelisted to `SETTINGS_UI`) |
| POST | `/api/delete` | delete items + siblings |

**Every POST** must carry `Content-Type: application/json` (or `application/octet-stream`, used by `/api/talk`; both need a preflight cross-site) and must not come from another origin (`Origin` host ≠ `Host`, or `Sec-Fetch-Site` not `same-origin`/`none`); otherwise **403**. This blocks CSRF: a cross-site page can't send JSON without a CORS preflight, which is never granted. `settings.json` writes are locked and atomic (temp file + rename).

**Security posture:** the UI assumes a trusted LAN (no auth; Traefik `lan-only`
middleware). The only hardening is: `/clips/` and delete validate names against a
regex and confine paths to the log dir (realpath check), and every POST is
JSON-only + same-origin (CSRF guard, above).
A reimplementation exposed more widely must add authn/authz.

Clip durations are probed lazily by `ffprobe` in a background thread, cached by
`(name, mtime)`. Deleting an item removes all siblings sharing its capture base
(`.jpg/.mp4/.ts/.thumb.jpg/.bin`).

---

## 12. Notifications (ntfy)

On a motion capture the server sends a push via ntfy (`notify_motion_snapshot`,
falling back to `notify_motion` if no thumbnail). The topic is a **secret** (read
access = anyone with it), so it is never baked into the image: read from
`$BLINK_NTFY_TOPIC` or the file `$BLINK_NTFY_TOPIC_FILE` (mounted read-only). No
topic ⇒ notifications off.

The snapshot push PUTs the JPEG as the body (ntfy shows it inline) with the text
in the `Message` header, `Click` → `$BLINK_NTFY_CLICK` (dashboard), and a "Watch
clip" action → `$BLINK_CLIP_BASE_URL/clips/<mp4>`. **Header values must be ASCII**
— urllib encodes headers as latin-1, so a non-ASCII char (em-dash etc.) raises
and the POST silently never goes out. Sends are fire-and-forget on a daemon
thread.

---

## 13. Retention

If `BLINK_RETENTION_DAYS > 0`, a janitor thread prunes files under `$LOGDIR`
older than that many days at startup and then hourly (`_prune_old_media`). The
persistent state files (`settings.json`, `state.json`, `kt_seq.json`,
`media_request.json`, `media_stop`) and `*.tmp` are always kept; now-empty
subdirectories are removed. `0` (or negative) = keep forever.

---

## 14. Templates

Prebuilt frames captured from the real cloud (or built once) in
`tools/templates/`, patched at runtime (clock, settings, seq, media IP):

| File | Used as |
|---|---|
| `heartbeat_0807.bin` | 210-byte heartbeat reply **and** the source of default settings |
| `config_0807.bin` | 174-byte post-onboarding config-fetch reply |
| `bundle.bin` | ~956 KB `BUND` onboarding bundle (our cert bank) |
| `start_liveview_0806.bin` | `0x0806` command, type `0x05` (START, IP patched in) |
| `arm_0806.bin` | `0x0806` command, type `0x07` (**snapshot** / keepalive echo; historical name) |
| `media_reply_live_0804.bin` | live-view session-config opening |
| `media_reply_snapshot_0804.bin` | snapshot session-config opening (first 182 B used) |
| `media_reply_0804.bin` | generic media opening (fallback) |

> These are the one piece that is camera/firmware/region specific. A different
> camera or region shard needs its own captures; the code (offsets, exchange
> logic) is otherwise general.

---

## 15. Environment variables

| Var | Default | Meaning |
|---|---|---|
| `BLINK_PORT` | `8080` | camera-facing TLS port |
| `BLINK_WEB_PORT` | `8090` | dashboard port (Traefik target) |
| `BLINK_MEDIA_IP` | `10.10.10.1` | IP handed to the camera as the media host (router, DNATs to us) |
| `BLINK_CS_HOST` | `ccs-e007.blink.lan` | CS hostname returned by `GET /api/v1/owl/status` (must resolve to us) |
| `BLINK_PUBLIC_IP` | `84.102.210.103` | reflexive candidate returned by REST `/ip_address` (must look public) |
| `BLINK_RETENTION_DAYS` | `7` | rolling media retention; `0` = forever |
| `BLINK_MOTION_COOLDOWN` | `35` | min seconds between motion-clip grants |
| `BLINK_SNAPSHOT_INTERVAL` | `0` | initial time-lapse interval in seconds (0 = off); the dashboard overrides it live via `settings.json` |
| `BLINK_NTFY_URL` | `https://ntfy.sh` | ntfy server |
| `BLINK_NTFY_TOPIC` | — | ntfy topic (secret); or via file |
| `BLINK_NTFY_TOPIC_FILE` | `/app/ntfy-topic` | file holding the topic (mounted read-only) |
| `BLINK_NTFY_CLICK` | `https://blink.lan` | tap target of a motion push |
| `BLINK_CLIP_BASE_URL` | `https://blink.lan` | base for the "Watch clip" action URL |
| `BLINK_DEBUG` | `0` | keep raw uploads, `.ts`, `flows/`, `polls/`, `talk_*.aac` and per-second talk stats for protocol debugging |
| `BLINK_POLL_HOLD` | `15.0` | max seconds to hold an `0x0806` long-poll open |
| `BLINK_LIVE_SECS` | `20` | default live-view/clip length |
| `BLINK_LIVE_DELAY` | `1.5` | local GStreamer playback cushion / PCR offset (s) |
| `BLINK_LIVE_PLAYER` | — | override the local GStreamer player command |
| `BLINK_MEDIA_IDLE` | `5` | idle-gap seconds that ends a drain |
| `BLINK_LATENCY` | `0` | artificial reply RTT (s), once per connection (diagnostic) |
| `BLINK_EMPTY_HOLLOW` | — | `1` = reply 0 bytes to a hollow poll (experiment) |

In-code constants worth knowing: `POLL_RETRIES=5`, `MEDIA_REQ_TTL=60`,
`MEDIA_MAX_SENDS=12`, `SEQ_START=0x0230`, `LIVE_MAX_SECS=600`,
`LIVE_HANDSHAKE=51`.

---

## 16. Reimplementation checklist

Minimum to keep a patched camera fully local:

1. **TLS listener** on one port, RSA cert the camera trusts, TLS 1.2 +
   `TLS_RSA_WITH_AES_128_CBC_SHA256` allowed (§3). Dispatch by first bytes (§4).
2. **REST**: `GET /owl/status` → `{"act":"cs","host":<us>,"wait":5}`;
   `POST …/ip_address` → a public-looking IP (not the camera's LAN IP);
   `POST …/onboarded` → the ack. Mirror the cloud's header set/order (§5).
3. **`0x0807`**: 366-byte first fetch → serve the `BUND` bundle (our certs) once,
   then the 174-byte config; 445-byte heartbeat → the 210-byte config, clock +
   settings patched (§6.3, §9).
4. **`0x0806`**: **ack each camera message as a non-5-byte record arrives**
   (3 for a plain poll), then **hold the connection open** and push commands
   after the acks; echo a session `Kt` seq if the poll carries one; on a `0x03`
   refusal resend with the next seq *in the same poll* (§6.2). This is the step
   that makes or breaks the whole thing.
5. **Media**: push a type-`0x05` (live) or `0x07` (snapshot) command with
   `04 <media-ip>`; on the camera's `0x0804` upload, send the session-config
   opening, run the handshake→go→report→stop exchange for live view (§6.4),
   de-chunk the two interleaved streams, remux the MPEG-TS to MP4 (§7).
6. **Settings & arm** via the `0x0807` reply bytes, remembering armed⇔motion
   coupling (§9). **Motion auto-record** by watching for a mid-poll `Kt` while
   armed (§8).

Keep the on-disk IPC contract (§10) if you want to reuse the dashboard, or build
your own control surface against the same actions.
