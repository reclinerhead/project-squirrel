# project-squirrel 🐿️

**Merle** is a personal learning project: a computer-vision system — and eventually a small rover — that watches the wildlife in my own driveway. Squirrels, mostly. Also chipmunks, turkeys, and whatever wanders through after dark. It's built for fun and to learn the craft (vision, model training, robotics, web dev), not as a surveillance product and not for watching people.

The name is Merle. The repo is `project-squirrel` because, let's be honest, it's 90% squirrels out there.

## What works today

A camera watches the driveway; a model finds and tracks every animal in frame in real time, labels each one, counts the crowd, and quietly collects its own future training data. The current model (`train-16`) sits around **0.94 mAP50** and is measurably better than the one before it — especially at spotting the fast, hard-to-catch chipmunks.

The interesting part isn't the detector, though — it's the loop that keeps making the detector better.

## The self-improving loop

Merle improves itself with a human (me) kept in the review seat:

1. **Watch** — `live.py` runs the model + tracker over the live camera feed, drawing boxes and counting animals.
2. **Notice what's hard** — when the model is *unsure* about an animal (confidence in a telltale "flicker band"), it saves that exact frame. Those uncertain moments are worth far more for training than another hundred easy, well-lit squirrels.
3. **Pre-label** — the model draws its own best-guess boxes on those saved frames, so labeling becomes *review-and-nudge* instead of *draw-from-scratch*.
4. **Thin the herd** — near-duplicate frames get set aside automatically, while rare visitors (chipmunks, turkeys) are always kept.
5. **Review & retrain** — I approve/correct the boxes, retrain locally, and measure the new model against the old one on the *same* held-out images before trusting it.

Each lap makes the model a little sharper, which makes its next round of pre-labels a little better. That's the flywheel.

## The pieces

| File | What it does |
|---|---|
| `live.py` | Live view: detection + tracking over the Amcrest RTSP feed. Draws tracked boxes, saves crowd snapshots, and banks "hard frames" with pre-drawn label sidecars. |
| `prelabel.py` | Runs the current model over a folder of stills to pre-draw labels — turns annotation into review-and-nudge. |
| `dedup.py` | Sets near-duplicate hard frames aside (compares box geometry, since the camera is fixed and image hashing would flag everything). |
| `label_utils.py` | Shared box-overlap / duplicate-suppression helpers. |
| `extract_frames.py` | Turns recorded clips into training stills. |
| `mcc/` | **Merle Control Center** — a Next.js dashboard, currently being built (see [#1](https://github.com/reclinerhead/project-squirrel/issues/1)). |

## The Merle Control Center (in progress)

Right now Merle lives in a Python window and terminal output. The MCC is a local web dashboard that becomes its proper face: the live annotated video, animal counts, controls, and — down the road — a review queue for identifying unfamiliar visitors.

The design keeps the "brain" (the Python vision service) and the "face" (the dashboard) as separate pieces that talk over a small local API, so the brain can eventually move onto dedicated hardware without the dashboard noticing. It runs entirely on my own network — nothing hosted, no cloud bill.

## Roadmap (someday)

- **Scene narrator** — an LLM playing a curious field researcher, filing dispatches about what it's watching.
- **Notifications** — a ping to my phone when something worth seeing happens.
- **Unknown-species discovery** — catch animals the model doesn't recognize (the night shift: raccoons, opossums, deer, the neighbor's cat), identify them with help, and grow the model's vocabulary.
- **The rover** — Merle on wheels, at ground level with the animals. Its own camera, its own challenges, and someday solar-charging and a home base.

## Running it

- Python side runs from a local `.venv` (ultralytics + OpenCV), against an NVIDIA GPU.
- The camera password is read from the `MERLE_RTSP_PASS` environment variable — see the note at the top of `live.py`.
- Datasets, model weights, and captured media are intentionally kept out of git.
- The dashboard lives in `mcc/` (Next.js + TypeScript + Tailwind, pnpm): `pnpm --dir mcc dev`.

For architecture, conventions, and the reasoning behind the non-obvious decisions, see [`TechnicalGuide.md`](TechnicalGuide.md).
