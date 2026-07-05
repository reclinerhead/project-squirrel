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
import os
import time
from dataclasses import dataclass

import cv2
import numpy as np

import perception

RECONNECT_INTERVAL = 3.0   # seconds between RTSP reconnect attempts


@dataclass
class Detection:
    """One tracked animal in one frame. `box` is (x1, y1, x2, y2) in pixels.
    `coasting` is True for a briefly-lost track still being drawn (greyed) until
    it re-matches or ages out -- the synthetic source never coasts."""
    track_id: int
    species: str
    box: tuple
    conf: float
    coasting: bool = False


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


class RTSPFrameSource(FrameSource):
    """The real perception source: the Amcrest RTSP feed through the same model +
    tracker as live.py, sharing perception.py so the two can't drift. Reads the
    camera password from MERLE_RTSP_PASS (never hardcoded) and the model from
    MERLE_MODEL (default models/current.pt). ultralytics is imported lazily here
    so the daemon and its tests stay importable without torch installed."""

    def __init__(self):
        from ultralytics import YOLO   # lazy: heavy, GPU-specific, camera-only path

        user = os.environ.get("MERLE_RTSP_USER", "admin")
        pw = os.environ.get("MERLE_RTSP_PASS")
        if not pw:
            raise RuntimeError("MERLE_RTSP_PASS is not set -- needed for the camera source.")
        self.host = os.environ.get("MERLE_RTSP_HOST", "192.168.1.102")
        self.url = (f"rtsp://{user}:{pw}@{self.host}:554"
                    "/cam/realmonitor?channel=1&subtype=0")
        # Force RTSP over TCP before opening (FFmpeg reads this once at open time);
        # UDP silently drops packets under load and smears frames.
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

        self.cap = self._open()
        if not self.cap.isOpened():
            raise RuntimeError(f"Could not open the RTSP stream at {self.host}. Check "
                               "the camera is reachable and MERLE_RTSP_* are correct.")
        self._last_reopen = 0.0

        self.model = YOLO(os.environ.get("MERLE_MODEL", "models/current.pt"))
        self.names = self.model.names
        self.tm = perception.TrackMemory()

    def _open(self):
        cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # newest frame only -> low latency
        return cap

    def _maybe_reopen(self):
        """A dropped/reconfigured stream (e.g. the camera restarting after a
        settings change) makes reads fail indefinitely until the capture is
        re-opened. Retry on an interval so we recover on our own instead of
        freezing on the last frame -- but not so fast we thrash FFmpeg."""
        now = time.time()
        if now - self._last_reopen < RECONNECT_INTERVAL:
            return
        self._last_reopen = now
        print(f"RTSP read failed -- reconnecting to {self.host}…")
        self.cap.release()
        self.cap = self._open()

    def read(self):
        ok, frame = self.cap.read()
        if not ok:
            self._maybe_reopen()
            return None, []
        results = self.model.track(frame, conf=perception.DETECT_FLOOR,
                                   imgsz=perception.IMGSZ, persist=True,
                                   tracker=perception.TRACKER_YAML, verbose=False)
        live, coasting = self.tm.ingest(
            perception.extract_detections(results[0], self.names))
        dets = [Detection(tid, perception.voted(t), tuple(t["xyxy"]), t["conf"])
                for tid, t in live]
        dets += [Detection(tid, perception.voted(t), tuple(t["xyxy"]), t["conf"],
                           coasting=True) for tid, t in coasting]
        return frame, dets

    def close(self):
        self.cap.release()
