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
| Deploys   | `merle-autodeploy`| —                              | Deploy watcher (issue #95): polls origin/main, pulls + restarts the Merle units on merge |
| Pi-hole   | `pihole-FTL`      | 53, 67 (DHCP), 80, 443         | Household DNS + DHCP                                           |

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
systemctl status merle-autodeploy
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

Expected: 22 (ssh), 53 (pihole), 80/443 (pihole web), 1883 + 9001 (mosquitto),
3000 (mcc-dashboard). Anything else deserves a question. (Marlin, Willard,
and the frame archiver listen on nothing — they only talk to the broker.)

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

The `offline` on both status topics is an MQTT Last Will: if the process
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

`Restart=always` in the unit is load-bearing: `narrator.py` calls `connect()`,
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
MERLE_MQTT=localhost:1883 python narrator.py --persona personas/marlin.yaml
```

---

## The weather post (Willard)

Unit: `/etc/systemd/system/willard-weather.service`
Code: `weather.py` in the same checkout + venv as the narrator.

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
  optionally picks the model (defaults to the code default in `narrator.py`).

State: `weather_history.json` in the repo dir (that's `WorkingDirectory` +
the default relative path) — the 48h rolling window (5-minute resolution)
behind the dashboard's observed trail, persisted so a restart doesn't blank
the chart. Safe to delete if it ever goes weird; it refills within 48h.

A gateway or OpenWeather hiccup is a skipped report, never a dead service —
the next poll retries. Look for `[weather] fetch failed` lines in the
journal; the OWM URL is never logged because it carries the API key.

To run it by hand (stop the service first):

```
sudo systemctl stop willard-weather
cd ~/project-squirrel && source venv/bin/activate
MERLE_MQTT=localhost:1883 MERLE_ECOWITT=192.168.1.210 MERLE_OWM_KEY=<key> python weather.py
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
Code: `frame_archiver.py` in the same checkout + venv as the narrator.

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
MERLE_MQTT=localhost:1883 python frame_archiver.py
```

Quick health check — count today's filings:

```
ls ~/project-squirrel/frames | wc -l
```

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
Environment="MERLE_DEPLOY_UNITS=narrator-marlin willard-weather frame-archiver"
Environment=MERLE_DEPLOY_MCC=1

[Install]
WantedBy=multi-user.target
```

(The quotes on the `MERLE_DEPLOY_UNITS` line matter — the value has spaces.
No `User=`: root on purpose. Optional knobs: `MERLE_DEPLOY_INTERVAL_S`,
`MERLE_DEPLOY_USER`, `MERLE_REPO`.)

To pause auto-deploys (manual pulls and `deploy-mcc.sh` work as before):

```
sudo systemctl stop merle-autodeploy      # start again to resume
```

One tick by hand, no loop (desk-testing):

```
sudo MERLE_DEPLOY_UNITS="narrator-marlin willard-weather frame-archiver" \
     MERLE_DEPLOY_MCC=1 ~/project-squirrel/Servers/autodeploy.sh --once
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

**Reaching the daemon**: the dashboard proxies to the perception daemon on
bluejay (`MERLE_DAEMON_URL` in the unit). For that to work the daemon on
bluejay must bind the LAN, not loopback — `python -m uvicorn merle_daemon:app
--host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 3` — and Windows
Firewall on bluejay needs a one-time inbound allow on TCP 8000. The shutdown
timeout is this dashboard's fault, same disease as the fast-stop drop-in
above: it holds an MJPEG `/stream` connection around the clock, and uvicorn's
graceful shutdown waits forever on a stream that never completes (Ctrl+C on
the daemon did nothing until the flag). The daemon being down is the *normal*
state (it only runs during bluejay sessions); the dashboard shows "Merle is
asleep" and journals only the down/up transitions.

---

## Pi-hole

Web UI: http://192.168.1.64/admin

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

Static DNS records: `/etc/pihole/custom.list` — for anything with a static
IP that never speaks DHCP (Pearl herself, the camera, the gateway). Devices
that lease from Pi-hole register their hostnames automatically; Pearl doesn't,
which is why she's in that file.

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
    /etc/netplan/
```

Copy it off the box. Monthly is plenty. The Merle unit files carry the
OpenWeather API key, so treat the tarball accordingly.

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
