# =============================================================================
# project-squirrel -- prelabel.py
#
# Backfill YOLO-format .txt label sidecars for images in hard_frames/ using our
# own trained model. This is free model-assisted labeling: the model's boxes on
# its own hard frames are usually placed right even when its confidence is low
# (low confidence means "unsure WHAT", not "unsure WHERE"), so annotation
# becomes review-and-nudge instead of draw-from-scratch. live.py now writes
# these sidecars at capture time; this script covers frames saved before that,
# and can re-label any folder of stills. Run in PowerShell:
#   python prelabel.py                 # label hard_frames/*.jpg missing a .txt
#   python prelabel.py --force         # re-label even if a .txt already exists
#   python prelabel.py --dir snapshots # label a different folder
# Also writes classes.txt (one class name per line, in class-id order) so
# downstream tools -- Roboflow upload, dedup.py -- know the id->name mapping.
# =============================================================================

import argparse
from pathlib import Path
from ultralytics import YOLO
from label_utils import dedupe_boxes

parser = argparse.ArgumentParser(description="Pre-label stills with our own model.")
parser.add_argument("--dir", default="hard_frames", help="folder of .jpg files")
parser.add_argument("--force", action="store_true", help="overwrite existing .txt files")
parser.add_argument("--conf", type=float, default=0.10,
                    help="detection floor; keep low, same rationale as live.py")
args = parser.parse_args()

folder = Path(args.dir)
if not folder.is_dir():
    raise SystemExit(f"No such folder: {folder}")

model = YOLO("C:/WEBDEV/project-squirrel/runs/detect/train-15/weights/best.pt")

# classes.txt: the id->name mapping this model (and these labels) use.
names = model.names
(folder / "classes.txt").write_text(
    "\n".join(names[i] for i in sorted(names)) + "\n")

labeled = skipped = 0
for jpg in sorted(folder.glob("*.jpg")):
    txt = jpg.with_suffix(".txt")
    if txt.exists() and not args.force:
        skipped += 1
        continue
    # imgsz=1920 matches live.py: distant animals in the 4K frame don't survive
    # the default 640 downscale.
    res = model(str(jpg), conf=args.conf, imgsz=1920, verbose=False)[0]
    # This model is NMS-free (end-to-end head), so its `iou` NMS knob is inert
    # and it occasionally emits two boxes for one animal. De-dupe here, most-
    # confident box first so the survivor is the one to trust.
    order = sorted(range(len(res.boxes)),
                   key=lambda i: float(res.boxes.conf[i]), reverse=True)
    xyxyn = res.boxes.xyxyn.tolist()
    keep = {order[k] for k in dedupe_boxes([xyxyn[i] for i in order])}
    lines, per_class = [], {}
    for i in sorted(keep):
        c = int(res.boxes.cls[i])
        x, y, w, h = res.boxes.xywhn[i].tolist()
        lines.append(f"{c} {x:.6f} {y:.6f} {w:.6f} {h:.6f}")
        per_class[names[c]] = per_class.get(names[c], 0) + 1
    txt.write_text("\n".join(lines) + "\n" if lines else "")
    summary = ", ".join(f"{n} {name}" for name, n in sorted(per_class.items())) or "nothing"
    print(f"{jpg.name}: {summary}")
    labeled += 1

print(f"\nDone: {labeled} labeled, {skipped} already had labels"
      f"{' (use --force to redo)' if skipped else ''}.")
