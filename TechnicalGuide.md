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
- **Model**: YOLO26s fine-tuned on driveway data (`runs/detect/train-16/weights/best.pt`, not in git). Inference at `imgsz=1920` so distant animals survive the downscale from 4K.
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

## Repo layout

- Root: Python vision stack (flat scripts, `.venv`, no packaging — deliberate for a single-machine project).
- `mcc/`: Next.js 16 App Router, TypeScript, Tailwind 4, pnpm. Tests: Vitest (`pnpm test`), CI runs them on every PR (`.github/workflows/tests.yml`).
- Not in git: datasets (`training/`), weights (`*.pt`), captures (`hard_frames/`, `snapshots/`, `debug_frames/`), `.venv/`.

## Project context

Personal learning project — wildlife observation in the author's own driveway, for fun and skill-building. Not a surveillance product. Long-term roadmap (future epics): scene narrator, push notifications, unknown-species discovery loop, and a rover.
