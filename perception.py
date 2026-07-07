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
# drawing a lost track (greyed) for COAST_FRAMES (~1s at 15fps), forget it after
# PRUNE_FRAMES (~6s).
COAST_FRAMES = 15
PRUNE_FRAMES = 90

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
    class vote) instead of blinking off. `ingest` advances one frame and returns
    the (live, coasting) split; `seen` accumulates every track ID ever seen with
    its voted class, for the run-total census."""

    def __init__(self, coast_frames=COAST_FRAMES, prune_frames=PRUNE_FRAMES):
        self.coast_frames = coast_frames
        self.prune_frames = prune_frames
        self.tracks = {}       # tid -> {"xyxy", "conf", "last_frame", "votes"}
        self.seen = {}         # tid -> voted class name (survives pruning)
        self.frame_idx = 0

    def ingest(self, detections):
        """detections: list of (tid, name, conf, xyxy) from extract_detections.
        Returns (live, coasting), each a list of (tid, track_dict). live = matched
        this frame; coasting = missed recently but within COAST_FRAMES."""
        self.frame_idx += 1
        for tid, name, conf, xyxy in detections:
            t = self.tracks.setdefault(tid, {"votes": Counter()})
            t["xyxy"] = [int(v) for v in xyxy]
            t["conf"] = float(conf)
            t["last_frame"] = self.frame_idx
            t["votes"][name] += 1
            self.seen[tid] = voted(t)

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
