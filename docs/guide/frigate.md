# Frigate — the NVR

> Spoke of the [Merle Technical Guide](../../TechnicalGuide.md) — read the hub first for the machine roster, quick start, and cross-cutting conventions.
>
> **Covers:** `frigate/` — the NVR: 24/7 recording, generalist object detection, the go2rtc restream every other camera consumer reads
> **Runs on:** pearl (Docker container — the box's only one)
> **Related:** epic #243

### What it is, and what it is not

Frigate is the **generalist of record**: it holds the only RTSP sessions to the house's Amcrests — `house-rear` (192.168.1.102, watching the driveway; named `driveway` until #270) and `house-front` (192.168.1.173, the front door, added by #269) — records each camera's 4K main stream around the clock to the Purple drive, and runs COCO-class detection (person, car, dog, cat, bird) for recording triage. Both cameras are the same model behind the same `FRIGATE_RTSP_PASSWORD`, so their config blocks are mirrors. Camera names are location-based on purpose (#270): each name doubles as the restream path and, downstream, as a published source label — the project feed registry (`feeds.yml`, repo root) is where consumers find the restreams. **Merle remains the naturalist** — the YOLO26s squirrel/turkey pipeline, its bus events, and the training flywheel are untouched by Frigate's existence; the two detectors answer different questions and neither replaces the other.

The stream split is deliberate and load-bearing on pearl's modest CPU (i5-4250U, shared with Earl's BirdNET habit), and applies per camera: **detect decodes the 704×480 sub-stream** (`subtype=1`), **record remuxes the 3840×2160 main stream** (`subtype=0`) without decoding it — recording is a copy, not a transcode, including the camera's AAC audio.

### The container, and how it deploys

Frigate is the one Docker container on pearl, deliberately **not** a systemd unit like every other Merle service — Frigate's supported deployment is a container, and `restart: unless-stopped` stands in for systemd's supervision. `frigate/compose.yaml` and `frigate/config.yml` are versioned here; the camera password rides `frigate/.env` on pearl (gitignored via the root `.env*` rule, template in `env.example`).

**`merle-autodeploy` does not deploy Frigate** — it knows nothing about containers. Config changes are a manual two-step from the pearl checkout (also in `compose.yaml`'s header):

```
cp frigate/config.yml /srv/frigate/config/config.yml
docker compose -f frigate/compose.yaml up -d --force-recreate frigate
```

The repo's `config.yml` is the source of truth; the copy under `/srv/frigate/config` is Frigate's working copy (it stamps config-migration versions into it, and the UI's config editor edits it). Re-deploying overwrites the working copy — that direction is the point.

### Storage

The external 4 TB WD Purple (`sdb1`, ext4, mounted at `/srv/frigate` by UUID with `nofail`) is **Frigate's alone**: `config/` (Frigate's DB + deployed config) and `media/` (recordings, snapshots). Retention starting point: continuous 14 days, alert/detection clips and snapshots 30 — knobs in `config.yml`, to be re-tuned against measured disk burn. Recordings are a rolling window; nothing here is on the irreplaceable-files list.

### The restream (why nothing else talks to the camera)

go2rtc inside Frigate republishes every camera's streams at `rtsp://pearl:8554/<name>` — `house-rear` (4K + audio) and `house-rear_sub`, and likewise `house-front` / `house-front_sub`. Everything that used to hold its own session to the Amcrest reads the restream since #247, and since #270 finds it in **the project feed registry** (`feeds.yml`, repo root — the hostname-form URLs resolve on pearl and the dev boxes alike): the Merle daemon and `tools/live.py` read the `naturalist` feed (`house-rear`) via `rtsp_url()` in `vision/frames.py`; Earl runs every feed flagged `earl: true` — both cameras' audio tracks plus the rover mic. The restream is credential-free, so no password rides along; the break-glass for a Frigate that's down is `MERLE_RTSP_URL` (the daemon) or a local `MERLE_FEEDS` registry carrying the direct URL (Earl), with the redactors keeping credentials out of logs either way. **Each camera serves exactly one client: Frigate.** Adding a camera is a copy-pasted `go2rtc`/`cameras` block pair (#269, house-front, is the worked example) plus a `feeds.yml` entry for whoever should consume it.

### Access and integration

- **UI / authenticated API on `:8971`** (the unauthenticated internal `:5000` is deliberately not published). Through the front door it's `http://frigate.lan` (its own hostname, not a `pearl/frigate` path — Frigate's UI breaks under subpath serving; Caddy is the house's one TLS-upstream proxy here, skip-verify on loopback). The Homestead's Security tile points at it, with its lamp fed by the retained `frigate/available` topic. First-run admin credentials appear once in `docker logs frigate`; users managed in the UI thereafter.
- **MQTT**: Frigate publishes `frigate/*` (events, stats, availability) to the house Mosquitto — same broker as `driveway/*` and `audio/*`, by host IP because the container's `localhost` is itself. Nothing subscribes yet; bridging `frigate/events` toward the narrators is on the epic's parked list.
- **Detector**: the Coral Edge TPU (`edgetpu`/`pci`, `/dev/apex_0` mapped into the container — driver friction log lives on #244). Measured ~10.6 ms inference; the Phase 1 CPU detector it replaced burned ~1.5 of pearl's 4 threads for the same work (#246's before/after table). Detect stays at 5 fps per camera — the banked headroom is what let house-front join without touching the detector config.

### Ops

`docker compose -f frigate/compose.yaml logs -f frigate` for logs, `docker stats frigate` for load, the UI's System page for per-camera fps/inference metrics. Runbook detail — including the Coral driver's next-kernel-bump recipe — lives in `Servers/Pearl.md` § Frigate.
