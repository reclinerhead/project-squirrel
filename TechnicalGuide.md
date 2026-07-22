# Merle Technical Guide

The living documentation of how project-squirrel (Merle) is built and why. This describes current state — chronology lives in git log and PR descriptions.

## Project context

Personal learning project — wildlife observation in the author's own driveway, for fun and skill-building. Not a surveillance product. The scene narrator's foundation (bus + one templated voice) shipped with issue #9; LLM narration via Ollama with issue #20; the weather post (service + dashboard panel) with issue #25; weather in the narrator's prompt (#26) and the narrator's own recent-lines memory (#28) followed; the second narrator (Jim on merle, mention-triggered follow-ups) landed with issue #80. Still ahead (future epics): real banter beats/casting, shared narrator memory, push notifications, unknown-species discovery loop, and a rover.

## System overview

```
 bluejay (Windows desktop: GPU)               │  pearl (192.168.1.64, always-on Ubuntu)
                                              │
Frigate restream ─rtsp:8554─▶ Merle daemon ───┼─MQTT driveway/events─▶ Mosquitto ◀──▶ narrator.py
              (YOLO26s + ByteTrack            │      (MERLE_MQTT)   (the event bus)     (Marlin)
               + FastAPI + SQLite)            │                          │                 │
                    │ localhost HTTP          │                          │                 │
                    ▼ (state/stream)          │                          │                 │
              MCC dashboard ◀─────────────────┼────────ws:9001───────────┘                 │
         (Next.js + TS + Tailwind)            │                                            │
                                              │                                            │
              Ollama (qwen2.5:14b) ◀──────────┼───────HTTP :11434 (MERLE_OLLAMA)───────────┘
                                              │
                                              │  weather.py ──(polls the Ecowitt GW2000B :80
                                              │     + OpenWeather forecast/garnish)──▶ Mosquitto
                                              │     retained weather/{current,forecast,history}

live.py remains the standalone desktop vision stack (hard_frames/ harvest,
snapshots/, debug_frames/); it shares perception.py with the daemon.

The Amcrest PoE cam's ONLY RTSP client is Frigate (NVR, Docker on pearl,
epic #243) -- 24/7 recording + Coral detection; the daemon, live.py, and
Earl's driveway audio all read its go2rtc restream (#247). See
docs/guide/frigate.md.
```

## Quick start — running the station

The station spans three machines, and one of them drives. **pearl** (`192.168.1.64`, always-on Ubuntu) hosts the broker (Mosquitto), the narrator (Marlin), the weather post (`weatherpost/weather.py`), the listener (Earl, `listener/earl.py`), and the production dashboard (MCC, `http://pearl:3000`) — always up, nothing to start. **bluejay** (the Windows desktop) runs only the perception daemon, the one process that needs its GPU and camera, from the repo root (PowerShell). **merle** (`192.168.1.103`, Pi 5) is the rover — the Waveshare UGV stack (`ugv`, `http://merle:5000`) with Jim the field narrator riding along; intermittently off or out of range by nature, and nothing else waits on it (runbook: `Servers/Merle.md`). Bluejay's standing Ollama install also serves the narrator's LLM calls (port 11434) — not a Merle process, nothing to start. Everything meets on the bus and tolerates the others being absent:

```powershell
# Perception daemon (needs MERLE_RTSP_PASS for the camera and MERLE_MQTT
# for the bus -- both set User-level on bluejay;
# set MERLE_SOURCE=synthetic for the camera-free world).
# --host 0.0.0.0 so pearl's production dashboard can reach it across the
# LAN (loopback-only was the one-box era); needs a one-time Windows
# Firewall inbound allow on TCP 8000 -- the firewall silently DROPS
# blocked packets, so the symptom is a hang, not a refusal.
# --timeout-graceful-shutdown: pearl's 24/7 dashboard always holds an MJPEG
# /stream connection, which never completes -- uvicorn's graceful shutdown
# would wait on it forever, so Ctrl+C hung until the flag bounded the wait
# (a second Ctrl+C is ignored too; the only other escape is killing the PID).
# --no-access-log: that dashboard also polls /state about twice a second, and
# each poll printed a line that buried the daemon's own event prints.
.\.venv\Scripts\python.exe -m uvicorn vision.merle_daemon:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 3 --no-access-log
```

`.\start-merle.ps1` runs exactly that in its own Windows Terminal tab (`-Synthetic` for the camera-free world). For dashboard *development* on bluejay, run `pnpm --dir mcc dev` by hand — it's deliberately not in the launcher, since the copy that matters is pearl's.

Rehearsal without live animals — republish archived events onto the bus with original (speed-scaled) timing:

```powershell
.\.venv\Scripts\python.exe -m tools.replay_events --last 100 --speed 4
```

**Everything Python is run as `-m package.module` from the repo root, never as a
file path** (issue #123). This is not style: Python puts the *script's* directory
on `sys.path`, so `python vision/merle_daemon.py` dies with
`ModuleNotFoundError: No module named 'bus'`, while `python -m
vision.merle_daemon` puts the repo root there and every import resolves. It is
also what keeps the CWD-relative defaults (`weather.db`, `merle.db`,
`models/current.pt`) landing exactly where they always have.

### Services on pearl (systemd)

The pearl-resident processes run as systemd units at `/etc/systemd/system/` — the narrator (Marlin), `willard-weather.service` (Willard), `frame-archiver` (the still-shot filing clerk), and `merle-autodeploy` (the deploy watcher, issue #95) — enabled so they survive reboots. Mosquitto is its own stock `mosquitto.service`. All Merle units follow one pattern: run as the login user, `WorkingDirectory=` the repo checkout (`/home/todd/project-squirrel`), `ExecStart=` the repo venv's python (`venv/bin/python` on pearl — never system python) invoking a **module, not a file** (`-m narration.narrator`, `-m weatherpost.weather` — see the `-m` note above; `frame_archiver.py` is still a root module and needs no package prefix), `Restart=on-failure`, and `Environment=` lines carrying the process's env (each service sets `MERLE_MQTT=localhost:1883` — the broker is local; the narrator adds `MERLE_OLLAMA`, the weather post adds `MERLE_OWM_KEY`). **Every unit needs `Environment=PYTHONUNBUFFERED=1`**: the scripts log with bare `print()`, and under systemd stdout is a pipe, so Python block-buffers — `journalctl` shows nothing for hours and a TERM on stop discards the buffer. `WorkingDirectory` doubles as data placement: the weather post's `weather_history.json` lands in the repo dir because its default path is relative.

Pearl also serves the production MCC as `mcc-dashboard.service` (port 3000,
`next start` + a `fast-stop.conf` drop-in — see [the MCC spoke](docs/guide/mcc.md) and
`Servers/Pearl.md` for the ops detail).

Deploying new code — **merging the PR is the deploy** (issue #95):
`merle-autodeploy` (`Servers/autodeploy.sh`, a root loop-service that demotes
git/pnpm to todd) polls origin/main every 60s on pearl *and* merle, pulls
`--ff-only`, restarts that box's Merle units, and rebuilds + restarts the MCC
only when the merge touched `mcc/`. A failed MCC build never restarts the
dashboard (old code keeps serving); a dirty checkout is skipped loudly, never
clobbered; quiet polls log nothing, so `journalctl -u merle-autodeploy` reads
as a deploy history. Manual fallback with the watcher stopped: pull + restart
for the Python services (they run from source); the MCC via
`Servers/deploy-mcc.sh` (pull → install → build → restart, failing loudly at
each step) — `next start` serves the compiled `.next/`, so pull + restart is
*not* a deploy for it. Runbooks: `Servers/Pearl.md` (The deploy watcher) and
`Servers/Merle.md`.

Everything else: `systemctl status <unit>` for health, `journalctl -u <unit> -f` for live logs, `systemctl cat <unit>` to see (and crib from) an existing unit when adding the next service — new unit file, then `sudo systemctl daemon-reload && sudo systemctl enable --now <unit>`.

## The spokes — where the rest of this guide lives

This file is the hub: project-wide context and conventions only. Everything component-specific lives in one spoke per component under `docs/guide/`.

**How to read this guide (Claude Code included): read this hub, then open only the spoke(s) for the components you are touching.** The living-documentation rule applies at spoke granularity — a PR that changes a component updates that component's spoke; a PR that changes a project-wide convention updates this hub. Deployment runbooks stay separate: `Servers/Pearl.md` and `Servers/Merle.md`.

| Spoke | Covers | Runs on | Related |
| --- | --- | --- | --- |
| [vision.md](docs/guide/vision.md) | Vision pipeline, training flywheel, daemon architecture: `vision/`, `tools/live.py` | bluejay | epic #1 |
| [bus.md](docs/guide/bus.md) | The event bus: `bus.py`, event still shots, `frame_archiver.py` | broker on pearl; everyone publishes | #90 |
| [narration.md](docs/guide/narration.md) | The narrators: `narration/` — personas, Editor, roles, journals | pearl (Marlin), merle (Jim) | #74 #80 #88 |
| [weather.md](docs/guide/weather.md) | The weather post + seasonal archive: `weatherpost/` | pearl | #25 #45 #51 #105 |
| [earl.md](docs/guide/earl.md) | The listener: `listener/` — Earl, gate, visits, clips, sightings | pearl | epic #133 |
| [aviary.md](docs/guide/aviary.md) | The Aviary — Earl's GUI + enrichment passes | pearl | epic #182 |
| [mcc.md](docs/guide/mcc.md) | The MCC dashboard proper: `mcc/` — panels, proxy, weather views, design language | bluejay (dev), pearl (prod) | epic #1, #113 |
| [music.md](docs/guide/music.md) | Music, catalog → app: `jukebox/`, `music/` | pearl | epic #115 |
| [homestead.md](docs/guide/homestead.md) | The front door (Caddy) + Homestead: `launchpad/` | pearl | epic #110 |
| [helm.md](docs/guide/helm.md) | The rover cockpit (stub — nothing merged yet) | merle + pearl | epic #127 |
| [frigate.md](docs/guide/frigate.md) | Frigate, the NVR: `frigate/` — 24/7 recording, detection, the go2rtc restream | pearl | epic #243 |

## Cross-cutting conventions

The rules that hold across every component, stated once. Each spoke assumes them.

- **`MERLE_*` env vars, required-no-default.** Anything a service cannot run correctly without (`MERLE_MQTT`, `MERLE_LATLON`, `MERLE_RTSP_PASS` where a camera is a source) raises at startup rather than defaulting — a misconfigured service must never look healthy while publishing into the void.
- **Retained topics carry state, event topics carry moments.** A retained message is "the current truth, answered instantly on subscribe" (presence lamps, journals, weather); an event is a thing that happened once. Never retain an event.
- **Timestamps split by namespace.** `driveway/*` and the daemon's SQLite use ISO-8601 local strings; the weather and audio namespaces (and their stores) use unix epoch seconds — a deliberate divergence, argued in [the weather spoke](docs/guide/weather.md).
- **Pure logic is unit-tested; I/O seams are desk-tested.** Ranking, gating, parsing, bucketing live in pure functions with tests beside them; camera/broker/LLM boundaries get desk procedures in the spokes and runbooks instead.
- **CI's test list is manual.** `.github/workflows/tests.yml` enumerates every pytest file by hand — a new `test_*.py` that isn't added there silently never runs. Check the list in every PR that adds a test file.
- **Packages are named for their role, not their contents** (`vision/`, `narration/`, `listener/`, `weatherpost/`, `jukebox/`) — see Repo layout below for why that's load-bearing.
- **The irreplaceable files** are `weather.db` (the seasonal archive), `music.db`'s `ratings` / `play_history`, and `earl.db`'s life list (first-heard dates). Everything else on every box is a cache or rebuilds from source; these do not. Back them up before touching them.
- **Enrichment is a pass, never a migration.** Every batch transformation (music metadata, art, bios, species profiles) is a worklist-driven, idempotent, per-entity function with a thin bulk CLI over it — re-runnable when new entities arrive, suggest-then-accept where an owner-edited row could be clobbered.

## Repo layout

**The Python is grouped into role packages** (issue #123). It was ~25 flat root scripts until then, and the guide used to defend that as *"deliberate for a single-machine project"* — a rationale that expired two machines ago. The repo is now three boxes plus two web apps, with an import boundary that must hold, so the shape follows the roles:

| Package | Holds | Runs on |
| --- | --- | --- |
| `vision/` | `perception.py` (the shared tracker/annotation brain), `merle_daemon.py` (FastAPI app), `frames.py` (frame sources), `storage.py` (SQLite), `label_utils.py` | bluejay |
| `narration/` | `narrator.py`, `personas/`, `character_bible.yaml` | pearl (Marlin), merle (Jim) |
| `weatherpost/` | `weather.py` (Willard), `weather_archive.py` (the seasonal archive) | pearl |
| `jukebox/` | `music_catalog.py` (the store), `music_index.py` (the read-only NAS indexer), `music_daemon.py` (playback: /stream + DLNA control + /queue, port 8090), `music_playlist.py` (the Phase 3 engine, pure), `music_analyze.py` (the offline analyzer), `music_import.py` (its importer), `music_bio.py` (artist bios, the one networked pass), `music_blurb.py` (album descriptions from the comment tag) | pearl (analyzer: bluejay) |
| `listener/` | `earl.py` (Earl, the audio daemon — BirdNET over the yard's mics), `gate.py` (the pure accept/reject core), `sightings.py` (the bird record: sightings + life list) | pearl |
| `tools/` | `replay_events.py`, `replay_fixture.py`, `record_fixture.py`, `extract_frames.py`, `dedup.py`, `prelabel.py`, `make_2class.py`, `live.py` | bluejay, by hand |
| *root* | `bus.py` (topics + publisher, the contract every bus process shares), `frame_archiver.py` (the still-shot archive on pearl) | everywhere |

**Packages are named for their role, not their contents, and that's load-bearing**: a package `X/` containing `X.py` breaks `import X` — the name resolves to the package, not the module — which is why it's `vision/` and not `perception/`, and `weatherpost/` and not `weather/`. `jukebox/` exists because `music/` is the Next app. Every file kept its own name through the move, so `git log --follow` still works. **All `__init__.py` files are empty on purpose** — a convenience re-export is exactly how merle's minimal venv dies (see below), and `test_import_boundary.py` fails if one appears.

`bus.py` stays at root because every box imports it and it belongs to none of them; `frame_archiver.py` stays because it's pearl-resident but needs no vision deps. The broker itself lives on pearl, and the repo-root `mosquitto.conf` is just a pointer to its config there.

**Tests stay at root** and import across (`from vision import perception`). `requirements.txt` pins the daemon/bus deps (plus `mutagen`, optional by design — the indexer's span parsing and hashing are pure stdlib, so a box without it still builds a correctly-identified catalog, just with null tags). **CI's Python job runs an explicit file list** (`.github/workflows/tests.yml`) — there is no `pytest.ini`/`testpaths` fallback, so a new `test_*.py` that isn't added to that line is silently never run. `test_bus.py` and `test_frame_archiver.py` proved that the hard way: they existed for months and never once ran, until #123 added them.

**`test_import_boundary.py` guards merle's venv.** The Pi runs `narrator-jim` and nothing else, on a deliberate two-package venv (`paho-mqtt` + `pyyaml`, never `requirements.txt` — the vision stack has no business on a Pi 5). Nothing enforced that; it held by luck. The test imports the narrator in a subprocess with `cv2`/`torch`/`ultralytics`/`fastapi`/`uvicorn`/`numpy` poisoned to raise, so a stray import fails in CI instead of taking merle down 60 seconds after a merge. It also asserts the poison bites and that the daemon *does* need the vision stack — otherwise the whole file could pass by doing nothing.
- `models/`: deployed-weights shelf — `current.pt` (what the app loads) plus versioned `merle-trainNN.pt` copies. Only its README is tracked; the `.pt` files are gitignored. See `models/README.md`.
- `mcc/`: Next.js 16 App Router, TypeScript, Tailwind 4, pnpm. Tests: Vitest (`pnpm test`), CI runs them on every PR (`.github/workflows/tests.yml`).
- `music/`: the music player UI (epic #115 / issues #116, #129) — same stack as `mcc/`, own lockfile and CI job (`web-music`), reading the real catalog and driving the playback daemon. See [the music spoke](docs/guide/music.md).
- `launchpad/`: Homestead, the house launchpad (issue #143) — deliberately static (no framework, no build, no lockfile, no CI job), served by Caddy from pearl's checkout. See [the homestead spoke](docs/guide/homestead.md).
- Not in git: datasets (`training/`), weights (`*.pt`, including `models/`), captures (`hard_frames/`, `snapshots/`, `debug_frames/`, `frames/` — the still-shot archive on pearl, and Earl's clips under `/srv/media-cache/earl`), SQLite stores (`*.db` + WAL companions — `merle.db` on bluejay, `weather.db`, `music.db`, and `earl.db` on pearl), `.venv/`. Gitignored means *state*, not *disposable*: `weather.db` cannot be regenerated (see [the weather spoke](docs/guide/weather.md)), and neither can `music.db`'s `ratings` / `play_history` or `earl.db`'s life list — the rest rebuilds, first-heard dates don't.
