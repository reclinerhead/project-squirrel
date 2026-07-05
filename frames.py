# =============================================================================
# project-squirrel -- frames.py
#
# A frame SOURCE feeds the daemon (merle_daemon.py). It yields a raw frame plus
# the detections in it; the daemon does the annotation, encoding, and persistence
# the same way no matter where the frames came from. That seam is the point:
#
#   - SyntheticFrameSource  (here)          -- camera-free, for tests / dev / the
#                                              MCC frontend before the real feed
#   - the real RTSP+YOLO source (Phase 2b-ii) -- same interface, real animals
#   - a rover camera (someday)               -- same interface again
#
# Keeping perception behind this interface means the daemon's presentation and
# storage logic has exactly one implementation, so it can't drift between the
# synthetic and real paths. This module imports NO ultralytics/torch, so the
# daemon and its tests stay light in CI.
# =============================================================================

import math
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Detection:
    """One tracked animal in one frame. `box` is (x1, y1, x2, y2) in pixels."""
    track_id: int
    species: str
    box: tuple
    conf: float


class FrameSource:
    """Base interface. read() returns (frame_bgr, [Detection, ...]); the frame is
    None when the source has ended. Sources may block in read() to pace to their
    real frame rate (a camera does); the daemon also caps its own loop rate, so a
    source that returns instantly (the synthetic one) won't spin the CPU."""

    def read(self):
        raise NotImplementedError

    def close(self):
        pass


class SyntheticFrameSource(FrameSource):
    """A camera-free stand-in: a couple of squirrels tracing lazy paths across a
    grey "driveway", with a chipmunk that darts in and out. Motion is a pure
    function of an internal frame counter, so a test can step it deterministically
    and get the same boxes every run. No sleeping here -- the daemon paces itself.
    """

    def __init__(self, width=1280, height=720):
        self.w = width
        self.h = height
        self.i = 0

    def read(self):
        self.i += 1
        # A flat grey field stands in for pavement. Real frames are 4K off the
        # Amcrest; this is deliberately small and cheap.
        frame = np.full((self.h, self.w, 3), (64, 66, 68), np.uint8)

        dets = []
        # Two squirrels on offset Lissajous paths -- always present.
        for tid, phase in ((1, 0.0), (2, 1.7)):
            cx = int(self.w * (0.5 + 0.38 * math.sin(self.i * 0.03 + phase)))
            cy = int(self.h * (0.5 + 0.28 * math.cos(self.i * 0.021 + phase)))
            dets.append(Detection(tid, "squirrel", (cx - 45, cy - 32, cx + 45, cy + 32), 0.72))

        # A chipmunk visits for ~90-frame stretches, then is gone for two more --
        # a fast little visitor, like the real ones.
        if (self.i // 90) % 3 == 0:
            cx = int(self.w * (0.5 + 0.45 * math.sin(self.i * 0.06)))
            cy = int(self.h * 0.55)
            dets.append(Detection(3, "chipmunk", (cx - 26, cy - 20, cx + 26, cy + 20), 0.41))

        return frame, dets
