Project Squirrel — Ideas & Horizon Backlog ("merle-someday")
Captured from brainstorming. This is a someday/horizon idea-bank, NOT a to-do list. Everything here sits DOWNSTREAM of the current near-term milestone. Read the next section first, every time.


⛔ NEAR-TERM MILESTONE COMES FIRST (the only thing that's actually "next")
Current reality: The detector is single-class (squirrel) and works well (~0.96 mAP50 from the last training session). It cannot yet tell a turkey or chipmunk from a squirrel. There are currently zero turkey photos and zero chipmunk photos in the dataset.

The actual next step:

Collect driveway photos of turkeys and chipmunks (varied poses, distances, lighting).
Label them — and label EVERY animal of EVERY class in each frame (unlabeled animals poison training). Multi-animal frames are especially valuable; label them completely.
Aim for rough class balance across squirrel / turkey / chipmunk.
Retrain on the combined dataset (data.yaml now lists 3 classes) from base weights, on the desktopi.
Prove it with live inference (old iPhone as IP camera → desktop).

Discipline reminder (from the project charter): build the foundation first; let each layer prove itself before adding the next; scale only where a measured shortfall — not a guess — justifies it. Nothing below is buildable until detection sees all three animals. Chipmunks are the hardest class (small, fast); the puffy-cheek + racing-stripe silhouette is a strong distinguishing signal, but include lean/no-cheek chipmunk images too so the model doesn't require the cheeks.


Guiding architectural principles (surfaced today, worth keeping)
Off-board brain / remote body. Desktop runs YOLO + all decision logic; the rover is a "dumb" body that streams camera up and receives simple motor commands down. Migrate to onboard (Jetson) only when a measured Wi-Fi/latency shortfall justifies it. Jetson is a later, earned purchase — start Pi-first.
Layered control: reflexes outrank thinking. A fast local safety loop on the rover can always override the smart layer, never the reverse. Safety never gets a personality and never waits on the network.
Two data streams: upstream = senses (camera + tiny status packets); downstream = commands (small JSON). WebSockets for messages; RTSP/HTTP for video.
Graceful degradation / failsafe ladder: full capability connected → reduced-but-safe alone → never a mode where confusion produces motion. Dead-man's switch: no command in ~1s → stop.
Event grammar is the backbone. Turn tracked-box geometry into named, timestamped events (NEW_ACTOR, COUNT_RISING, FLOCK_EVENT, POSSIBLE_STANDOFF, THREAT_PROXIMITY, etc). Behavior, narration, nemesis system, overlays all consume the same event stream.
Save raw video + synchronized event log together. Treat the aligned pair as the real archive; rendered clips are disposable and regenerable. This is the one data-hygiene rule to get right early.
Everything below is desk-testable against recorded video before the rover exists. Point the retired iPhone at the driveway, record real turkey/squirrel mornings, and prototype tracking, events, standoff detection, narration, and overlays with no hardware.


Idea bank (organized by theme)
⭐ Starred favorites (the ones that clearly light this project up)
⭐ The Nemesis System. Emergent rivalries from logged interactions. A ledger of who-displaces-whom between tracked individuals → detect recurring pairings → head-to-head records ("Squirrel #4 vs Tom #2: 2–9"), win streaks, upsets (auto-flagged as highlights). Standoffs are detectable (two actors, close, stationary, facing, sustained). Rivalries should decay when an animal stops appearing; stay tentative when re-ID is uncertain. Popular because the drama is real, not scripted.
⭐ Narrator Personas. Same event stream + persona prompt + TTS voice = infinite shows. Documentarian / Sports Commentator / Noir Detective / Nervous Intern. Persona can also tune behavior knobs (linger time, proximity threshold, silence between lines) — but the reflex/safety layer never gets a personality. Keep a shared "character bible" of world facts (Big Chonk's history, daily schedule) separate from persona so new narrators inherit continuity for free. Personas are testable content: same clip, 4 narrators.
Perception & recognition
Individual re-ID / driveway census. Recognize which squirrel (Big Chonk), not just "squirrel." Dashboard becomes a roster: who visited, who's new, who's missing.
Behavior recognition. Foraging / sitting / freezing / bolting / caching from video. This is the roadmap layer that upgrades standoff/battle prediction from pure geometry ("close + still") to posture-aware ("close + still + aggression display").
Standoff / "battle brewing" detection. Built from spatiotemporal reasoning on tracked boxes: distance, speed, facing, duration, and their trends (closing, freezing, holding). Probabilistic — speak in likelihoods, not certainties (better content anyway).
The robot as naturalist
The naturalist's field journal. Auto daily log: first visitor, peak activity windows, individuals seen, flock events, weather correlation, overnight visitors. Uses every sensor.
Dawn patrol. Wake at first light, slow perimeter pass, park at seed station, file a "good morning" report before you're up. A complete autonomous mission.
The gentle documentarian move. Detect foraging animal → hold DEAD STILL → track with camera only (pan/tilt), capturing calm close-ups precisely by not chasing. Patience beats pursuit; it's easy to program and it's the emotional core of the project (the lawn-chair test). Audio/voice goes to an outdoor speaker at the driveway end + dashboard, NOT on the robot (spooks wildlife). Habituation is real and on your side — start volume low.
Sensors (reasonable budget), by capability unlocked
Microphone → ears. Cheap; new sense. Audio as attention-director; species-specific calls; alarm chatter = predator early-warning. Future audio-classification sub-project.
IMU (accel/gyro) → inner ear. Nearly free, often already on-board. Tip-over detection, bump/contact detection, better dead-reckoning when fused with wheel encoders. High value.
Pan/tilt camera mount → track by moving the camera, not the chassis. Smoother, less spooking; lets the robot hold still while following action.
Weather sensors → field station. Ground-truth temp/humidity/pressure stamped on every observation. Barometric pressure drop → activity spikes → "front coming in?"
Thermal camera → warm-body vision (the splurge/showpiece). Beats camouflage, works in total darkness (night shift!), fuses with RGB ("warm blob" + "it's a squirrel"). Single most transformative sensor if budget stretches.
Depth / stereo → real distance. "Maintain exactly 2m" instead of guessing from box size; precise anti-turkey perimeter.
Oddballs (because we can): light sensor (day rhythm), UV, soil moisture.
Safety & navigation (the "don't get stepped on / stay home" problems)
Anti-trample reflex. Local proximity sensors (ultrasonic / time-of-flight) + a tiny fast loop: too close → stop, pivot away, then report. Vision feeds it predictively (boxes growing fast = incoming). Humble version: when crowd count > N, just STOP. Turkeys are a merciful first adversary (big, slow-ish, ground-based, visible).
Boundary / geofence — it's a localization problem. Options, easiest first:
Visual boundary: pavement-vs-grass check on bottom of frame (virtual cliff detector). Best starting answer — no infrastructure, uses skills already being built.
Dead reckoning + map polygon: wheel encoders estimate position; refuse to exit rect. Drifts over time — supplement, not foundation.
Fixed-camera localization (sneaky-clever): AprilTag on rover, porch/overhead camera tracks it, pixel→driveway coordinate map. Drift-free, ~zero cost, uses a camera you were mounting anyway. Best fit for a fixed, camera-covered area.
RTK GPS: cm-accurate but needs a correction source and degrades badly near buildings / under tree canopy — wrong layer for a driveway next to a house. File with ROS 2.
Return-to-home / failsafe on Wi-Fi dropout. Runs entirely on the rover (no desktop). Ladder: L0 stop-and-wait (right v1); L1 breadcrumb return via dead reckoning; L2 visual homing to an AprilTag at base (drift-free final approach); L3 onboard localization (Jetson era). "Home" = a safe zone out of turkey traffic, visible to house camera. A moving blind robot takes more risk than a parked one — if things are nearby, hold. Escalate by duration, not on a 2-second blip.
Output, sharing & the "show"
Narration pipeline: perceive → interpret → script → speak.
Tier 1 template narration (Mad Libs; forces the event-stream + pacing work).
Tier 2 LLM-scripted narration (story + continuity; the real thing).
Tier 3 VLM narration (comments on what it literally sees — tail flicks etc.).
Silence is most of the show: event-driven with cooldowns/thresholds.
Narrate the ARCHIVE first (auto-narrated highlight reels), live later.
Original narrator character, never a cloned real voice.
Data-driven video overlay pipeline. Burn narration + graphics onto shareable clips.
L1 burned-in subtitles (FFmpeg + .srt). Timing is free — events are already timestamped.
L2 broadcast lower-third + info bar (species tags, turkey counter, rivalry stat). Can render the graphics layer in HTML/CSS (reuses dashboard skills).
L3 tracking overlays that follow animals (name cards glued to Big Chonk, path trails) — "AR nature doc" look, rendered with OpenCV from the tracking data.
Offline (render after the event) is far easier and higher-quality than live — start there.
Auto highlight reel / daily digest. Clip interesting moments (new individual, rare behavior, 3 species in frame, upsets). Doubles as pre-sorted future training data.
Life list. Auto-logged first-ever detection of each species. First robin, first deer.
Seed-pile leaderboard. Dominance rankings / time-at-pile from data already derived.
Active-time-lapse. Nightly 60s stitch of only the active moments of the day.
"Merle's mood." One-line event-state → emoji/word on the dashboard. Cheap; feels alive.
Call-sign moment. Re-ID of a tagged individual triggers a chime / name card flourish.
Predictive arrival. Time-series model on logged history → "flock likely in ~20 min." A non-vision ML sub-project; feeds pre-positioning behavior.
Platform / integration
Merle as a Hearth agent. Merle is a mobile sensor that publishes timestamped events — architecturally already a Hearth (home-awareness) data source. Publish over MQTT to a broker; Hearth subscribes; the squirrel dashboard becomes a Hearth page. Gives Hearth a narrated outdoors — a category of one.
Fleet abstraction (comes free from hub-and-spoke). Agents publish to a hub → the difference between one rover and several is a config entry + topic namespace. Merle → then Pearl / Earl. Multi-agent coordination is advanced, but the plumbing falls out on day one. Build a fleet architecture with a fleet size of one.
Keep the empire inside the property line. Camera-bearing robots roaming the neighborhood is a thicket of privacy/neighbor/regulation issues — the driveway version is pure upside and charming; the neighborhood version is a screenplay, not a build plan.
Far horizon (explicitly long-term, from the original charter)
Reinforcement learning: robot develops squirrel-like behavior via trial-and-reward, trained in simulation first, transferred to hardware (sim-to-real). Today's logged observations (real trajectories, arrival schedules, behavior distributions) become the seed for a simulation of your actual driveway ecosystem.
LLM-in-the-decision-loop ("showrunner"): LLM not just narrating but directing camera position / storyline choice, with the reflex safety layer firmly in charge underneath. Active frontier — self-assembled, not a paved path. Plausible far layer, not a next step.


Glossary added today
Distillation (knowledge distillation): training a small "student" model to imitate a larger "teacher" model's outputs. Cousin of the transfer learning you already do; relevant later for shrinking a model to run on the Jetson (edge).
Spatiotemporal reasoning: reasoning about where things are and how that changes over time — i.e. arithmetic on tracked boxes across frames.
TTS (text-to-speech), VLM (vision-language model), persona prompt (standing instructions giving the narrator a character).
FFmpeg (universal video tool), hardsub / burned-in (text baked into pixels), .srt (subtitle format), lower-third (broadcast caption bar), compositing (layering graphics over video).
MQTT: lightweight publish/subscribe messaging protocol standard in home automation.
AprilTag: QR-like fiducial marker read natively by CV libraries; gives position + pose.
Differential drive: tank-style steering (left/right wheel speeds). PID: ~15 lines of Python for smooth pursuit. Perception-action loop (sense-plan-act): the robot's core.

Most Recent Notes:

## Portability principle (surfaced today — the session's real insight)

- **Build seams, not second implementations.** The engine (sense-decide-act loop, reflex-safety layer, JSON-to-subcontroller plumbing, MQTT event grammar) is environment-agnostic. What's environment-*specific* is a thin layer: the model's classes, the specific reflexes, a few tuned thresholds. So "bring Merle indoors" is a *reconfigure*, not a re-engineer — but only if seams are left as you go. A seam is cheap (model path in config, reflexes as swappable modules, no "driveway" hardcoded into variable names); take it now. A second implementation is expensive (cat-detection reflex, stair-cliff sensor logic); defer it until there's an indoor rover to run it on and a measured need. Same antibody as the Jetson rule: don't build the layer until a measured shortfall justifies it.
- **The human-in-the-loop training pipeline IS the portability mechanism.** Record footage → auto-propose labels → human reviews/corrects → retrain → validate on held-out frames. Once this machine exists and is trusted, adapting to a new world becomes boring: run the machine on new footage. It pays rent immediately — it's also the tool that closes the current turkey/chipmunk gap and harvests highlight clips as pre-sorted training data. Build the machine once; every environment after is cheap.

### Indoor cat-rover (filed someday — NOT a second active track)

- **"Merle, indoor edition."** Same engine, roof added. Cats become a new detection class (same retrain exercise as turkeys). Enclosed space teaches real pathing (go *around* the couch) and pursuit of an uncontrolled moving target — muscles the flat driveway can't build. Controlled light, holds-still obstacles, subject lives there — an easier *start* but a bigger *detour*. Chosen on purpose as a weekend side-quest if/when it delights, not merged into the driveway line.
- **Indoor difficulty is not flat.** Follow-the-cat and don't-hit-walls are beginner-friendly (walls hold still). "Am I too tall to fit under that" is a step up (needs depth/height reasoning — earned layer). **Stairs are the trap:** a down-staircase is an obstacle that isn't *there* — forward "something's close" sensors see open air and drive off the edge. Needs downward-pointing cliff sensors built deliberately. Until that exists and is trusted, a baby gate is legitimate robotics safety equipment. (Reflexes/failsafes before autonomy — same charter rule.)

### Chassis lead (was open, now has a concrete candidate)

- **Waveshare UGV Rover, "Acce" variant (~$245 base, no Pi).** 2mm all-aluminum shell (turkey-rated), 4 encoder gearmotors w/ closed-loop PID, ESP32 sub-controller already reading a 9-axis IMU, 3S lithium UPS module (batteries NOT included — order 18650s alongside), optional pan-tilt w/ camera. The ESP32/Pi split *is* the layered-control principle sold pre-built: reflexes on the ESP32, brain on the removable Pi 5 (Jetson-swappable later). Trades away learning-every-wire for skipping the fussy-setup phase — a fair trade given the joy lives in the squirrels, not the soldering. Add-ons to reach a working rover under $500: Pi 5 8GB, 18650 cells + charger, 2–3× VL53L1X ToF sensors (anti-trample reflex), spare battery set, printed AprilTag for overhead-camera localization.




Reminder: the sky is genuinely the limit here — imagination is the surplus, not the constraint. The scarce thing is the one built layer that turns all of this from possible into working. That layer is a 3-class detector. Go take the turkey photos.

