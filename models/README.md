# models/

The **promoted-weights shelf** — the trained models the app actually runs, kept
deliberately separate from `runs/`, which is ultralytics' training scratch space
(every `train-N` it has ever produced, plots and all).

## Convention

| File | Role |
|---|---|
| `current.pt` | The model the app loads by default. A **copy** of whichever versioned model is deployed. |
| `merle-trainNN.pt` | An immutable, named copy of a training run's `best.pt`, kept for traceability back to its `yolo val` numbers. |

`live.py` and `prelabel.py` load `models/current.pt` by default, or whatever
`MERLE_MODEL` points at:

```powershell
# temporarily A/B a candidate without touching current.pt
# (-m from the repo root: MERLE_MODEL's default is CWD-relative, so this is
#  also what keeps "models/current.pt" resolving -- see TechnicalGuide.md)
$env:MERLE_MODEL = "models/merle-train17.pt"; python -m tools.live
```

## Promoting a new model

After a training round wins its `yolo val` comparison against the current model
(same valid split — see TechnicalGuide.md), promote it:

```powershell
# 1. keep a traceable, versioned copy
Copy-Item runs\detect\train-17\weights\best.pt models\merle-train17.pt
# 2. point the app at it
Copy-Item models\merle-train17.pt models\current.pt
```

That's it — no code edit. Both live.py and prelabel.py pick it up next run.

## Git

The `.pt` files are intentionally **not committed** (`*.pt` is gitignored) — they're
large (~20MB), change every round, and are reproducible from the dataset. Only
this README is tracked, so the convention lives in the repo even though the
weights don't. If a deployed model ever needs off-machine backup, use Git LFS or
a GitHub release asset rather than committing it here.

## Current deployment

`current.pt` = `merle-train18.pt` (YOLO26s, **2-class** squirrel/turkey).
Baseline: 0.864 mAP50 / 0.838 recall on the 0707 2-class valid split (squirrel
0.908, turkey 0.820). Rollback: `merle-train16.pt` (the last 3-class model,
0.936 mAP50 all-classes on 0705 — not same-ruler comparable across the class
change).
