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

import os
from collections import Counter

import cv2

# --- Detection & tracker config (shared by live.py and the RTSP source) -------
# DETECT_FLOOR: lowest score a detection needs to REACH the tracker. Kept low
# (0.10) on purpose -- ByteTrack re-uses these weak detections to hold an
# already-established track together through the low-confidence frames of a
# walking animal (the flicker). It does NOT spawn junk tracks: a NEW track only
# starts from a confident detection (new_track_thresh: 0.5 in the yaml).
DETECT_FLOOR = 0.10
IMGSZ = 1920                 # infer at 3x the trained 640 so distant animals survive downscale
TRACKER_YAML = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "bytetrack_squirrel.yaml")

# Coasting: ByteTrack keeps a lost track alive internally but only OUTPUTS tracks
# matched THIS frame, so a single missed frame used to blink the box off. Keep
# drawing a lost track (greyed) for COAST_FRAMES (~1s at 15fps); it stays in
# memory as a stitch target until PRUNE_FRAMES (~30s -- longer than ByteTrack's
# 12s track_buffer, because re-minted IDs by definition appear after it).
COAST_FRAMES = 15
PRUNE_FRAMES = 450

# Identity stitching (issue #22): a stationary feeding squirrel flickers out of
# detection long enough for ByteTrack to mint it a NEW id on re-acquisition --
# same animal, extra "visitor" in the census, and its still-coasting ghost
# double-counts against the crowd threshold. The camera is fixed and the whole
# failure mode is an animal that ISN'T moving, so a brand-new id whose box sits
# on a recently-lost track of the same species is that track come back: adopt
# the old identity. Trade-off, accepted: a different animal taking the exact
# same spot within the prune window merges with its predecessor -- rare at
# ~30s, and the census error it replaces ran 5x the other way. NOTE all these
# frame-denominated windows assume the fixed 15fps camera; the rover era
# (moving camera, ~60fps) is a different tuning regime.
STITCH_IOU = 0.4

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
                 census_after=CENSUS_AFTER_FRAMES):
        self.coast_frames = coast_frames
        self.prune_frames = prune_frames
        self.census_after = census_after
        self.tracks = {}       # canonical tid -> {"xyxy", "conf", "last_frame", "frames", "votes"}
        self.aliases = {}      # re-minted ByteTrack tid -> canonical tid, forever
        self.seen = {}         # canonical tid -> voted class name (survives pruning)
        self.frame_idx = 0

    def _absorb(self, tid, name, conf, xyxy):
        t = self.tracks.setdefault(tid, {"votes": Counter(), "frames": 0})
        t["xyxy"] = [int(v) for v in xyxy]
        t["conf"] = float(conf)
        t["last_frame"] = self.frame_idx
        t["frames"] += 1
        t["votes"][name] += 1
        # Census tenure: a one-blink track is tracked and drawn but never
        # counted as a visitor. Once tenured, keep adopting the latest vote.
        if t["frames"] >= self.census_after:
            self.seen[tid] = voted(t)

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

    def ingest(self, detections):
        """detections: list of (tid, name, conf, xyxy) from extract_detections.
        Returns (live, coasting), each a list of (canonical_tid, track_dict).
        live = matched this frame; coasting = missed recently but within
        COAST_FRAMES. Duplicates collapse first (an NMS-free model can put two
        boxes on one animal); then two passes -- known ids first -- so every
        track matched this frame is on the books before any stitch decision
        is made."""
        self.frame_idx += 1
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
            if canon is not None:
                # aliases map to CANONICAL ids, so chains flatten (B->A then
                # C->A, never C->B->A) and one lookup always lands.
                self.aliases[raw_tid] = canon
            self._absorb(canon if canon is not None else raw_tid, name, conf, xyxy)

        live, coasting = [], []
        for tid, t in list(self.tracks.items()):
            age = self.frame_idx - t["last_frame"]
            if age == 0:
                live.append((tid, t))
            elif age <= self.coast_frames:
                coasting.append((tid, t))
            elif age > self.prune_frames:
                del self.tracks[tid]
        return live, coasting


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
