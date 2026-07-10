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
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

import perception

RECONNECT_INTERVAL = 3.0   # seconds between RTSP reconnect attempts
READ_TIMEOUT = 1.0         # seconds read() waits for a fresh frame before
                           # reporting "no signal" (the daemon shows its
                           # reconnecting veil while reads return None)


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


class FreshestFrameReader(threading.Thread):
    """Drains a capture as fast as frames arrive and keeps only the newest one.

    Why this exists: FFmpeg queues an RTSP stream without bound (OpenCV's
    CAP_PROP_BUFFERSIZE is a no-op on the FFmpeg backend -- cap.set returns
    False), and a consumer that reads at exactly the camera's frame rate can
    never drain a backlog once one forms. Model load + first-inference CUDA
    warmup used to build ~6-7s of queued frames at daemon startup, and that
    delay then persisted for the life of the process (measured on the real
    camera: after a 5s reader stall, ~75 frames came back as instant reads).
    This thread reads continuously -- the camera paces it once it's caught up
    -- so the newest frame is always the one on offer and any backlog drains
    at decode speed (~11ms/frame for 4K H.264 on this machine).

    A failed read triggers a throttled reopen via the capture factory (the
    camera restarts after a settings change, network blips), so the source
    stays self-healing without the consumer noticing anything but a gap.
    """

    def __init__(self, cap, open_capture, label="camera"):
        super().__init__(daemon=True)
        self._cap = cap                 # an already-open capture (validated by caller)
        self._open = open_capture       # zero-arg factory for reconnects
        self._label = label             # for the reconnect log line
        self._cond = threading.Condition()
        self._frame = None
        self._seq = 0                   # bumps once per stored frame
        self._last_reopen = 0.0
        self._stopping = threading.Event()

    def run(self):
        while not self._stopping.is_set():
            ok, frame = self._cap.read()
            if not ok:
                self._reconnect()
                continue
            with self._cond:
                self._frame = frame
                self._seq += 1
                self._cond.notify_all()
        self._cap.release()

    def _reconnect(self):
        """Reopen the capture, at most once per RECONNECT_INTERVAL. The wait
        rides the stopping event so close() isn't held up by a dead camera."""
        pause = RECONNECT_INTERVAL - (time.time() - self._last_reopen)
        if pause > 0 and self._stopping.wait(pause):
            return
        if self._stopping.is_set():
            return
        self._last_reopen = time.time()
        print(f"RTSP read failed -- reconnecting to {self._label}…")
        self._cap.release()
        self._cap = self._open()

    def next_frame(self, last_seq, timeout=READ_TIMEOUT):
        """Block until a frame NEWER than last_seq is available (so a consumer
        faster than the camera never processes the same frame twice), and return
        (frame, seq). On timeout: (None, last_seq) -- the no-signal case."""
        with self._cond:
            if self._cond.wait_for(lambda: self._seq > last_seq, timeout):
                return self._frame, self._seq
            return None, last_seq

    def stop(self):
        self._stopping.set()


class RTSPFrameSource(FrameSource):
    """The real perception source: the Amcrest RTSP feed through the same model +
    tracker as live.py, sharing perception.py so the two can't drift. Reads the
    camera password from MERLE_RTSP_PASS (never hardcoded) and the model from
    MERLE_MODEL (default models/current.pt). ultralytics is imported lazily here
    so the daemon and its tests stay importable without torch installed.

    Latency discipline (issue #29): the model is loaded AND warmed up before the
    RTSP connection opens (so no frames queue while CUDA initializes), and a
    FreshestFrameReader drains the stream from the moment it opens. read() hands
    the worker the newest frame every time; the old fixed 6-7s backlog can no
    longer form -- at startup or after any mid-run stall."""

    def __init__(self):
        from ultralytics import YOLO   # lazy: heavy, GPU-specific, camera-only path

        user = os.environ.get("MERLE_RTSP_USER", "admin")
        pw = os.environ.get("MERLE_RTSP_PASS")
        if not pw:
            raise RuntimeError("MERLE_RTSP_PASS is not set -- needed for the camera source.")
        self.host = os.environ.get("MERLE_RTSP_HOST", "192.168.1.102")
        self.url = (f"rtsp://{user}:{pw}@{self.host}:554"
                    "/cam/realmonitor?channel=1&subtype=0")
        # Force RTSP over TCP (UDP silently drops packets under load and smears
        # frames) and trim FFmpeg's own buffering: nobuffer skips the demuxer's
        # startup buffering, low_delay tells the decoder not to hold frames.
        # Must be set BEFORE the capture opens -- FFmpeg reads it once at open.
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS",
                              "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay")

        # Model first, camera second: YOLO(...) takes seconds and the first
        # inference pays CUDA init on top. With the stream already open that
        # wait used to pile up as queued frames -- the daemon's permanent 6-7s
        # delay. The dummy predict (not track: the tracker must see only real
        # frames) forces the one-time warmup cost before any frame can queue.
        self.model = YOLO(os.environ.get("MERLE_MODEL", "models/current.pt"))
        self.model.predict(np.zeros((360, 640, 3), np.uint8),
                           imgsz=perception.IMGSZ, quantize=perception.QUANTIZE,
                           verbose=False)
        self.names = self.model.names
        self.tm = perception.TrackMemory()

        cap = self._open()
        if not cap.isOpened():
            raise RuntimeError(f"Could not open the RTSP stream at {self.host}. Check "
                               "the camera is reachable and MERLE_RTSP_* are correct.")
        self._reader = FreshestFrameReader(cap, self._open, label=self.host)
        self._reader.start()
        self._seq = 0

    def _open(self):
        # NOTE: no CAP_PROP_BUFFERSIZE here -- it is silently ignored by the
        # FFmpeg backend (cap.set returns False). Low latency comes from the
        # FreshestFrameReader draining the queue, not from a buffer setting.
        return cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)

    def read(self):
        frame, self._seq = self._reader.next_frame(self._seq)
        if frame is None:
            return None, []
        results = self.model.track(frame, conf=perception.DETECT_FLOOR,
                                   imgsz=perception.IMGSZ,
                                   quantize=perception.QUANTIZE, persist=True,
                                   tracker=perception.TRACKER_YAML, verbose=False)
        live, coasting = self.tm.ingest(
            perception.extract_detections(results[0], self.names))
        dets = [Detection(tid, perception.voted(t), tuple(t["xyxy"]), t["conf"])
                for tid, t in live]
        dets += [Detection(tid, perception.voted(t), tuple(t["xyxy"]), t["conf"],
                           coasting=True) for tid, t in coasting]
        return frame, dets

    def close(self):
        # The reader owns the capture and releases it as its thread exits. If
        # it's wedged in a blocking read (dead network), the join times out and
        # the daemon-thread flag lets process exit reap it.
        self._reader.stop()
        self._reader.join(timeout=2)
