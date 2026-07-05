# =============================================================================
# project-squirrel -- live view
#
# In-window keys:  Q quit  |  +/- zoom  |  S save one frame  |  V start/stop clip
#                  (S frames and V clips are written to debug_frames/)
#
# Turn a recorded clip into Roboflow-ready stills. Roboflow rejects the raw video
# codec (our recorder writes mp4v; phones write HEVC), but it takes JPGs fine --
# so extract frames and upload those. Run in PowerShell:
#   python extract_frames.py "debug_frames\clip_20260703_180000.mp4"
#   python extract_frames.py "C:\Users\toddw\OneDrive\Pictures\Camera Roll\2026\07\yourclip.MOV"
#   (add  --fps 1  for fewer / more varied frames;  --out FOLDER  to pick the output dir)
# =============================================================================

import cv2
import os
import sys
import time
import atexit
import ctypes
import numpy as np
from datetime import datetime
from collections import deque, Counter
from ultralytics import YOLO
from label_utils import dedupe_boxes
import perception

# --- Single-instance guard ------------------------------------------------
# A second copy of this script -- or a crashed/zombie one that never let go --
# can keep the camera opened exclusively, which locks out everything else: other
# instances, the 'c' driver dialog, even the Logitech control panel, until the
# handle is freed (that's the bug that needed a reboot to clear). A Windows named
# mutex stops the "two copies at once" case cleanly: the OS releases it the
# moment the process ends, so there's no stale lock file to clean up after a
# crash. Kept in a global so it isn't garbage-collected -- that would close the
# handle and drop the guard while we're still running.
if sys.platform == "win32":
    _instance_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "project_squirrel_live")
    if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
        print("live.py is already running in another window -- close that one first.")
        print("(If it crashed and the camera still seems locked, reboot to clear it.)")
        raise SystemExit

    # Raise the Windows timer resolution to 1ms. By default it's ~15ms, and
    # cv2.waitKey(1) rounds its wait UP to that granularity -- which alone cost
    # ~10ms per frame and capped the loop near 34fps. With this, the display step
    # drops to ~2ms and the loop runs ~46fps. Restore it on exit (system-wide).
    ctypes.windll.winmm.timeBeginPeriod(1)
    atexit.register(ctypes.windll.winmm.timeEndPeriod, 1)

# Load the deployed model. It lives in models/ -- the promoted-weights shelf,
# kept separate from the runs/ training scratch. By default we load
# models/current.pt (a copy of whichever training run is live); promoting a new
# model is just copying its best.pt over current.pt -- no code edit here. Set
# MERLE_MODEL to point at a versioned file instead (e.g. to A/B a candidate).
MODEL_PATH = os.environ.get("MERLE_MODEL", "models/current.pt")
if not os.path.exists(MODEL_PATH):
    print(f"Model not found: {MODEL_PATH}")
    print("Copy a trained best.pt into models/current.pt (see models/README.md),")
    print("or set MERLE_MODEL to a model file. Training weights live under runs/.")
    raise SystemExit
model = YOLO(MODEL_PATH)

# --- Camera source: Amcrest PoE camera over RTSP --------------------------
# The USB webcam (and its MSMF/MJPG backend dance) is retired; video now comes
# from the Amcrest IP camera on the wired network. Dahua/Amcrest RTSP paths:
#   subtype=0  main stream (full resolution -- what we use)
#   subtype=1  substream (low-res; switch to it if decode ever needs to be cheaper)
# The login is the same one as the camera's web UI at http://192.168.1.102/.
# The password lives in the MERLE_RTSP_PASS environment variable -- never in
# this file (the repo is public on GitHub). One-time setup in PowerShell:
#   [Environment]::SetEnvironmentVariable("MERLE_RTSP_PASS", "<password>", "User")
# then restart the terminal so the new variable is picked up.
RTSP_USER = os.environ.get("MERLE_RTSP_USER", "admin")
RTSP_PASS = os.environ.get("MERLE_RTSP_PASS")
if not RTSP_PASS:
    print("MERLE_RTSP_PASS is not set. One-time setup in PowerShell:")
    print('  [Environment]::SetEnvironmentVariable("MERLE_RTSP_PASS", "<password>", "User")')
    print("then open a new terminal and run live.py again.")
    raise SystemExit
RTSP_URL = (f"rtsp://{RTSP_USER}:{RTSP_PASS}@192.168.1.102:554"
            "/cam/realmonitor?channel=1&subtype=0")

# Force RTSP over TCP. FFmpeg's default (UDP) silently drops packets under
# network load, which shows up as grey smears and corrupted frames. This env
# var must be set BEFORE the capture opens -- FFmpeg reads it once at open time.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

print("Connecting to the camera over RTSP...")
cap = cv2.VideoCapture(RTSP_URL, cv2.CAP_FFMPEG)

if not cap.isOpened():
    print("Could not open the RTSP stream. Check that the camera is reachable at")
    print("192.168.1.102 and that RTSP_USER/RTSP_PASS at the top of live.py match")
    print("the camera's web login.")
    raise SystemExit

# Always release the camera on exit -- including on an unhandled error or Ctrl-C
# -- so a crash can never leave the handle locked. atexit hooks run on normal
# exit AND when an exception unwinds the interpreter, which the old cleanup at
# the bottom of the file did not (an error in the loop skipped it entirely).
atexit.register(cv2.destroyAllWindows)
atexit.register(cap.release)

# Resolution, frame rate, and codec are NOT set here anymore: an RTSP stream
# arrives however the camera encodes it, and cap.set() requests are ignored.
# To change them, use the camera's web UI (Setup > Camera > Video).
# Buffer only the newest frame so we always process live reality, not a backlog.
# This -- not the raw frame rate -- is what actually keeps end-to-end latency low.
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

# isOpened() can succeed with bad credentials on some firmware -- the failure
# only surfaces on the first read -- so pull a frame to confirm auth worked.
ok, _ = cap.read()
if not ok:
    print("Opened the RTSP connection but couldn't read a frame -- this usually")
    print("means the username/password is wrong, or the stream path doesn't match.")
    raise SystemExit

# Read back what the camera is actually sending (configured in its web UI,
# not here). The fourcc will be the stream codec, e.g. h264/hevc.
fourcc_int = int(cap.get(cv2.CAP_PROP_FOURCC))
fourcc = "".join(chr((fourcc_int >> (8 * i)) & 0xFF) for i in range(4)).strip()
print(f"Capture: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x"
      f"{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))} @ "
      f"{cap.get(cv2.CAP_PROP_FPS):.0f}fps ({fourcc})")

# The detection floor, tracker config, inference size, coasting windows, class
# colors, the hard-example flicker band, and the box-drawing all live in
# perception.py now -- shared with the daemon's camera source so the two can't
# drift. See there for the rationale behind each.

# Snapshot settings: save a picture when a crowd of animals is in frame. The
# threshold counts every detection regardless of class (squirrel, chipmunk,
# turkey, ...), so a mixed group still trips it.
os.makedirs("snapshots", exist_ok=True)   # folder for saved shots (made if missing)
CROWD_THRESHOLD = 5                        # save when this many (or more) are in frame
SNAPSHOT_COOLDOWN = 10.0                    # seconds between saves, so 100+ FPS doesn't
                                           # flood the folder with near-identical shots
last_snapshot = 0.0                        # time.time() of the most recent save

# Confidence smoothing: the model re-scores every frame, so the raw number
# jitters. Remember the last N frames' scores and show their average instead.
SMOOTHING_FRAMES = 30
conf_history = deque(maxlen=SMOOTHING_FRAMES)   # auto-drops anything older than N frames
count_history = deque(maxlen=SMOOTHING_FRAMES)  # same window, but for the per-class counts
fps_history = deque(maxlen=SMOOTHING_FRAMES)    # end-to-end loop rate, for the on-screen counter

# --- Live image controls -------------------------------------------------
# The old webcam's Cam Bright / Exposure / Cam Gain sliders are gone: an IP
# camera ignores cap.set() for sensor properties. Exposure, brightness, and the
# rest are set in the camera's own web UI (http://192.168.1.102/, Setup >
# Camera > Image), which is far more capable than the webcam driver ever was.
# The one slider that survives is SW Gamma -- it's applied post-capture on our
# side, so it works the same regardless of where the frames come from.
#   SW Gamma    post-capture midtone curve, for taste   (100 = no change)
# Display scaling is done in software: the 4K frame is shrunk by DISPLAY_SCALE
# before imshow, and the default AUTOSIZE window then fits it exactly. We tried
# WINDOW_NORMAL + WINDOW_KEEPRATIO, but OpenCV's Win32 GUI backend doesn't
# honour KEEPRATIO (it's Qt-only) -- dragging the corner only changed the
# height and stretched the picture. Scaling both axes by the same factor here
# makes the aspect ratio exact by construction. Press +/- to zoom the view;
# capture and snapshots stay full 4K regardless -- only the display shrinks.
# 0.60 x 3840x2160 = 2304x1296, which fits a 1440p monitor with room to spare.
display_scale = 0.60
cv2.namedWindow("Live")
cv2.createTrackbar("SW Gamma", "Live", 100, 300, lambda v: None)  # pos/100, higher=brighter

# Rebuild the gamma LUT only when the slider moves. Start from an identity LUT
# (SW Gamma at its 100 default = no change).
prev_gamma = None
gamma_lut = np.arange(256, dtype="uint8")

print("Live view starting -- Q quit, +/- zoom, S snapshot, V start/stop recording.")
print("Exposure/brightness are now set in the camera's web UI, not here.")

prev_t = time.perf_counter()                  # for the end-to-end FPS counter

# On-screen confirmation banner: set by the 's' snapshot key, drawn over the
# video until its expiry time so you don't need the console to know it worked.
flash_msg = ""
flash_until = 0.0

# Video recording state. 'v' toggles recording the RAW frames (what the model
# sees -- no boxes drawn) to a .mp4 in debug_frames, so a clip can be sampled for
# training stills later. Same spirit as the 's' snapshot, but continuous. The
# writer is created on start and released on stop (and on exit, below the loop).
recording = False
video_writer = None
RECORD_FPS = 15   # the camera runs ~15fps; playback speed is therefore approximate

# The tracker's coasting + class-voting bookkeeping lives in
# perception.TrackMemory (shared with the daemon). Inside it, tm.seen is the
# run-total census: every distinct ByteTrack ID this run mapped to its voted
# class. Caveat: it counts track IDs, so it OVER-counts when the tracker
# fragments one animal into a new ID (after a long occlusion, or when two
# look-alikes cross and swap) -- a lively upper estimate, not an exact census.
tm = perception.TrackMemory()

CLASS_COLORS = perception.class_colors(model.names)
NAME_TO_ID = {name: i for i, name in model.names.items()}  # for YOLO label sidecars

# --- Hard-example saver ----------------------------------------------------
# When a LIVE track's confidence sits in the "flicker band" (perception.HARD_LO
# .. HARD_HI), the model is telling us this exact frame is hard for it. Save the
# raw frame (no boxes) to hard_frames/ for labeling -- for the next training
# round these are worth far more than another hundred easy, well-lit squirrels.
HARD_COOLDOWN = 5.0          # seconds between saves
last_hard = 0.0

while True:
    ok, frame = cap.read()                    # grab one frame from the webcam
    if not ok:
        print("Lost the camera feed.")
        break

    # Software gamma: a post-capture taste knob; rebuild its LUT only on change.
    gamma_pos = cv2.getTrackbarPos("SW Gamma", "Live")
    if gamma_pos != prev_gamma:
        g = max(gamma_pos, 1) / 100.0                             # avoid /0 at pos 0
        gamma_lut = np.clip((np.arange(256) / 255.0) ** (1.0 / g) * 255,
                            0, 255).astype("uint8")
        prev_gamma = gamma_pos

    # Apply gamma *before* detection so the model and the display match.
    frame = cv2.LUT(frame, gamma_lut)

    # If a recording is running, append this raw frame (no boxes) to the clip.
    if recording and video_writer is not None:
        video_writer.write(frame)

    # imgsz=1920: infer at 3x the trained 640 so distant squirrels survive the
    # downscale. At 640, a squirrel ~120px tall in the 4K frame shrinks to ~20px
    # -- far below anything in the training set -- and the model misses whole
    # groups of them (observed on the driveway cam). At 1920 they stay ~60px.
    # Costs more GPU per frame, but the RTX 5070 Ti keeps up at the camera's 15fps.
    #
    # model.track (not plain model()) runs the same detector, then adds a tracker
    # on top that assigns each animal a persistent ID across frames. persist=True
    # says "these frames are one continuous stream" so IDs carry over between
    # calls. ByteTrack is the tracker: when a walking squirrel's score dips, its
    # weak detection (down to DETECT_FLOOR) is re-used to keep its existing track
    # alive -- so the box stays put through the dip instead of blinking off.
    results = model.track(frame, conf=perception.DETECT_FLOOR, imgsz=perception.IMGSZ,
                          persist=True, tracker=perception.TRACKER_YAML, verbose=False)
    raw = results[0].boxes
    names = results[0].names

    # Feed this frame's ID-carrying detections into the shared tracker memory,
    # which returns the live (matched now) / coasting (missed recently, still
    # drawn greyed) split and keeps the class votes + run-total census (tm.seen).
    live_tracks, coasting = tm.ingest(perception.extract_detections(results[0], names))

    # Draw boxes ourselves instead of results[0].plot(): plot() only knows this
    # frame's matches, so it can neither coast a briefly-lost track nor use the
    # voted label. At full 4K, scale=1.0 renders the same as before.
    annotated = frame.copy()
    items = [(tid, perception.voted(t), t["xyxy"], True) for tid, t in live_tracks]
    items += [(tid, perception.voted(t), t["xyxy"], False) for tid, t in coasting]
    perception.draw_tracks(annotated, items, CLASS_COLORS, scale=1.0)

    # An animal counts while its track is live OR coasting -- the same
    # hysteresis the boxes get, so the readout doesn't dip on a missed frame.
    total = len(live_tracks) + len(coasting)
    counts = Counter(t["votes"].most_common(1)[0][0] for _, t in live_tracks + coasting)

    # TEMP diagnostic: print the raw detector output and scores, so we can see
    # whether an animal is scoring low vs not at all. Flags the leak case where
    # detections arrive without track IDs (those are ignored above).
    if len(raw) > 0:
        suffix = "" if raw.id is not None else "   (no track IDs -- ignored)"
        print("  detected:", ", ".join(f"{names[int(c)]} {float(s):.2f}"
                                        for c, s in zip(raw.cls, raw.conf)) + suffix)

    # Bank a hard example when a live track sits in the flicker band. Saves the
    # raw frame (boxes drawn INTO the image would poison it as training data)
    # plus a YOLO-format .txt sidecar of every current box, so labeling later is
    # "review and nudge" instead of "draw from scratch". Low confidence means
    # the model is unsure WHAT it sees, not WHERE -- the box itself is usually
    # placed right. Coasting boxes are included: an animal left unlabeled would
    # teach the next model "that's background", which is worse than a box a few
    # frames stale. Every sidecar gets human review before training anyway.
    if (time.time() - last_hard >= HARD_COOLDOWN
            and any(perception.HARD_LO <= t["conf"] < perception.HARD_HI
                    for _, t in live_tracks)):
        os.makedirs("hard_frames", exist_ok=True)
        stem = f"hard_frames/hard_{datetime.now():%Y%m%d_%H%M%S}"
        cv2.imwrite(stem + ".jpg", frame)
        img_h, img_w = frame.shape[:2]
        # live first -> a fragment's fresh box outranks its stale coasting twin
        cand = []
        for _, t in live_tracks + coasting:
            x1, y1, x2, y2 = t["xyxy"]
            x1, x2 = max(x1, 0), min(x2, img_w)   # clamp: boxes can poke past the edge
            y1, y2 = max(y1, 0), min(y2, img_h)
            cand.append((t["votes"].most_common(1)[0][0], (x1, y1, x2, y2)))
        # The model is NMS-free and ByteTrack can fragment one animal into two
        # IDs, so drop boxes that duplicate a higher-priority one before writing.
        label_lines = []
        for i in dedupe_boxes([box for _, box in cand]):
            name, (x1, y1, x2, y2) = cand[i]
            label_lines.append(
                f"{NAME_TO_ID[name]} {(x1 + x2) / 2 / img_w:.6f} {(y1 + y2) / 2 / img_h:.6f} "
                f"{(x2 - x1) / img_w:.6f} {(y2 - y1) / img_h:.6f}")
        with open(stem + ".txt", "w") as f:
            f.write("\n".join(label_lines) + "\n")
        print(f"Hard example saved: {stem}.jpg  ({len(label_lines)} boxes pre-labeled)")
        last_hard = time.time()

    # Enough animals in frame, and enough time since the last save? Snapshot it.
    if total >= CROWD_THRESHOLD and time.time() - last_snapshot >= SNAPSHOT_COOLDOWN:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # Summarise the mix in the filename, e.g. "2squirrel_1chipmunk".
        summary = "_".join(f"{n}{name}" for name, n in sorted(counts.items()))
        filename = f"snapshots/{summary}_{stamp}.jpg"
        cv2.imwrite(filename, annotated)      # save the frame with the boxes drawn on
        print(f"Snapshot saved: {filename}  ({total} animals!)")
        last_snapshot = time.time()

    # Smooth BOTH the confidence and the per-class counts over the last N frames,
    # so the on-screen numbers stay steady instead of flickering every frame.
    count_history.append(counts)              # this frame's per-class Counter (may be empty)
    if live_tracks:                           # only live tracks have a fresh score
        conf_history.append(sum(t["conf"] for _, t in live_tracks) / len(live_tracks))

    # Average each class's count across the window and round it. A class seen in
    # only a few of the last N frames averages below 0.5 and drops out, so brief
    # mis-detections (a one-frame "turkey") never reach the readout.
    smoothed_counts = {}
    for name in set().union(*count_history):
        avg = sum(c.get(name, 0) for c in count_history) / len(count_history)
        if round(avg) > 0:
            smoothed_counts[name] = round(avg)

    if smoothed_counts:
        smoothed_conf = sum(conf_history) / len(conf_history) if conf_history else 0.0
        # e.g. "chipmunk: 1   squirrel: 2   avg conf: 87%"
        breakdown = "   ".join(f"{name}: {n}" for name, n in sorted(smoothed_counts.items()))
        readout = f"{breakdown}   avg conf: {smoothed_conf:.0%}"
    else:
        readout = "searching..."

    # Measure the real end-to-end loop rate (capture + inference + draw) and show
    # it -- this is the true throughput, and a proxy for how live the feed is.
    now = time.perf_counter()
    fps_history.append(1.0 / max(now - prev_t, 1e-6))
    prev_t = now
    readout = f"{readout}   {sum(fps_history) / len(fps_history):.0f} fps"

    # Second line: cumulative distinct animals seen since startup, per class.
    run_counts = Counter(tm.seen.values())
    if run_counts:
        line2 = "run total: " + "   ".join(f"{n} {name}"
                                            for name, n in sorted(run_counts.items()))
    else:
        line2 = "run total: none yet"

    # Draw both lines top-left, BIG, on one translucent dark band so the green
    # text stays legible over grass, pavement, or bright sun -- anything.
    FONT, SCALE, THICK, PAD, GAP = cv2.FONT_HERSHEY_SIMPLEX, 1.1, 2, 12, 8
    lines = [readout, line2]
    sizes = [cv2.getTextSize(t, FONT, SCALE, THICK) for t in lines]
    glyph_h = max(s[0][1] for s in sizes)            # tallest glyph height
    base = max(s[1] for s in sizes)                  # tallest baseline drop
    text_w = max(s[0][0] for s in sizes)
    row_h = glyph_h + base
    bw = min(text_w + 2 * PAD, annotated.shape[1])   # don't run past the frame edge
    bh = len(lines) * row_h + (len(lines) - 1) * GAP + 2 * PAD
    band = annotated[0:bh, 0:bw]
    annotated[0:bh, 0:bw] = cv2.addWeighted(band, 0.4, np.zeros_like(band), 0.6, 0)
    y = PAD + glyph_h
    for t in lines:
        cv2.putText(annotated, t, (PAD, y), FONT, SCALE, (0, 255, 0), THICK, cv2.LINE_AA)
        y += row_h + GAP

    # Draw the snapshot-confirmation banner (bottom-left) while it's alive,
    # same translucent-band style as the readout so it reads over any scene.
    if time.time() < flash_until:
        (fw, fh), fbase = cv2.getTextSize(flash_msg, FONT, SCALE, THICK)
        bw2 = min(fw + 2 * PAD, annotated.shape[1])
        bh2 = fh + fbase + 2 * PAD
        y0 = annotated.shape[0] - bh2
        band2 = annotated[y0:, 0:bw2]
        annotated[y0:, 0:bw2] = cv2.addWeighted(band2, 0.4, np.zeros_like(band2), 0.6, 0)
        cv2.putText(annotated, flash_msg, (PAD, annotated.shape[0] - PAD - fbase),
                    FONT, SCALE, (0, 255, 0), THICK, cv2.LINE_AA)

    # Recording indicator (top-right, DISPLAY only -- it's drawn on 'annotated'
    # after the raw frame was already written, so it never bakes into the clip).
    if recording:
        cx = annotated.shape[1] - 60
        cv2.circle(annotated, (cx, 55), 18, (0, 0, 255), -1)
        cv2.putText(annotated, "REC", (cx - 190, 72), FONT, SCALE, (0, 0, 255),
                    THICK, cv2.LINE_AA)

    # Shrink the 4K frame for display (INTER_AREA is the high-quality choice
    # for downscaling). The full-res annotated frame above is what snapshots
    # save, so this costs no detail where it matters.
    if display_scale != 1.0:
        disp = cv2.resize(annotated, None, fx=display_scale, fy=display_scale,
                          interpolation=cv2.INTER_AREA)
    else:
        disp = annotated
    cv2.imshow("Live", disp)                  # show it in a window

    # waitKey(1) keeps the window responsive. Q quits; +/- zooms the view.
    # (The old 'c' driver dialog was a USB/DirectShow feature; the IP camera's
    # controls are in its web UI instead.)
    key = cv2.waitKey(1) & 0xFF
    if key == ord("q"):
        break
    if key in (ord("+"), ord("=")):           # '=' is the unshifted + key
        display_scale = min(display_scale + 0.05, 1.0)
    if key == ord("-"):
        display_scale = max(display_scale - 0.05, 0.20)
    if key == ord("s"):
        # TEMP: save the raw frame the model sees, so it can be analysed offline
        # at very low confidence to confirm whether the model detects the animal.
        os.makedirs("debug_frames", exist_ok=True)
        dbg = f"debug_frames/frame_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        cv2.imwrite(dbg, frame)
        print(f"Saved debug frame: {dbg}")
        flash_msg = f"Saved {dbg}"
        flash_until = time.time() + 2.0       # keep the banner up ~2s
    if key == ord("v"):
        # Toggle raw-frame video recording into debug_frames.
        if not recording:
            os.makedirs("debug_frames", exist_ok=True)
            clip = f"debug_frames/clip_{datetime.now():%Y%m%d_%H%M%S}.mp4"
            h, w = frame.shape[:2]
            video_writer = cv2.VideoWriter(clip, cv2.VideoWriter_fourcc(*"mp4v"),
                                           RECORD_FPS, (w, h))
            if video_writer.isOpened():
                recording = True
                flash_msg = f"Recording... (v to stop)  {clip}"
                print(f"Recording started: {clip}")
            else:                              # codec/path failure -- don't half-start
                video_writer = None
                flash_msg = "Recording failed to start (codec/path)."
                print("VideoWriter failed to open -- check codec/path.")
        else:
            video_writer.release()
            video_writer = None
            recording = False
            flash_msg = "Recording saved to debug_frames."
            print("Recording stopped and saved.")
        flash_until = time.time() + 2.5

# If a clip was still recording when the loop ended (Q or lost feed), close it
# cleanly so the .mp4 file isn't left truncated/corrupt.
if video_writer is not None:
    video_writer.release()

# Camera release and window teardown are handled by the atexit hooks registered
# near the top, so they run even if the loop above exits via an exception.
