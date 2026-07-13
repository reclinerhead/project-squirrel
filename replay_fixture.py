# =============================================================================
# project-squirrel -- replay_fixture.py
#
# Replay a fixture clip (record_fixture.py) through the FULL live perception
# stack and print the churn metrics (issue #74) -- the before/after ruler
# every Phase 2 tracking change must be graded against, on the same frames.
#
#   python replay_fixture.py debug_frames/fixture_20260713_140000.avi
#   python replay_fixture.py fixture.avi --track-log     # per-track lifecycle
#
# The pipeline is deliberately identical to the daemon's RTSPFrameSource:
# model.track (same conf / imgsz / quantize / tracker yaml, persist across
# frames) -> extract_detections -> TrackMemory (dedupe + stitch + census) ->
# SpeciesPresence (the daemon's event debounce), with the clock derived from
# frame index / fps so a run is deterministic for a given clip and weights.
# Needs the GPU + ultralytics (bluejay), like live.py -- this is a workbench
# tool, not CI material; the logic it drives is what test_perception.py covers.
# =============================================================================

import argparse
import json
import os
import time
from collections import Counter

import cv2

import perception


def sidecar_fps(path, fallback=15.0):
    """The capture fps from the recorder's sidecar json, when present."""
    try:
        with open(os.path.splitext(path)[0] + ".json", encoding="utf-8") as f:
            return float(json.load(f)["fps"])
    except (OSError, KeyError, ValueError, json.JSONDecodeError):
        return fallback


def main():
    ap = argparse.ArgumentParser(
        description="Replay a fixture clip through the live perception stack (issue #74)")
    ap.add_argument("clip", help="fixture video from record_fixture.py")
    ap.add_argument("--fps", type=float, default=None,
                    help="clip frame rate (default: the recorder's sidecar, else 15)")
    ap.add_argument("--track-log", action="store_true",
                    help="print per-track birth/stitch/death lifecycle lines")
    args = ap.parse_args()

    fps = args.fps if args.fps is not None else sidecar_fps(args.clip)

    from ultralytics import YOLO   # lazy: heavy, GPU-only path
    model_path = os.environ.get("MERLE_MODEL", "models/current.pt")
    model = YOLO(model_path)
    # Warmup predict locks in FP16 before the first tracked frame, exactly
    # like the daemon source (precision locks when the predictor builds).
    import numpy as np
    model.predict(np.zeros((360, 640, 3), np.uint8),
                  imgsz=perception.IMGSZ, quantize=perception.QUANTIZE,
                  verbose=False)

    cap = cv2.VideoCapture(args.clip)
    if not cap.isOpened():
        raise SystemExit(f"Could not open {args.clip}")

    tm = perception.TrackMemory(log=print if args.track_log else None)
    presence = perception.SpeciesPresence()
    events = []
    frame_idx = 0
    resolution = None
    t0 = time.time()

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        frame_idx += 1
        if resolution is None:
            h, w = frame.shape[:2]
            resolution = (w, h)
        results = model.track(frame, conf=perception.DETECT_FLOOR,
                              imgsz=perception.IMGSZ,
                              quantize=perception.QUANTIZE, persist=True,
                              tracker=perception.TRACKER_YAML, verbose=False)
        live, _ = tm.ingest(perception.extract_detections(results[0], model.names))
        t = frame_idx / fps
        for kind, details in presence.observe(
                Counter(perception.voted(tr) for _, tr in live), t):
            events.append((t, kind, details))
        if frame_idx % round(fps * 30) == 0:
            print(f"[replay] {frame_idx} frames ({t:.0f}s of clip) ...")
    cap.release()

    if frame_idx == 0:
        raise SystemExit("No frames in the clip.")

    wall = time.time() - t0
    m = tm.metrics(fps)
    census = Counter(tm.seen.values())
    print(f"\n== replay report: {args.clip} ==")
    print(f"frames: {frame_idx} ({frame_idx / fps:.0f}s at {fps:g}fps), "
          f"processed in {wall:.0f}s wall ({frame_idx / wall:.1f} fps)")
    print(f"pipeline: model={model_path} imgsz={perception.IMGSZ} "
          f"quantize={perception.QUANTIZE} native={resolution[0]}x{resolution[1]} "
          f"tracker={os.path.basename(perception.TRACKER_YAML)}")
    print("\nchurn metrics (the Phase 2 ruler):")
    for key in ("ids_minted", "ids_minted_window", "ids_per_minute", "births",
                "births_window", "stitches", "raised", "deaths_window",
                "never_confirmed_window", "median_lifetime_frames",
                "mean_concurrency", "fragmentation", "canonical_fragmentation",
                "window_minutes"):
        print(f"  {key}: {m[key]}")
    print(f"\ncensus (tenured visitors): {dict(census) or 'nobody'}")
    kinds = Counter(kind for _, kind, _ in events)
    print(f"\nevents: {len(events)} total {dict(kinds) or ''}")
    for t, kind, details in events[:50]:
        print(f"  t={t:7.1f}s  {kind:9s}  {details}")
    if len(events) > 50:
        print(f"  ... and {len(events) - 50} more")


if __name__ == "__main__":
    main()
