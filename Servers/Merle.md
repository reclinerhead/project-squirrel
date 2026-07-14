# Merle

Raspberry Pi 5 (8GB), Raspberry Pi OS (64-bit, Bookworm).
Static DHCP lease `192.168.1.103` (reserved in Pi-hole on pearl).
Hostname `merle`. SSH in as the login user (`todd`).

The rover's future brain. Until the rover hardware lands, merle's job is
**Jim** — the second narrator (issue #80), the field correspondent to
Marlin's studio host. The narration angle and the deployment target converge
on purpose: when the Pi rides the rover, Jim is already installed, already
the guy out in the field among the beasts — the unit rides along.

*Canonical copy of this doc lives in the repo at `Servers/Merle.md`; keep any
copy on merle in sync when it changes.*

---

## What runs here

| Service | Unit               | Ports | Purpose                                                     |
| ------- | ------------------ | ----- | ----------------------------------------------------------- |
| Jim     | `narrator-jim`     | —     | Second narrator: field correspondent, mention-triggered follow-ups |
| Deploys | `merle-autodeploy` | —     | Deploy watcher (issue #95): polls origin/main, pulls + restarts Jim on merge |

Everything else lives elsewhere: the broker, Marlin, Willard, and the
production MCC are on pearl (`192.168.1.64` — see `Servers/Pearl.md`); the
perception daemon, camera, and Ollama are on bluejay (`192.168.1.79`).
Jim listens on nothing — he only talks to the broker.

---

## Setup from a bare Pi

Steps 1–3 are done once per SD card; they're recorded here so the box is
rebuildable from nothing.

**1. OS baseline.** Flash Raspberry Pi OS (64-bit), hostname `merle`, enable
SSH. The static DHCP lease for `.103` is already reserved in Pi-hole on pearl
(Settings → DHCP), so the Pi gets its address the moment it asks.

**2. The checkout.**

```
sudo apt update && sudo apt install -y git python3-venv
git clone https://github.com/reclinerhead/project-squirrel.git ~/project-squirrel
```

**3. Python env — the narrator subset only.**

```
cd ~/project-squirrel
python3 -m venv venv
venv/bin/pip install paho-mqtt pyyaml
```

Deliberately **not** `pip install -r requirements.txt`: that drags in
opencv/fastapi/numpy for the vision daemon, none of which the narrator
imports. Don't install the vision stack on the Pi.

**4. Desk test by hand** (before wiring the service):

```
cd ~/project-squirrel
MERLE_MQTT=192.168.1.64:1883 MERLE_OLLAMA=192.168.1.79:11434 \
    venv/bin/python narrator.py --persona personas/jim.yaml
```

You should see Jim announce his narration tier, his `answering to: Jim`
line, and "on the air". Ctrl+C signs him off cleanly.

**5. The unit.** Write `/etc/systemd/system/narrator-jim.service` (cribbed
from pearl's Merle-unit pattern — `systemctl cat narrator-marlin` on pearl to
compare):

```ini
[Unit]
Description=Merle narrator -- Jim, the field correspondent
After=network-online.target
Wants=network-online.target

[Service]
User=todd
WorkingDirectory=/home/todd/project-squirrel
ExecStart=/home/todd/project-squirrel/venv/bin/python narrator.py --persona personas/jim.yaml
Environment=PYTHONUNBUFFERED=1
Environment=MERLE_MQTT=192.168.1.64:1883
Environment=MERLE_OLLAMA=192.168.1.79:11434
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Two lines are load-bearing in every Merle unit (the pearl convention):

- `Environment=PYTHONUNBUFFERED=1` — the scripts log with bare `print()`,
  and under systemd stdout is a pipe, so Python block-buffers: without this,
  `journalctl` shows nothing for hours.
- `Environment=MERLE_MQTT=192.168.1.64:1883` — **required, no default**;
  the broker lives on pearl, not here. `bus.py` raises at startup without
  it, so a misconfigured Jim can't look healthy while publishing into the
  void.

And two more explain themselves once:

- `Environment=MERLE_OLLAMA=192.168.1.79:11434` — the LLM tier runs on
  bluejay's GPU (a Pi 5 doesn't serve gemma3:12b). When bluejay is off, Jim
  degrades to template lines exactly like Marlin does — check the startup
  "narration tier" line in the journal when his prose sounds suspiciously
  Mad-Libs.
- `Restart=always` — `narrator.py` calls `connect()`, not `connect_async()`,
  so it exits if the broker isn't reachable yet. On a cold boot (or a pearl
  reboot) it can lose that race; restarting after 5s turns a fatal race into
  a shrug.

**6. Enable it — unattended, starts at power-up:**

```
sudo systemctl daemon-reload
sudo systemctl enable --now narrator-jim
systemctl status narrator-jim
```

`enable` makes it start on every boot; `--now` also starts it immediately.
Green dot = Jim is on the air. Done — the Pi can now be power-cycled and
Jim comes back on his own.

**7. The deploy watcher** (issue #95) — after this, merges to main reach the
Pi on their own. Write `/etc/systemd/system/merle-autodeploy.service`:

```ini
[Unit]
Description=Merle deploy watcher -- merges to main deploy themselves
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/home/todd/project-squirrel/Servers/autodeploy.sh
Restart=on-failure
RestartSec=10
Environment=MERLE_DEPLOY_UNITS=narrator-jim

[Install]
WantedBy=multi-user.target
```

```
sudo systemctl daemon-reload
sudo systemctl enable --now merle-autodeploy
```

No `User=` — it runs as root on purpose (its job is restarting units without
a sudo password), demoting git to `todd` via `runuser`. No `MERLE_DEPLOY_MCC`
— the dashboard lives on pearl. The full story (pause/resume, `--once`
desk-ticks, the failed-build and dirty-tree rules) is in `Servers/Pearl.md`
under The deploy watcher; the script is one and the same.

---

## Day-to-day

```
systemctl status narrator-jim             # green dot = running
journalctl -u narrator-jim -f             # watch live (Ctrl+C detaches)
journalctl -u narrator-jim -n 50          # last 50 lines
sudo systemctl restart narrator-jim       # after a git pull
```

Deploying new code — **merging the PR is the deploy** (issue #95):
`merle-autodeploy` polls origin/main every 60s and pulls + restarts Jim on
its own. Watch one land with `journalctl -u merle-autodeploy -f`. The manual
way still works whenever the watcher is stopped (Jim runs from source):

```
cd ~/project-squirrel && git pull
sudo systemctl restart narrator-jim
```

To run him by hand (stop the service first):

```
sudo systemctl stop narrator-jim
cd ~/project-squirrel
MERLE_MQTT=192.168.1.64:1883 MERLE_OLLAMA=192.168.1.79:11434 \
    venv/bin/python narrator.py --persona personas/jim.yaml
```

---

## What Jim is

Same script as Marlin (`narrator.py`), different persona
(`personas/jim.yaml`), same shared world canon (`character_bible.yaml` —
written for exactly this day). What makes Jim Jim:

- **The announcer** (issue #88): Jim is the field man and the first voice on
  any new development — arrivals, departures, crowds, and scene updates all
  clear his announcer-level knobs. Marlin defers the raw play-by-play to him
  while Jim's presence lamp is on, and covers it (with remarks about coffee
  breaks) whenever merle is off — so a dead rover never silences the show.
- **Mention triggers** (issue #80): the `answers_to: [Jim]` knob subscribes
  him to `narration/lines`; a Marlin line naming him becomes a
  `colleague_mention` event, and Jim follows up within about a minute —
  context-aware, riding the same Editor rate limit as everything else.
  Never a direct reply channel; everything rides the bus, and a follow-up
  line never triggers a follow-up (the reply-to-a-reply guard, issue #88).
- **His own journal window**: `narration/journal/jim`, retained, backed by
  `narration_journal.json` in the WorkingDirectory
  (`MERLE_NARRATION_JOURNAL` overrides). Safe to delete if it ever goes
  weird; the journal starts fresh.
- **Presence**: `narrators/jim/status`, retained `online`/`offline` with the
  Last-Will contract — a crash (or `systemctl stop`) flips the dashboard
  lamp within seconds.

Rehearsal from any repo checkout: `replay_events.py` replays archived events
onto the bus, or desk-test the mention trigger directly by publishing a
crafted line (from pearl, or anywhere with mosquitto clients):

```
mosquitto_pub -h 192.168.1.64 -t narration/lines -m \
  '{"ts":"2026-07-13T12:00:00","narrator":"Marlin","mqtt_id":"marlin","voice":"David","text":"My trusty assistant Jim would normally be down there.","event_kind":"arrival"}'
```

---

## Quick health check from any machine on the LAN

```
mosquitto_sub -h 192.168.1.64 -t 'narrators/jim/status' -C 1 -v
mosquitto_sub -h 192.168.1.64 -t 'narration/journal/jim' -C 1 -v
```

Both retained — they answer instantly if Jim has ever been on the air.

---

## The rest of the system

- pearl `192.168.1.64` — broker (Mosquitto), Marlin (`narrator-marlin`),
  Willard (`willard-weather`), production MCC (`:3000`), Pi-hole. See
  `Servers/Pearl.md`.
- bluejay `192.168.1.79` — desktop, RTX 5070 Ti: perception daemon (`:8000`),
  camera, Ollama (`:11434`) — Jim's LLM tier.
- merle `192.168.1.103` — you are here. Future rover brain; the narrator
  unit rides along.
