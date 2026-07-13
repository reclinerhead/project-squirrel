# =============================================================================
# project-squirrel -- record_fixture.py
#
# Capture a raw fixture clip off the camera for the churn workbench (issue
# #74, Phase 0.5). Run it while a typical steady feeding scene is on the
# pavement; replay_fixture.py then runs the clip through the full live
# perception stack and prints the churn metrics -- the before/after ruler for
# every Phase 2 tracking change. Same philosophy as replay_events.py, one
# level down: frames instead of events.
#
#   python record_fixture.py --seconds 240
#   python record_fixture.py --seconds 240 --out debug_frames/feeding.avi
#
# It opens the SAME main-stream RTSP feed the daemon watches (frames.rtsp_url
# -- one URL construction in the codebase) with the same FFmpeg options, and
# writes the decoded frames as MJPG (each frame an independent JPEG): no
# motion-compression artifacts stacked on top of the camera's H.264, so the
# replay sees essentially what the daemon's model saw. Deliberately
# dependency-free (no ffmpeg on PATH for a raw -c copy); the cost is disk --
# roughly 10-15 MB/s at 4K15, so a 4-minute fixture runs a few GB. Fixtures
# land in debug_frames/ (gitignored) by default.
#
# The camera serves multiple RTSP clients, so the daemon can keep running
# while this records -- both see the same scene.
# =============================================================================

import argparse
import json
import os
import time
from datetime import datetime

import cv2

import frames


def main():
    ap = argparse.ArgumentParser(description="Record a raw RTSP fixture clip (issue #74)")
    ap.add_argument("--seconds", type=float, default=240.0,
                    help="capture length (default 240 = 4 minutes)")
    ap.add_argument("--out", default=None,
                    help="output path (default debug_frames/fixture_<stamp>.avi)")
    args = ap.parse_args()

    url, redacted = frames.rtsp_url()
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", frames.RTSP_FFMPEG_OPTIONS)

    out = args.out
    if out is None:
        os.makedirs("debug_frames", exist_ok=True)
        out = f"debug_frames/fixture_{datetime.now():%Y%m%d_%H%M%S}.avi"

    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise SystemExit(f"Could not open {redacted} -- camera reachable? MERLE_RTSP_* set?")
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    ok, frame = cap.read()
    if not ok:
        raise SystemExit("Stream opened but no frame arrived.")
    h, w = frame.shape[:2]
    print(f"[fixture] {redacted}")
    print(f"[fixture] native {w}x{h} @ {fps:g}fps -> {out}")

    writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"MJPG"), fps, (w, h))
    if not writer.isOpened():
        raise SystemExit(f"Could not open the writer for {out}")
    # JPEG quality 95: near-lossless against the already-decoded frames, and
    # the whole reason MJPG was picked over mp4v's default-bitrate smear.
    writer.set(cv2.VIDEOWRITER_PROP_QUALITY, 95)

    target = round(args.seconds * fps)
    written = 0
    t0 = time.time()
    try:
        while written < target:
            if ok:
                writer.write(frame)
                written += 1
                if written % round(fps * 15) == 0:
                    print(f"[fixture] {written}/{target} frames "
                          f"({written / fps:.0f}s of {args.seconds:g}s)")
            ok, frame = cap.read()
            if not ok:
                # A dropped read mid-capture: keep trying briefly rather than
                # tossing the fixture; a short gap is survivable, a dead
                # camera is not.
                time.sleep(0.2)
                ok, frame = cap.read()
                if not ok:
                    print("[fixture] stream died mid-capture -- keeping what we have")
                    break
    except KeyboardInterrupt:
        print("\n[fixture] stopped early -- keeping what we have")
    finally:
        writer.release()
        cap.release()

    wall = time.time() - t0
    sidecar = os.path.splitext(out)[0] + ".json"
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump({"url": redacted, "resolution": [w, h], "fps": fps,
                   "frames": written, "recorded_at": datetime.now().isoformat(
                       timespec="seconds"), "wall_seconds": round(wall, 1)}, f, indent=2)
    size_mb = os.path.getsize(out) / 1e6 if os.path.exists(out) else 0
    print(f"[fixture] done: {written} frames ({written / fps:.0f}s) in {wall:.0f}s "
          f"wall, {size_mb:.0f} MB -> {out} (+ {sidecar})")


if __name__ == "__main__":
    main()
