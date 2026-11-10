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
snapshots, plays live view in-browser, edits settings, and browses/deletes saved
motion clips.

> **Reimplementing or hacking on the server?** See
> [`docs/BACKEND.md`](docs/BACKEND.md) — a full low-level reference: process
> model, the camera-facing REST + CS binary protocol (wire formats, the
> `0x0806` lock-step poll, media framing), every action, the settings byte
> maps, state files, HTTP API, and a from-scratch checklist.

## Layout

| Path | What |
|------|------|
| `tools/local_server.py` | Camera-facing server: TLS, REST, `CS` command channel |
| `tools/webui.py`        | LAN dashboard (snapshots, live view, settings, gallery) |
| `tools/blink_proto.py`  | Blink `CS` binary-protocol parser/encoder |
| `tools/media_watch.py`  | Reassembles `0x0804` uploads into JPEG / MP4 |
| `tools/templates/`      | Prebuilt command/config frames pushed to the camera |
| `tools/static/`         | Vendored `mpegts.js` for in-browser live view |
| `certs/v2/`             | TLS chain the camera trusts (**private key not committed**) |
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

## Run

```bash
docker compose up -d --build
```

Config via env in `compose.yaml`: `BLINK_MEDIA_IP` (address the camera dials for
media), `BLINK_WEB_PORT`, `BLINK_RETENTION_DAYS` (rolling media retention),
`BLINK_NTFY_URL` / `BLINK_NTFY_TOPIC_FILE` (motion push notifications),
`BLINK_DEBUG`.
