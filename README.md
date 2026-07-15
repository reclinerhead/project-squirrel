# project-squirrel 🐿️

**Merle** is a personal learning project: a computer-vision wildlife station — and eventually a small rover — that watches the animals in my own driveway. Squirrels, mostly. Also turkeys, and whatever wanders through after dark. It's built for fun and to learn the craft (vision, model training, distributed systems, web dev, robotics), not as a surveillance product and not for watching people.

The name is Merle. The repo is `project-squirrel` because, let's be honest, it's 90% squirrels out there.

## What it is today

What started as a Python script drawing boxes in an OpenCV window has grown into a small distributed system spanning two machines:

- A **perception daemon** watches the driveway camera in real time — detecting, tracking, and counting every animal in frame — and publishes what it sees.
- An **MQTT event bus** carries those moments to whoever's listening.
- A **scene narrator** (Marlin, an exaggerated *Wild Kingdom* homage) subscribes to the bus and files LLM-written dispatches about the action, pacing itself like a real broadcaster — silence is most of the show.
- A **weather post** (Willard, booming folksy showmanship) polls OpenWeather, keeps a rolling history, and delivers an on-air conditions-and-outlook segment every half hour. The squirrels are his viewership.
- The **Merle Control Center** — a Next.js dashboard styled as a "Ranger Station, Night Watch" — pulls it all together: the live annotated feed, animal census, the narrators' Field Journal (with optional text-to-speech), an interactive weather chart, and station records going back days.

```
 GPU desktop (camera + inference)          │   home server (always on)
                                           │
 PoE camera ──RTSP──▶ Merle daemon ────────┼──events──▶ Mosquitto ◀──subscribe── Marlin (narrator)
                (YOLO + ByteTrack          │         (the MQTT bus) ◀──publish── weather post (Willard)
                 + FastAPI + SQLite)       │                ▲
                       ▲                   │                │ websocket
                       │ HTTP state/stream │                │
                       └───────────────────┼──── MCC dashboard (Next.js, on 24/7)
                                           │
 Ollama (local LLM) ◀──── narration + weather-segment calls ride back over the LAN
```

Everything runs on my own network. No cloud, no hosting bill — the LLM narration is a local Ollama model sharing the GPU with the detector (which is why the detector runs FP16: token generation is memory-bandwidth-hungry, and halving the detector's memory traffic keeps both fast under contention).

## The self-improving loop

The detector improves itself with a human (me) kept in the review seat:

1. **Watch** — the model + tracker run over the live camera feed, drawing boxes and counting animals.
2. **Notice what's hard** — when the model is *unsure* about an animal (confidence in a telltale "flicker band"), it saves that exact frame. Those uncertain moments are worth far more for training than another hundred easy, well-lit squirrels.
3. **Pre-label** — the model draws its own best-guess boxes on those saved frames, so labeling becomes *review-and-nudge* instead of *draw-from-scratch*.
4. **Thin the herd** — near-duplicate frames get set aside automatically (by comparing box geometry — the camera is fixed, so image hashing would flag everything), while frames with rare visitors are always kept.
5. **Review & retrain** — I approve/correct the boxes, retrain locally, and measure the new model against the old one on the *same* held-out images before trusting it.

Each lap makes the model a little sharper, which makes its next round of pre-labels a little better. That's the flywheel.

The current deployed model (`train-18`) is the first of a new **two-class lineage** (squirrel, turkey) — chipmunks proved too small and rare for the overhead camera, so their annotations are preserved in the dataset waiting for the rover era's ground-level view. Every training round is logged and charted on the dashboard, with the honest caveat printed right on the panel: metrics across different validation splits aren't on the same ruler.

## Engineering notes

A few things I'm proud of under the hood:

- **The daemon owns all state; the dashboard talks to it over HTTP only.** That seam let the system grow from one box to two with an env var change, and a future dedicated vision box (mini-PC or Jetson) is the same move.
- **Durable vs. live is a clean split**: SQLite is the archive, MQTT is the transport. A message dropped while the broker is down is never a lost record — just a moment nobody narrated. Retained topics carry *state* (weather, presence); event topics carry *moments*.
- **Tracking is tuned for real animal behavior.** Stationary feeding animals flicker out of detection for seconds at a time, so the tracker coasts lost tracks, stitches re-minted identities back together by box overlap, debounces arrival/departure events at the species level, and only counts a visitor after it's held a track for a couple of seconds. Before all that, one squirrel could be counted as five.
- **Perception is swappable.** A synthetic frame source (deterministic cartoon squirrels) powers tests, CI, and frontend work with no camera or GPU; the real RTSP source shares the exact same tracking and drawing code, so the two paths can't drift.
- **Pure logic gets tests; I/O gets desk-tested.** Python's tracker bookkeeping, storage queries, narrator prompt-building, and weather shaping all run camera-free in CI (pytest + Vitest, on every PR). Camera capture, MQTT plumbing, and live LLM calls are verified against the real hardware.
- **The narrators degrade gracefully.** No Ollama? Marlin falls back to template lines — the show never goes silent. No weather service? The narrator's prompt quietly loses its conditions paragraph rather than narrating yesterday's rain. Every service announces presence via MQTT Last Will, so the dashboard's "on the air" lamps flip within seconds of a crash with zero cleanup code.
- **No layout shift, ever.** The dashboard's panels reserve their full footprint before data arrives; empty states dim instead of disappearing; species rows never reorder as counts change. A one-second misidentification lights a gauge — it doesn't shove the page around.

## The pieces

| Piece | What it does |
|---|---|
| `vision/merle_daemon.py` + `vision/frames.py` | The perception daemon: FastAPI app running the model + tracker over the RTSP feed, serving live state, MJPEG stream, snapshots, and history from SQLite. |
| `vision/perception.py` | The shared tracking brain — coasting, identity stitching, census bookkeeping, box drawing — used by both the daemon and the standalone desktop stack. |
| `vision/storage.py` | SQLite archive: sightings, events, training runs. |
| `bus.py` | MQTT topics and publisher; the contract every bus process shares. Stays at the root because every box imports it and it belongs to none of them. |
| `narration/narrator.py` + `narration/personas/` | Marlin: event-driven LLM narration with persona files, a shared character bible, pacing gates, and template fallback. |
| `weatherpost/weather.py` | Willard: OpenWeather polling, 48-hour rolling history, retained bus topics, and the half-hourly LLM on-air segment. |
| `jukebox/` | The music catalog: our own store over the ~27k-track NAS library, and the read-only indexer that fills it. |
| `tools/live.py`, `tools/prelabel.py`, `tools/dedup.py` | The training flywheel: hard-frame harvesting, pre-labeling, near-duplicate thinning. |
| `tools/replay_events.py` | Rehearsal: republish archived events onto the bus with original timing — the narrator can't tell the difference. |
| `mcc/` | The **Merle Control Center** — Next.js + TypeScript + Tailwind dashboard, deployed 24/7 on the home server. |
| `music/` | The music player UI — a second Next app, peer to `mcc/`. |

Python runs as `-m package.module` from the repo root (`python -m vision.merle_daemon`), never as a file path — packages are named for their role because a package `X/` holding `X.py` would shadow the module.

## Roadmap

- **More voices** — additional narrator personas and banter between them; the producer/roster machinery is already shaped for a cast.
- **Notifications** — a ping to my phone when something worth seeing happens.
- **Unknown-species discovery** — catch animals the model doesn't recognize (the night shift: raccoons, opossums, deer, the neighbor's cat), identify them with help, and grow the model's vocabulary.
- **The rover** — Merle on wheels, at ground level with the animals. Its own camera, its own tuning regime, and someday solar-charging and a home base. (This is when the chipmunks come back.)

## Running it

The station spans two machines: the always-on server hosts the broker, narrator, weather post, and production dashboard as systemd services; the desktop runs the GPU-and-camera work (the daemon, plus Ollama for the LLM calls).

- Python side runs from a local `.venv` (ultralytics + OpenCV) against an NVIDIA GPU; daemon/bus deps are in `requirements.txt`.
- Secrets (camera password, OpenWeather key) and machine wiring (broker address, Ollama host) come from environment variables — nothing sensitive is committed.
- Datasets, model weights, and captured media are intentionally kept out of git.
- The dashboard: `pnpm --dir mcc dev` for development; tests with `pnpm --dir mcc test`.

For the full architecture, conventions, and the reasoning behind the non-obvious decisions, see [`TechnicalGuide.md`](TechnicalGuide.md).
