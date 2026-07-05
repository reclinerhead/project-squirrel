# Merle Technical Guide

The living documentation of how project-squirrel (Merle) is built and why. This describes current state — chronology lives in git log and PR descriptions.

## System overview

```
Amcrest PoE cam ──RTSP/TCP──▶ Python vision stack (live.py: YOLO26s + ByteTrack)
                                     │
                                     ├─ hard_frames/  (self-labeling training harvest)
                                     ├─ snapshots/    (crowd moments, annotated)
                                     └─ debug_frames/ (manual stills + clips)

Planned (epic #1): the vision stack becomes a long-running daemon (FastAPI + SQLite)
and the MCC (mcc/ — Next.js dashboard) becomes its face:

Amcrest ──RTSP──▶ Merle daemon (YOLO + ByteTrack + FastAPI + SQLite)
                        │  localhost HTTP / WebSocket
                        ▼
                  MCC (Next.js + TS + Tailwind, pnpm)
```

## Vision pipeline (current)

- **Camera**: Amcrest PoE at `192.168.1.102`, RTSP main stream over TCP (`OPENCV_FFMPEG_CAPTURE_OPTIONS=rtsp_transport;tcp` — UDP drops packets under load and smears frames). Camera image settings live in its web UI; `cap.set()` is ignored for RTSP. Credentials come from the `MERLE_RTSP_PASS` env var (`MERLE_RTSP_USER` optional, defaults `admin`) — never committed.
- **Model**: YOLO26s fine-tuned on driveway data. The deployed weights live in `models/` (the promoted-weights shelf), loaded from `models/current.pt` by default or `MERLE_MODEL` if set — `runs/` is training scratch and is never loaded directly. Weights are not in git (`*.pt` ignored). See `models/README.md` for the promote-a-new-model steps. Inference at `imgsz=1920` so distant animals survive the downscale from 4K.
- **Important: the model is NMS-free** (end-to-end head). The ultralytics `iou=` argument is a no-op; duplicate boxes on one animal are possible and must be removed explicitly — `dedupe_boxes()` in `label_utils.py` (greedy, class-agnostic, IoU ≥ 0.7).
- **Tracking**: ByteTrack (`bytetrack_squirrel.yaml`). `DETECT_FLOOR=0.10` deliberately low — weak detections sustain existing tracks through confidence dips (tracks only *start* at ≥ 0.5). Lost tracks coast for 15 frames (~1s) to paper over single-frame misses; pruned after 90.
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
- `POST /control` — `start`/`stop` the loop, `record_on`/`record_off`, `set_crowd_threshold`.

The **frame source** (`frames.py`) is the seam that keeps perception swappable. `FrameSource.read()` returns `(frame, [Detection, ...])` (each `Detection` has a `coasting` flag); the daemon's annotation/encoding/persistence has one implementation regardless of source, so the synthetic and real paths can't drift. Selected at startup by `MERLE_SOURCE` (`camera` → real feed; anything else → synthetic). Sources:

- `SyntheticFrameSource` — camera-free (a couple of squirrels + a visiting chipmunk, motion a pure function of a frame counter so it's deterministic). Powers the tests, local dev, and MCC frontend work with no camera or model. Imports no ultralytics, so CI stays light.
- `RTSPFrameSource` — the real Amcrest feed through the same model + tracker as live.py, sharing `perception.py`. Reads the password from `MERLE_RTSP_PASS`, the model from `MERLE_MODEL`; lazy-imports ultralytics so the daemon stays importable without torch. Coasting tracks are flagged (drawn grey, and not counted as matched frames in `sightings`).

### Shared perception (`perception.py`)

The tracker bookkeeping and box-drawing are shared by **both** live.py and `RTSPFrameSource` so they can never drift (a real bug we already hit once between two files). It holds the detector/tracker config (`DETECT_FLOOR`, `IMGSZ`, tracker yaml, coasting windows, the hard-example flicker band, class colors), `extract_detections()` (pull ID-carrying detections out of a `model.track` result, ignoring the ID-less leak), `TrackMemory` (the coast/prune/vote bookkeeping + the `seen` run-total census), and `draw_tracks()` (boxes + labels; `scale=1.0` is tuned for 4K — the daemon passes `frame_height/2160`). Imports no ultralytics, so `test_perception.py` covers the bookkeeping camera-free. live.py is now a thin consumer of it; the model inference and RTSP capture (the untestable-in-CI parts) live in `RTSPFrameSource` and are verified against the real camera.

`test_daemon.py` drives the app via FastAPI `TestClient` against the synthetic source + an in-memory DB (no camera/model), so the full HTTP surface runs in CI. The live MJPEG stream can't be consumed cleanly through TestClient (infinite multipart blocks it), so the frame framing is unit-tested via `mjpeg_frame()` and the stream is verified end-to-end by running uvicorn.

Deps for the daemon are in `requirements.txt` (fastapi, uvicorn, opencv, numpy); ultralytics/torch stay out (installed per-machine, GPU-specific). CI installs headless opencv.

### The MCC dashboard (`mcc/`)

The daemon's face: a Next.js App Router app, one page, one client component (`components/Dashboard.tsx`) that polls `/daemon/state` every second and renders the live MJPEG stream plus the instrument rail (current counts, run census, controls, event log, and coming-soon placeholders for future panels).

- **All daemon traffic goes through a rewrite proxy** (`next.config.ts`: `/daemon/:path*` → `MERLE_DAEMON_URL`, default `localhost:8000`). The browser stays same-origin — no CORS in the daemon — and a phone on the LAN reaching the dev server also reaches the daemon through it. Verified that the infinite MJPEG stream flows through the rewrite un-buffered.
- **Design language: "Ranger Station, Night Watch"** — pine-black panels with topographic-contour background, Fraunces display type + Sometype Mono telemetry, and species accent colors that are the *actual box colors* the vision stack draws (squirrel `#FF7031`, chipmunk `#FF3838`, turkey `#CFD231`), so the UI and stream read as one instrument. Tokens live as CSS variables in `app/globals.css`.
- **Daemon-down UX**: a failed `/state` poll shows the "Merle is asleep" panel with the wake command; when polls recover, the `<img>` is remounted (key bump) to reconnect the stream.
- Pure display logic (event-line formatting, count sorting) lives in `lib/daemon.ts` with Vitest coverage; components themselves are visual and untested per the testing policy.
- Known UI gotchas encoded in comments: no `Date.now()` in render (SSR/client hydration mismatch), and rapid threshold-stepper clicks go through a ref so they compound instead of re-sending one stale value.

## Repo layout

- Root: Python vision stack (flat scripts, `.venv`, no packaging — deliberate for a single-machine project). `perception.py` is the shared tracker/annotation brain (used by live.py and the daemon). The daemon is `merle_daemon.py` (FastAPI app) + `frames.py` (frame sources) + `storage.py` (SQLite); `test_perception.py` / `test_daemon.py` / `test_storage.py` are the tests; `requirements.txt` pins the daemon deps.
- `models/`: deployed-weights shelf — `current.pt` (what the app loads) plus versioned `merle-trainNN.pt` copies. Only its README is tracked; the `.pt` files are gitignored. See `models/README.md`.
- `mcc/`: Next.js 16 App Router, TypeScript, Tailwind 4, pnpm. Tests: Vitest (`pnpm test`), CI runs them on every PR (`.github/workflows/tests.yml`).
- Not in git: datasets (`training/`), weights (`*.pt`, including `models/`), captures (`hard_frames/`, `snapshots/`, `debug_frames/`), `.venv/`.

## Project context

Personal learning project — wildlife observation in the author's own driveway, for fun and skill-building. Not a surveillance product. Long-term roadmap (future epics): scene narrator, push notifications, unknown-species discovery loop, and a rover.
