# =============================================================================
# project-squirrel -- perception.py
#
# The shared brain. Both the desktop live view (live.py) and the daemon's real
# camera source (frames.py) run the same detector + tracker, then do the same
# bookkeeping: remember each track, coast a briefly-lost one, vote its class,
# prune it when long gone -- and draw boxes the same way. Keeping that logic in
# ONE place is deliberate: it already bit us once when two files drifted apart
# (a labeling script pinned to an older model than the live view). Everything
# tricky and worth testing lives here.
#
# This module imports NO ultralytics/torch -- it operates on detections already
# pulled out of a model result -- so its tests run fast and camera-free.
# =============================================================================

import math
import os
import statistics
from collections import Counter, deque

import cv2

# --- Detection & tracker config (shared by live.py and the RTSP source) -------
# DETECT_FLOOR: lowest score a detection needs to REACH the tracker. Kept low
# (0.10) on purpose -- ByteTrack re-uses these weak detections to hold an
# already-established track together through the low-confidence frames of a
# walking animal (the flicker). It does NOT spawn junk tracks: a NEW track only
# starts from a confident detection (new_track_thresh: 0.5 in the yaml).
DETECT_FLOOR = 0.10
IMGSZ = 1920                 # infer at 3x the trained 640 so distant animals survive downscale
# QUANTIZE: run the detector in FP16 (issue #33). Ollama serves the narrator's
# LLM from this same GPU, and token generation saturates MEMORY BANDWIDTH (each
# token streams the full weight set from VRAM) while barely using compute --
# that contention, not a utilization cap, is what dropped the loop from 15fps
# to ~7 during narration. FP16 halves the detector's memory traffic per frame
# so inference rides out the squeeze better (solo it's a modest win: 19.7 ->
# 17.4 ms/frame at imgsz=1920, desk-tested). Accuracy impact is nil in practice
# (FP16 is the deployment norm); ultralytics keeps FP32 on CPU regardless.
# `quantize=16` is the current spelling -- `half=True` still works but is
# deprecated and would warn on EVERY call. GOTCHA: precision locks in when the
# first inference call builds the predictor; later calls can't change it, so
# every first-touch call site (warmup included) must pass this.
QUANTIZE = 16
TRACKER_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "bytetrack_squirrel.yaml")

# Coasting: ByteTrack keeps a lost track alive internally but only OUTPUTS tracks
# matched THIS frame, so a single missed frame used to blink the box off. Keep
# drawing a lost track (greyed) for COAST_FRAMES (~1s at 15fps); it stays in
# memory as a stitch/necromancer target until PRUNE_FRAMES (~60s). The prune
# window was 450 (~30s -- "longer than ByteTrack's 12s track_buffer"); the
# issue #74 gap analysis found stationary feeders re-appearing ON their own
# grave (145px) 44.5s and 51.6s after vanishing -- past the old window, each
# one a fresh census visitor. 900 covers the observed gaps with ~15% margin
# at half the same-spot-false-merge exposure of a 90s window.
COAST_FRAMES = 15
PRUNE_FRAMES = 900

# Identity stitching (issue #22): a stationary feeding squirrel flickers out of
# detection long enough for ByteTrack to mint it a NEW id on re-acquisition --
# same animal, extra "visitor" in the census, and its still-coasting ghost
# double-counts against the crowd threshold. The camera is fixed and the whole
# failure mode is an animal that ISN'T moving, so a brand-new id whose box sits
# on a recently-lost track of the same species is that track come back: adopt
# the old identity. Trade-off, accepted: a different animal taking the exact
# same spot within the prune window (~60s since issue #74) merges with its
# predecessor -- rare, and the census error it replaces ran 5x the other way.
# NOTE all these
# frame-denominated windows assume the fixed 15fps camera; the rover era
# (moving camera, ~60fps) is a different tuning regime.
STITCH_IOU = 0.4

# Fragment re-association -- the "necromancer" pass (issue #74, Phase 2.4).
# The IoU stitch above only resurrects an animal that came back WHERE it
# vanished; the crowd fixture showed that in a feeding scene under half of the
# re-mints overlap their corpse (23 of 62) -- squirrels shuffle a body length
# or two between the tracker losing them and re-acquiring them, and each leak
# became a fresh census "visitor" (19 counted for ~5 real animals). So when no
# stitch target exists, a brand-new id of species S born within reach of
# where an S track was LOST (not matched this frame, not yet pruned) is that
# track come back: same alias mechanics, same census identity; ByteTrack's
# fresh id stays internal. The reach scales with the dead track's box (a near
# animal is bigger AND moves more pixels per body length -- one constant
# serves the whole yard). Accepted trade-off, same direction as the stitch's:
# a different squirrel claiming a dead one's patch within the prune window
# merges into its census identity -- the overcount it replaces ran ~4x the
# other way on the fixture.
#
# The reach is TWO-TIER. Gap analysis on the crowd fixture found the single
# biggest leak was FAST REPOSITIONS: a squirrel hops, ByteTrack fails the IoU
# association mid-hop, and a new id births 2-12 frames later only 30-230px
# away -- zero overlap, old track still coasting (5 of the 10 leaked births).
# So a coasting corpse is raisable too, but only within ONE body length:
# tight, because a track missed for under a second is still effectively on
# screen and a real second animal beside it must not merge into it. A
# vanished corpse (past the coast window) gets the full reach -- that animal
# had time to wander. Matched-this-frame tracks are never raisable, same as
# the stitch.
NECRO_REACH = 2.0            # vanished corpse: body-lengths of wander allowed
NECRO_REACH_COASTING = 1.0   # coasting corpse: the mid-hop re-mint only

# The model is NMS-free (end-to-end head), so it can emit TWO boxes on one
# animal. The labeling path dedupes these (label_utils.dedupe_boxes); the live
# path must too, BEFORE any track bookkeeping -- each duplicate that reaches
# ByteTrack is confident enough to mint a parallel track riding the same
# squirrel (issue #24). Same threshold and greedy keep-highest-conf approach
# as the labeling dedupe.
DEDUPE_IOU = 0.7

# A track becomes a census "visitor" only after this many matched frames
# (~2s at 15fps). One-blink junk tracks are still tracked and drawn -- they
# just never count as an animal that visited.
CENSUS_AFTER_FRAMES = 30

# Hard-example "flicker band": a live track scoring in here is one the model
# finds genuinely hard -- exactly the frame worth banking for the next round.
HARD_LO, HARD_HI = 0.15, 0.50

# Churn metrics window (issue #74, Phase 0): ten minutes at the fixed camera's
# 15fps. The issue's success criteria are per-10-minutes ("<= 2 ids per real
# animal per 10 min"), so the rolling window every rate below is judged over
# matches the ruler. Frame-denominated like every other window here (same
# rover-era caveat as COAST/PRUNE above).
METRICS_WINDOW_FRAMES = 9000

# Stable per-class box colors (BGR); coasted boxes draw grey.
PALETTE = [(56, 56, 255), (31, 112, 255), (49, 210, 207), (10, 249, 72),
           (255, 149, 0), (255, 0, 170), (29, 178, 255)]
GREY = (128, 128, 128)

# Colors keyed by NAME, not palette position -- these mirror the frontend accent
# tokens in mcc/app/globals.css so the stream and the UI read as one instrument.
# Keying by name is load-bearing: the palette is positional, so when chipmunk
# (index 0) left the class list, a position-based map would have slid squirrel
# from orange to chipmunk's red. Names pin each species to its color regardless
# of how many classes exist or their order. chipmunk stays defined -- dormant
# now, back when the rover gets a close-up camera.
SPECIES_COLORS = {
    "squirrel": (31, 112, 255),   # #FF7031
    "turkey":   (49, 210, 207),   # #CFD231
    "chipmunk": (56, 56, 255),    # #FF3838
}


def class_colors(names):
    """Map each class name to its stable color. Known species get their fixed
    color (see SPECIES_COLORS); anything unexpected falls back to the positional
    palette so a brand-new class still draws *something* distinct."""
    return {name: SPECIES_COLORS.get(name, PALETTE[i % len(PALETTE)])
            for i, name in names.items()}


def voted(track):
    """The track's majority-voted class over its lifetime."""
    return track["votes"].most_common(1)[0][0]


def iou(a, b):
    """Intersection-over-union of two xyxy boxes."""
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


def dedupe_detections(detections):
    """Collapse same-frame duplicate boxes (IoU >= DEDUPE_IOU) to the highest-
    confidence one, class-agnostic -- see DEDUPE_IOU above. Greedy over
    detections sorted by confidence, mirroring label_utils.dedupe_boxes."""
    kept = []
    for d in sorted(detections, key=lambda d: -d[2]):
        if all(iou(d[3], k[3]) < DEDUPE_IOU for k in kept):
            kept.append(d)
    return kept


def extract_detections(result, names):
    """Pull (track_id, class_name, conf, xyxy) tuples out of a model.track()
    result. Returns [] when nothing carried a track ID -- ultralytics leaks
    ID-less raw detections on a no-match frame, and drawing/counting those is
    what caused one-frame phantom boxes, so we ignore them."""
    boxes = result.boxes
    if boxes.id is None:
        return []
    ids = boxes.id.int().tolist()
    out = []
    for tid, cls_id, score, xyxy in zip(ids, boxes.cls, boxes.conf, boxes.xyxy):
        out.append((int(tid), names[int(cls_id)], float(score),
                    [int(v) for v in xyxy]))
    return out


class TrackMemory:
    """Remembers tracks across frames so a briefly-lost one keeps its box (and
    class vote) instead of blinking off, and STITCHES a re-minted ByteTrack id
    back onto the lost track it replaced (see STITCH_IOU above) so one animal
    stays one identity. `ingest` advances one frame and returns the
    (live, coasting) split; `seen` accumulates every canonical track ID with
    its voted class, for the run-total census."""

    def __init__(self, coast_frames=COAST_FRAMES, prune_frames=PRUNE_FRAMES,
                 census_after=CENSUS_AFTER_FRAMES,
                 metrics_window=METRICS_WINDOW_FRAMES, log=None):
        self.coast_frames = coast_frames
        self.prune_frames = prune_frames
        self.census_after = census_after
        self.tracks = {}       # canonical tid -> {"xyxy", "conf", "last_frame",
                               #   "frames", "votes", "born", "conf_min",
                               #   "conf_max", "conf_sum"}
        self.aliases = {}      # re-minted ByteTrack tid -> canonical tid, forever
        self.seen = {}         # canonical tid -> voted class name (survives pruning)
        self.frame_idx = 0
        # --- churn instrumentation (issue #74, Phase 0) -----------------------
        # `log` is the lifecycle narrator: a callable taking one string (the
        # daemon passes print; tests pass list.append; None = silent, so the
        # pure default stays pure). Cheap enough to leave on: a line per track
        # birth/stitch/death, not per frame.
        self.log = log
        self.metrics_window = metrics_window
        self.total_minted = 0    # raw ByteTrack ids ever seen (tracker churn)
        self.total_births = 0    # canonical tracks created (post-stitch churn)
        self.total_stitches = 0  # re-minted ids folded back by the IoU stitch
        self.total_raised = 0    # ...and by the necromancer's distance gate
        self._known = set()      # every raw ByteTrack id ever seen
        self._minted = deque()   # frame_idx of each raw-id mint, window-pruned
        self._births = deque()   # frame_idx of each canonical birth, window-pruned
        self._deaths = deque()   # (frame_idx, matched_frames, tenured), window-pruned
        self._live_counts = deque(maxlen=metrics_window)   # matched tracks per frame

    def _absorb(self, tid, name, conf, xyxy):
        t = self.tracks.get(tid)
        if t is None:
            t = self.tracks[tid] = {
                "votes": Counter(), "frames": 0, "born": self.frame_idx,
                # Per-track confidence min/mean/max over its life -- the
                # instrument that shows the dips killing stationary feeders.
                "conf_min": float(conf), "conf_max": float(conf), "conf_sum": 0.0,
            }
            self.total_births += 1
            self._births.append(self.frame_idx)
            if self.log:
                cx = (int(xyxy[0]) + int(xyxy[2])) // 2
                cy = (int(xyxy[1]) + int(xyxy[3])) // 2
                self.log(f"[track] born #{tid} {name} conf={conf:.2f} "
                         f"@({cx},{cy}) f={self.frame_idx}")
        t["xyxy"] = [int(v) for v in xyxy]
        t["conf"] = float(conf)
        t["conf_min"] = min(t["conf_min"], float(conf))
        t["conf_max"] = max(t["conf_max"], float(conf))
        t["conf_sum"] += float(conf)
        t["last_frame"] = self.frame_idx
        t["frames"] += 1
        t["votes"][name] += 1
        # Census tenure: a one-blink track is tracked and drawn but never
        # counted as a visitor. Once tenured, keep adopting the latest vote.
        if t["frames"] >= self.census_after:
            self.seen[tid] = voted(t)

    def _bury(self, tid, t):
        """Record (and narrate) a pruned track's death. `tenured` separates the
        real losses (an animal's track died and was probably re-minted) from
        one-blink junk that never confirmed."""
        tenured = t["frames"] >= self.census_after
        self._deaths.append((self.frame_idx, t["frames"], tenured))
        if self.log:
            mean = t["conf_sum"] / t["frames"]
            why = "tenured" if tenured else "never confirmed"
            x1, y1, x2, y2 = t["xyxy"]
            self.log(f"[track] died #{tid} {voted(t)} after {t['frames']} matched "
                     f"frames ({why}), conf min/mean/max "
                     f"{t['conf_min']:.2f}/{mean:.2f}/{t['conf_max']:.2f}, "
                     f"last seen f={t['last_frame']} @({(x1 + x2) // 2},{(y1 + y2) // 2})")

    def _stitch_target(self, name, xyxy):
        """The lost track a brand-new id should adopt, if any: the best-
        overlapping track of the same voted species NOT matched this frame.
        Matched tracks are excluded so a real second animal standing next to a
        live one can never merge into it."""
        best, best_iou = None, STITCH_IOU
        for tid, t in self.tracks.items():
            if t["last_frame"] == self.frame_idx or voted(t) != name:
                continue
            overlap = iou(t["xyxy"], xyxy)
            if overlap >= best_iou:
                best, best_iou = tid, overlap
        return best

    def _necro_target(self, name, xyxy):
        """The lost track a brand-new id should resurrect, if any: the nearest
        same-species track not matched this frame whose grave lies within
        reach of the newcomer -- the full NECRO_REACH for a vanished corpse,
        the tight NECRO_REACH_COASTING for one still coasting (see the
        two-tier rationale above). Runs only after _stitch_target comes up
        empty -- an overlapping corpse is the stronger claim -- and catches
        the animal that MOVED between losing the tracker and being
        re-acquired."""
        cx = (xyxy[0] + xyxy[2]) / 2
        cy = (xyxy[1] + xyxy[3]) / 2
        best, best_dist = None, None
        for tid, t in self.tracks.items():
            age = self.frame_idx - t["last_frame"]
            if age == 0 or voted(t) != name:
                continue
            bodies = NECRO_REACH if age > self.coast_frames else NECRO_REACH_COASTING
            bx1, by1, bx2, by2 = t["xyxy"]
            reach = bodies * max(bx2 - bx1, by2 - by1)
            dist = math.hypot((bx1 + bx2) / 2 - cx, (by1 + by2) / 2 - cy)
            if dist <= reach and (best is None or dist < best_dist):
                best, best_dist = tid, dist
        return best

    def ingest(self, detections):
        """detections: list of (tid, name, conf, xyxy) from extract_detections.
        Returns (live, coasting), each a list of (canonical_tid, track_dict).
        live = matched this frame; coasting = missed recently but within
        COAST_FRAMES. Duplicates collapse first (an NMS-free model can put two
        boxes on one animal); then two passes -- known ids first -- so every
        track matched this frame is on the books before any stitch decision
        is made."""
        self.frame_idx += 1
        # Mint counting rides the RAW detections, before dedupe: a duplicate
        # box's fresh id never reaches the bookkeeping below, but ByteTrack
        # DID mint it -- and tracker churn is what the metric measures.
        for raw_tid, *_ in detections:
            if raw_tid not in self._known:
                self._known.add(raw_tid)
                self.total_minted += 1
                self._minted.append(self.frame_idx)
        detections = dedupe_detections(detections)
        fresh = []
        for raw_tid, name, conf, xyxy in detections:
            tid = self.aliases.get(raw_tid, raw_tid)
            if tid in self.tracks:
                self._absorb(tid, name, conf, xyxy)
            else:
                fresh.append((raw_tid, name, conf, xyxy))
        for raw_tid, name, conf, xyxy in fresh:
            canon = self._stitch_target(name, xyxy)
            raised = False
            if canon is None:
                canon = self._necro_target(name, xyxy)
                raised = canon is not None
            if canon is not None:
                # aliases map to CANONICAL ids, so chains flatten (B->A then
                # C->A, never C->B->A) and one lookup always lands.
                self.aliases[raw_tid] = canon
                if raised:
                    self.total_raised += 1
                    if self.log:
                        gap = self.frame_idx - self.tracks[canon]["last_frame"]
                        self.log(f"[track] #{raw_tid} raised onto #{canon} "
                                 f"({name}, dead {gap}f)")
                else:
                    self.total_stitches += 1
                    if self.log:
                        self.log(f"[track] #{raw_tid} stitched onto #{canon} ({name})")
            self._absorb(canon if canon is not None else raw_tid, name, conf, xyxy)

        live, coasting = [], []
        for tid, t in list(self.tracks.items()):
            age = self.frame_idx - t["last_frame"]
            if age == 0:
                live.append((tid, t))
            elif age <= self.coast_frames:
                coasting.append((tid, t))
            elif age > self.prune_frames:
                self._bury(tid, t)
                del self.tracks[tid]

        # Roll the metrics window forward (the live-count deque prunes itself
        # via maxlen).
        self._live_counts.append(len(live))
        horizon = self.frame_idx - self.metrics_window
        while self._minted and self._minted[0] <= horizon:
            self._minted.popleft()
        while self._births and self._births[0] <= horizon:
            self._births.popleft()
        while self._deaths and self._deaths[0][0] <= horizon:
            self._deaths.popleft()
        return live, coasting

    def metrics(self, fps=15.0):
        """The churn numbers every Phase 2 change is graded against (issue
        #74): id-mint rate, lifetime, and the fragmentation ratio (ids minted
        per smoothed concurrent animal) over the rolling window. `fps` converts
        the frame-denominated window to wall-clock -- pass the measured loop
        rate when you have one."""
        window = min(self.frame_idx, self.metrics_window)
        minutes = window / fps / 60 if fps > 0 and window else 0.0
        lifetimes = [frames for _, frames, _ in self._deaths]
        concurrency = (sum(self._live_counts) / len(self._live_counts)
                       if self._live_counts else 0.0)
        minted_w = len(self._minted)
        return {
            "ids_minted": self.total_minted,
            "ids_minted_window": minted_w,
            "ids_per_minute": round(minted_w / minutes, 2) if minutes else None,
            "births": self.total_births,
            "births_window": len(self._births),
            "stitches": self.total_stitches,
            "raised": self.total_raised,
            "deaths_window": len(self._deaths),
            "never_confirmed_window": sum(1 for *_, ten in self._deaths if not ten),
            "median_lifetime_frames": (statistics.median(lifetimes)
                                       if lifetimes else None),
            "mean_concurrency": round(concurrency, 2),
            # Both fragmentations guarded against an empty pavement: with
            # ~nobody on screen the ratio would divide by ~zero and read as
            # infinite churn. `fragmentation` is RAW tracker churn (ByteTrack
            # mints per concurrent animal -- what the tracker-tuning phases
            # act on); `canonical_fragmentation` is what survives stitching +
            # the necromancer (identities per concurrent animal -- the number
            # the census actually experiences, and the issue #74 target).
            "fragmentation": (round(minted_w / concurrency, 1)
                              if concurrency >= 0.05 else None),
            "canonical_fragmentation": (round(len(self._births) / concurrency, 1)
                                        if concurrency >= 0.05 else None),
            "window_minutes": round(minutes, 1),
        }


# Arrival/departure debounce defaults (species-level; see SpeciesPresence).
# Canonical HERE so the daemon and the offline fixture runner can never drift;
# merle_daemon re-exports them as ARRIVE_AFTER/DEPART_AFTER for its tests.
ARRIVE_AFTER_S = 2.0     # a count INCREASE must hold this long to be an arrival
DEPART_AFTER_S = 12.0    # a DECREASE must hold this long -- longer than any
                         # realistic churn gap, so lost-and-reminted ids never
                         # read as leave-and-return


class SpeciesPresence:
    """Debounced species-level arrival/departure -- the daemon's event
    machinery, MOVED here from merle_daemon.Worker (issue #74, Phase 0.5) so
    the offline fixture runner can replay the exact same debounce the live
    path runs; the daemon passes its own ARRIVE_AFTER/DEPART_AFTER constants
    in, so behavior and tunability are unchanged.

    A species' observed count must hold at a new value for `arrive_after`
    seconds (up) or `depart_after` seconds (down) before the change is
    announced; any wobble back to the announced count resets the timer.
    Tracker id churn (same animal re-minted under a new id after a detection
    gap) dips a count for a few seconds at most, so it produces NO events --
    which is the whole point. `duration_s` rides a departure only when the
    last one leaves (counts above zero can't know which individual left).

    observe(counts, now) takes this frame's {species: matched-track count} and
    the wall clock, and returns the [(kind, details), ...] announced this
    tick. Pure logic, injected clock -- covered in test_perception.py."""

    def __init__(self, arrive_after=ARRIVE_AFTER_S, depart_after=DEPART_AFTER_S):
        self.arrive_after = arrive_after
        self.depart_after = depart_after
        self._species = {}

    def observe(self, counts, now):
        events = []
        for sp in set(counts) | set(self._species):
            observed = counts.get(sp, 0)
            st = self._species.setdefault(
                sp, {"count": 0, "candidate": 0, "candidate_since": None,
                     "present_since": None})
            if observed == st["count"]:
                st["candidate_since"] = None   # settled back -- forget the wobble
                continue
            if st["candidate_since"] is None or observed != st["candidate"]:
                st["candidate"] = observed     # new challenger -- start the clock
                st["candidate_since"] = now
                continue
            wait = self.arrive_after if observed > st["count"] else self.depart_after
            if now - st["candidate_since"] < wait:
                continue
            old = st["count"]
            st["count"] = observed
            st["candidate_since"] = None
            if observed > old:
                if old == 0:
                    st["present_since"] = now
                events.append(("arrival", {"species": sp, "count": observed}))
            else:
                details = {"species": sp, "count": observed}
                if observed == 0 and st["present_since"] is not None:
                    details["duration_s"] = round(now - st["present_since"], 1)
                    st["present_since"] = None
                events.append(("departure", details))
        return events


def draw_tracks(frame, items, colors, scale=1.0):
    """Draw boxes + labels onto `frame`. `items` is a list of
    (track_id, label, box_xyxy, is_live); coasting (not-live) boxes draw grey.
    `scale` sizes the text/lines for the frame resolution -- 1.0 is tuned for 4K
    (matching live.py's look); the daemon passes frame_height/2160 so smaller
    frames get proportionally smaller annotations."""
    font = cv2.FONT_HERSHEY_SIMPLEX
    fs = 1.3 * scale
    text_thick = max(1, round(3 * scale))
    box_thick = max(1, round(4 * scale))
    for tid, label, box, is_live in items:
        color = colors.get(label, GREY) if is_live else GREY
        x1, y1, x2, y2 = box
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, box_thick)
        tag = f"{label} #{tid}"
        (tw, th), tb = cv2.getTextSize(tag, font, fs, text_thick)
        pad = round(8 * scale)
        ty = y1 - 10 if y1 - th - tb - 10 >= 0 else y2 + th + tb + 10
        cv2.rectangle(frame, (x1, ty - th - tb), (x1 + tw + pad, ty + tb), color, -1)
        cv2.putText(frame, tag, (x1 + round(4 * scale), ty), font, fs,
                    (255, 255, 255), text_thick, cv2.LINE_AA)
    return frame
