# Pearl

Intel NUC D54250WYK (Haswell i5-4250U, 16GB DDR3L, 128GB mSATA).
Ubuntu Server 26.04 LTS. Basement rack, UPS, hardwired to the 5G switch.
Static IP `192.168.1.64` on `eno1`. Gateway `192.168.1.254`.

Always-on infrastructure. BIOS is set to power on after power loss, so an
outage brings her back without anyone going downstairs.

*Canonical copy of this doc lives in the repo at `Servers/Pearl.md`; keep the
`~/PEARL.md` copy on pearl in sync when it changes.*

---

## What runs here

| Service   | Unit              | Ports                          | Purpose                                                        |
| --------- | ----------------- | ------------------------------ | -------------------------------------------------------------- |
| Mosquitto | `mosquitto`       | 1883 (MQTT), 9001 (WebSockets) | The Merle event bus                                            |
| Marlin    | `narrator-marlin` | —                              | Scene narrator, subscribes to events, publishes narration      |
| Willard   | `willard-weather` | —                              | Weather post: polls the Ecowitt gateway (+ OpenWeather forecast), publishes retained `weather/*` |
| Frames    | `frame-archiver`  | —                              | Still-shot archive (issue #90): files the daemon's event frames to disk for the Field Journal |
| MCC       | `mcc-dashboard`   | 3000 (HTTP)                    | The Merle dashboard, production build (`next start`)           |
| Music     | `music-daemon`    | 8090 (HTTP)                    | Playback daemon (issue #129): streams the catalog, drives the Denon over DLNA, writes `play_history` |
| Jukebox   | `music-app`       | 3001 (HTTP)                    | The music player UI (issue #131), production build (`next start`) — http://192.168.1.64:3001 |
| Earl      | `earl-listener`   | —                              | The ears (issue #172): BirdNET over the yard's mics, publishes `audio/*` — runs from its OWN venv (`~/earl-venv`), not the repo one |
| Sightings | `earl-sightings`  | —                              | The bird record (issue #172): subscribes `audio/events`, writes sightings + the life list to `earl.db` |
| Deploys   | `merle-autodeploy`| —                              | Deploy watcher (issue #95): polls origin/main, pulls + restarts the Merle units on merge |
| Caddy     | `caddy`           | 80 (HTTP)                      | The front door (issue #141): named URLs instead of ports — `pearl/mole` → Pi-hole, `mcc.lan` → :3000, `music.lan` → :3001 |
| Pi-hole   | `pihole-FTL`      | 53, 67 (DHCP), web on 127.0.0.1:8081 | Household DNS + DHCP; admin UI reached through Caddy at `pearl/mole` |

Not here: the perception daemon and camera (those live on bluejay,
`192.168.1.79` — they need the GPU), and Jim, the second narrator (he lives
on merle, `192.168.1.103` — see `Servers/Merle.md`).

---

## The one command to remember

```
systemctl status <unit>
```

Green dot = running. That's it. Everything below is elaboration.

```
systemctl status mosquitto
systemctl status narrator-marlin
systemctl status willard-weather
systemctl status frame-archiver
systemctl status mcc-dashboard
systemctl status music-daemon
systemctl status music-app
systemctl status merle-autodeploy
systemctl status caddy
systemctl status pihole-FTL
```

To see everything that's failed:

```
systemctl --failed
```

---

## Reading logs

Services no longer print to a terminal. Their output goes to the journal.

```
journalctl -u narrator-marlin -f          # watch live (Ctrl+C detaches, doesn't stop)
journalctl -u willard-weather -n 50       # last 50 lines
journalctl -u pihole-FTL --since "10 minutes ago"
journalctl -u mosquitto --since today | grep -i error
```

`-f` follows. `-u` selects the unit. `--since` takes plain English.

---

## Starting and stopping

```
sudo systemctl restart narrator-marlin
sudo systemctl stop willard-weather
sudo systemctl start willard-weather
```

`enable` / `disable` control whether it comes back after a reboot. All seven
services are enabled. To check:

```
systemctl is-enabled willard-weather
```

**Deploying new Merle code: merging the PR is the deploy** (issue #95).
`merle-autodeploy` polls origin/main every 60s and brings the box current on
its own — pull, restart the three Python services, and rebuild + restart the
MCC when the merge touched `mcc/`. Watch one land:

```
journalctl -u merle-autodeploy -f
```

The manual path still works whenever the watcher is stopped (or you're
impatient). Python services (narrator, weather, frame archiver) run from
source, so pull + restart is their whole deploy:

```
cd ~/project-squirrel && git pull
sudo systemctl restart narrator-marlin willard-weather frame-archiver
```

The **MCC is different** — see The MCC dashboard below. Pull + restart is
*not* a deploy for it; use the script:

```
~/project-squirrel/Servers/deploy-mcc.sh
```

---

## What's listening

```
sudo ss -tlnp
```

Expected: 22 (ssh), 53 (pihole), 80 (caddy), 1883 + 9001 (mosquitto),
3000 (mcc-dashboard), 3001 (music-app), 8090 (music-daemon), and
127.0.0.1:8081 (pihole web, loopback only — Caddy is the only way in).
Nothing on 443: Caddy runs plain HTTP on the LAN (`auto_https off`), and
TLS is the epic's Deferred section. Anything else deserves a question.
(Marlin, Willard, and the frame archiver listen on nothing — they only talk
to the broker.)

---

## MQTT: is the bus alive?

Subscribe to everything and watch:

```
mosquitto_sub -h localhost -t '#' -v
```

This blocks. Publish from another machine (or another SSH session):

```
mosquitto_pub -h 192.168.1.64 -t 'test/hello' -m 'ping'
```

If the message appears in the subscriber, the bus works across the LAN.

Topics:

- `driveway/events` — daemon → world, one JSON event each
- `driveway/frames/<frame_id>/{full,thumb}` — daemon → world (issue #90):
  each arrival/departure/crowd_snapshot's still shot, **raw JPEG bytes, not
  JSON** (don't `mosquitto_sub -v` the wildcard into a terminal you like).
  Non-retained, fire-and-forget; `frame-archiver` here subscribes and files
  them to disk
- `narration/lines` — narrator → world (both of them: Marlin here, Jim on
  merle). Both also *subscribe* to it (issues #80/#88): a line naming a
  colleague is that colleague's cue, and a follow-up never triggers a
  follow-up (the reply-to-a-reply guard)
- `narration/journal/<id>` — the field journal windows (issue #58,
  per-narrator since #80): each narrator's last 50 spoken lines, **retained**
  and republished whole on every new line, so a fresh dashboard tab gets the
  journal back on reload (the dashboard subscribes `narration/journal/+` and
  merges). Marlin's is backed by `narration_journal.json` in the repo dir
  (see The narrator below); Jim's by the same file on merle
- `narrators/<id>/status` — retained presence, `online` / `offline`
  (`marlin` here, `jim` from merle). Marlin also *subscribes* to it (issue
  #88): while Jim's lamp is on, Marlin leaves the announcements to the
  field and only follows up; when it goes dark he covers them himself
- `weather/current`, `weather/forecast`, `weather/history` — Willard's
  reports, all **retained**: weather is state, not a moment, so a late
  joiner (fresh dashboard tab) gets the latest report straight from the
  broker with no HTTP path or poll loop of its own
- `weather/report` — Willard's on-air segment (issue #45): the conditions +
  outlook narrated by the LLM on bluejay in his Willard Scott voice, every
  ~30 minutes, **retained** like the rest of the weather set. Only published
  when the unit carries `MERLE_OLLAMA`; without it the topic simply never
  exists
- `weather/status` — Willard's retained presence, same `online`/`offline`
  contract as the narrators but in its own namespace so the dashboard's
  narrator wildcard never picks up a weather reporter
- `music/status` — the playback daemon's retained presence (issue #129),
  same contract, its own namespace for the same reason
- `services/<name>/status` — the house-wide presence namespace (issue #147)
  for anything that's neither narrator, reporter, nor jukebox; the existing
  namespaces stay where they are. First tenant:
  `services/merle-daemon/status` (the perception daemon on bluejay — down is
  its *normal* state, it only runs during console sessions). The launchpad's
  tile lamps subscribe to whatever topics `launchpad/tiles.json` names

The `offline` on the status topics is an MQTT Last Will: if the process
dies without saying goodbye, the broker publishes it. The Will fires on any
socket death without a clean MQTT DISCONNECT — a crash, but also systemd's
SIGTERM — so `systemctl stop` flips the dashboard lamp within seconds with
no cleanup code anywhere.

Config: `/etc/mosquitto/conf.d/squirrel.conf`
(the main `mosquitto.conf` is stock — don't duplicate keys between them,
mosquitto treats duplicates as fatal, not last-write-wins)

---

## The Merle units, one pattern

`narrator-marlin`, `willard-weather`, and `frame-archiver` all follow the
same shape: run as the login user,
`WorkingDirectory=/home/todd/project-squirrel` (the repo checkout),
`ExecStart=` the repo venv's python (`venv/bin/python`, never system
python), and `Environment=` lines carrying the process's env.
(`merle-autodeploy` is the one deliberate exception — it runs as root
because its whole job is restarting the others; see The deploy watcher
below.)

Two lines are load-bearing in every Merle unit:

- `Environment=PYTHONUNBUFFERED=1` — the scripts log with bare `print()`,
  and under systemd stdout is a pipe, so Python block-buffers: without this,
  `journalctl` shows nothing for hours and a TERM on stop discards the buffer.
- `Environment=MERLE_MQTT=localhost:1883` — `bus.py` **requires** the var
  (no default; it raises at startup without it, so a misconfigured process
  can't look healthy while publishing into the void). On pearl the broker is
  local; on bluejay it must be `192.168.1.64:1883`.

Adding the next service: `systemctl cat willard-weather` to crib from, write
the new unit file, then `sudo systemctl daemon-reload && sudo systemctl
enable --now <unit>`.

---

## The narrator (Marlin)

Unit: `/etc/systemd/system/narrator-marlin.service`
Code: `/home/todd/project-squirrel/` (venv at `venv/`)
Persona: `personas/marlin.yaml`
World facts: `character_bible.yaml`

Role since issue #88: the studio. Jim (on merle) announces the events;
Marlin follows up on field reports that name him, and only takes over the
announcements himself while Jim's presence lamp is dark — expect his lines
to be reactions, not play-by-play, whenever both are on the air.
Extra env in the unit: `MERLE_OLLAMA=192.168.1.79:11434` (bluejay's GPU
serves the LLM; if it's unreachable the narrator silently degrades to
template lines — check the log's "narration tier" line when prose sounds
suspiciously Mad-Libs).

`Restart=always` in the unit is load-bearing: `narration/narrator.py` calls `connect()`,
not `connect_async()`, so it exits if the broker isn't up yet. On a cold boot
it can lose that race. Restarting after 5s turns a fatal race into a shrug.

State: `narration_journal.json` in the repo dir (`WorkingDirectory` + the
default relative path; `MERLE_NARRATION_JOURNAL` overrides) — the field
journal window (issue #58): the last 50 spoken lines behind the dashboard's
Field Journal, persisted so a restart doesn't blank the show's record and
published retained to `narration/journal/marlin` (per-narrator since #80).
Safe to delete if it ever goes weird; the journal simply starts fresh.

To run it by hand (stop the service first):

```
sudo systemctl stop narrator-marlin
cd ~/project-squirrel && source venv/bin/activate
MERLE_MQTT=localhost:1883 python -m narration.narrator --persona narration/personas/marlin.yaml
```

---

## The weather post (Willard)

Unit: `/etc/systemd/system/willard-weather.service`
Code: `weatherpost/weather.py` in the same checkout + venv as the narrator.

Reads the driveway's own weather station (issue #51) — the Ecowitt GW2000B
gateway at `192.168.1.210`, polled every 60 s over its local HTTP JSON API —
as the system of truth for everything measured, and keeps OpenWeather's
classic free APIs for the two jobs the station can't do: the 5-day/3-hour
forecast (hourly) and the sky garnish (condition text, sunrise/sunset, every
10 minutes; ~170 calls/day against free limits of 60/min and 1M/month).
Publishes the retained `weather/*` topics above, including the LLM-narrated
on-air segment when `MERLE_OLLAMA` is set. Consumers: the dashboard's
Weather Post panel + station view (the "willard with the weather" masthead
and the segment beneath the chart), and the narrator's prompt context.

Extra env in the unit:

- `MERLE_ECOWITT=192.168.1.210` — the gateway, **required, no default**
  (`host` or `host:port`). The station is the system of truth; a service
  that can't reach it has no job, so it fails at startup (the MERLE_MQTT
  philosophy). A gateway outage at runtime is a skipped poll retried every
  60 s, never a fallback to OpenWeather numbers.
- `MERLE_OWM_KEY` — the OpenWeather API key, **required, no default**. A
  keyless service would poll 401s while looking healthy, so it fails at
  startup instead.
- `MERLE_WEATHER_LOC` — optional; `zip`, `zip,CC`, or `lat,lon`
  (default `49001,US`, the station's home turf).
- `MERLE_OLLAMA=192.168.1.79:11434` — optional; turns on Willard's on-air
  segment (issue #45), narrated by bluejay's Ollama every ~30 minutes and
  published retained to `weather/report`. Same var, same semantics as the
  narrator unit; unset means no segment and everything else runs as before.
  An unreachable Ollama is a skipped broadcast retried on the next 10-minute
  pass — look for `[ollama]` lines in the journal. `MERLE_OLLAMA_MODEL`
  optionally picks the model (defaults to the code default in `narration/narrator.py`).
- `MERLE_WEATHER_DB=/home/todd/project-squirrel/weather.db` — optional; the
  seasonal archive (issue #105), the permanent 5-minute record behind the
  dashboard's deep-history charts. Default is `weather.db` under
  `WorkingDirectory`, which is that same path. **The `mcc-dashboard` unit must
  carry the same value** — the `/weather/history` route reads the file this
  service writes. Give it as an ABSOLUTE path in both units: the two have
  different `WorkingDirectory` values (this one the repo root, the MCC's the
  `mcc/` subdirectory), so the same *relative* value would name two different
  files and the route would serve an empty archive forever. Same coupling and
  same NAS-migration story as `MERLE_FRAMES_DIR` below: repoint the var in
  both units, nothing else changes. A path that can't be opened fails at
  startup, on purpose — better a dead service than a week of weather published
  but never recorded.

State: two files in the repo dir (`WorkingDirectory` + the default relative
paths), and they have **opposite** disaster stories:

- `weather_history.json` — the 48h rolling window (5-minute resolution) behind
  the dashboard's observed trail, persisted so a restart doesn't blank the
  chart. Safe to delete if it ever goes weird; it refills within 48h.
- `weather.db` — the seasonal archive (issue #105). **NOT safe to delete. It
  never refills.** This is the one irreplaceable file the whole stack owns:
  every other piece of state is a cache of something re-fetchable, but a
  deleted observation is a moment of driveway weather that no API sells back.
  Deleting it throws away every reading since the archive started, and the
  most it could ever rebuild is the 48h sitting in the JSON window. Back it up
  before anything clever; it is append-only, so a copy is only ever stale,
  never wrong. (`weather.db-wal` and `weather.db-shm` beside it are SQLite's
  WAL companions — copy the whole set, or `sqlite3 weather.db ".backup ..."`.)

A gateway or OpenWeather hiccup is a skipped report, never a dead service —
the next poll retries. Look for `[weather] fetch failed` lines in the
journal; the OWM URL is never logged because it carries the API key.

To run it by hand (stop the service first):

```
sudo systemctl stop willard-weather
cd ~/project-squirrel && source venv/bin/activate
MERLE_MQTT=localhost:1883 MERLE_ECOWITT=192.168.1.210 MERLE_OWM_KEY=<key> python -m weatherpost.weather
```

The gateway itself answers on the LAN with no auth — a quick sanity check
that the station is up and transmitting:

```
curl -s http://192.168.1.210/get_livedata_info | head -c 400
```

Quick health check from any machine on the LAN — the retained report comes
back instantly if Willard has ever filed one:

```
mosquitto_sub -h 192.168.1.64 -t 'weather/current' -C 1 -v
mosquitto_sub -h 192.168.1.64 -t 'weather/status' -C 1 -v
```

---

## The frame archive (frame-archiver)

Unit: `/etc/systemd/system/frame-archiver.service`
Code: `frame_archiver.py` in the same checkout + venv as the narrator. It
stays a ROOT module (it needs no vision deps and belongs to no package), so
unlike the others its unit needed no change in #123.

The still-shot filing clerk (issue #90): subscribes to
`driveway/frames/#` and writes each event's JPEGs (full + thumb) to disk,
where the MCC's `/frames` route serves them to the Field Journal. It lives
here — not behind the daemon's HTTP surface on bluejay — because daemon-down
is the dashboard's steady state, and journal thumbnails must survive
bluejay's nap the way the journal itself does (retained topics).

Env in the unit (plus the two standard lines every Merle unit carries):

- `MERLE_FRAMES_DIR` — optional; where the JPEGs land. Default is `frames/`
  under `WorkingDirectory` (i.e. `~/project-squirrel/frames/`, gitignored).
  **The `mcc-dashboard` unit must carry the same value** — the route reads
  the folder the archiver writes. When the USB NAS arrives, migration is
  repointing this var (in both units) at the mount; nothing else changes.
- `MERLE_FRAMES_KEEP_DAYS` — optional; retention window in days (default
  14). Files older than this are pruned hourly; the journal shows a quiet
  "faded" placeholder for anything pruned.

State: the `frames/` folder itself. Safe to delete files (or the folder)
any time — the journal degrades to placeholders for those entries; new
frames keep filing. Rough budget at stream scale: a busy day is a few MB,
so a 14-day window stays well under a GB.

A dropped frame (broker restart, missed message) is a moment nobody
archived — the event row in SQLite on bluejay still has the `frame_id` —
never a lost record. Look for `[frames]` lines in the journal.

To run it by hand (stop the service first):

```
sudo systemctl stop frame-archiver
cd ~/project-squirrel && source venv/bin/activate
MERLE_MQTT=localhost:1883 python -m frame_archiver
```

Quick health check — count today's filings:

```
ls ~/project-squirrel/frames | wc -l
```

---

## The listener (Earl) and the bird record (issue #172)

Units: `/etc/systemd/system/earl-listener.service`, `/etc/systemd/system/earl-sightings.service`
Code: `/home/todd/project-squirrel/listener/`
**Venv: `~/earl-venv` for Earl — NOT the repo venv.** BirdNET rides TensorFlow,
and TF has no wheels for the repo venv's Python; Earl gets a `uv`-managed
Python 3.11 of his own. The sightings unit needs only paho and runs from the
repo venv like `frame-archiver`.

One-time setup (repeatable — the onboarding rule):

```
pip3 install --user --break-system-packages uv     # once; lands in ~/.local/bin
~/.local/bin/uv venv --python 3.11 ~/earl-venv
~/.local/bin/uv pip install --python ~/earl-venv/bin/python birdnet paho-mqtt
sudo mkdir -p /srv/media-cache/earl && sudo chown todd:todd /srv/media-cache/earl
```

First run downloads the BirdNET acoustic (77 MB) + geo (46 MB) models to
`~/.cache`; the geo download has flaked mid-transfer once — Earl retries and
runs unmasked (loudly) if it can't load, so a bad first day is a noisy day,
not a dead one.

`earl-listener.service` (the Merle-unit pattern, two deviations: the venv
path, and `ExecStart` must use Earl's python):

```
[Unit]
Description=Earl -- the ears of the house (issue #172)
After=network-online.target mosquitto.service

[Service]
User=todd
WorkingDirectory=/home/todd/project-squirrel
ExecStart=/home/todd/earl-venv/bin/python -m listener.earl
Restart=on-failure
Environment=PYTHONUNBUFFERED=1
Environment=MERLE_MQTT=localhost:1883
Environment=MERLE_LATLON=42.29,-85.59
Environment=MERLE_RTSP_PASS=<the camera password>
Environment=MERLE_EARL_SOURCES=amcrest
Environment=MERLE_EARL_CLIPS=/srv/media-cache/earl

[Install]
WantedBy=multi-user.target
```

`earl-sightings.service` is the same skeleton with
`ExecStart=/home/todd/project-squirrel/venv/bin/python -m listener.sightings`,
`Environment=MERLE_EARL_DB=/home/todd/project-squirrel/earl.db`, and only
`MERLE_MQTT` besides. Then the usual:
`sudo systemctl daemon-reload && sudo systemctl enable --now earl-listener earl-sightings`.

Adding the rover as a second source (when pearl→merle keys exist:
`ssh-keygen` as todd, `ssh-copy-id todd@merle`, `BatchMode` must work):
set `Environment=MERLE_EARL_SOURCES=amcrest,rover` in the unit. A rover
that's off or unreachable is just a dark entry on `audio/sources` cycling
its restart backoff — that's the designed steady state, not a problem.

Watching Earl work:

```
journalctl -u earl-listener -f            # detections + source state changes
mosquitto_sub -h localhost -v -t 'audio/#'  # what the bus sees
sqlite3 is not installed here -- query earl.db via python3 (the music.db note)
```

**`earl.db` is irreplaceable** (the weather.db honor, third member: after
`weather.db` and `music.db`'s ratings): the life list's first-heard dates
cannot be re-derived. `/srv/media-cache/earl` clips are semi-precious — a
lifer's `first_clip` points there. Back up `earl.db` the same way as the
others (see Backups: sqlite3 .backup, never cp on a live WAL db).

---

## The deploy watcher (merle-autodeploy)

Unit: `/etc/systemd/system/merle-autodeploy.service`
Code: `Servers/autodeploy.sh` in the checkout it deploys.

Merging a PR is the deploy (issue #95): the watcher polls origin/main every
60s, and on a change pulls (`--ff-only`, never force), restarts the three
Python services, and — only when the merge touched `mcc/` — runs the
install → build → restart order from `deploy-mcc.sh`. A failed MCC build is
a loud journal line and **no restart**: the old build keeps serving until a
good merge lands. A dirty checkout is skipped loudly, never clobbered —
clean it and the next tick deploys.

It's a **loop service, not a systemd timer** (a timer's start/finish lines
every minute are the #35 journal-spam disease), so the journal reads as a
deploy history: quiet polls log nothing, deploys log what restarted.

It runs as **root** — that's the point: restarts without a sudo password —
but every git/pnpm step is demoted to `todd` via `setpriv`, so the checkout
and `.next/` never grow root-owned files that would break a manual deploy.
(`setpriv`, not `runuser`/`sudo`: those open a PAM session per call, which
was ~7k "session opened/closed" journal lines a day — the #35 disease.)

```ini
[Unit]
Description=Merle deploy watcher -- merges to main deploy themselves
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/home/todd/project-squirrel/Servers/autodeploy.sh
Restart=on-failure
RestartSec=10
Environment="MERLE_DEPLOY_UNITS=narrator-marlin willard-weather frame-archiver music-daemon"
Environment=MERLE_DEPLOY_MCC=1
Environment=MERLE_DEPLOY_MUSIC=1

[Install]
WantedBy=multi-user.target
```

(The quotes on the `MERLE_DEPLOY_UNITS` line matter — the value has spaces.
No `User=`: root on purpose. Optional knobs: `MERLE_DEPLOY_INTERVAL_S`,
`MERLE_DEPLOY_USER`, `MERLE_REPO`. Since #131 `music-daemon` rides the
restart list and `MERLE_DEPLOY_MUSIC=1` gates the music app's
install → build → restart the way `MERLE_DEPLOY_MCC` gates the MCC's —
on this box both land via the drop-in
`/etc/systemd/system/merle-autodeploy.service.d/music.conf` rather than
edits to the unit file itself.)

To pause auto-deploys (manual pulls and `deploy-mcc.sh` work as before):

```
sudo systemctl stop merle-autodeploy      # start again to resume
```

One tick by hand, no loop (desk-testing):

```
sudo MERLE_DEPLOY_UNITS="narrator-marlin willard-weather frame-archiver music-daemon" \
     MERLE_DEPLOY_MCC=1 MERLE_DEPLOY_MUSIC=1 \
     ~/project-squirrel/Servers/autodeploy.sh --once
```

The script self-updates: a merge that changes `autodeploy.sh` deploys like
anything else (the running loop exec's the new copy). Merle runs the same
unit with `MERLE_DEPLOY_UNITS=narrator-jim` and no MCC — see
`Servers/Merle.md`.

---

## The MCC dashboard

Unit: `/etc/systemd/system/mcc-dashboard.service`
Drop-in: `/etc/systemd/system/mcc-dashboard.service.d/fast-stop.conf`
Code: `mcc/` in the same checkout. Serves http://192.168.1.64:3000

The production Next.js build, served by `next start`. It's a stateless proxy
in front of the daemon on bluejay — the dashboard's state lives in the
browser tab and the daemon; nothing on pearl is worth waiting for.

**Deploying: normally automatic** — `merle-autodeploy` (issue #95, above)
rebuilds and restarts on any merge that touches `mcc/`. When deploying by
hand (watcher stopped), **`~/project-squirrel/Servers/deploy-mcc.sh` is
*the* way.** Pull + restart is not a deploy — `next start` serves the
compiled `.next/`, not source, so a restart without a build re-serves the
old code, and a build run after the restart rewrites `.next/` under the live
server (`Failed to load static file` errors). The script enforces the one
valid order — pull → install → build → restart — and fails loudly at each
step, so a broken build never restarts the service. The watcher mirrors the
same order.

**The fast-stop drop-in** sets `TimeoutStopSec=5`. Next's graceful shutdown
waits on connections that browser tabs hold open (HTTP keep-alive), and
systemd's default stop timeout is 90s — so `systemctl stop` used to sit the
full 90s before SIGKILL. The MCC is stateless, so waiting buys nothing;
stops now take ≤5s.

**Serving the journal's still shots** (issue #90): the unit carries
`MERLE_FRAMES_DIR` matching the `frame-archiver` unit's value (default
`/home/todd/project-squirrel/frames`) — the `/frames/[id]` route reads the
folder the archiver writes. Unset, the route just 404s and the journal
shows placeholders; nothing else cares.

**Serving the weather archive** (issue #105): the same pattern one file over.
The unit carries `MERLE_WEATHER_DB` matching the `willard-weather` unit's
value — the `/weather/history` route reads the SQLite file that service
writes. **Give it as an absolute path** (`/home/todd/project-squirrel/weather.db`):
this unit's `WorkingDirectory` is the `mcc/` subdirectory, not the repo root,
so a relative `weather.db` would resolve to `mcc/weather.db` — a file nothing
writes — and the route would serve an empty archive with no error anywhere.
The route has no default for exactly that reason. Unset, it quietly answers
`{"points": []}` and the deep-history charts draw blank; nothing else cares.
It opens the file **read-only, per request** — the MCC never writes to the
archive, and both units run as `todd`, so WAL reads need no ceremony.
The route reads it with **`node:sqlite`** (the stdlib module — no dependency,
no native build on pearl). It needs Node ≥ 23.4 to be unflagged; pearl runs
24.x, so it just works. If pearl's Node is ever downgraded below that, this
route is the thing that breaks.

**Reaching the daemon**: the dashboard proxies to the perception daemon on
bluejay (`MERLE_DAEMON_URL` in the unit). For that to work the daemon on
bluejay must bind the LAN, not loopback — `python -m uvicorn vision.merle_daemon:app
--host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 3` — and Windows
Firewall on bluejay needs a one-time inbound allow on TCP 8000. The shutdown
timeout is this dashboard's fault, same disease as the fast-stop drop-in
above: it holds an MJPEG `/stream` connection around the clock, and uvicorn's
graceful shutdown waits forever on a stream that never completes (Ctrl+C on
the daemon did nothing until the flag). The daemon being down is the *normal*
state (it only runs during bluejay sessions); the dashboard shows "Merle is
asleep" and journals only the down/up transitions.

---

## The music catalog (issue #120)

**Not a unit — a command you run by hand.** It's a one-time pass with an end,
plus occasional re-runs; a `while True` daemon for a job that finishes is the
wrong shape. No port, no bus topic, nothing to `systemctl status`.

The library mount is already here and is **read-only on purpose** — the audio
files are an immutable input and we never write tags back to them:

```
//hummingbird/music on /mnt/music type cifs (ro,relatime,vers=2.0,...)
```

Confirm it before a pass — an unmounted share walks to zero files, which looks
exactly like a successful pass over a library that vanished:

```
mountpoint /mnt/music && ls /mnt/music | head
```

Running it:

```
cd ~/project-squirrel
MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db python3 -m jukebox.music_index
```

| Flag | What it does |
| --- | --- |
| *(none)* | Full pass, honoring the hash cache |
| `--limit N` | Stop after N files — a smoke test |
| `--rehash` | Ignore the cache, re-hash everything |
| `--prune` | Also drop locations the pass didn't see |
| `--dry-run` | Walk and hash, write nothing |

**The first pass takes ~3 hours** (612.7 GB, 26,590 files, ~56 MB/s including
tag reads — it's wire-limited, not CPU-bound). **Every pass after that is
seconds**: the hash cache is keyed on `(path, size, mtime)` and re-reads
nothing that hasn't changed. Ctrl-C is a pause, not a loss — it commits what it
has and the next run resumes.

Env:

- `MERLE_MUSIC_DB` — the catalog. Default `music.db` under the process's
  `WorkingDirectory`. Give it as an **absolute path**; if MCC ever reads it,
  that unit must carry the same absolute value, for the same reason
  `MERLE_WEATHER_DB` does.
- `MERLE_MUSIC_ROOT` — the library. Default `/mnt/music`.

`--prune` **refuses to run below a 50% floor** and says so. That guard exists
because the indexer can't tell "the files moved" from "the share isn't
mounted" — both look like paths that stopped existing, and acting on the second
would wipe every location the catalog has. A prune only ever drops *locations*;
tracks, ratings, and history are never touched by it.

### Importing an analysis run (issue #136)

The audio-analysis backfill (BPM / ReplayGain / dynamic range) **runs on
bluejay** — beat tracking is CPU-heavy and needs librosa + ffmpeg, neither of
which pearl carries. It emits a JSONL keyed by content hash; pearl only
*imports* it, which is a one-shot manual step, not a unit:

```
# from bluejay, after the pass:  scp music_analysis.jsonl todd@pearl:/tmp/
# on pearl -- back up first, this writes the catalog:
python3 -c "import sqlite3; s=sqlite3.connect('music.db'); d=sqlite3.connect('/tmp/music.db.pre-import'); s.backup(d)"
MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db \
    python3 -m jukebox.music_import /tmp/music_analysis.jsonl
```

The import **UPDATEs** existing rows and is idempotent — re-running it writes
the same values and moves no row count, so a re-run after a better analysis
pass is safe. It never inserts: an id the catalog doesn't know is skipped, not
created. Measurements land in `bpm`/`replaygain_db`/`dynamic_range_db`; a track
that wouldn't decode lands in `needs_attention` with the ffmpeg reason. The
first run took coverage 0 → 92.3% (the gap is location-less ghost tracks, not a
failure). Nothing on pearl depends on the analyzer existing — once imported,
bluejay can be powered off for good.

**`music.db` needs backing up, and the reason is narrow.** The catalog itself
is disposable — it rebuilds from the NAS in ~3 hours. But `ratings` and
`play_history` **do not rebuild at all**: they're accumulated by living with the
system, and they're what Phase 3's engine and Phase 4's agent both read. Same
standing as `weather.db`. The config tarball above doesn't cover it:

```
python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
    ~/project-squirrel/music.db ~/music-backup-$(date +%F).db
```

Use the backup API, **not `cp`** — the file is WAL, so a live copy can be torn.
(There's no `sqlite3` CLI on this box; Python's stdlib has the same API and is
always here.)

**Since issue #129 the backup is no longer optional in spirit**: the playback
daemon writes `play_history` on every play, so the irreplaceable tables are
accumulating *now*. The indexer also skips the share's `#recycle` bin
(`EXCLUDED_DIRS`) and a one-time `--prune` reclaimed the 3,096 deleted-track
locations the first pass had cataloged.

---

## The music daemon (issue #129)

Unit: `/etc/systemd/system/music-daemon.service`
Code: `jukebox/music_daemon.py` in the same checkout + venv as the narrator.
Port: **8090** — the music GUI's `/api/player/*` proxy and the Denon's
stream fetches both land here.

Streams catalog tracks over HTTP (`/stream/{id}`, Range-capable) and drives
the **Denon AVR-X4000** over UPnP/DLNA (`/play`, `/pause`, `/stop`, `/seek`,
`/state`). The Denon is discovered by SSDP at startup — if it was off, the
first `/play` retries. Every play ends as one `play_history` row: `completed`
or `skipped`, judged by the watcher thread. Presence rides `music/status`
(retained; `offline` is the Last Will, so `systemctl stop` flips it within
seconds — verified).

Since #139 it also hosts the playlist engine's one endpoint: `POST /queue`
`{seed, n, exclude}` returns an ordered track list from
`jukebox/music_playlist.py` (pure scoring over the analysis axes — see
`TechnicalGuide.md`). It generates lists only: no playback started, no queue
state held, the transport verbs stay one-track-at-a-time. No new unit —
it's the same daemon.

It is also **the catalog's only writer** (issue #135): `POST /rate`
`{track_id, value}` records the thumbs, with `value: 0` clearing one. The
music app reads `music.db` directly but read-only, so every write — history
and ratings alike — arrives through this daemon. **That makes this unit's
uptime the thing standing between a click and the one table on pearl that
cannot be rebuilt**; `music.db` is to music what `weather.db` is to the
station, and it belongs in whatever backs that one up.

The venv needs `fastapi` + `uvicorn` (installed 2026-07-16; they're in
`requirements.txt`, they'd just never been needed on pearl before). The
browser output additionally needs **`ffmpeg`** (apt, installed 2026-07-17)
for the ALAC→FLAC repack — see the media-cache section below.

The unit, following the house pattern (crib from `willard-weather`):

```ini
[Unit]
Description=Merle music playback daemon (issue #129)
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
User=todd
WorkingDirectory=/home/todd/project-squirrel
Environment=PYTHONUNBUFFERED=1
Environment=MERLE_MQTT=localhost:1883
Environment=MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db
Environment=MERLE_MUSIC_STREAM_BASE=http://192.168.1.64:8090
ExecStart=/home/todd/project-squirrel/venv/bin/python -m uvicorn \
    jukebox.music_daemon:app --host 0.0.0.0 --port 8090 \
    --no-access-log --timeout-graceful-shutdown 3
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

The browser output's env (issue #149) rides a drop-in rather than an edit to
the unit above — the box's established pattern (autodeploy's `music.conf`):

```
# /etc/systemd/system/music-daemon.service.d/cache.conf
[Service]
Environment=MERLE_MUSIC_CACHE=/srv/media-cache/music
# Optional; the in-code default is 40:
# Environment=MERLE_MUSIC_CACHE_CAP_GB=40
```

**Unset `MERLE_MUSIC_CACHE` is the kill switch**: the daemon runs
Denon-only and the picker's "This browser" row reads unavailable. Set but
missing (LV didn't mount) kills the daemon loudly at startup, on purpose.

### The media-cache LV (issue #149)

`/srv/media-cache` is a 48 GiB ext4 LV (`ubuntu-vg/media-cache`, carved
2026-07-17 from the mSATA's unallocated half; ~10 GiB VG slack remains for
`lvextend`) mounted `noatime` via fstab, owned `todd:todd`. It is **shared
fast local storage for multimedia caching, not music's volume**: each tenant
gets a subdirectory and manages its own retention — `music/` (the FLAC
cache, LRU-capped at 40 GiB, swept by the daemon) today; Earl's bird-audio
buffers (#133) and short-term rover audio are the anticipated next tenants.
Re-budget the per-tenant caps when a second one lands. Everything on it is
derived and disposable — it needs no backup, ever.

### The art store (issue #153)

`/srv/media-cache/music-art/` — the media-cache LV's second tenant (sibling
of the FLAC cache, never inside it: the cache sweep eats unrecognized
files). Content-addressed originals plus pre-generated WebP sizes,
~300–500 MB, all rebuildable. The music app serves it via `/api/art` and
needs `MERLE_MUSIC_ART=/srv/media-cache/music-art` in a `music-app` drop-in
(`/etc/systemd/system/music-app.service.d/art.conf`, same pattern as the
daemon's `cache.conf`). The venv needs **pillow + mutagen** (both installed
2026-07-17 — mutagen had only ever lived in system python3, where the
indexer runs; the art pass runs from the venv like the daemon does).

The pass (worklist-driven — a re-run after ingesting new albums touches
only those albums; full coverage is a seconds-long no-op):

```
cd ~/project-squirrel && \
MERLE_MUSIC_ART=/srv/media-cache/music-art \
MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db \
    venv/bin/python -m jukebox.music_art
```

Owner-picked art (`source='owner'` rows in `album_art`/`artist_art`)
survives every re-run by construction — the upsert refuses to touch it.

### Catalog normalization (issues #163 genre, #152 artist)

`genre_rules.yaml` (in the repo — it IS the ruleset; edits are commits) drives
one pass that fills `tracks.genre_norm` (22-tag canonical vocabulary) and
`tracks.artist_norm` (case-collapsed artist identity). Idempotent and
diff-writing: re-run after any rules edit or ingestion; `--dry-run` previews
with zero writes. The venv needs **pyyaml** (present since 2026-07-18).

```
cd ~/project-squirrel && MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db \
    venv/bin/python -m jukebox.music_genre [--dry-run]
```

After the FIRST artist-normalizing run on a populated catalog, run the art
pass (above) once more: art keys are minted from the canonical identity now,
so albums that were filed under a minority casing re-enter the art worklist
and re-extract. Self-healing, minutes. The pass ends with an UNMAPPED report
— a new genre tag from a future ingestion shows up there, gets one rules
line, and a re-run closes the vocabulary again.

### Codec backfill (issue #149, one-time)

`format` can't tell ALAC from purchase-AAC inside `.m4a`, and the browser
policy needs to know. New files get their codec at index time; rows indexed
before the column existed get it from a header-only pass (minutes over SMB):

```
cd ~/project-squirrel && MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db \
    venv/bin/python -m jukebox.music_index --codecs
```

Resumable and re-runnable for free — the worklist is whatever is still NULL.

**Both uvicorn flags are load-bearing.** `--no-access-log`: the music GUI
polls `/state` (issue #125's flood, preempted). `--timeout-graceful-shutdown
3`: the **Denon holds `/stream` open for the whole song**, and without the
bound a SIGTERM leaves a zombie draining that connection while its
replacement binds the port — observed live during #129's verification, the
same trap the perception daemon's flag fixes on bluejay.

`MERLE_MUSIC_STREAM_BASE` is the daemon's own LAN-visible URL: the Denon
*fetches* audio from it, so `localhost` here means silence in the living
room. Fail-loudly config, no default.

Hardware notes, so nobody re-diagnoses the AVR: it answers `GetProtocolInfo`
with HTTP 500 (capability is a table in the code, not a negotiation), and it
refuses AVTransport `Seek` on external streams (the GUI restarts the track
instead; the browser output is where real scrubbing lives — Range against
the cached FLAC). It plays ALAC natively and untranscoded — that's the whole
point of that output, and the browser path never touches it: `?output=`
defaults to `denon`, raw bytes, byte-identical to 2a.

To run it by hand (stop the service first):

```
cd ~/project-squirrel && \
MERLE_MQTT=localhost:1883 \
MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db \
MERLE_MUSIC_STREAM_BASE=http://192.168.1.64:8090 \
MERLE_MUSIC_CACHE=/srv/media-cache/music \
venv/bin/python -m uvicorn jukebox.music_daemon:app --host 0.0.0.0 \
    --port 8090 --no-access-log --timeout-graceful-shutdown 3
```

---

## The music app (issue #131)

Unit: `/etc/systemd/system/music-app.service`
Drop-in: `/etc/systemd/system/music-app.service.d/fast-stop.conf`
Code: `music/` in the same checkout. Serves http://192.168.1.64:3001

The production Next.js build of the player UI, served by `next start` —
`mcc-dashboard`'s pattern one directory over, including the **fast-stop
drop-in** (`TimeoutStopSec=5`): browser tabs hold keep-alive connections and
Next's graceful shutdown would otherwise sit out systemd's 90s default.

**Deploying: normally automatic** — `merle-autodeploy` rebuilds + restarts
on any merge touching `music/` (`MERLE_DEPLOY_MUSIC=1`). By hand,
**`~/project-squirrel/Servers/deploy-music.sh` is *the* way**, for
`deploy-mcc.sh`'s reason verbatim: `next start` serves the compiled
`.next/`, so pull + restart re-serves old code, and a build after the
restart rewrites `.next/` under the live server. Pull → install → build →
restart, failing loudly, or nothing.

```ini
[Unit]
Description=Merle music app (Next.js, issue #131)
After=network-online.target music-daemon.service
Wants=network-online.target

[Service]
User=todd
WorkingDirectory=/home/todd/project-squirrel/music
ExecStart=/home/todd/project-squirrel/music/node_modules/.bin/next start -p 3001
Environment=NODE_ENV=production
Environment=MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db
Environment=MERLE_MUSIC_DAEMON=http://127.0.0.1:8090
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Both env vars carry the house's absolute-path lesson.** The unit's
`WorkingDirectory` is the `music/` subdirectory, so a relative
`MERLE_MUSIC_DB` would name `music/music.db` — a file nothing writes — and
the app would serve an empty library with no error anywhere (the
`MERLE_WEATHER_DB` trap, same words). It reads the **live catalog** — the
same file `music-daemon` writes — so the recently-played shelf updates in
real time; read-only per request over WAL, both units run as `todd`, no
ceremony needed. `MERLE_MUSIC_DAEMON` is loopback because the daemon is one
unit over; the browser never sees this address (the app proxies
`/api/player/*` server-side).

---

## The front door (Caddy)

Unit: `caddy` (the stock apt unit — it already runs `/usr/bin/caddy run
--config /etc/caddy/Caddyfile` and enables on install, so unlike the Merle
units there was nothing to write).
Config: `/etc/caddy/Caddyfile` — **canonical copy in the repo at
`Servers/Caddyfile`**, same arrangement as this document itself. Changing it
means editing the repo copy, then:

```
sudo cp ~/project-squirrel/Servers/Caddyfile /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

(`reload` is graceful — Caddy re-reads config without dropping connections.
The autodeploy watcher does *not* apply Caddyfile changes; the copy is a
deliberate manual step, because a bad reverse-proxy config landing itself on
merge could take every web surface down at once.)

**The `caddy` user is in the `todd` group, and that's load-bearing**: Ubuntu
creates home directories `750`, so without the group the launchpad
`file_server` can't traverse `/home/todd` and every `/home/` request answers
403 (found the hard way at first deploy — the fix was
`sudo usermod -aG todd caddy` + a restart; a *restart*, not a reload, because
supplementary groups are only picked up by a new process). On a rebuilt
pearl, re-run that usermod before expecting the porch to serve.

One front door on port 80 (issue #141, epic #110 Phase 1): named URLs
instead of memorized ports, and the single choke point where TLS/auth would
be added later without touching any app. Plain HTTP on the LAN
(`auto_https off`), so **nothing listens on 443** — that's expected, not a
gap.

What routes where:

| You type | Caddy does |
| --- | --- |
| `pearl/` | 302 → `/home/` — the Homestead launchpad, the bookmarkable front door (issue #143) |
| `pearl/home/` | serves `~/project-squirrel/launchpad/` as static files, straight from the checkout |
| `pearl/mole` (or `.64/mole`) | proxies Pi-hole's admin on loopback:8081 — the UI lives at `/mole` natively (`webserver.paths.webhome = "/mole/"` in `pihole.toml`, issue #143), so nothing rewrites paths; `/api` rides along because the v6 admin UI calls it and its path ignores webhome |
| `mcc/` or `mcc.lan` | proxies the MCC dashboard (:3000) |
| `music/` or `music.lan` | proxies the music app (:3001) |

**Homestead deploys by pull alone** — it's static files with no build step,
so `merle-autodeploy`'s ordinary `git pull` *is* its deploy; no gate, no
restart, nothing in the watcher's log. That's correct, not broken. Adding a
tile is one entry in `launchpad/tiles.json` (merge → pull → refresh); the
page fetches it with `cache: no-store`, so a refresh is enough.

The short names work because the house's DHCP hands out `lan` as the search
domain, so a desktop typing `mcc/` really asks for `mcc.lan` — but the Host
header still says what was typed, which is why the Caddyfile lists both
spellings for every site. Phones don't reliably apply the suffix; use the
full `.lan` names there (the launchpad's tiles do).

The names themselves live in Pi-hole (Settings → Local DNS Records):
`mcc.lan` and `music.lan`, both → `192.168.1.64`. `pearl.lan` already
resolved before any of this and needed nothing.

**Not proxied on purpose**: the broker's WebSocket (:9001) — browsers speak
MQTT to it directly and the MCC's `NEXT_PUBLIC_MERLE_MQTT_WS` names it
absolutely, and since #147 the launchpad's status lamps do the same via the
`bus` key in `tiles.json`. Phase 4 recorded the decision: no WS proxy until
something needs it (TLS would be that something); the music daemon (:8090) —
only the Denon and the music app's server side talk to it.

Health check from anything on the LAN:

```
curl -sI http://mcc.lan/ | head -1        # HTTP/1.1 200 OK
curl -sI http://pearl/mole/ | head -1     # 200 or a login redirect — either means alive
```

---

## Pi-hole

Web UI: http://pearl/mole (through Caddy; the web server itself sits on
loopback:8081 since issue #141 and is unreachable directly from the LAN.
The UI's home path is `/mole/` — `webserver.paths.webhome` in `pihole.toml`,
renamed from the stock `/admin/` in issue #143 to match the Mole tile)

**The webhome rename needs a symlink, and it's easy to forget**: FTL serves
the UI's files from `webroot + webhome`, so `/mole/` means it looks in
`/var/www/html/mole/` — a directory Pi-hole never installs. Without the link
the login page 404s while the `/mole/` → `/mole/login` redirect still works,
which looks like a Caddy bug and isn't. The fix (survives `pihole -up`,
which updates the real `admin/` dir the link points at):

```
sudo ln -s /var/www/html/admin /var/www/html/mole
```

Pearl is DNS and DHCP for the whole house. The AT&T gateway (BGW,
`192.168.1.254`) won't let you set DHCP DNS servers, so its DHCP is disabled
and Pi-hole's is on. IPv6 is off at the gateway — otherwise router
advertisements hand out the gateway's own v6 DNS and clients sail straight
past Pi-hole.

```
pihole status
pihole -g                    # rebuild gravity (after changing lists)
pihole -t                    # tail the DNS log live
pihole disable 5m            # pause blocking, auto-resume
pihole setpassword
```

Static DNS records: Settings → Local DNS Records in the web UI (the file
behind it is `/etc/pihole/hosts/custom.list` — v6 moved it into `hosts/`) —
for anything with a static IP that never speaks DHCP (Pearl herself, the
camera, the gateway), plus the front door's named URLs (`mcc.lan`,
`music.lan` — see The front door above). Devices that lease from Pi-hole
register their hostnames automatically; Pearl doesn't, which is why she's in
that file.

Static DHCP leases: in the web UI, Settings → DHCP.
bluejay `.79`, merle `.103`.

Something broke? Query Log → find the red entry → Allow. Thirty seconds.
A false positive is a log entry, not a crisis.

---

## Reboots

```
sudo reboot
```

Everything comes back on its own. Verified.

Unattended security upgrades are on, with automatic reboot at 02:00.
Config: `/etc/apt/apt.conf.d/50unattended-upgrades`

Careful: while Pearl is down, the house has no DNS and no DHCP.
A reboot is thirty seconds nobody notices. A shutdown is not.

Note: "power on after power failure" fires when AC returns, not when you
halt the machine. `shutdown -h now` leaves her off until you press the button.

---

## Backups

The OS is replaceable. The config isn't.

```
sudo tar czf ~/pearl-config-$(date +%F).tar.gz \
    /etc/pihole/ \
    /etc/mosquitto/conf.d/ \
    /etc/systemd/system/narrator-marlin.service \
    /etc/systemd/system/willard-weather.service \
    /etc/systemd/system/mcc-dashboard.service \
    /etc/systemd/system/mcc-dashboard.service.d/ \
    /etc/caddy/ \
    /etc/netplan/
```

Copy it off the box. Monthly is plenty. The Merle unit files carry the
OpenWeather API key, so treat the tarball accordingly.

### The databases — and why `cp` is the wrong tool

The tarball above is **config only**. It does not cover `weather.db`, which
this runbook elsewhere calls *the one irreplaceable file the whole stack owns*
— it is append-only, it never refills, and no API sells the readings back.
`music.db`'s catalog rebuilds from the NAS in ~3 hours, but its `ratings` and
`play_history` do not rebuild at all. Those are the two files worth a copy.

**`cp weather.db` gives you an empty file. This is not a warning, it is what
happens.** Measured on 2026-07-15 with 2.9 days of history in the archive:

```
weather.db          4096 bytes     <-- the whole file
weather.db-wal    997072 bytes     <-- all 721 observations are in HERE
```

Willard holds its connection open around the clock, so the WAL has **never
checkpointed** back into the main file. `cp weather.db ~/` produces 4 KB in
which the `observations` table does not even exist (`sqlite3.OperationalError:
no such table: observations`) — and it fails **silently**, so you find out on
the day you need it.

Copying all three (`weather.db`, `-wal`, `-shm`) would work but is racy against
a live writer. Use the backup API, which reads *through* the WAL and writes one
consolidated file:

```
python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" \
    ~/project-squirrel/weather.db ~/weather-backup-$(date +%F).db
```

Same for `music.db`. Verify rather than trust — a backup nobody has read is a
hope:

```
python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print('rows:', c.execute('SELECT COUNT(*), MIN(ts), MAX(ts) FROM observations').fetchone())
" ~/weather-backup-$(date +%F).db
```

(There is no `sqlite3` CLI on this box; Python's stdlib has the same API and is
always here.)

**Restoring is almost never the right move.** Willard appends continuously, so
copying an old snapshot back discards every observation recorded since it was
taken. A restore is for a lost or corrupt file, not for "I changed something
and want to be safe" — for that, take the backup and expect never to open it.

---

## The rest of the system

- bluejay `192.168.1.79` — desktop, RTX 5070 Ti. Perception daemon
  (`:8000`, LAN-bound so pearl's dashboard can reach it), camera,
  Ollama (`:11434`), MCC dev server. Needs `MERLE_MQTT=192.168.1.64:1883`
  in its environment.
- merle `192.168.1.103` — Raspberry Pi 5: Jim, the second narrator
  (`narrator-jim`, needs the bus here and Ollama on bluejay — see
  `Servers/Merle.md`). The rover's future brain; the unit rides along.
- pearl `192.168.1.64` — you are here.
