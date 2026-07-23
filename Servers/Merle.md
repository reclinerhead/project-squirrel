# Merle

Raspberry Pi 5 (8GB), Raspberry Pi OS Lite (64-bit, Bookworm).
Static DHCP lease `192.168.1.103` (reserved in Pi-hole on pearl).
Hostname `merle`. SSH in as the login user (`todd` — bare `ssh merle` tries
the wrong user and hangs, same trap as pearl).

**Merle is the rover.** The SD card was reflashed 2026-07-15 for the
Waveshare `ugv_rpi` install, and the rover has been driving the driveway
under its own power since. **Jim** — the second narrator (issue #80), the
field correspondent to Marlin's studio host — still runs here, exactly as
planned back when this box was "the rover's future brain": the Pi rides the
rover, and Jim is the guy out in the field among the beasts. That part of
the old framing came true and is now just how it is.

A rover that is **off, out of range, or holding its battery elsewhere is a
normal state, not a fault** — expect `ssh` to time out sometimes. Everything
below that could only be verified with the box up is dated; anything that
could not be re-checked is marked as such.

*Canonical copy of this doc lives in the repo at `Servers/Merle.md`; keep any
copy on merle in sync when it changes.*

---

## Wi-Fi — the U7 must win, or the rover silently falls back home

Since the reflash, Wi-Fi is managed by **NetworkManager** (`nmcli`), not
`wpa_supplicant`. Two profiles exist:

- `project-squirrel-u7` — the outdoor Ubiquiti U7 AP (set up 2026-07-18).
- `preconfigured` — Todd's home 2.4GHz network, created by Pi OS imaging.
  This is the **original** setup network and stays as a deliberate fallback.

NetworkManager does **not** roam between different SSIDs on its own — once
connected to one, it stays until that one drops. At equal autoconnect-priority
(both default 0), any blip (rover booting before the U7 is up, a weak U7
moment, the U7 rebooting) makes merle grab `preconfigured` and sit there — it
looks exactly like a failed or weak U7 when the U7 is fine. This cost an hour
of AP troubleshooting on 2026-07-21 before we found it had silently fallen
back.

**Fix (applied 2026-07-21):** give the U7 a higher priority so it always wins
when visible, keeping home as a genuine fallback:

```
sudo nmcli connection modify project-squirrel-u7 connection.autoconnect-priority 10
nmcli -f NAME,AUTOCONNECT-PRIORITY connection show     # U7=10, preconfigured=0
```

Check what it's actually on, and force a switch if needed:

```
nmcli -f NAME,DEVICE connection show --active          # want project-squirrel-u7 on wlan0
iwgetid -r                                             # prints the live SSID
sudo nmcli connection up project-squirrel-u7           # force it back onto the U7
```

Adding a new AP later: `sudo nmcli device wifi connect "SSID" password "PW"`,
then set its priority the same way. Don't delete `preconfigured` — a rover
with zero known networks is a rover you're plugging a keyboard into.

**AccessPopup — installed, behavior unverified.** Waveshare's installer left
`AccessPopup` under `~/ugv_rpi`, `hostapd` masked, and `dnsmasq` enabled
(observed 2026-07-18). AccessPopup's advertised job is to stand up the Pi's
own access point when no known Wi-Fi is in range — which would matter for a
rover that drives out of coverage — but what this configuration *actually
does* on merle has not been watched happen: merle went unreachable during
the investigation that was meant to check it (see above: normal state), and
was unreachable again when this doc was rewritten (2026-07-21). Verify on
the box before relying on it; update this paragraph when you do.

---

## What runs here

| Service | Unit               | Ports  | Purpose                                                     |
| ------- | ------------------ | ------ | ----------------------------------------------------------- |
| Rover   | `ugv`              | `5000` | Waveshare UGV web UI + driver-board bridge — drive, pan/tilt, lights, speed modes |
| Jim     | `narrator-jim`     | —      | Second narrator: field correspondent, mention-triggered follow-ups |
| Deploys | `merle-autodeploy` | —      | Deploy watcher (issue #95): polls origin/main, pulls + restarts Jim on merge |

All three observed running together 2026-07-18. Everything else lives
elsewhere: the broker, Marlin, Willard, Earl, and the production MCC are on
pearl (`192.168.1.64` — see `Servers/Pearl.md`); the perception daemon,
camera, and Ollama are on bluejay (`192.168.1.79`). Jim listens on nothing —
he only talks to the broker.

**One more job uses this box without running on it: merle is one of Earl's
ears.** Pearl's `earl-listener` pulls the rover's USB camera mic over
ssh — the `rover` feed in the repo's `feeds.yml` (issue #270; a `command`
kind, `earl: true`) is literally
`ssh todd@merle arecord -D plughw:0,0 …` piped back to BirdNET on pearl
(pearl→merle ssh keys are a deploy step; editing the feed's `cmd` changes
the capture). Nothing to install or restart on merle for this; when the
rover is off or out of range the source shows `offline` on `audio/sources`
and cycles its restart backoff — the designed steady state, not a fault.
See the Earl spoke (`docs/guide/earl.md`).

---

## The rover stack (`ugv.service`)

The single most important thing on the box and, until the Helm cutover
(epic #127), the only control path for the rover. Drive it at
`http://merle:5000`. The unit as installed (2026-07-15, observed running
2026-07-18):

```ini
[Unit]
Description=Waveshare UGV rover -- web UI + driver board bridge
After=network-online.target
Wants=network-online.target

[Service]
User=todd
WorkingDirectory=/home/todd/ugv_rpi
ExecStart=/home/todd/ugv_rpi/ugv-env/bin/python /home/todd/ugv_rpi/app.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Load-bearing facts that the unit file doesn't show:

- **`~/ugv_rpi` is not the project-squirrel checkout.** It's a separate
  clone of Waveshare's repo with its own venv (`ugv-env`, owned by `todd`).
  `merle-autodeploy` runs with `MERLE_DEPLOY_UNITS=narrator-jim`, so **rover
  code does not autodeploy** — changes there are pulled and restarted by
  hand (`cd ~/ugv_rpi && git pull && sudo systemctl restart ugv`).
- **`app.py` owns `/dev/ttyAMA0` and the USB camera exclusively.** Two
  processes reading the serial port split the bytes between them, and
  nothing else can open the camera while `app.py` holds it. This is why the
  Helm's rover-hands **replaces** `ugv.service` at cutover rather than
  joining it (Trap 6 in `docs/rover-cockpit-epic.md`).
- A healthy driver board streams feedback like `{"T":1001,…,"v":1089}` —
  `v` is battery millivolts-ish (~10.9 V there). And
  `[base_ctrl.feedback_data] error: Expecting value: line 1 column 1 (char 0)`
  in the journal is **benign Waveshare sloppiness** (it `json.loads` an empty
  read when the serial buffer is idle) — ignore it, don't chase it.

### Installing it — Waveshare's instructions do not work on Pi OS Lite

Recorded from the actual 2026-07-15 install so the box is rebuildable; this
was the single most expensive thing to figure out. **Do not run
`sudo ./setup.sh` or `./autorun.sh`:**

- `requirements.txt` is a ~350-package `pip freeze` of their **desktop**
  image (thonny, PyQt5, torch, sense-hat, ~130 `types-*` stubs, all three
  opencv variants), not a dependency list. `python-apt==2.6.0` can't install
  from pip at all, so the all-or-nothing `pip install -r` hard-fails —
  after PyQt5 spends hours compiling C++.
- `sudo ./setup.sh` creates a **root-owned venv** that `./autorun.sh`
  (running as `todd`) then can't write to. Lite also lacks
  `python3-picamera2`, which setup.sh never installs.
- `./autorun.sh` needs Jupyter (not installed) and writes
  `c.NotebookApp.token=''` / `password=''` — an **unauthenticated Jupyter
  (remote code execution) on the LAN**. The systemd unit above replaces it.

**What the code actually imports** is ~11 third-party packages: flask,
flask_socketio, werkzeug, picamera2, aiortc, mediapipe, depthai, imageio,
imutils, pygame, pyttsx3 (plus cv2/numpy/yaml/serial/psutil/netifaces from
apt). No torch, no Qt, no Jupyter. `depthai` is a hard top-level import in
`cv_ctrl.py` — required even with no OAK camera attached.

**The recipe that works** (result: a 481 MB venv; piwheels is already in
`/etc/pip.conf`, so ARM wheels are prebuilt):

```
# apt for everything Debian packages:
sudo apt install -y python3-picamera2 python3-pygame python3-flask \
    python3-werkzeug python3-opencv python3-numpy python3-serial \
    python3-yaml python3-psutil python3-netifaces \
    libcamera-dev portaudio19-dev espeak ffmpeg

# venv AS TODD (not sudo), inheriting the apt packages:
cd ~/ugv_rpi && python3 -m venv --system-site-packages ugv-env

# pip only for what apt can't provide, then re-pin numpy:
ugv-env/bin/pip install Flask-SocketIO aiortc imageio imutils pyttsx3 \
    mediapipe depthai
ugv-env/bin/pip install 'numpy<2' 'matplotlib<3.9'
ugv-env/bin/pip uninstall -y opencv-contrib-python   # let apt's cv2 win
```

Two traps that cost the most time:

- A `--system-site-packages` venv **also inherits `~/.local`**, so a stale
  `~/.local/lib/python3.11/site-packages` silently shadows both apt and the
  venv — fixes never stick until it's deleted (the failed official installs
  left 1.5 GB there).
- Letting pip resolve mediapipe drags in numpy 2.x + a second OpenCV, which
  breaks apt's numpy-1.x-ABI extensions (`numpy.dtype size changed,
  Expected 96 … got 88`) — hence the explicit `numpy<2` pin and the
  opencv-contrib uninstall above.

**Audio out**: Waveshare's `asound.conf` hardcodes `card 3`, which on merle
is an unplugged HDMI port — `pygame.mixer.init()` dies with ALSA error 524
and `audio_ctrl.py` swallows it into a silent "audio usb not connected".
Card indexes also shuffle across boots, so `/etc/asound.conf` now targets
the device **by id** (`type plug` + `slave.pcm "hw:Device,0"` — "Device" =
the USB PnP Audio Device); the original is saved as
`/etc/asound.conf.waveshare-orig`. Healthy startup logs
`mixer OK -> (44100, -16, 2)`.

**Hardware config** (per-SD-card, already applied): `uart0=on`, Bluetooth
disabled, serial console off the cmdline, `todd` in `dialout`. Pi 5 serial is
`/dev/ttyAMA0` (`app.py` picks it via a Pi-5 check).

### Lidar — on the bench

The Lidar module has arrived but is **not installed** — not mounted, not
wired, not configured. This note exists so the doc keeps telling the truth
about what is and isn't on the rover; replace it with real documentation
when the module goes on.

---

## Setup from a bare Pi — the narrator side

Steps 1–3 are done once per SD card; they're recorded here so the box is
rebuildable from nothing. (The rover stack above is its own separate
checkout and install.)

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
imports. Don't install the vision stack in *this* venv — the rover's CV
stack lives in its own venv under `~/ugv_rpi` (see The rover stack above),
and `test_import_boundary.py` guards this one.

**4. Desk test by hand** (before wiring the service):

```
cd ~/project-squirrel
MERLE_MQTT=192.168.1.64:1883 MERLE_OLLAMA=192.168.1.79:11434 \
    venv/bin/python -m narration.narrator --persona narration/personas/jim.yaml
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
ExecStart=/home/todd/project-squirrel/venv/bin/python -m narration.narrator --persona narration/personas/jim.yaml
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
- `Restart=always` — `narration/narrator.py` calls `connect()`, not `connect_async()`,
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
a sudo password), demoting git to `todd` via `setpriv` (PAM-silent — see
Pearl.md for why not `runuser`). No `MERLE_DEPLOY_MCC`
— the dashboard lives on pearl. `MERLE_DEPLOY_UNITS=narrator-jim` only —
the rover stack is deliberately outside the watcher's reach. The full story
(pause/resume, `--once` desk-ticks, the failed-build and dirty-tree rules)
is in `Servers/Pearl.md` under The deploy watcher; the script is one and
the same.

---

## Day-to-day

`ssh todd@merle` (never bare `ssh merle`). If it times out, remember what
this box is: "off" and "out of range" are normal rover states, not faults —
check whether it's on the charger or out past the U7's reach before
suspecting the software. When it *is* up but on the wrong network, see the
Wi-Fi section above.

**The rover** — drive at `http://merle:5000`:

```
systemctl status ugv                  # green dot = rover is up
journalctl -u ugv -f                  # watch live (Ctrl+C detaches)
journalctl -u ugv -n 100              # last 100 lines
sudo systemctl restart ugv            # rover UI wedged / driver board unresponsive
```

**Jim**:

```
systemctl status narrator-jim         # green dot = on the air
journalctl -u narrator-jim -f         # watch live
journalctl -u narrator-jim -n 100     # last 100 lines
sudo systemctl restart narrator-jim   # after a manual git pull
```

**The deploy watcher**:

```
systemctl status merle-autodeploy     # green dot = watching
journalctl -u merle-autodeploy -f     # watch a deploy land
journalctl -u merle-autodeploy -n 100 # recent deploy history (quiet polls log nothing)
sudo systemctl restart merle-autodeploy
```

Deploying new code, two different worlds:

- **project-squirrel (Jim)** — merging the PR is the deploy (issue #95):
  `merle-autodeploy` polls origin/main every 60s and pulls + restarts Jim on
  its own. Manual fallback whenever the watcher is stopped (Jim runs from
  source): `cd ~/project-squirrel && git pull && sudo systemctl restart narrator-jim`.
- **ugv_rpi (the rover)** — never autodeployed. By hand, deliberately:
  `cd ~/ugv_rpi && git pull && sudo systemctl restart ugv`.

To run Jim by hand (stop the service first):

```
sudo systemctl stop narrator-jim
cd ~/project-squirrel
MERLE_MQTT=192.168.1.64:1883 MERLE_OLLAMA=192.168.1.79:11434 \
    venv/bin/python -m narration.narrator --persona narration/personas/jim.yaml
```

---

## What Jim is

Same module as Marlin (`narration/narrator.py`), different persona
(`narration/personas/jim.yaml`), same shared world canon
(`narration/character_bible.yaml` —
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
mosquitto_sub -h 192.168.1.64 -t 'audio/sources' -C 1 -v
```

All retained — the first two answer instantly if Jim has ever been on the
air, and `audio/sources` shows whether Earl currently has the rover's mic
(`"rover": {"state": "online"}`) or the rover is dark (`offline` — normal
when it's off or out of range). The rover's own liveness from the couch:
`http://merle:5000` loads, or it doesn't.

---

## The rest of the system

- pearl `192.168.1.64` — broker (Mosquitto), Marlin (`narrator-marlin`),
  Willard (`willard-weather`), Earl the listener (`earl-listener` +
  `earl-sightings` + `earl-enrichment`, issues #172/#175), production MCC
  (`:3000`), Pi-hole. See `Servers/Pearl.md`.
- bluejay `192.168.1.79` — desktop, RTX 5070 Ti: perception daemon (`:8000`),
  camera, Ollama (`:11434`) — Jim's LLM tier.
- merle `192.168.1.103` — you are here. **The rover**: Waveshare UGV
  (`ugv`, `:5000`), Jim riding along (`narrator-jim`), and Earl's second
  pair of ears when it's in range.
