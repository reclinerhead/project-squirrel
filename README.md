# project-squirrel (Merle)

A personal learning project: computer vision, and eventually a small rover, observing the wildlife in my own driveway — squirrels, chipmunks, turkeys, and whatever walks through at night. Built for fun and skill-building; not a surveillance product.

## What's here

| Piece | What it does |
|---|---|
| `live.py` | Live view: YOLO + ByteTrack over the Amcrest RTSP feed. Draws tracked boxes, saves crowd snapshots, and banks "hard frames" (low-confidence moments) with pre-drawn YOLO label sidecars for the next training round. |
| `prelabel.py` | Backfills YOLO label sidecars for a folder of stills using the current model — annotation becomes review-and-nudge instead of draw-from-scratch. |
| `dedup.py` | Moves near-duplicate hard frames aside (label-geometry comparison — the camera is fixed, so image hashing won't work). |
| `label_utils.py` | Shared box IoU / duplicate-suppression helpers (the model is NMS-free, so duplicates must be removed explicitly). |
| `extract_frames.py` | Turns recorded clips into training stills. |
| `mcc/` | The Merle Control Center — Next.js dashboard (in progress, see issue #1). |

## Setup notes

- Python side runs from `.venv` (ultralytics, OpenCV).
- The camera password is read from the `MERLE_RTSP_PASS` environment variable — see the comment at the top of `live.py`.
- Datasets, weights, and captured media are intentionally not in git.

See `TechnicalGuide.md` for architecture and conventions.
