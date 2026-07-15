# =============================================================================
# project-squirrel -- dedup.py
#
# Thin out near-duplicate hard frames BEFORE any human looks at them. Fifty
# frames of the same squirrel frozen at the seed pile are worth about one
# frame of training value; this keeps the one.
#
# Why not perceptual image hashing: the camera is fixed, so every frame's
# background is ~identical and a whole-image hash calls EVERYTHING a duplicate.
# What actually distinguishes frames is where the animals are -- so two frames
# are duplicates when their label sidecars agree: same class counts, and every
# box overlaps a same-class box in the kept frame at IoU >= threshold. Frames
# with no boxes at all also dedup down to one (same reasoning: identical empty
# driveway).
#
# Rare classes are precious: any frame containing a non-squirrel class
# (chipmunk, turkey, ...) is ALWAYS kept, never even considered as a dupe.
# At a 90/5/5 class mix, those frames are where the next accuracy gain lives.
#
# Nothing is deleted -- dupes (jpg + txt) move to hard_frames/dupes/ so any
# call can be reversed by moving files back. Run in PowerShell:
#   python dedup.py                 # dedup hard_frames/
#   python dedup.py --dry-run       # report only, move nothing
#   python dedup.py --iou 0.5       # stricter or looser overlap threshold
# Requires .txt sidecars: run prelabel.py first for frames that lack them
# (unlabeled frames are left alone, with a warning).
# =============================================================================

import argparse
import shutil
from pathlib import Path

parser = argparse.ArgumentParser(description="Move near-duplicate hard frames aside.")
parser.add_argument("--dir", default="hard_frames", help="folder of jpg+txt pairs")
parser.add_argument("--iou", type=float, default=0.6,
                    help="boxes overlapping at least this much count as 'same box'")
parser.add_argument("--dry-run", action="store_true", help="report only, move nothing")
args = parser.parse_args()

folder = Path(args.dir)
classes_file = folder / "classes.txt"
if not classes_file.exists():
    raise SystemExit(f"{classes_file} not found -- run prelabel.py first.")
class_names = classes_file.read_text().split()
SQUIRREL = class_names.index("squirrel")


def read_boxes(txt):
    """Parse a YOLO label file into [(class_id, x1, y1, x2, y2)], normalized."""
    boxes = []
    for line in txt.read_text().split("\n"):
        if not line.strip():
            continue
        c, x, y, w, h = line.split()
        c, x, y, w, h = int(c), float(x), float(y), float(w), float(h)
        boxes.append((c, x - w / 2, y - h / 2, x + w / 2, y + h / 2))
    return boxes


def iou(a, b):
    ix = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    iy = max(0.0, min(a[4], b[4]) - max(a[2], b[2]))
    inter = ix * iy
    area = lambda r: (r[3] - r[1]) * (r[4] - r[2])
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def same_scene(boxes_a, boxes_b, thresh):
    """True when every box in A pairs 1:1 with a same-class box in B at IoU>=thresh."""
    if len(boxes_a) != len(boxes_b):
        return False
    unmatched = list(boxes_b)
    for a in boxes_a:
        best, best_iou = None, thresh
        for b in unmatched:
            if b[0] == a[0] and iou(a, b) >= best_iou:
                best, best_iou = b, iou(a, b)
        if best is None:
            return False
        unmatched.remove(best)
    return True


kept, dupes, unlabeled, rare_kept = [], [], 0, 0
for jpg in sorted(folder.glob("*.jpg")):        # chronological: keep the earliest
    txt = jpg.with_suffix(".txt")
    if not txt.exists():
        print(f"{jpg.name}: no label sidecar -- left alone (run prelabel.py)")
        unlabeled += 1
        continue
    boxes = read_boxes(txt)
    if any(b[0] != SQUIRREL for b in boxes):    # chipmunk/turkey frame: always keep
        rare_kept += 1
        continue
    if any(same_scene(boxes, k, args.iou) for k in kept):
        dupes.append(jpg)
    else:
        kept.append(boxes)

print(f"\n{len(kept)} distinct squirrel scenes kept, {rare_kept} rare-class frames "
      f"kept unconditionally, {len(dupes)} duplicates, {unlabeled} unlabeled.")

if not dupes:
    raise SystemExit("Nothing to move.")
if args.dry_run:
    print("Dry run -- would move:")
    for jpg in dupes:
        print(f"  {jpg.name}")
    raise SystemExit()

dupe_dir = folder / "dupes"
dupe_dir.mkdir(exist_ok=True)
for jpg in dupes:
    shutil.move(str(jpg), dupe_dir / jpg.name)
    shutil.move(str(jpg.with_suffix(".txt")), dupe_dir / jpg.with_suffix(".txt").name)
print(f"Moved {len(dupes)} jpg+txt pairs to {dupe_dir}\\ (reversible -- just move back).")
