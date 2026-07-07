# =============================================================================
# project-squirrel -- make_2class.py
#
# Convert a 3-class Roboflow export (chipmunk/squirrel/turkey) into the 2-class
# world (squirrel/turkey) we actually train, WITHOUT touching Roboflow --
# upstream stays 3-class with every chipmunk annotation preserved for the
# rover era. (Roboflow's own "Modify Classes" version step is paid-plan-only;
# this is the free, verifiable equivalent.)
#
#   python make_2class.py training/0705            -> training/0705_2class
#   python make_2class.py training/0705 --out training/0706
#
# Rules (issue #12):
#   - chipmunk boxes are DROPPED (never renamed to squirrel -- tiny ambiguous
#     blobs would poison the squirrel class).
#   - chipmunk is class 0, so the survivors renumber: squirrel 1->0, turkey 2->1.
#   - a label file stripped to empty that HAD chipmunk boxes was a chipmunk-only
#     frame: the frame is dropped entirely (image too) -- its only animal would
#     otherwise be unlabeled, teaching the model that patch is background.
#   - an ALREADY-empty label file is a deliberate background frame: kept as-is.
#   - mixed frames keep their squirrels/turkeys; the chipmunk becomes background
#     on purpose (it is no longer a class we detect).
#
# Safety: writes a NEW folder (refuses to overwrite), verifies the class list
# before starting, and self-checks the output (squirrel/turkey instance counts
# unchanged, no class id > 1 anywhere, drop count matches) -- on any mismatch
# the output folder is deleted and the run fails loudly.
# =============================================================================

import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path

import yaml

EXPECTED_NAMES = ["chipmunk", "squirrel", "turkey"]
DROP = "chipmunk"
SPLITS = ["train", "valid", "test"]


def transform_lines(lines, drop_id, remap):
    """Strip boxes of `drop_id` and renumber the rest per `remap` (old->new).
    Returns (new_lines, dropped_count). Unknown class ids abort the whole run:
    that means the export isn't what we think it is, and guessing would
    silently corrupt labels."""
    out, dropped = [], 0
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        cid = int(parts[0])
        if cid == drop_id:
            dropped += 1
            continue
        if cid not in remap:
            raise ValueError(f"unknown class id {cid} in line: {line!r}")
        out.append(" ".join([str(remap[cid])] + parts[1:]))
    return out, dropped


def file_action(new_lines, dropped):
    """What to do with a label file after transform_lines: 'keep' the frame, or
    'drop' it (it was chipmunk-only -- keeping it would ship an unlabeled
    animal as background). Already-empty files stay: those are deliberate
    background frames."""
    if not new_lines and dropped > 0:
        return "drop"
    return "keep"


def count_instances(labels_dir):
    """Class-id -> box count across every label file in a directory."""
    counts = Counter()
    for f in Path(labels_dir).glob("*.txt"):
        for line in f.read_text().splitlines():
            if line.strip():
                counts[int(line.split()[0])] += 1
    return counts


def main():
    ap = argparse.ArgumentParser(description="3-class Roboflow export -> 2-class training set")
    ap.add_argument("export_dir", help="Roboflow export folder, e.g. training/0705")
    ap.add_argument("--out", help="output folder (default: <export_dir>_2class)")
    args = ap.parse_args()

    src = Path(args.export_dir)
    dst = Path(args.out) if args.out else src.with_name(src.name + "_2class")
    if not (src / "data.yaml").exists():
        sys.exit(f"{src}/data.yaml not found -- is this a Roboflow export?")
    if dst.exists():
        sys.exit(f"{dst} already exists -- refusing to overwrite. Delete it or pass --out.")

    data = yaml.safe_load((src / "data.yaml").read_text())
    names = list(data.get("names", []))
    if names != EXPECTED_NAMES:
        sys.exit(f"class list is {names}, expected {EXPECTED_NAMES} -- "
                 "refusing to guess a remap (already converted? different export?).")
    drop_id = names.index(DROP)
    kept_names = [n for n in names if n != DROP]
    remap = {names.index(n): kept_names.index(n) for n in kept_names}

    in_counts = Counter()
    out_expect = Counter()
    stats = {"files": 0, "boxes_dropped": 0, "frames_dropped": 0, "kept": 0}

    for split in SPLITS:
        ldir, idir = src / split / "labels", src / split / "images"
        if not ldir.is_dir():
            continue
        (dst / split / "labels").mkdir(parents=True)
        (dst / split / "images").mkdir(parents=True)
        for lf in sorted(ldir.glob("*.txt")):
            stats["files"] += 1
            lines = lf.read_text().splitlines()
            for line in lines:
                if line.strip():
                    in_counts[int(line.split()[0])] += 1
            new_lines, dropped = transform_lines(lines, drop_id, remap)
            stats["boxes_dropped"] += dropped
            if file_action(new_lines, dropped) == "drop":
                stats["frames_dropped"] += 1
                continue
            stats["kept"] += 1
            (dst / split / "labels" / lf.name).write_text(
                "\n".join(new_lines) + ("\n" if new_lines else ""))
            for cid in (int(l.split()[0]) for l in new_lines):
                out_expect[cid] += 1
            imgs = list(idir.glob(lf.stem + ".*"))
            if len(imgs) != 1:
                shutil.rmtree(dst)
                sys.exit(f"expected exactly one image for {lf.stem}, found {len(imgs)} -- aborting.")
            shutil.copy2(imgs[0], dst / split / "images" / imgs[0].name)

    # data.yaml for the 2-class world. Paths carried over; roboflow block kept
    # (with the original version) so the lineage back upstream isn't lost.
    out_yaml = {k: data[k] for k in ("train", "val", "test") if k in data}
    out_yaml["nc"] = len(kept_names)
    out_yaml["names"] = kept_names
    if "roboflow" in data:
        out_yaml["roboflow"] = data["roboflow"]
    (dst / "data.yaml").write_text(
        f"# 2-class training set derived from {src.name} by make_2class.py\n"
        f"# ({DROP} stripped; Roboflow upstream remains 3-class -- see issue #12)\n"
        + yaml.safe_dump(out_yaml, sort_keys=False))

    # --- Self-check: recount the OUTPUT from disk and compare ----------------
    problems = []
    actual = Counter()
    for split in SPLITS:
        if (dst / split / "labels").is_dir():
            actual.update(count_instances(dst / split / "labels"))
    for name in kept_names:
        want = in_counts[names.index(name)]
        got = actual[kept_names.index(name)]
        if want != got:
            problems.append(f"{name}: {want} boxes in, {got} out")
    if any(cid > len(kept_names) - 1 for cid in actual):
        problems.append(f"class id out of range in output: {dict(actual)}")
    if stats["boxes_dropped"] != in_counts[drop_id]:
        problems.append(f"dropped {stats['boxes_dropped']} chipmunk boxes, input had {in_counts[drop_id]}")
    if problems:
        shutil.rmtree(dst)
        sys.exit("VERIFICATION FAILED (output deleted):\n  " + "\n  ".join(problems))

    print(f"{src} -> {dst}")
    print(f"  label files:      {stats['files']}  (kept {stats['kept']}, "
          f"dropped {stats['frames_dropped']} chipmunk-only frames)")
    print(f"  chipmunk boxes:   {stats['boxes_dropped']} removed")
    for name in kept_names:
        print(f"  {name + ':':17} {actual[kept_names.index(name)]} boxes "
              f"(unchanged, now class {kept_names.index(name)})")
    print("  verification:     OK")


if __name__ == "__main__":
    main()
