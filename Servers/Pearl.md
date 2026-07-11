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
| Willard   | `willard-weather` | —                              | Weather post: polls OpenWeather, publishes retained `weather/*` |
| MCC       | `mcc-dashboard`   | 3000 (HTTP)                    | The Merle dashboard, production build (`next start`)           |
| Pi-hole   | `pihole-FTL`      | 53, 67 (DHCP), 80, 443         | Household DNS + DHCP                                           |

Not here: the perception daemon and camera (those live on bluejay,
`192.168.1.79` — they need the GPU).

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
systemctl status mcc-dashboard
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

`enable` / `disable` control whether it comes back after a reboot. All five
services are enabled. To check:

```
systemctl is-enabled willard-weather
```

Deploying new Merle code (all units run out of the same checkout). For the
**Python services** (narrator, weather), pull + restart is the whole deploy —
they run from source:

```
cd ~/project-squirrel && git pull
sudo systemctl restart narrator-marlin willard-weather
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
3000 (mcc-dashboard). Anything else deserves a question. (Marlin and Willard
listen on nothing — they only talk to the broker.)

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
- `narration/lines` — narrator → world
- `narrators/<id>/status` — retained presence, `online` / `offline`
- `weather/current`, `weather/forecast`, `weather/history` — Willard's
  reports, all **retained**: weather is state, not a moment, so a late
  joiner (fresh dashboard tab) gets the latest report straight from the
  broker with no HTTP path or poll loop of its own
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

Both `narrator-marlin` and `willard-weather` follow the same shape: run as
the login user, `WorkingDirectory=/home/todd/project-squirrel` (the repo
checkout), `ExecStart=` the repo venv's python (`venv/bin/python`, never
system python), and `Environment=` lines carrying the process's env.

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
Extra env in the unit: `MERLE_OLLAMA=192.168.1.79:11434` (bluejay's GPU
serves the LLM; if it's unreachable the narrator silently degrades to
template lines — check the log's "narration tier" line when prose sounds
suspiciously Mad-Libs).

`Restart=always` in the unit is load-bearing: `narrator.py` calls `connect()`,
not `connect_async()`, so it exits if the broker isn't up yet. On a cold boot
it can lose that race. Restarting after 5s turns a fatal race into a shrug.

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

Polls OpenWeather's classic free APIs — current conditions every 10 minutes,
the 5-day/3-hour forecast every hour (~170 calls/day against free limits of
60/min and 1M/month) — and publishes the retained `weather/*` topics above.
Consumers: the dashboard's Weather Post panel (the "willard with the
weather" masthead), and eventually the narrator's prompt context.

Extra env in the unit:

- `MERLE_OWM_KEY` — the OpenWeather API key, **required, no default**. A
  keyless service would poll 401s while looking healthy, so it fails at
  startup instead (the MERLE_MQTT philosophy).
- `MERLE_WEATHER_LOC` — optional; `zip`, `zip,CC`, or `lat,lon`
  (default `49001,US`, the station's home turf).

State: `weather_history.json` in the repo dir (that's `WorkingDirectory` +
the default relative path) — the 48h rolling window behind the dashboard's
observed-temperature trail, persisted so a restart doesn't blank the chart.
Safe to delete if it ever goes weird; it refills within 48h and OpenWeather
is the archive of record.

An OpenWeather hiccup is a skipped report, never a dead service — the next
poll retries. Look for `[weather] fetch failed` lines in the journal; the
URL is never logged because it carries the API key.

To run it by hand (stop the service first):

```
sudo systemctl stop willard-weather
cd ~/project-squirrel && source venv/bin/activate
MERLE_MQTT=localhost:1883 MERLE_OWM_KEY=<key> python weather.py
```

Quick health check from any machine on the LAN — the retained report comes
back instantly if Willard has ever filed one:

```
mosquitto_sub -h 192.168.1.64 -t 'weather/current' -C 1 -v
mosquitto_sub -h 192.168.1.64 -t 'weather/status' -C 1 -v
```

---

## The MCC dashboard

Unit: `/etc/systemd/system/mcc-dashboard.service`
Drop-in: `/etc/systemd/system/mcc-dashboard.service.d/fast-stop.conf`
Code: `mcc/` in the same checkout. Serves http://192.168.1.64:3000

The production Next.js build, served by `next start`. It's a stateless proxy
in front of the daemon on bluejay — the dashboard's state lives in the
browser tab and the daemon; nothing on pearl is worth waiting for.

**Deploying: `~/project-squirrel/Servers/deploy-mcc.sh` is *the* way.**
Pull + restart is not a deploy — `next start` serves the compiled `.next/`,
not source, so a restart without a build re-serves the old code, and a build
run after the restart rewrites `.next/` under the live server (`Failed to
load static file` errors). The script enforces the one valid order — pull →
install → build → restart — and fails loudly at each step, so a broken build
never restarts the service.

**The fast-stop drop-in** sets `TimeoutStopSec=5`. Next's graceful shutdown
waits on connections that browser tabs hold open (HTTP keep-alive), and
systemd's default stop timeout is 90s — so `systemctl stop` used to sit the
full 90s before SIGKILL. The MCC is stateless, so waiting buys nothing;
stops now take ≤5s.

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
- merle `192.168.1.103` — Raspberry Pi 5, the rover.
- pearl `192.168.1.64` — you are here.
