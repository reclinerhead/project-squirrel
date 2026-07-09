# =============================================================================
# project-squirrel -- merle_daemon.py
#
# Merle's brain-as-a-service: a long-running process that owns the camera, the
# perception loop, and the SQLite database, and exposes them over a small local
# HTTP API. The MCC (Next.js dashboard) is just a client of this -- it never
# touches the DB or filesystem directly.
#
#   GET  /state        live counts + tracks + fps, run totals, recent events (JSON)
#   GET  /stream       the annotated video as MJPEG (an <img src> in the browser)
#   GET  /snapshot     the latest annotated frame, one JPEG
#   GET  /history      N-day census + hard-frame trend + training runs (JSON)
#   GET  /history/day  hourly arrivals for one date (JSON)
#   POST /control      start/stop the loop, toggle recording, set the crowd threshold
#
# Events also go out live on the MQTT bus (bus.py, topic driveway/events) for
# decoupled subscribers -- narrators, dashboards, future rover processes. SQLite
# stays the durable archive; the bus is the live transport, and the daemon runs
# fine (just unnarrated) when no broker is up.
#
# The frame source is selected by MERLE_SOURCE: 'camera' (default, the real
# RTSP + YOLO + ByteTrack feed) or 'synthetic' (camera-free, used by tests/CI
# and frontend work).
#
# Run it:  uvicorn merle_daemon:app        (MERLE_DB overrides the db path;
#                                           MERLE_MQTT -- required -- points at
#                                           the broker on pearl, see bus.py)
# =============================================================================

import asyncio
import os
import threading
import time
from collections import Counter, deque
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

import cv2
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

import bus
import perception
import storage
from frames import SyntheticFrameSource

# Per-class colors from the shared palette so the daemon stream reads the same
# as the desktop window. Colors are keyed by name in perception.SPECIES_COLORS,
# so this list only has to name the species the model emits (2-class:
# squirrel/turkey) -- the indices are immaterial. live.py derives the same map
# straight from model.names.
CLASS_COLORS = perception.class_colors({0: "squirrel", 1: "turkey"})

TARGET_FPS = 15          # cap the loop; the real camera runs ~15fps anyway
CROWD_COOLDOWN = 10.0    # seconds between crowd-snapshot events, like live.py
NO_FRAME_RETRY = 0.25    # pause between reads while the source has no frame

# Arrival/departure debounce (SPECIES-level, not track-level). ByteTrack mints a
# new track id when it loses an animal for more than its buffer and re-acquires
# it -- same squirrel, new identity. Track-level events turned every one of
# those into a phantom departure+arrival pair. Species counts don't care which
# id is which; they only dip briefly during churn, and the debounce absorbs it:
ARRIVE_AFTER = 2.0       # a count INCREASE must hold this long to be an arrival
DEPART_AFTER = 12.0      # a DECREASE must hold this long -- longer than any
                         # realistic churn gap, so lost-and-reminted ids never
                         # read as leave-and-return


HARD_FRAMES_DIR = "hard_frames"   # live.py's training harvest; the daemon only counts it


def hard_frames_by_day(days, today, folder=None):
    """Hard-frame harvest counts per day -- [{"date", "n"}], oldest first,
    padded like the census so the two charts line up. Counted from file mtimes
    in hard_frames/ (live.py banks them there; there's no DB record), so this
    works no matter which process did the banking. Missing folder = zeros."""
    end = date.fromisoformat(today)
    window = [(end - timedelta(days=d)).isoformat() for d in range(days - 1, -1, -1)]
    counts = dict.fromkeys(window, 0)
    root = folder if folder is not None else HARD_FRAMES_DIR
    if os.path.isdir(root):
        for entry in os.scandir(root):
            if entry.is_file() and entry.name.lower().endswith(".jpg"):
                day = date.fromtimestamp(entry.stat().st_mtime).isoformat()
                if day in counts:
                    counts[day] += 1
    return [{"date": d, "n": counts[d]} for d in window]


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
        self.signal = True          # is the source currently delivering frames?


class Worker(threading.Thread):
    """The perception loop, headless. Pulls frames from the source, annotates,
    encodes, publishes to SharedState, and persists sightings/events to SQLite."""

    def __init__(self, source, state, conn, control, db_lock, publisher):
        super().__init__(daemon=True)
        self.source = source
        self.state = state
        self.conn = conn
        self.control = control
        self.db_lock = db_lock   # serialize DB access with the request threads
        self.publisher = publisher   # bus.EventPublisher (or a test fake)
        self._stop = threading.Event()
        self._last_crowd = 0.0
        self._loop_times = deque(maxlen=30)   # wall-clock interval between loops
        self._prev_loop = None
        self._species = {}   # species -> presence state, see _species_presence
        self.writer = None                    # cv2.VideoWriter while recording
        self.clip_path = None

    def stop(self):
        self._stop.set()

    def _event(self, ts, kind, details):
        """Every notable moment goes two places: SQLite (the durable archive,
        what /state and history read) and the bus (the live transport narrators
        and dashboards subscribe to). One helper so the two can't diverge."""
        with self.db_lock:
            storage.record_event(self.conn, ts, kind, details)
        self.publisher.publish(bus.EVENTS_TOPIC,
                               {"ts": ts, "kind": kind, "details": details})

    def _species_presence(self, counts, ts, now):
        """Debounced species-level arrival/departure. A species' observed count
        must hold at a new value for ARRIVE_AFTER (up) or DEPART_AFTER (down)
        before the change is announced; any wobble back to the announced count
        resets the timer. Tracker id churn (same animal re-minted under a new
        id after a detection gap) dips a count for a few seconds at most, so it
        produces NO events -- which is the whole point. `duration_s` rides on a
        departure only when the last one leaves (counts above zero can't know
        which individual left)."""
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
            wait = ARRIVE_AFTER if observed > st["count"] else DEPART_AFTER
            if now - st["candidate_since"] < wait:
                continue
            old = st["count"]
            st["count"] = observed
            st["candidate_since"] = None
            if observed > old:
                if old == 0:
                    st["present_since"] = now
                self._event(ts, "arrival", {"species": sp, "count": observed})
            else:
                details = {"species": sp, "count": observed}
                if observed == 0 and st["present_since"] is not None:
                    details["duration_s"] = round(now - st["present_since"], 1)
                    st["present_since"] = None
                self._event(ts, "departure", details)

    def _record(self, annotated, ts):
        """Drive the clip recorder off control.recording. Records the ANNOTATED
        stream (boxes and all): the dashboard's clips are for watching/sharing a
        moment. live.py's V key still writes RAW clips, which is what you sample
        for training stills. All handled in this one worker thread, so the
        VideoWriter is never touched cross-thread."""
        if self.control.recording:
            if self.writer is None:
                os.makedirs("debug_frames", exist_ok=True)
                self.clip_path = f"debug_frames/clip_{datetime.now():%Y%m%d_%H%M%S}.mp4"
                h, w = annotated.shape[:2]
                writer = cv2.VideoWriter(self.clip_path,
                                         cv2.VideoWriter_fourcc(*"mp4v"),
                                         TARGET_FPS, (w, h))
                if not writer.isOpened():
                    print(f"Recording failed to open: {self.clip_path}")
                    self.control.recording = False   # flip back so /state is honest
                    self.clip_path = None
                    return
                self.writer = writer
            self.writer.write(annotated)
        elif self.writer is not None:
            self._finish_clip(ts)

    def _finish_clip(self, ts):
        self.writer.release()
        self._event(ts, "clip_recorded", {"path": self.clip_path})
        print(f"Clip saved: {self.clip_path}")
        self.writer = None
        self.clip_path = None

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
                # No frame this cycle -- the source is reconnecting (e.g. the
                # camera restarted after a settings change). Flag "no signal" and
                # keep the loop ALIVE; a single dropped read must never kill
                # perception (that used to freeze the feed until a full restart).
                # The source reconnects itself; we just keep asking.
                with self.state.lock:
                    self.state.signal = False
                time.sleep(NO_FRAME_RETRY)
                continue

            ts = datetime.now().isoformat(timespec="seconds")
            with self.db_lock:
                for d in dets:
                    if d.coasting:
                        continue   # bank only frames the animal was actually matched
                    storage.upsert_sighting(self.conn, self.state.session_id,
                                            d.track_id, d.species, ts, d.conf)

            # Arrivals and departures -- the narrator's bread and butter.
            # Species-level, from MATCHED tracks only (a coasting ghost is a
            # briefly-lost track, not a new animal): see _species_presence.
            now = time.time()
            self._species_presence(
                Counter(d.species for d in dets if not d.coasting), ts, now)

            # Crowd moment: enough animals at once, and cooled down since the last.
            if len(dets) >= self.control.crowd_threshold and now - self._last_crowd >= CROWD_COOLDOWN:
                counts = dict(Counter(d.species for d in dets))
                self._event(ts, "crowd_snapshot",
                            {"total": len(dets), "counts": counts})
                self._last_crowd = now

            annotated = annotate(frame, dets)
            self._record(annotated, ts)
            ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])

            counts = dict(Counter(d.species for d in dets))
            tracks = [{"track_id": d.track_id, "species": d.species,
                       "conf": round(d.conf, 3), "box": list(d.box),
                       "coasting": d.coasting} for d in dets]
            with self.state.lock:
                self.state.signal = True
                if ok:
                    self.state.jpeg = buf.tobytes()
                self.state.counts = counts
                self.state.tracks = tracks
                if len(self._loop_times) > 1:
                    self.state.fps = round(len(self._loop_times) / sum(self._loop_times), 1)

            # Pace to TARGET_FPS (measured into the next loop's interval above).
            dt = time.perf_counter() - t0
            time.sleep(max(0.0, 1.0 / TARGET_FPS - dt))

        # Loop ended (stop or lost feed): close any open clip so it isn't left
        # truncated.
        if self.writer is not None:
            self._finish_clip(datetime.now().isoformat(timespec="seconds"))


class ControlCommand(BaseModel):
    action: str                      # start | stop | record_on | record_off | set_crowd_threshold
    value: int | None = None


def _read_db_summary(conn, session_id, db_lock):
    """The DB-backed part of /state, read under the shared lock."""
    with db_lock:
        return {
            "totals": storage.species_totals(conn, session_id),
            "recent_events": storage.recent_events(conn, 10),
        }


def create_app(source, conn, publisher=None):
    """Build the FastAPI app around a frame source and an open DB connection.
    Factored out so tests can pass a synthetic source + in-memory DB + a fake
    publisher (asserting on bus traffic without a broker). The default publisher
    is resilient to a missing broker, so the daemon runs fine without Mosquitto
    -- events then live only in SQLite and nobody narrates them."""
    storage.seed_training_runs(conn)
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    state = SharedState(session_id)
    control = Control()
    # One SQLite connection is shared between the worker thread (writing every
    # frame) and the request threadpool (reading /state). A single connection is
    # NOT safe for concurrent use across threads -- check_same_thread=False only
    # silences the guard, it doesn't serialize access, and the race surfaces as
    # "sqlite3.InterfaceError: bad parameter or other API misuse". This lock is
    # held around every DB access so no two threads touch the connection at once.
    db_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(app):
        # `source` may be a FrameSource (tests pass one directly) OR a zero-arg
        # factory. The runtime app passes a factory so that IMPORTING this module
        # never opens the camera or loads the model -- that only happens here, at
        # server startup, which pytest/CI never trigger for the module-level app.
        # Same deal for the bus connection: built here, not at import.
        pub = bus.EventPublisher("merle-daemon").start() if publisher is None else publisher
        src = source() if callable(source) else source
        worker = Worker(src, state, conn, control, db_lock, pub)
        worker.start()
        yield
        worker.stop()
        worker.join(timeout=2)
        src.close()
        if publisher is None:
            pub.close()

    app = FastAPI(title="Merle daemon", lifespan=lifespan)
    # Exposed for tests to reach the worker/control directly.
    app.state.shared = state
    app.state.control = control

    @app.get("/state")
    def get_state():
        with state.lock:
            live = {"counts": dict(state.counts),
                    "tracks": list(state.tracks),
                    "fps": state.fps,
                    "signal": state.signal}
        return {
            "session_id": session_id,
            "running": control.running,
            "recording": control.recording,
            "crowd_threshold": control.crowd_threshold,
            "live": live,
            **_read_db_summary(conn, session_id, db_lock),
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

    @app.get("/history")
    def history(days: int = 14):
        # The Station Records feed: everything the history panels need in one
        # fetch (it's on-demand + slow-poll, not the 1s /state loop). Clamped so
        # a typo'd query can't ask SQLite to bucket ten years.
        days = max(1, min(days, 90))
        today = date.today().isoformat()
        with db_lock:
            census = storage.census_by_day(conn, days=days, today=today)
            runs = storage.training_runs(conn)
        return {"census": census,
                "hard_frames": hard_frames_by_day(days, today),
                "training_runs": runs}

    @app.get("/history/day")
    def history_day(day: str):
        try:
            date.fromisoformat(day)
        except ValueError:
            return Response(status_code=422, content=f"not an ISO date: {day}")
        with db_lock:
            hours = storage.day_hours(conn, day)
        return {"date": day, "hours": hours}

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
    """Pick the frame source from MERLE_SOURCE. Default is 'camera' (the real
    Amcrest feed) so `uvicorn merle_daemon:app` just works day to day; set
    MERLE_SOURCE=synthetic for the camera-free test world. Called at startup, not
    at import, so importing this module never opens the camera or loads the model."""
    if os.environ.get("MERLE_SOURCE", "camera") == "synthetic":
        return SyntheticFrameSource()
    from frames import RTSPFrameSource   # lazy: heavy + camera-only
    return RTSPFrameSource()


# Module-level app for `uvicorn merle_daemon:app`. make_source is passed as a
# FACTORY (not called here) so import stays side-effect-free; the lifespan calls
# it at startup.
app = create_app(make_source, storage.connect(os.environ.get("MERLE_DB", "merle.db")))
