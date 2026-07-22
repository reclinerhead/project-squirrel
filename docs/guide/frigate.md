# Frigate — the NVR

> Spoke of the [Merle Technical Guide](../../TechnicalGuide.md) — read the hub first for the machine roster, quick start, and cross-cutting conventions.
>
> **Covers:** `frigate/` — the NVR: 24/7 recording, generalist object detection, the go2rtc restream every other camera consumer reads
> **Runs on:** pearl (Docker container — the box's only one)
> **Related:** epic #243

### What it is, and what it is not

Frigate is the **generalist of record**: it holds the only RTSP sessions to the driveway Amcrest, records the 4K main stream around the clock to the Purple drive, and runs COCO-class detection (person, car, dog, cat, bird) for recording triage. **Merle remains the naturalist** — the YOLO26s squirrel/turkey pipeline, its bus events, and the training flywheel are untouched by Frigate's existence; the two detectors answer different questions and neither replaces the other.

The stream split is deliberate and load-bearing on pearl's modest CPU (i5-4250U, shared with Earl's BirdNET habit): **detect decodes the 704×480 sub-stream** (`subtype=1`), **record remuxes the 3840×2160 main stream** (`subtype=0`) without decoding it — recording is a copy, not a transcode, including the camera's AAC audio.

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

go2rtc inside Frigate republishes both camera streams at `rtsp://pearl:8554/driveway` (4K + audio) and `rtsp://pearl:8554/driveway_sub`. Everything that used to hold its own session to the Amcrest — the Merle daemon on bluejay, Earl's `amcrest` audio source, `tools/live.py` — repoints at the restream in Phase 3 (#247), after which the camera serves exactly one client. Adding a camera is a copy-pasted `go2rtc`/`cameras` block pair plus the wiring.

### Access and integration

- **UI / authenticated API on `:8971`** (the unauthenticated internal `:5000` is deliberately not published). First-run admin credentials appear in `docker logs frigate`. A Caddy route + Homestead placard are Phase 4 (#248).
- **MQTT**: Frigate publishes `frigate/*` (events, stats, availability) to the house Mosquitto — same broker as `driveway/*` and `audio/*`, by host IP because the container's `localhost` is itself. Nothing subscribes yet; bridging `frigate/events` toward the narrators is on the epic's parked list.
- **Detector**: CPU (`num_threads: 2`) as the Phase 1 stopgap; Phase 2 (#246) swaps in the Coral Edge TPU (`/dev/apex_0`, PCIe — driver friction log lives on #244).

### Ops

`docker compose -f frigate/compose.yaml logs -f frigate` for logs, `docker stats frigate` for load, the UI's System page for per-camera fps/inference metrics. Runbook detail (including the Coral driver's next-kernel-bump notes) lands in `Servers/Pearl.md` with Phase 4.
