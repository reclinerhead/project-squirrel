# =============================================================================
# project-squirrel -- frames.py
#
# A frame SOURCE feeds the daemon (merle_daemon.py). It yields a raw frame plus
# the detections in it; the daemon does the annotation, encoding, and persistence
# the same way no matter where the frames came from. That seam is the point:
#
#   - SyntheticFrameSource  (here)          -- camera-free, for tests / dev / the
#                                              MCC frontend before the real feed
#   - TrackedStreamSource   (here)          -- any video stream through the real
#                                              model + tracker: the Amcrest over
#                                              RTSP ('driveway') or the rover's
#                                              MJPEG feed ('rover', issue #236)
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

from vision import perception

RECONNECT_INTERVAL = 3.0   # seconds between RTSP reconnect attempts
READ_TIMEOUT = 1.0         # seconds read() waits for a fresh frame before
                           # reporting "no signal" (the daemon shows its
                           # reconnecting veil while reads return None)

# Force RTSP over TCP (UDP silently drops packets under load and smears
# frames) and trim FFmpeg's own buffering: nobuffer skips the demuxer's
# startup buffering, low_delay tells the decoder not to hold frames.
# Must be in the environment BEFORE a capture opens -- FFmpeg reads it once at
# open. A module constant so the fixture recorder opens the stream with the
# exact same knobs as the daemon.
RTSP_FFMPEG_OPTIONS = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"


def rtsp_url():
    """The camera URL from MERLE_RTSP_* -> (url, redacted_url). ONE
    construction shared by the daemon source and the fixture recorder (issue
    #74 Phase 0: what stream we're on must never be a mystery, so there is
    exactly one place that decides it). subtype=0 is the Amcrest MAIN stream
    -- the sub-stream (subtype=1) is low-res and would starve distant-animal
    detection. The redacted twin is for logs and /state; the password never
    leaves the process."""
    user = os.environ.get("MERLE_RTSP_USER", "admin")
    pw = os.environ.get("MERLE_RTSP_PASS")
    if not pw:
        raise RuntimeError("MERLE_RTSP_PASS is not set -- needed for the camera source.")
    host = os.environ.get("MERLE_RTSP_HOST", "192.168.1.102")
    path = "/cam/realmonitor?channel=1&subtype=0"
    return (f"rtsp://{user}:{pw}@{host}:554{path}",
            f"rtsp://{user}:***@{host}:554{path}")


def rover_url():
    """The rover camera URL from MERLE_ROVER_STREAM -> (url, redacted_url). The
    Waveshare ugv app owns the rover's camera and serves it as HTTP MJPEG on
    :5000 (until the Helm B0 cutover, #203); the daemon consumes it read-only,
    like any browser tab would. No credentials in this URL, so the redacted
    twin is the URL itself -- returned as a pair anyway so both URL builders
    share one shape."""
    url = os.environ.get("MERLE_ROVER_STREAM", "http://merle:5000/video_feed")
    return url, url


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
    source that returns instantly (the synthetic one) won't spin the CPU.

    provenance() answers "what is this source actually connected to" (issue
    #74 Phase 0 -- stream, native resolution, imgsz, model, classes); the
    daemon logs it once and serves it on /state so it is never a mystery.
    metrics(fps) surfaces the tracker's churn numbers, or None for sources
    with no tracker."""

    def read(self):
        raise NotImplementedError

    def provenance(self):
        return {"source": type(self).__name__}

    def metrics(self, fps=15.0):
        return None

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

    def provenance(self):
        # The full shape the RTSP source serves, with the camera-only fields
        # honestly null -- the dashboard can render one schema for both worlds.
        return {"source": "synthetic", "url": None,
                "resolution": [self.w, self.h],
                "imgsz": None, "quantize": None, "target_fps": None,
                "model": None,
                "classes": ["squirrel", "chipmunk"]}

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


# The rover's app answers instantly when up, but a powered-off rover means TCP
# packets into the void -- FFmpeg's default open would sit there for minutes,
# and a source SWAP (issue #236) happens in the worker's own thread. Bound it:
# a switch to a dead rover must fail in seconds, not wedge perception.
ROVER_OPEN_TIMEOUT_MS = 4000

# Per-source frame rate + inference size (issue #238). fps is a source PROPERTY,
# not a global cap: the daemon paces the worker (and sizes recorded clips) to
# the ACTIVE source's rate, so the two cameras run at their own honest speeds.
DEFAULT_FPS = 15         # the safe default; the Amcrest is ~15fps native
ROVER_FPS = 30           # the rover's MJPEG feed measured at ~30fps (its whole
                         # point is smooth motion for a moving camera)
# The rover is 640x480 native, so perception.IMGSZ (1920, a 3x upscale tuned to
# rescue tiny distant animals in the Amcrest's 4K frame) buys no real detail and
# ~9x the GPU work -- exactly the headroom 30fps inference needs on a card shared
# with Ollama. 640 keeps the rover at its trained scale; revisit per the field
# once real animals are in view (the issue's measure-then-tune note).
ROVER_IMGSZ = 640

_MODEL = None   # (model, path) -- see load_model()


def load_model():
    """The shared YOLO model, loaded AND warmed up exactly once per process ->
    (model, model_path). Model first, camera second (issue #29): YOLO(...)
    takes seconds and the first inference pays CUDA init on top; with a stream
    already open that wait used to pile up as queued frames -- the daemon's
    permanent 6-7s delay. The dummy predict (not track: the tracker must see
    only real frames) forces the one-time warmup before any frame can queue.
    Cached at module level (issue #236) so a runtime source SWAP costs an
    open-the-capture, never a second model load. ultralytics is imported
    lazily so the daemon and its tests stay importable without torch."""
    global _MODEL
    if _MODEL is None:
        from ultralytics import YOLO   # lazy: heavy, GPU-specific, camera-only path
        model_path = os.environ.get("MERLE_MODEL", "models/current.pt")
        model = YOLO(model_path)
        model.predict(np.zeros((360, 640, 3), np.uint8),
                      imgsz=perception.IMGSZ, quantize=perception.QUANTIZE,
                      verbose=False)
        _MODEL = (model, model_path)
    return _MODEL


class TrackedStreamSource(FrameSource):
    """The real perception source: a video stream through the same model +
    tracker as live.py, sharing perception.py so the two can't drift. Which
    stream is the constructor's business (issue #236) -- the Amcrest over RTSP
    (driveway_source) and the rover's HTTP MJPEG feed (rover_source) are the
    same machinery with a different URL.

    Latency discipline (issue #29): the model is loaded AND warmed up before the
    stream connection opens (so no frames queue while CUDA initializes), and a
    FreshestFrameReader drains the stream from the moment it opens. read() hands
    the worker the newest frame every time; the old fixed 6-7s backlog can no
    longer form -- at startup or after any mid-run stall."""

    def __init__(self, source_id, url, redacted, open_params=None,
                 imgsz=None, target_fps=None):
        self.source_id = source_id
        self.url = url
        self._open_params = list(open_params or [])
        # Per-source inference size and frame rate (issue #238). imgsz is used
        # in read() and reported in provenance; target_fps is advertised for the
        # daemon to pace the worker and size recorded clips to THIS source.
        self.imgsz = imgsz if imgsz is not None else perception.IMGSZ
        self.target_fps = target_fps if target_fps is not None else DEFAULT_FPS
        # Read once at open by FFmpeg. rtsp_transport is meaningless for the
        # rover's HTTP feed (FFmpeg ignores options the demuxer doesn't know,
        # at worst with a log line); nobuffer/low_delay apply to both.
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", RTSP_FFMPEG_OPTIONS)

        self.model, model_path = load_model()
        self.names = self.model.names
        # A fresh source must start with a fresh tracker: the first track()
        # call goes out with persist=False, which makes ultralytics rebuild its
        # internal trackers instead of stitching this stream onto the previous
        # source's track state (verified in ultralytics' on_predict_start:
        # existing trackers are kept only when persist is set). Subsequent
        # calls persist as always.
        self._persist = False
        # Track lifecycle logging (issue #74, Phase 0): on by default, cheap
        # (a line per birth/stitch/death, not per frame); MERLE_TRACK_LOG=0
        # silences it.
        track_log = print if os.environ.get("MERLE_TRACK_LOG", "1") != "0" else None
        self.tm = perception.TrackMemory(log=track_log)

        # Runtime provenance (issue #74, Phase 0): exactly what this source is
        # connected to and how it infers -- logged by the daemon and served on
        # /state so the stream/imgsz question is never a mystery again.
        # Resolution starts as the capture's claim and is overwritten by the
        # first real frame (the one honest source; also tracks a mid-run
        # camera settings change).
        self._provenance = {
            "source": source_id, "url": redacted, "resolution": None,
            "imgsz": self.imgsz, "quantize": perception.QUANTIZE,
            "target_fps": self.target_fps,
            "model": model_path,
            "classes": [self.names[i] for i in sorted(self.names)],
        }

        cap = self._open()
        if not cap.isOpened():
            raise RuntimeError(f"Could not open the {source_id} stream at {redacted}. "
                               "Check the camera is reachable and its env vars are correct.")
        w, h = cap.get(cv2.CAP_PROP_FRAME_WIDTH), cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        if w and h:
            self._provenance["resolution"] = [int(w), int(h)]
        self._reader = FreshestFrameReader(cap, self._open, label=source_id)
        self._reader.start()
        self._seq = 0

    def _open(self):
        # NOTE: no CAP_PROP_BUFFERSIZE here -- it is silently ignored by the
        # FFmpeg backend (cap.set returns False). Low latency comes from the
        # FreshestFrameReader draining the queue, not from a buffer setting.
        return cv2.VideoCapture(self.url, cv2.CAP_FFMPEG, self._open_params)

    def provenance(self):
        return dict(self._provenance)

    def metrics(self, fps=15.0):
        return self.tm.metrics(fps)

    def read(self):
        frame, self._seq = self._reader.next_frame(self._seq)
        if frame is None:
            return None, []
        h, w = frame.shape[:2]
        if self._provenance["resolution"] != [w, h]:
            self._provenance["resolution"] = [w, h]
        results = self.model.track(frame, conf=perception.DETECT_FLOOR,
                                   imgsz=self.imgsz,
                                   quantize=perception.QUANTIZE,
                                   persist=self._persist,
                                   tracker=perception.TRACKER_YAML, verbose=False)
        self._persist = True
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


def driveway_source():
    """The Amcrest driveway cam over RTSP -- the original daemon source."""
    url, redacted = rtsp_url()
    return TrackedStreamSource("driveway", url, redacted)


def rover_source():
    """The rover's camera via the Waveshare app's MJPEG feed (issue #236),
    with a bounded open so switching to a powered-off rover fails in seconds
    (the worker then stays on its previous source) instead of wedging the
    perception loop for FFmpeg's default forever. Runs at 30fps / imgsz 640
    (issue #238): its own rate for smooth motion, its own inference size for
    the headroom to sustain it."""
    url, redacted = rover_url()
    return TrackedStreamSource("rover", url, redacted,
                               open_params=[cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
                                            ROVER_OPEN_TIMEOUT_MS],
                               imgsz=ROVER_IMGSZ, target_fps=ROVER_FPS)
