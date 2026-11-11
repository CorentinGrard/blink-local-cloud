# blink-local-cloud

A fully local, cloud-free replacement for Amazon's Blink cloud, for a
**Blink Mini** camera whose firmware has been flash-patched to trust a local CA.
The camera talks to this server instead of `immedia-semi.com`, so it keeps
working — streaming, motion recording, settings — with **no internet**.

Runs as a container on the home NAS, published LAN-only through Traefik at
`https://blink.lan`.

## How it works

The camera's `:443` cloud traffic is DNAT'd by the router to this server on
`:8080`. The server:

- terminates TLS with a cert chain the (patched) camera trusts (`certs/v2/`),
- answers Blink's REST control API,
- speaks the `CS` binary command channel (`0x0804` media, `0x0806` command poll,
  `0x0807` config/heartbeat) well enough to keep the camera onboarded and to
  **push** commands to it (liveview, snapshot, arm/disarm),
- captures and decrypts whatever the camera uploads.

A separate dashboard (default `:8090`, Traefik-fronted) shows the camera, takes
snapshots, plays live view in-browser (with **two-way talk**: hold a button to
speak through the camera's speaker), edits settings, browses/deletes saved
motion clips, and can take **snapshots automatically on an interval**
(time-lapse; off by default).

> **Reimplementing or hacking on the server?** See
> [`docs/BACKEND.md`](docs/BACKEND.md) — a full low-level reference: process
> model, the camera-facing REST + CS binary protocol (wire formats, the
> `0x0806` lock-step poll, media framing), every action, the settings byte
> maps, state files, HTTP API, and a from-scratch checklist.

## Layout

| Path | What |
|------|------|
| `tools/local_server.py` | Camera-facing server (entry point): TLS, REST, `CS` command channel |
| `tools/liveview.py`     | `0x0804` media sessions: live view, snapshots, motion clips |
| `tools/talk.py`         | Two-way talk: browser mic → camera speaker |
| `tools/notify.py`, `tools/common.py` | ntfy pushes; shared paths, logging, live state |
| `tools/webui.py`        | LAN dashboard server (snapshots, live view, settings, gallery) |
| `tools/web/`            | Dashboard pages: HTML, CSS and JS |
| `tools/blink_proto.py`  | Blink `CS` binary-protocol parser/encoder |
| `tools/media_watch.py`  | Reassembles `0x0804` uploads into JPEG / MP4 |
| `tools/templates/`      | Prebuilt command/config frames pushed to the camera |
| `tools/static/`         | Vendored `mpegts.js` for in-browser live view |
| `certs/v2/`             | TLS chain the camera trusts (**private key not committed**) |
| `tests/`                | Test suite (`unittest`) |
| `compose.yaml`, `Dockerfile` | Container + Traefik wiring |

## The CA key (not in git)

The camera is flash-patched to trust a self-generated CA. The server loads:

- `certs/v2/chain-dup.pem` — the cert chain (committed), and
- `certs/v2/my-ca2-key.pem` — its **private key** (git-ignored).

It must be the key matching the CA baked into the camera firmware; without it
the server can't start its TLS listener. It is **not** copied into the image
(`.dockerignore`): `compose.yaml` bind-mounts it read-only from
`/mnt/evi-pool/home/corentin/.blinkcloud/my-ca2-key.pem`, so the stack can be
built straight from a git checkout (Arcane GitOps sync on the NAS).

## Build & deploy

The image is built by **GitHub Actions** (`.github/workflows/build.yml`) on every
push to `main` and published to the GitHub Container Registry:

```
ghcr.io/corentingrard/blink-local-cloud:latest
```

(git tags `vX.Y.Z` also publish an immutable version tag.) Each build first runs
the tests, on the runner and again inside the built image (so they use the
image's own ffmpeg), and only pushes if they pass. `compose.yaml` pulls
that image with `pull_policy: always`, so **Arcane** on the NAS re-pulls the
latest build on each deploy — no building on the NAS. The CA key is still mounted
at runtime (above), so the published image carries no secret.

To run by hand on the NAS:

```bash
docker compose pull && docker compose up -d
```

### Environment variables

Set in `compose.yaml`. All are optional.

| Variable | Default | Purpose |
|---|---|---|
| `BLINK_PORT` | `8080` | camera-facing TLS port |
| `BLINK_WEB_PORT` | `8090` | dashboard port (Traefik target) |
| `BLINK_MEDIA_IP` | `10.10.10.1` | address the camera dials for media (the router, which DNATs to us) |
| `BLINK_CS_HOST` | `ccs-e007.blink.lan` | command-server hostname handed to the camera in `owl/status` (must resolve to us) |
| `BLINK_PUBLIC_IP` | `84.102.210.103` | public-looking IP returned by `/ip_address` |
| `BLINK_RETENTION_DAYS` | `7` | rolling media retention; `0` = keep forever |
| `BLINK_MOTION_COOLDOWN` | `35` | min seconds between motion-clip grants |
| `BLINK_SNAPSHOT_INTERVAL` | `0` | initial time-lapse interval in seconds (`0` = off); also settable live from the dashboard |
| `BLINK_NTFY_URL` | `https://ntfy.sh` | ntfy server for motion push notifications |
| `BLINK_NTFY_TOPIC` | — | ntfy topic (secret); or use the file below |
| `BLINK_NTFY_TOPIC_FILE` | `/app/ntfy-topic` | file holding the ntfy topic (mounted read-only) |
| `BLINK_NTFY_CLICK` | `https://blink.lan` | where tapping a motion push opens |
| `BLINK_CLIP_BASE_URL` | `https://blink.lan` | base URL for the "Watch clip" action |
| `BLINK_LIVE_SECS` | `20` | default live-view / clip length (s) |
| `BLINK_DEBUG` | `0` | `1` keeps raw uploads, protocol captures and talk recordings for debugging |

Protocol tuning and diagnostic knobs (`BLINK_LOG_DIR`, `BLINK_POLL_HOLD`, `BLINK_MEDIA_IDLE`,
`BLINK_LIVE_DELAY`, `BLINK_LIVE_PLAYER`, `BLINK_LATENCY`, `BLINK_EMPTY_HOLLOW`)
are described in [docs/BACKEND.md](docs/BACKEND.md).

## Tests

Standard library only (`unittest`). The talk tests need `ffmpeg` and the
page-script check needs `node`; each is skipped when missing.

```bash
python3 -m unittest discover -s tests -v
```

They use a fake camera on a socket pair (no hardware, no CA key) and a temp
log dir, and take about 20 s. To run them against the image's ffmpeg:

```bash
docker build -t blink-local-cloud:test .
docker run --rm -v "$PWD:/src" -w /src -e PYTHONDONTWRITEBYTECODE=1 \
  blink-local-cloud:test python3 -m unittest discover -s tests
```
