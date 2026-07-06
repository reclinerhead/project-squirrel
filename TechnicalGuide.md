# Merle Technical Guide

The living documentation of how project-squirrel (Merle) is built and why. This describes current state — chronology lives in git log and PR descriptions.

## Quick start — running the station

Each process gets its own terminal, all from the repo root (PowerShell). Order doesn't strictly matter — everything meets on the bus and tolerates the others being absent — but this order gives a quiet, sensible boot:

```powershell
# 1. The event bus (Mosquitto, console-run by design -- the installer's Windows
#    service is set to Manual and should stay that way)
& "C:\Program Files\mosquitto\mosquitto.exe" -c mosquitto.conf -v

# 2. Perception daemon (needs MERLE_RTSP_PASS for the camera;
#    set MERLE_SOURCE=synthetic for the camera-free world)
.\.venv\Scripts\python.exe -m uvicorn merle_daemon:app --port 8000

# 3. The narrator
.\.venv\Scripts\python.exe narrator.py --persona personas/marlin.yaml

# 4. The dashboard -> http://localhost:3000
pnpm --dir mcc dev
```

Rehearsal without live animals — republish archived events onto the bus with original (speed-scaled) timing:

```powershell
.\.venv\Scripts\python.exe replay_events.py --last 100 --speed 4
```

## System overview

```
Amcrest PoE cam ──RTSP/TCP──▶ Merle daemon (YOLO26s + ByteTrack + FastAPI + SQLite)
                                   │                     │
                       localhost HTTP (state/stream)     │ MQTT driveway/events
                                   ▼                     ▼
                             MCC dashboard ◀──ws:9001── Mosquitto ◀──▶ narrator.py
                        (Next.js + TS + Tailwind)   (the event bus)   (Marlin, v1)

live.py remains the standalone desktop vision stack (hard_frames/ harvest,
snapshots/, debug_frames/); it shares perception.py with the daemon.
```

## Vision pipeline (current)

- **Camera**: Amcrest PoE at `192.168.1.102`, RTSP main stream over TCP (`OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp` — UDP drops packets under load and smears frames). Camera image settings live in its web UI; `cap.set()` is ignored for RTSP. Credentials come from the `MERLE_RTSP_PASS` env var (`MERLE_RTSP_USER` optional, defaults `admin`) — never committed.
- **Model**: YOLO26s fine-tuned on driveway data. The deployed weights live in `models/` (the promoted-weights shelf), loaded from `models/current.pt` by default or `MERLE_MODEL` if set — `runs/` is training scratch and is never loaded directly. Weights are not in git (`*.pt` ignored). See `models/README.md` for the promote-a-new-model steps. Inference at `imgsz=1920` so distant animals survive the downscale from 4K.
- **Important: the model is NMS-free** (end-to-end head). The ultralytics `iou=` argument is a no-op; duplicate boxes on one animal are possible and must be removed explicitly — `dedupe_boxes()` in `label_utils.py` (greedy, class-agnostic, IoU ≥ 0.7).
- **Tracking**: ByteTrack (`bytetrack_squirrel.yaml`). `DETECT_FLOOR=0.10` deliberately low — weak detections sustain existing tracks through confidence dips (tracks only *start* at ≥ 0.5). Lost tracks coast for 15 frames (~1s) to paper over single-frame misses; pruned after 90. `track_buffer: 180` (~12s of id memory) because stationary feeding animals flicker out of detection for many seconds — shorter buffers re-minted ids, inflating the census and (before the species-level event debounce) manufacturing phantom arrive/depart events.
- **Classes**: `0=chipmunk, 1=squirrel, 2=turkey` — **append-only**. New classes get new trailing IDs; never reorder (every YOLO label file on disk depends on the mapping).

## Training flywheel

1. **Hard-frame banking** (live.py): when a live track's confidence sits in the flicker band (0.15–0.50), the raw frame (never annotated — drawn boxes would poison training data) is saved to `hard_frames/` with a YOLO-format `.txt` sidecar of every current box, deduplicated. Low confidence means "unsure WHAT", not "unsure WHERE" — so the boxes are usually right and human labeling becomes review-and-nudge.
2. **Backfill/re-label** (`prelabel.py`): same idea for any folder of stills.
3. **Dedup** (`dedup.py`): near-duplicate frames moved aside by comparing *label geometry* (the camera is fixed, so whole-image hashing would call everything a duplicate). Frames containing rare classes are always kept.
4. **Review** in Roboflow (upload jpg+txt+classes.txt together; boxes arrive pre-drawn). New harvests go **100% to the train split** — random splitting of temporally-correlated frames leaks near-duplicates into valid and inflates metrics.
5. **Train locally** (ultralytics). Augmentation happens online during training (fliplr, hsv, mosaic) — no pre-baked augmented dataset versions needed.
6. **Evaluate on the same ruler**: `yolo val` for both old and new weights against the *same* valid split before claiming improvement. Baseline: train-16, 0.936 mAP50 / 0.887 recall (all classes) on the 0705 valid split.

## MCC + daemon architecture (epic #1, in progress)

Design rules:

- **The daemon owns all state** (SQLite file, image folders). The MCC talks to it over localhost HTTP/WebSocket only — never touches the DB or filesystem directly. This keeps the daemon portable to a future mini-PC/Jetson without MCC changes.
- **Images and clips live on the filesystem; SQLite stores metadata and paths.** No blobs in the DB.
- **Video to the browser is MJPEG** (`/stream`, downscale-then-encode, q≈85) — browsers can't speak RTSP. Full-res single frames via `/snapshot`. WebRTC only if remote/multi-viewer needs appear.
- **Runtime is local-only.** The MCC is not hostable on Vercel (needs LAN camera + GPU). Remote access later = Tailscale.

### Storage (`storage.py`)

The daemon's persistent memory is a single SQLite file (stdlib `sqlite3`, no server, no extra deps — back it up by copying the file). Timestamps are ISO-8601 strings passed in by the caller, never generated inside storage, so every function is pure and deterministic for tests. Three tables:

- **`sightings`** — one row per tracked animal, keyed by `(session_id, track_id)`. ByteTrack ids only mean something within a single daemon run (they reset on restart), so `session_id` (the run identifier) disambiguates. Accumulates `frames`, `first/last_seen`, and running `max_conf`; `species` holds the latest voted class.
- **`events`** — notable moments (hard frame saved, crowd snapshot, clip recorded). `kind` classifies; `details` is a free-form JSON blob so new event types need no schema change. Per the filesystem rule, an event records a frame's *path*, not the frame.
- **`training_runs`** — one row per training round for the dashboard's metrics-over-time panel. Headline `map50`/`recall`/`map50_95` are columns; per-class detail is JSON in `metrics`. Keyed by `run_name` (PK) so seeding is idempotent. Seeded with the train-15 and train-16 baseline (0705 valid split).

Pure-logic tests in `test_storage.py` run in CI (stdlib-only, no model/camera). `connect()` enables WAL (on-disk only) so the perception thread can write while request threads read.

### The daemon (`merle_daemon.py`) and frame sources (`frames.py`)

The daemon is a FastAPI app. A background `Worker` thread runs the perception loop — pull a frame + its detections from a **frame source**, annotate, JPEG-encode, publish to a lock-guarded `SharedState`, and persist sightings/events — while request handlers read that state. Endpoints:

- `GET /state` — session id, control flags, live counts/tracks/fps, run totals, recent events (JSON).
- `GET /stream` — the annotated feed as `multipart/x-mixed-replace` MJPEG (an `<img src>` in the browser; no player). Async + `is_disconnected()` so a closed tab frees the generator.
- `GET /snapshot` — the latest annotated frame, one JPEG.
- `POST /control` — `start`/`stop` the loop, `record_on`/`record_off`, `set_crowd_threshold`. Recording writes the **annotated** stream to `debug_frames/clip_*.mp4` (mp4v) from the worker thread — for watching/sharing a moment. (live.py's `V` key still writes *raw* clips, which is what you sample for training stills.) The writer is closed cleanly on `record_off`, lost feed, or shutdown, and a `clip_recorded` event is logged.

The **frame source** (`frames.py`) is the seam that keeps perception swappable. `FrameSource.read()` returns `(frame, [Detection, ...])` (each `Detection` has a `coasting` flag); the daemon's annotation/encoding/persistence has one implementation regardless of source, so the synthetic and real paths can't drift. Selected by `MERLE_SOURCE` — **default `camera`** (the real feed, so `uvicorn merle_daemon:app` just works); set `MERLE_SOURCE=synthetic` for the camera-free world. The source is built lazily **at server startup** (the module-level app passes a factory, not an instance), so importing the daemon never opens the camera or loads the model — that's what keeps tests/CI camera-free even with a camera default. Sources:

- `SyntheticFrameSource` — camera-free (a couple of squirrels + a visiting chipmunk, motion a pure function of a frame counter so it's deterministic). Powers the tests, local dev, and MCC frontend work with no camera or model. Imports no ultralytics, so CI stays light.
- `RTSPFrameSource` — the real Amcrest feed through the same model + tracker as live.py, sharing `perception.py`. Reads the password from `MERLE_RTSP_PASS`, the model from `MERLE_MODEL`; lazy-imports ultralytics so the daemon stays importable without torch. Coasting tracks are flagged (drawn grey, and not counted as matched frames in `sightings`). **Self-healing**: a failed read (camera restarted after a settings change, network blip) triggers a throttled reconnect (`RECONNECT_INTERVAL`) instead of freezing forever. The worker treats a `None` frame as transient — it flags `live.signal=false` and keeps polling rather than exiting, so a dropped stream never kills perception (it used to `break`, which froze the feed until a manual restart). The dashboard shows a distinct "reconnecting" veil while `signal` is false.

### Shared perception (`perception.py`)

The tracker bookkeeping and box-drawing are shared by **both** live.py and `RTSPFrameSource` so they can never drift (a real bug we already hit once between two files). It holds the detector/tracker config (`DETECT_FLOOR`, `IMGSZ`, tracker yaml, coasting windows, the hard-example flicker band, class colors), `extract_detections()` (pull ID-carrying detections out of a `model.track` result, ignoring the ID-less leak), `TrackMemory` (the coast/prune/vote bookkeeping + the `seen` run-total census), and `draw_tracks()` (boxes + labels; `scale=1.0` is tuned for 4K — the daemon passes `frame_height/2160`). Imports no ultralytics, so `test_perception.py` covers the bookkeeping camera-free. live.py is now a thin consumer of it; the model inference and RTSP capture (the untestable-in-CI parts) live in `RTSPFrameSource` and are verified against the real camera.

`test_daemon.py` drives the app via FastAPI `TestClient` against the synthetic source + an in-memory DB (no camera/model), so the full HTTP surface runs in CI. The live MJPEG stream can't be consumed cleanly through TestClient (infinite multipart blocks it), so the frame framing is unit-tested via `mjpeg_frame()` and the stream is verified end-to-end by running uvicorn.

Deps for the daemon are in `requirements.txt` (fastapi, uvicorn, opencv, numpy); ultralytics/torch stay out (installed per-machine, GPU-specific). CI installs headless opencv.

### The event bus (Mosquitto + `bus.py`)

Live event distribution rides an MQTT broker (Mosquitto), decoupling producers from consumers: the daemon publishes, and narrators/dashboards/future rover processes subscribe without anyone knowing who else exists. **SQLite stays the durable archive; the bus is the live transport** — a message dropped while the broker is down is never a lost record, just a moment nobody narrated. The daemon publishes fire-and-forget (QoS 0, auto-reconnect in the background) and runs identically with no broker at all.

- **Broker**: Mosquitto, run in a console via the repo's `mosquitto.conf` (see Quick start). The Windows installer registers an auto-start service; it is deliberately set to **Manual** — nothing runs on the desktop unbidden. Two listeners: `1883` plain MQTT (Python processes, paho-mqtt) and `9001` WebSockets (the browser — Next.js rewrites can't proxy WebSockets, so the dashboard connects to the broker directly). Anonymous auth; the bus never leaves the LAN.
- **Topics** (constants in `bus.py`, so a typo'd string can't split the system): `driveway/events` (daemon → world, one JSON event each), `narration/lines` (narrator → world), `narrators/<id>/status` (retained `online`/`offline` presence; `offline` is each narrator's MQTT Last Will, so a crash flips the dashboard lamp with no cleanup code).
- **Event kinds on the bus** (same shape as the `events` table: `ts`, `kind`, `details`): `arrival` and `departure`, plus `crowd_snapshot` and `clip_recorded`. Every event goes to SQLite and the bus through one `Worker._event()` helper so the two can't diverge.
- **Arrival/departure are SPECIES-level and debounced** (`Worker._species_presence`), not track-level. ByteTrack re-mints a new track id when it loses a stationary animal past its buffer and re-acquires it — track-level events turned every one of those into a phantom departure+arrival pair (a real spam problem on the live feed). Instead the Worker watches per-species counts of *matched* (non-coasting) tracks: a count increase must hold `ARRIVE_AFTER` (2s) to fire `arrival {species, count}`, a decrease must hold `DEPART_AFTER` (12s — longer than any realistic churn gap) to fire `departure {species, count}`; any wobble back resets the clock, so id churn produces zero events. `duration_s` rides on a departure only when the count hits 0 (the species' whole visit — with several present you can't know which individual left). The per-track `sightings` table and Run Census are untouched (they stay honest upper estimates).
- `MERLE_MQTT` (`host` or `host:port`, default `localhost:1883`) points the Python processes at the broker; `NEXT_PUBLIC_MERLE_MQTT_WS` overrides the browser's WebSocket URL (default: the page's own hostname, port 9001 — which is what makes phone-on-LAN work unconfigured). One exception: when the page is served on `localhost`, `busUrl()` pins the broker to `127.0.0.1` (IPv4). Windows browsers resolve `localhost` to IPv6 `::1` first, and the WebSocket to Mosquitto over `::1` connects at TCP level but never completes the MQTT handshake (`connack timeout`) — the same trap `next.config.ts` dodges for the daemon proxy. A LAN IP/hostname is left untouched.

### The narrator (`narrator.py`)

v1 of the scene narrator: **one voice, template prose, real pacing**. A single process subscribes to `driveway/events` and publishes spoken-style lines to `narration/lines` — it never plays audio itself; a consumer (the dashboard's TTS) speaks. Design decisions that outlive v1:

- **Persona vs bible**: a persona YAML (`personas/marlin.yaml` — name, `mqtt_id`, `tts_voice` hint, `personality_prompt`, pacing knobs) is one voice; `character_bible.yaml` is shared world canon (seed-pile location, Big Chonk lore). Kept separate from day one so multiple narrators never need untangling.
- **One pacing gate** (`worth_speaking()`): cooldown first, then per-kind interest scaled by the persona's `chattiness` against its `interest_threshold`. Silence is most of the show — with the default knobs, most events pass unremarked.
- **Tier-1 narration**: `generate()` fills Mad-Libs templates from the event + bible. It's the single swap point for the future LLM tier (`personality_prompt` is already in the persona waiting for it). Pacing mattered more than prose in v1.
- **Embedded producer**: the producer/orchestrator lives inside `narrator.py` as `Producer`, deliberately shaped around a *roster* (a set of voices) even though the roster is one — `cast(event)` picks who speaks, so solo-beat/banter-beat casting slots in later without a rewrite. What narrators do when a standalone producer is absent is a known future question, deferred to the promotion issue.
- Pure logic (gate, scoring, templates, persona loading) is covered by `test_narrator.py`; the MQTT plumbing is desk-tested against the real broker.
- **Rehearsal**: `replay_events.py` republishes archived SQLite events onto the bus with original relative timing (`--speed`, `--kinds`, long silences clamped by `--max-gap`) — the narrator can't tell the difference, which is the point of the bus.

### The MCC dashboard (`mcc/`)

The daemon's face: a Next.js App Router app, one page, one client component (`components/Dashboard.tsx`) that polls `/daemon/state` every second and renders the live MJPEG stream plus the instrument rail (current counts, run census, controls, event log, and coming-soon placeholders for future panels).

- **All daemon traffic goes through a rewrite proxy** (`next.config.ts`: `/daemon/:path*` → `MERLE_DAEMON_URL`, default `localhost:8000`). The browser stays same-origin — no CORS in the daemon — and a phone on the LAN reaching the dev server also reaches the daemon through it. Verified that the infinite MJPEG stream flows through the rewrite un-buffered. **Bus traffic is the one exception**: rewrites can't carry WebSockets, so the browser connects to Mosquitto directly (`lib/bus.ts` builds `ws://<page hostname>:9001`).
- **Field Journal** (in `Dashboard.tsx`): the narration panel. Subscribes over mqtt.js to `narration/lines` (entries render in the display face — the narrator's *voice* against the mono telemetry) and `narrators/+/status` (presence lamp: `online` → "on the air", `offline` → "off the air", any other retained payload shown verbatim — a future narrator can be "on coffee break"). Each empty state says exactly which command to run (bus down vs no narrator hired), and a failed connection surfaces its reason inline (`busError`). A default-muted speaker toggle TTS-speaks new lines via `speechSynthesis`, matching the persona's `tts_voice` hint against installed voices by substring (`pickVoice`). Pure parsing (`parseLine`, `statusTopicId`, `busUrl`, `pickVoice`) lives in `lib/bus.ts` with Vitest coverage.
- **Gotcha — import the browser build of mqtt.js.** `Dashboard.tsx` imports `mqtt/dist/mqtt.esm`, not the default `mqtt`. The default entry is mqtt.js's **Node** build (Buffer + node streams); Turbopack bundles it for the browser, but it can't serialize packets there, so the CONNECT never sends and the client dies with `connack timeout` (~30s) — the connection opens but never completes the MQTT handshake. The `dist/mqtt.esm` build is self-contained for browsers. Types are borrowed for that subpath in `lib/mqtt.browser.d.ts`. An `error` handler on the client is mandatory: mqtt.js is an EventEmitter, and an unhandled `error` throws in the browser and wedges its own reconnect loop.
- **Design language: "Ranger Station, Night Watch"** — pine-black panels with topographic-contour background, Fraunces display type + Sometype Mono telemetry, and species accent colors that are the *actual box colors* the vision stack draws (squirrel `#FF7031`, chipmunk `#FF3838`, turkey `#CFD231`), so the UI and stream read as one instrument. Tokens live as CSS variables in `app/globals.css`.
- **Daemon-down UX**: a failed `/state` poll shows the "Merle is asleep" panel with the wake command; when polls recover, the `<img>` is remounted (key bump) to reconnect the stream.
- **Live Watch** (`VideoFeed`) has a YouTube-style fullscreen toggle (bottom-right, hover-revealed) using the browser Fullscreen API on the feed container; double-click toggles, Escape exits natively. The stream freezes on its last frame whenever it isn't live, so three distinct veils cover it: **stand down** (engine idle, `running=false`), **reconnecting** (`live.signal=false`), and **asleep** (daemon unreachable) — a frozen frame is never mistaken for a live one.
- Pure display logic (event-line formatting, count sorting) lives in `lib/daemon.ts` with Vitest coverage; components themselves are visual and untested per the testing policy.
- Known UI gotchas encoded in comments: no `Date.now()` in render (SSR/client hydration mismatch), and rapid threshold-stepper clicks go through a ref so they compound instead of re-sending one stale value.

## Repo layout

- Root: Python vision stack (flat scripts, `.venv`, no packaging — deliberate for a single-machine project). `perception.py` is the shared tracker/annotation brain (used by live.py and the daemon). The daemon is `merle_daemon.py` (FastAPI app) + `frames.py` (frame sources) + `storage.py` (SQLite). The bus layer is `mosquitto.conf` (broker config) + `bus.py` (topics, publisher) + `narrator.py` (+ `personas/`, `character_bible.yaml`) + `replay_events.py`. Tests: `test_perception.py` / `test_daemon.py` / `test_storage.py` / `test_narrator.py` / `test_replay.py`; `requirements.txt` pins the daemon/bus deps.
- `models/`: deployed-weights shelf — `current.pt` (what the app loads) plus versioned `merle-trainNN.pt` copies. Only its README is tracked; the `.pt` files are gitignored. See `models/README.md`.
- `mcc/`: Next.js 16 App Router, TypeScript, Tailwind 4, pnpm. Tests: Vitest (`pnpm test`), CI runs them on every PR (`.github/workflows/tests.yml`).
- Not in git: datasets (`training/`), weights (`*.pt`, including `models/`), captures (`hard_frames/`, `snapshots/`, `debug_frames/`), `.venv/`.

## Project context

Personal learning project — wildlife observation in the author's own driveway, for fun and skill-building. Not a surveillance product. The scene narrator's foundation (bus + one templated voice) shipped with issue #9; still ahead (future epics): more narrators + banter, LLM narration, shared narrator memory, push notifications, unknown-species discovery loop, and a rover.
