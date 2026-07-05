# =============================================================================
# project-squirrel -- merle_daemon.py
#
# Merle's brain-as-a-service: a long-running process that owns the camera, the
# perception loop, and the SQLite database, and exposes them over a small local
# HTTP API. The MCC (Next.js dashboard) is just a client of this -- it never
# touches the DB or filesystem directly.
#
#   GET  /state     live counts + tracks + fps, run totals, recent events (JSON)
#   GET  /stream    the annotated video as MJPEG (an <img src> in the browser)
#   GET  /snapshot  the latest annotated frame, one JPEG
#   POST /control   start/stop the loop, toggle recording, set the crowd threshold
#
# Phase 2b-i: this runs against a SYNTHETIC frame source (frames.py) so the whole
# HTTP surface and DB wiring work with no camera -- testable in CI, and something
# the MCC frontend can be built against. Phase 2b-ii swaps in the real
# RTSP + YOLO + ByteTrack source behind the same FrameSource interface.
#
# Run it:  uvicorn merle_daemon:app        (MERLE_DB overrides the db path)
# =============================================================================

import asyncio
import os
import threading
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from datetime import datetime

import cv2
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

import perception
import storage
from frames import SyntheticFrameSource

# Per-class colors from the shared palette (same ordering as live.py) so the
# daemon stream reads the same as the desktop window.
CLASS_COLORS = perception.class_colors({0: "chipmunk", 1: "squirrel", 2: "turkey"})

TARGET_FPS = 15          # cap the loop; the real camera runs ~15fps anyway
CROWD_COOLDOWN = 10.0    # seconds between crowd-snapshot events, like live.py


def mjpeg_frame(jpeg):
    """Wrap one JPEG as a multipart/x-mixed-replace part for the MJPEG stream."""
    return b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"


def annotate(frame, dets):
    """Draw boxes + labels via the shared drawer, so the stream matches live.py
    (coasting tracks grey; annotations scaled for the frame size, since it's
    tuned for 4K)."""
    items = [(d.track_id, d.species, d.box, not d.coasting) for d in dets]
    scale = frame.shape[0] / 2160
    return perception.draw_tracks(frame, items, CLASS_COLORS, scale=scale)


class Control:
    """Mutable knobs the /control endpoint flips. Plain object guarded by the
    worker reading it each loop -- no lock needed for these simple scalars."""

    def __init__(self):
        self.running = True
        self.recording = False
        self.crowd_threshold = 5


class SharedState:
    """The latest annotated frame + live readout, written by the worker and read
    by request handlers. One lock guards all of it."""

    def __init__(self, session_id):
        self.lock = threading.Lock()
        self.session_id = session_id
        self.jpeg = None            # latest annotated frame, JPEG bytes
        self.counts = {}            # live per-class counts
        self.tracks = []            # live tracks: [{track_id, species, conf, box}]
        self.fps = 0.0


class Worker(threading.Thread):
    """The perception loop, headless. Pulls frames from the source, annotates,
    encodes, publishes to SharedState, and persists sightings/events to SQLite."""

    def __init__(self, source, state, conn, control):
        super().__init__(daemon=True)
        self.source = source
        self.state = state
        self.conn = conn
        self.control = control
        self._stop = threading.Event()
        self._last_crowd = 0.0
        self._loop_times = deque(maxlen=30)   # wall-clock interval between loops
        self._prev_loop = None

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            # Measure the true loop rate: wall-clock between the START of each
            # loop, which includes the pacing sleep below. (Timing only the work
            # would report the potential fps, not the actual throughput.)
            now_perf = time.perf_counter()
            if self._prev_loop is not None:
                self._loop_times.append(now_perf - self._prev_loop)
            self._prev_loop = now_perf
            t0 = now_perf

            if not self.control.running:
                time.sleep(0.05)
                continue

            frame, dets = self.source.read()
            if frame is None:
                break

            ts = datetime.now().isoformat(timespec="seconds")
            for d in dets:
                if d.coasting:
                    continue   # bank only frames the animal was actually matched
                storage.upsert_sighting(self.conn, self.state.session_id,
                                        d.track_id, d.species, ts, d.conf)

            # Crowd moment: enough animals at once, and cooled down since the last.
            now = time.time()
            if len(dets) >= self.control.crowd_threshold and now - self._last_crowd >= CROWD_COOLDOWN:
                counts = dict(Counter(d.species for d in dets))
                storage.record_event(self.conn, ts, "crowd_snapshot",
                                     {"total": len(dets), "counts": counts})
                self._last_crowd = now

            annotated = annotate(frame, dets)
            ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])

            counts = dict(Counter(d.species for d in dets))
            tracks = [{"track_id": d.track_id, "species": d.species,
                       "conf": round(d.conf, 3), "box": list(d.box),
                       "coasting": d.coasting} for d in dets]
            with self.state.lock:
                if ok:
                    self.state.jpeg = buf.tobytes()
                self.state.counts = counts
                self.state.tracks = tracks
                if len(self._loop_times) > 1:
                    self.state.fps = round(len(self._loop_times) / sum(self._loop_times), 1)

            # Pace to TARGET_FPS (measured into the next loop's interval above).
            dt = time.perf_counter() - t0
            time.sleep(max(0.0, 1.0 / TARGET_FPS - dt))


class ControlCommand(BaseModel):
    action: str                      # start | stop | record_on | record_off | set_crowd_threshold
    value: int | None = None


def create_app(source, conn):
    """Build the FastAPI app around a frame source and an open DB connection.
    Factored out so tests can pass a synthetic source + in-memory DB."""
    storage.seed_training_runs(conn)
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    state = SharedState(session_id)
    control = Control()
    worker = Worker(source, state, conn, control)

    @asynccontextmanager
    async def lifespan(app):
        worker.start()
        yield
        worker.stop()
        worker.join(timeout=2)
        source.close()

    app = FastAPI(title="Merle daemon", lifespan=lifespan)
    # Exposed for tests to reach the worker/control directly.
    app.state.shared = state
    app.state.control = control

    @app.get("/state")
    def get_state():
        with state.lock:
            live = {"counts": dict(state.counts),
                    "tracks": list(state.tracks),
                    "fps": state.fps}
        return {
            "session_id": session_id,
            "running": control.running,
            "recording": control.recording,
            "crowd_threshold": control.crowd_threshold,
            "live": live,
            "totals": storage.species_totals(conn, session_id),
            "recent_events": storage.recent_events(conn, 10),
        }

    @app.get("/snapshot")
    def snapshot():
        with state.lock:
            jpeg = state.jpeg
        if jpeg is None:
            return Response(status_code=503, content="no frame yet")
        return Response(jpeg, media_type="image/jpeg")

    @app.get("/stream")
    async def stream(request: Request):
        # multipart/x-mixed-replace: the browser keeps the connection open and
        # swaps in each new JPEG -- an <img src="/stream"> just works, no player.
        # Async + an is_disconnected() check so a closed tab frees the generator
        # promptly instead of looping forever (a sync generator blocked in sleep
        # never notices the client left).
        async def gen():
            while not await request.is_disconnected():
                with state.lock:
                    jpeg = state.jpeg
                if jpeg is not None:
                    yield mjpeg_frame(jpeg)
                await asyncio.sleep(1.0 / TARGET_FPS)
        return StreamingResponse(
            gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    @app.post("/control")
    def post_control(cmd: ControlCommand):
        if cmd.action == "start":
            control.running = True
        elif cmd.action == "stop":
            control.running = False
        elif cmd.action == "record_on":
            control.recording = True
        elif cmd.action == "record_off":
            control.recording = False
        elif cmd.action == "set_crowd_threshold":
            if cmd.value is None or cmd.value < 1:
                return Response(status_code=422, content="value must be >= 1")
            control.crowd_threshold = cmd.value
        else:
            return Response(status_code=422, content=f"unknown action: {cmd.action}")
        return {"running": control.running, "recording": control.recording,
                "crowd_threshold": control.crowd_threshold}

    return app


def make_source():
    """Pick the frame source from MERLE_SOURCE: 'camera' for the real Amcrest
    feed (lazy-imports ultralytics), anything else (default) for the synthetic
    source. The synthetic default keeps `uvicorn merle_daemon:app`, tests, and
    MCC frontend work camera-free."""
    if os.environ.get("MERLE_SOURCE") == "camera":
        from frames import RTSPFrameSource
        return RTSPFrameSource()
    return SyntheticFrameSource()


# Module-level app for `uvicorn merle_daemon:app`.
app = create_app(make_source(), storage.connect(os.environ.get("MERLE_DB", "merle.db")))
