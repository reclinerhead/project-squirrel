r"""Extract still frames from a video clip, ready to upload to Roboflow.

Roboflow rejects some video codecs -- our live.py 'v' recorder writes MPEG-4
Part 2 ('mp4v'), and phones write HEVC/H.265 ("too new") -- but it accepts JPG
images with no codec fuss at all. So rather than fight the video format, pull
frames out of the clip and upload those. Bonus: you decide how many frames, which
avoids the near-duplicate frames that hurt training.

Usage (PowerShell):
    python extract_frames.py "debug_frames\clip_20260703_180000.mp4"
    python extract_frames.py "C:\path\to\phoneclip.MOV" --fps 2 --out my_frames

--fps  frames per second to KEEP (default 3). Lower = fewer, more varied stills.
--out  output folder (default: <clipname>_frames beside the clip).
"""
import cv2, os, sys, argparse

ap = argparse.ArgumentParser()
ap.add_argument("video", help="path to the video clip")
ap.add_argument("--fps", type=float, default=3.0, help="frames per second to keep")
ap.add_argument("--out", default=None, help="output folder")
a = ap.parse_args()

cap = cv2.VideoCapture(a.video)
if not cap.isOpened():
    sys.exit(f"Could not open {a.video} -- check the path.")

src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
stride = max(1, round(src_fps / a.fps))          # keep every Nth frame
stem = os.path.splitext(os.path.basename(a.video))[0]
out = a.out or os.path.join(os.path.dirname(a.video) or ".", f"{stem}_frames")
os.makedirs(out, exist_ok=True)

i = saved = 0
while True:
    ok, frame = cap.read()
    if not ok:
        break
    if i % stride == 0:
        cv2.imwrite(os.path.join(out, f"{stem}_{i:05d}.jpg"), frame)
        saved += 1
    i += 1
cap.release()
print(f"Read {i} frames @ {src_fps:.0f}fps; kept every {stride}th -> "
      f"{saved} JPGs in {out}\\")
