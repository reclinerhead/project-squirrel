# Epic: The Cockpit — rover control, and a multi-source Live Watch

**Epic / tracking issue.** Sub-issues get spun out per phase; this issue is the map, not the work. Nothing here is a contract until it lands in a sub-issue.

*This epic lives as **GitHub issue #127**, which is where it gets edited and argued with — the repo's other epics (#110, #115) live there and nowhere else. This file is the copy that was written first; if the two disagree, **the issue wins**.*

## Why

Two surfaces, one deliberate seam.

The **MCC** shows one video feed today: the perception daemon's annotated MJPEG stream. There is now a second camera in the world (the rover's), and soon a third reason to look at the driveway from more than one angle. Live Watch should become a multi-source watch surface — pick a source, or watch two side by side — while staying exactly what it is: **an observatory. Read-only. No rover ever moves because of something clicked on the dashboard.**

The **cockpit** (`rovercontrol/`) is where all active control lives: steering, pan-tilt, lights, speed modes, telemetry, gamepad someday. It replaces the Waveshare web UI at `merle:5000`. It is where the dead-man timeout and the single command authority live, and it is designed from day one for more than one rover.

The two apps share exactly one real thing: **a source registry** — what video sources exist, and which are live right now. The command path is not shared. It exists only in the cockpit.

## North star

Open the cockpit, see the rover's camera with low enough latency to drive it, hold a key, and the rover moves — and if the network drops, the rover stops on its own without anyone deciding it should. Open the MCC, see the driveway and the rover side by side, and be unable to touch either one.

---

## What the code actually says

The premises in the design brief were checked against the repo. Most hold. **Six do not**, and they change the plan.

### ✅ Confirmed

| Premise | Reality |
|---|---|
| Live Watch is coupled to the daemon's MJPEG | `VideoFeed` (`Dashboard.tsx:1305`) renders one `<img src={`${STREAM_URL}?v=${streamKey}`}>`. `STREAM_URL = "/daemon/stream"` is a module constant in `lib/daemon.ts`. No source concept, no player, no `<video>`. |
| The cockpit is a new sibling app | **`music/` already is one** — sibling to `mcc/`, own `package.json` (port 3001), own lockfile, own `node_modules`, own CI job (`web-music`). `rovercontrol/` is the third instance of an established pattern, not a new idea. |
| Video transport should be per-pane | Nothing blocks it. Today's `<img>` MJPEG path only serves MJPEG; WebRTC/MSE need `<video>`. Per-pane transport means `VideoFeed` becomes a renderer picker. |
| Dead-man timeout is non-negotiable | Already the charter: *"no command in ~1s → stop"* and *"never a mode where confusion produces motion"* (`docs/rover idea sessions.md`). This epic implements a rule the project already wrote down. |

### ❌ Contradicted — read these before planning

**1. go2rtc does not exist. Anywhere.**
Zero occurrences in the repo outside one line of `TechnicalGuide.md` — *"WebRTC only if remote/multi-viewer needs appear."* It is not deployed, not configured, not a dependency. **"Source discovery should come from go2rtc's `/api/streams`"** describes querying a service that has never run. Standing it up is a phase, not an assumption.

**2. "go2rtc as the single RTSP client per camera" would re-plumb the most latency-tuned code in the repo — for no measured reason.**
Today `vision/frames.py:rtsp_url()` is *"the ONE place that decides"* the camera URL, and `RTSPFrameSource` around it is the product of issue #29: model loaded and warmed **before** the RTSP connect, a `FreshestFrameReader` thread draining continuously because `CAP_PROP_BUFFERSIZE` is a silent no-op on the FFmpeg backend, tuned `rtsp_transport;tcp|fflags;nobuffer|flags;low_delay`, throttled self-healing reconnects. Putting go2rtc in front of that means re-tuning all of it. The Amcrest serves multiple concurrent RTSP clients fine. **"Single client per camera" is an aesthetic, not a measured constraint** — and the project's own rule is *"scale only where a measured shortfall, not a guess, justifies it."*
→ **Recommendation: go2rtc owns *browser delivery* and the *rover* camera. The daemon keeps its direct RTSP connection.** Revisit only if the camera actually complains.

**3. 🔴 The Amcrest via go2rtc is a *different picture* than Live Watch shows today — you would lose the boxes.**
`/stream` serves the **annotated** frame: detection boxes, species labels, the colors that are literally `perception.py`'s palette. That is the entire premise of the design language — *"the UI and the stream read as one instrument."* go2rtc serves the **raw** camera. It has never heard of the model.
So "the Amcrest becomes a go2rtc source" silently swaps the annotated feed for a raw one. **These are two different sources of the same camera**, and the pill row has to say so:

| Pill | Transport | What you see |
|---|---|---|
| `driveway` | daemon MJPEG (unchanged) | annotated — boxes, labels, tracks |
| `rover` | go2rtc WebRTC/MSE | raw |
| `driveway (raw)` *(optional)* | go2rtc | raw, low-latency, no boxes |

This is the single easiest thing in this epic to get wrong and only notice after the boxes are gone.

**4. 🔴 "Only online sources appear" violates house rule #1.**
The no-layout-shift rule is the project's #1 UI rule, and the codebase is unanimous about the idiom:
- Rail panels: *"a zero-count row **dims instead of disappearing**. Rows never insert, remove, or reorder."*
- Station chart: *"the snap-back control is **always rendered and merely disabled** while live — a control that appeared on pan would shove the legend sideways (house rule #1)."*

A pill row whose members appear and vanish as the rover sleeps is exactly the thing both of those exist to prevent — and the rover being off *is* the steady state, same as bluejay.
→ **Recommendation: render every known source always; dim + disable the offline ones.** The reserved-space rule then comes free, and "the rover is off" becomes readable information rather than an absence you have to notice.

**5. `merle/rover/telemetry` breaks the topic convention.**
Every topic in `bus.py` is domain-first with no project prefix: `driveway/events`, `narration/lines`, `narrators/<id>/status`, `weather/*`. And they are **constants in `bus.py`** *"so a typo'd string can't split the system"* — a hand-written topic string in a TS file is already off-pattern. Worse, `merle` is both the project name *and* the Pi's hostname, which is ambiguous the moment there are two rovers.
→ **Recommendation: `rover/<id>/telemetry` + `rover/<id>/status`** (retained, Last Will), mirroring `narrators/<id>/status` exactly — including `bus.py` helpers (`rover_status_topic()`, `rover_telemetry_topic()`) and their TS mirrors in `lib/bus.ts`.

**6. 🔴 rover-hands cannot coexist with the `ugv` unit. It replaces it.**
The Waveshare stack we just installed on merle runs as `ugv.service` and **continuously reads `/dev/ttyAMA0`** (`base_ctrl.feedback_data` in a loop) and **owns the USB camera** (`cv_ctrl` → `/video_feed`). Two processes reading one serial port split the bytes between them; go2rtc cannot open a camera `app.py` already holds.
So rover-hands is a **cutover, not an addition** — and at cutover the Waveshare UI at `:5000` dies. **That is today's only way to drive the rover.** The cockpit must reach parity *before* the switch, or there is a window with no working control path.
→ This is the hardest sequencing constraint in the epic and it belongs in Phase B0.

**7. The Waveshare HUD is not baked pixels — it is already DOM.**
`templates/index.html` positions `<span id="CPU">`, `<span id="rssi">`, `<span id="fps">`, `<span id="tem">` *over* the MJPEG `<img>`; `cv_ctrl.py` draws no voltage/CPU/RSSI/attitude on-frame, and `config.yaml` ships `add_osd: false`. Good news — the data path is already JSON over socket.io, and `app.py:update_data_websocket_single()` is a working reference for **exactly which box knows which field**. Nothing is trapped in pixels, so nothing is lost. (Their socket keys are opaque integers — `cpu_load: 106`, `base_voltage: 112` — via `config.yaml`'s `fb:` block. We are not copying that.)

**8. The HUD's ladder and compass are the *gimbal*, not the IMU.**
The brief calls them "a pitch/roll ladder and heading tape from the IMU." They are `pan_angle` / `tilt_angle` — **dead-reckoned app-side state**, not a sensor reading: `cv_ctrl.py:446` does `self.pan_angle += (gx - fx) * iterate`, clamps it, then **sends it to** the servo (`{"T":CMD_GIMBAL,"X":pan_angle,"Y":tilt_angle}`). Open-loop; nothing reads back. **No IMU-derived attitude is displayed anywhere today.** A real attitude HUD is net-new work — see the telemetry table.

### ⚠️ Also worth knowing

- **MCC already sends commands** — `sendControl()` → `POST /daemon/control` (start/stop/record/threshold). "Read-only" means *no physical action*, not *no writes*; say it that way or the first PR will "fix" the contradiction.
- **`/daemon/[...path]` is a wildcard catch-all** that forwards any path and method to `MERLE_DAEMON_URL`. The read-only guarantee is a property of *what sits behind that env var*, not of the proxy. "Impossible to cause physical action from the dashboard" needs a mechanism: **the MCC never learns a rover command URL** — no rover host in its env, the registry yields video URLs only, and rover-hands is not reachable through `/daemon`. Worth an explicit test.
- **`Dashboard.tsx` is 4,008 lines** and holds every panel including the masthead. #115's D1 already flagged this. Multi-source Live Watch is a good excuse for the first component extraction — and a bad thing to do accidentally.
- **merle's venv is a guarded boundary.** `test_import_boundary.py` exists because *"merle runs ONE thing: narrator-jim"* and its venv is `paho-mqtt` + `pyyaml` — enforced by a subprocess test that poisons vision imports. That comment is already false (merle now runs `ugv` + `narrator-jim` + `merle-autodeploy`, and `ugv-env` carries the entire CV stack the boundary exists to keep off the Pi — in its own venv). rover-hands wants FastAPI + pyserial. **Give it its own venv** (the `ugv-env` precedent), keep the narrator's boundary intact, and extend the test to rover-hands.
- **No app on pearl is reverse-proxied yet.** Pi-hole holds 80/443; Caddy is #110 Phase 1 and is a *sequencing constraint* there. `rovercontrol` gets a port (3002) and a unit, like `music` did.
- **`autodeploy.sh`'s build gate is `grep -q "^mcc/"`** — hardcoded. `music/` isn't autodeployed either. A third app makes this the third instance of a seam nobody has generalized. Fix it once, in this epic or #110, but *notice* it.

---

## Architectural principles

1. **The dashboard cannot move anything.** Not "shouldn't" — *cannot*, because it is never told how. No rover command URL in MCC's environment, no rover host behind `/daemon`, registry yields video only.
2. **The rover stops when nobody is holding it.** The dead-man timeout is a property of rover-hands, not of the cockpit. A crashed browser, a dropped socket, a killed tab, a network blip: all identical, all → stop. Safety never waits on the network and never gets a personality.
3. **Liveness is a retained topic with a Last Will**, not a poll. The project already has this idiom twice (`narrators/<id>/status`, `weather/status`) — a crash flips the lamp with no cleanup code.
4. **The registry is a contract, not a package.** See D2.
5. **Duplicate deliberately; note the seam.** The repo's answer to sharing between apps, stated in `music/app/globals.css`: *"carried over verbatim so the two apps read as one household."*
6. **Every known source is always rendered.** Offline dims. Nothing appears, nothing vanishes, nothing shifts.

---

## Open decisions — resolve before Phase 0 opens

### D1 — What does "online" mean for a go2rtc source?
go2rtc's `/api/streams` lists **configured** streams, and by default it connects to a source **on demand** — no consumer, no RTSP connection. So a configured-but-idle stream may look identical to a dead camera. Does liveness need an active probe, a `?src=` health check, or does the config listing suffice for the Amcrest (whose brief says "go2rtc alone")? **Unknown until go2rtc runs.** This is why Phase 0 measures before anything depends on it.

### D2 — Where does the registry contract live? *(recommendation: nowhere new)*
The brief asks: workspace package, shared lib, or duplicate? The repo has already answered this shape twice — **there is no workspace** (`mcc/pnpm-workspace.yaml` has no `packages:` key; it's pnpm `allowBuilds` settings), no root `package.json`, no shared package, no cross-app imports, and `globals.css` is copy-pasted between `mcc/` and `music/` on purpose. Python↔TS types are **hand-mirrored** already (`fetchArchive()` in `lib/weather.ts`: *"thin fetch, hand-mirrored types"*).
→ **Recommendation: the registry is a *service*, and the "shared code" is a ~30-line type plus a fetch — hand-mirrored in each app, exactly like `fetchArchive`.** Inventing a workspace for two consumers buys a build-order problem and contradicts the one precedent that exists. Note the seam in both files; revisit at the third consumer. **The contract's shape is the shared thing; the code is not.**

### D3 — Who serves the registry?
Both apps need it and it must survive bluejay's nap (daemon-down is the steady state).
- **(a)** A route in each app that reads go2rtc + MQTT itself — duplicates logic in two places.
- **(b)** A small pearl-resident service (the `frame_archiver.py` / `weather.py` shape) publishing a retained `sources/registry` to the bus — both apps subscribe, zero HTTP, rehydrates instantly on a fresh tab, matches how the Field Journal and Weather Post already work. **Recommended.**
- **(c)** go2rtc queried directly from the browser — a second origin, CORS, and no MQTT half.

(b) makes liveness a bus fact, which is what principle 3 already wants.

### D4 — Does the cockpit drive through go2rtc's WebRTC, or its own?
go2rtc gives WebRTC for free. But the driving pane's latency budget is the one number that decides whether this is fun or awful, and **nobody has measured it**. Phase B1 measures glass-to-glass before the cockpit is scaffolded around an assumption.

---

## Phase 0 — Prove the registry's shape *(blocks everything)*

**Goal.** One honest answer to "what video sources exist and which are live right now," proven against real hardware before either app depends on it.

- Stand up **go2rtc on pearl** with the Amcrest and the rover camera as sources. First time it has ever run here.
- Answer **D1** by measurement: kill the rover, watch what `/api/streams` says; leave it idle, watch again.
- Define the contract: `{id, label, kind, transports[], online, since}` — and the rule that **rover liveness = go2rtc has the stream AND `rover/<id>/telemetry` is fresh**, Amcrest liveness = go2rtc alone (or whatever D1 proves).
- Add `rover/<id>/*` topic helpers to `bus.py` with tests.

**Exit criterion.** A registry payload on the bus that flips a rover's `online` to `false` **within 5 seconds of pulling its power**, observed with `mosquitto_sub` — no dashboard, no app, no cockpit. If the Last Will doesn't fire, nothing downstream is trustworthy.

**Test contract.** Pure liveness-fusion logic (`fresh()`, registry merge) is Vitest/pytest-covered. Topic helpers mirror `narrator_status_topic()`'s existing tests.

---

## Track A — MCC: read-only multi-source Live Watch

**Depends on:** Phase 0. **A3 additionally depends on B0** (the rover must be a go2rtc source, which means the cutover has happened).

### A1 — Extract Live Watch from the monolith
**Goal.** `VideoFeed` becomes a real component with a source prop, before it grows a second one. First extraction out of `Dashboard.tsx` (4,008 lines).
**Exit.** Live Watch renders **exactly what it renders today** — annotated MJPEG, all three veils (stand down / reconnecting / asleep), fullscreen, the `?v=` cache-buster, the streamKey remount — from its own file, with a `source` prop it ignores. A pure-refactor PR: zero visual diff.

### A2 — The pill row + per-pane transport
**Goal.** Source pills in the Live Watch header; a renderer picked per pane (`<img>` MJPEG · MSE · WebRTC).
**Exit.** Every registry source has a pill from first paint. Offline pills are **dimmed and disabled, never absent** (house rule #1). Switching pills swaps the pane with **no layout shift** and no stream leak (the old connection actually closes — check the daemon's byte counter, per issue #49's lesson). The `driveway` pill still shows **boxes**.
**Note.** Today's veils are daemon-global props (`paused`, `reconnecting`, `asleep`). Multi-source needs **per-source** liveness; those props do not generalize and must move into the source model.

### A3 — Side-by-side
**Goal.** A layout toggle: single, or two sources at once.
**Exit.** Driveway + rover visible simultaneously, each with its own transport and its own veil, on a laptop and a phone. Toggling reserves its space either way.
**Blocked by B0** — until the cutover, `app.py` owns the rover camera and go2rtc cannot have it.

### A4 — The read-only guarantee, enforced
**Goal.** Make principle 1 mechanical.
**Exit.** A test asserts MCC ships no rover command path: no rover host in its env, `/daemon` cannot reach rover-hands, registry payloads carry video URLs only. **The MCC's package should not be able to express a drive command.**

---

## Track B — rover-hands: the service that owns the hardware

**Depends on:** nothing. **Start here** — it is the long pole and the only track with a physical failure mode.

### B0 — Prove the hardware from a bare script *(no web app, no framework)*
**Goal.** Before any service exists: drive a wheel, and **read the whole telemetry set** — both boxes — merged into one payload. The HUD is redrawn from this payload in the cockpit as real DOM/SVG, so if a field is unobtainable we find out here, not in Track C.

**The telemetry set, and who knows what.** The fields are split across two machines that do not know about each other. rover-hands is the only process positioned to merge them.

**ESP32, over serial (`/dev/ttyAMA0` @115200, `T:1001` streams ~10 Hz unprompted):**

| Field | Wire | Notes |
|---|---|---|
| Battery voltage | `v` | ⚠️ **Scaling is unverified.** Their code comment claims `'v': 11` (whole volts); the real board sent `"v":1089` on the bench = centivolts. The comment and the firmware disagree — **read it, don't trust either.** |
| Motor speeds | `L`, `R` | |
| Wheel odometry | `odl`, `odr` | Present in the real payload; absent from their comment. Free dead-reckoning input later. |
| Raw accel | `ax`, `ay`, `az` | Live (`az≈8526` at rest). |
| Raw gyro | `gx`, `gy`, `gz` | Live. |
| Raw magnetometer | `mx`, `my`, `mz` | Live. |
| Fused attitude | `T:1002` → `r`, `p`, `y`, `q0..q3` | 🔴 **Returned all zeros on the bench** — including an all-zero quaternion, which is not a valid orientation (identity is `q0=1`). See awkward #1. |

**Pi-side (merle's OS — the ESP32 cannot know any of these):**

| Field | Mechanism | Notes |
|---|---|---|
| RAM % | `psutil.virtual_memory().percent` | Easy. |
| CPU % | `psutil.cpu_percent()` | ⚠️ Their call is `interval=2` — a **blocking 2-second sleep**. `interval=None` returns `0.0` on first call. Needs a sampling thread, never a request-path call. |
| CPU temp | `vcgencmd measure_temp` | ⚠️ Shells out per read. `/sys/class/thermal/thermal_zone0/temp` is cheaper and subprocess-free. |
| WiFi RSSI | `/sbin/iwconfig` + regex | 🔴 Their mechanism is broken on Lite — see awkward #3. **merle is on WiFi (confirmed);** this is a real, always-present field and a safety-relevant one. |
| IP / link | `netifaces` | Easy. |
| Photos/Videos MB | `os.walk` + `getsize` | ⚠️ Walks the whole tree **per read**. And these are `ugv_rpi`'s folders — they **cease to exist at cutover**. Probably drop the field. |

**Explicitly NOT in the payload:**
- **Stream FPS.** Today it's `cv_ctrl.py:312` — `video_fps = fps_count/2`, measured inside the app's own capture loop. **After the cutover rover-hands has no camera** (go2rtc owns it), so it *cannot* produce this number. See awkward #2.
- **Gimbal pan/tilt.** Commanded state, not telemetry (contradiction #8). It belongs to whoever owns the gimbal — rover-hands, as command echo — not to the sensor merge.
- **Zoom.** Client-side view state.

**Fields I expect to be awkward:**

1. 🔴 **Attitude (pitch/roll/heading) — the hard one.** `T:1002` gave all zeros while raw IMU streamed fine, so the board's fusion is off, absent, or needs enabling. Two ways out: fix the ESP32 side, or fuse on the Pi. **Pitch/roll from accel is trivial arithmetic. Heading is not** — a tilt-compensated magnetic heading needs hard/soft-iron calibration, on an **aluminium chassis with four motors and their currents swinging around it**. That's a sub-project with its own issue, not a field to read. **Recommendation: ship pitch/roll (accel-derived) and treat heading as deferred.** An attitude HUD that lies about which way you're pointed is worse than one that doesn't claim to know.
2. 🔴 **Stream FPS is on the wrong box.** Post-go2rtc nothing on the rover sees the delivered stream. **This is a feature, not a loss:** the number worth showing is the FPS you are *actually receiving*, which only the browser knows — `RTCPeerConnection.getStats()` → `framesPerSecond`. **Recommendation: the cockpit measures it client-side and it never enters the payload.** It stops being a claim by the rover and starts being a measurement of the link.
3. 🔴 **RSSI's mechanism is broken — and the field matters more here than it did for them.** `iwconfig` is **deprecated and not installed on Bookworm Lite** (`wireless-tools` isn't a default package), so their regex parses the output of a command that isn't there. Use `iw dev <iface> link` or `/proc/net/wireless`.
   **merle is on WiFi — it drives around the driveway, so the link is the rover's tether.** That makes RSSI the one Pi-side field that isn't a curiosity: **it is the leading indicator of the dead-man timeout.** A rover driving away from the AP with a falling RSSI is a rover about to stop itself, and the cockpit should say so *before* it happens rather than have the stop arrive as a surprise. Design consequence: RSSI belongs somewhere prominent with a trend, not in a corner as a number — and B1's "pull the Wi-Fi" test is a rehearsal of the normal failure, not an exotic one.
   A failed *read* still renders `—` (the Weather Post's em-dash convention), never `0 dBm`, which reads as catastrophic signal rather than "we didn't ask successfully."
4. ⚠️ **CPU% wants a sampler.** A blocking 2s read cannot sit in a telemetry loop that publishes at 10 Hz.
5. ⚠️ **Voltage scaling.** Comment says volts, board says centivolts. One line of code, one wrong assumption away from a HUD that reads `1089 V`.
6. ⚠️ **Photos/Videos MB** dies with the ugv stack and costs an `os.walk` per read. Drop unless something asks for it.

**Exit.** A standalone Python script on merle — `ugv.service` **stopped**, because it holds the port — that:
- turns **one wheel** for one second, and
- prints **one merged JSON payload** carrying **every field above that survives triage**: ESP32 values (voltage at the *verified* scale, raw IMU, odometry) **and** Pi-side values (CPU, RAM, temp, RSSI-or-null), stamped and shaped exactly as it will ride the bus.

The point is the **merge**, not the voltage: it proves both sources can be read from one process at a steady cadence, and it forces every awkward field above to be settled with a measurement instead of a plan. Glass-to-glass camera latency measured and written down in the same sitting (answers D4).
**This phase also decides the cutover.** `ugv` and rover-hands cannot both run. Options — pick in this phase, don't drift into one:
- **(a)** Cut over now: disable `ugv`, accept no control until the cockpit works. Honest, and briefly hands-off.
- **(b)** Keep `ugv` enabled until Track C reaches parity, developing rover-hands with `ugv` stopped by hand. Slower, always has a fallback. **Recommended.**

*Known-benign noise while testing:* `[base_ctrl.feedback_data] error: Expecting value: line 1 column 1 (char 0)` is a Waveshare bug (it drains `while in_waiting > 0`, then `readline()`s anyway and `json.loads('')`). Not your bug. Don't chase it.

### B1 — The dead-man timeout *(non-negotiable; before any drive API ships)*
**Goal.** The rover stops itself. Always.
**Exit.** With the rover **up on blocks, wheels spinning**: kill the WebSocket client, and the wheels stop within the timeout. Then `kill -9` the client. Then pull the Wi-Fi. Then suspend the laptop mid-command. **All four stop the rover.** Timeout logic is pure and unit-tested with an injected clock (the `Editor`/`SpeciesPresence` pattern).
Nothing in Track C starts until this passes. A cockpit that can start a rover it cannot reliably stop is the one outcome this epic must not produce.

### B2 — The service
**Goal.** FastAPI on merle: WebSocket for drive, REST for toggles, telemetry → MQTT.
**Exit.** `rover-hands.service` enabled, survives a reboot, publishes `rover/<id>/telemetry` at a steady cadence and `rover/<id>/status` retained with a Last Will that flips within seconds of `systemctl stop`. Own venv (the `ugv-env` precedent). `PYTHONUNBUFFERED=1` — the journal is silent without it, and `Servers/Merle.md` says so twice.
**Test contract.** Command validation, clamping, and the timeout are pure and covered. Serial I/O is not (I/O-bound, per CLAUDE.md).

### B3 — Single command authority
**Goal.** Two open cockpits must not fight over one rover.
**Exit.** A second client connecting is refused or demoted to observer — deterministically, stated in the payload, and visible in the UI. (Open question: refuse, or steal-with-notice? The rover is one physical object; two drivers is not a merge conflict.)

---

## Track C — The cockpit (`rovercontrol/`)

**Depends on:** B1 (hard — no control UI before the timeout is proven), Phase 0 (registry), B2 (the service).

### C1 — Scaffold, shaped for real-time
**Goal.** Third Next app, sibling to `mcc/` and `music/`. Port 3002, own lockfile, own CI job (`web-rovercontrol`, mirroring `web-music`), own unit on pearl.
**Not the MCC's shape.** MCC is a passive 1s-poll dashboard. The cockpit is a **client-side real-time control surface**: a held WebSocket, keyboard-hold input, later gamepad. Same stack, opposite cadence. Tokens copied verbatim from `mcc/app/globals.css` with the `music/` comment convention naming the relationship.
**Exit.** `pnpm --dir rovercontrol dev` serves a page that reads the registry and lists rovers. CI green. Zero rover commands yet.

### C2 — The driving pane
**Goal.** WebRTC video + keyboard-hold steering.
**Exit.** Drive the rover, watching only the cockpit — no line of sight. Release the key and it stops. Close the tab mid-throttle and **it stops** (B1, from the UI's side).

### C3 — Instruments + toggles
**Goal.** Telemetry (voltage, IMU), lights, speed modes, pan-tilt.
**Exit.** Voltage on screen matches a multimeter (or the board's own reading) within tolerance. Every toggle round-trips. The no-layout-shift rule applies here too — reserve the space.

### C4 — Multi-rover
**Goal.** The fleet-of-one abstraction the charter already asked for: *"the difference between one rover and several is a config entry + topic namespace."*
**Exit.** With a **fake second rover** publishing to `rover/fake/*`, the cockpit lists two, switches between them, and drives **exactly one** — the other's dead-man never fires because it was never driving. Proves the namespace with no second chassis.

---

## Cross-track dependencies

```
Phase 0 (registry + go2rtc)
   ├──> A2 (pills)
   ├──> A3 (side-by-side) ──── also needs ──┐
   └──> C1 (cockpit scaffold)               │
                                            │
B0 (bare script + CUTOVER DECISION) ────────┤  rover camera can't reach
   └──> B1 (DEAD-MAN — hard gate) ──┐       │  go2rtc while app.py holds it
          └──> B2 (service) ────────┼───────┘
                 └──> B3 (authority)│
                                    └──> C2 (driving) ──> C3 ──> C4
```

**The two gates:** nothing in Track C starts before **B1**. Nothing in A3 works before **B0's cutover**.

---

## Open questions

1. **D1** — what does go2rtc call "online"? Unanswerable until it runs.
2. **D3** — registry as a pearl service on the bus (recommended) or a route per app?
3. **B3** — two cockpits, one rover: refuse, or steal-with-notice?
4. **Does the `driveway (raw)` pill earn its place?** The annotated feed is the product; a raw low-latency twin may be a solution to no problem.
5. **`autodeploy.sh`'s `^mcc/` gate** — generalize here, or in #110? A third un-deployed app makes the seam undeniable.
6. **Does `test_import_boundary.py`'s premise get rewritten now?** "merle runs ONE thing" is already false.
7. **Rover camera latency** (B0) may kill WebRTC-through-go2rtc (D4). Measure before scaffolding around it.
8. **Does the cockpit belong behind #110's Caddy** from day one, or take a port like `music/` did?
9. ~~Is merle on WiFi or wired?~~ **Resolved: WiFi.** It's the rover's brain and drives out into the driveway — the link is the tether. RSSI is a real field and a safety signal (B0, awkward #3).
10. **Heading: fix the ESP32's fusion, or defer it?** (B0, awkward #1.) Magnetic heading on a steel-and-motors chassis is a calibration project. Deferring costs a compass tape; faking it costs trust in the whole HUD.
11. **Does the HUD want the fields the old one had, or the fields we now know exist?** The Waveshare HUD never showed odometry or raw IMU — both are free on the wire. Copying its layout inherits its blind spots.

## What I think we got wrong

- **The annotated-vs-raw distinction (#3 above).** The brief treats "the Amcrest becomes a go2rtc source" as plumbing. It is a product change that removes the boxes — the thing Live Watch is *for*.
- **"Only online sources appear" (#4).** It contradicts the project's #1 UI rule and the idiom used everywhere else. Dim, don't hide.
- **"go2rtc as the single RTSP client" (#2).** Unmeasured, and it puts a service in front of the most carefully-tuned code in the repo.
- **The cutover (#6) is under-modeled.** rover-hands doesn't get added to merle; it *replaces* what's there, and it takes the only working control path with it.
- **The shared-code question (D2) has already been answered** by `music/` — twice, in writing. The honest recommendation is "duplicate a 30-line type and say so," which is less satisfying than a workspace and more likely to still be true in a year.
- **The HUD was never baked into pixels (#7), and its ladder was never the IMU (#8).** The overlay is already DOM over the `<img>`, and the ladder/compass are dead-reckoned *gimbal* angles being sent *to* a servo. So "we lose the HUD when we move to go2rtc" was the wrong worry — the real one is that **two of its fields can't come from the rover at all**: stream FPS belongs to the browser after the cutover, and attitude doesn't exist yet in any usable form.
