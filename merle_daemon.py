# =============================================================================
# project-squirrel -- merle_daemon.py
#
# Merle's brain-as-a-service: a long-running process that owns the camera, the
# perception loop, and the SQLite database, and exposes them over a small local
# HTTP API. The MCC (Next.js dashboard) is just a client of this -- it never
# touches the DB or filesystem directly.
#
#   GET  /state        live counts + tracks + fps, run totals, recent events,
#                      source provenance + tracker churn metrics (JSON)
#   GET  /stream       the annotated video as MJPEG (an <img src> in the browser)
#   GET  /snapshot     the latest annotated frame, one JPEG
#   GET  /history      N-day census + hard-frame trend + training runs (JSON)
#   GET  /history/day  hourly arrivals for one date (JSON)
#   POST /control      start/stop the loop, toggle recording, set the crowd threshold
#
# Events also go out live on the MQTT bus (bus.py, topic driveway/events) for
# decoupled subscribers -- narrators, dashboards, future rover processes. SQLite
# stays the durable archive; the bus is the live transport, and the daemon runs
# fine (just unnarrated) when no broker is up. Each arrival/departure/
# crowd_snapshot also ships its still shot -- the annotated frame the event
# fired on -- to driveway/frames/<frame_id>/{full,thumb} (issue #90), where
# frame_archiver on pearl files it for the Field Journal.
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
CLASS_NAMES = {0: "squirrel", 1: "turkey"}
CLASS_COLORS = perception.class_colors(CLASS_NAMES)
# The roster /state advertises so the dashboard can render a fixed row per
# class (stable panel geometry) instead of only the species currently counted.
SPECIES = [CLASS_NAMES[i] for i in sorted(CLASS_NAMES)]

TARGET_FPS = 15          # cap the loop; the real camera runs ~15fps anyway
CROWD_COOLDOWN = 10.0    # seconds between crowd-snapshot events, like live.py
NO_FRAME_RETRY = 0.25    # pause between reads while the source has no frame
STREAM_WIDTH = 1920      # /stream rides a downscaled copy: the camera's 4K
                         # JPEGs are ~1.7MB each, which at 15fps is ~26MB/s
                         # through the MCC proxy on pearl PER TAB (issue #49).
                         # 1080p is a quarter of that; /snapshot stays full-res.
THUMB_WIDTH = 320        # the event still shot's thumbnail (issue #90): the
                         # daemon encodes it because it owns cv2 -- consumers
                         # (archiver, MCC) stay image-dep-free.

# Arrival/departure debounce (SPECIES-level, not track-level). ByteTrack mints a
# new track id when it loses an animal for more than its buffer and re-acquires
# it -- same squirrel, new identity. Track-level events turned every one of
# those into a phantom departure+arrival pair. Species counts don't care which
# id is which; they only dip briefly during churn, and the debounce absorbs it.
# The machinery (and the canonical defaults) live in perception.SpeciesPresence
# since issue #74 so the offline fixture runner replays the exact same logic;
# re-exported here as the names the tests monkeypatch.
ARRIVE_AFTER = perception.ARRIVE_AFTER_S
DEPART_AFTER = perception.DEPART_AFTER_S


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


def encode_stream_jpeg(annotated):
    """The /stream copy of an annotated frame: downscaled to STREAM_WIDTH (when
    the source is wider) and JPEG-encoded. Returns bytes, or None if the encode
    fails. The full-res encode for /snapshot happens separately."""
    h, w = annotated.shape[:2]
    if w > STREAM_WIDTH:
        annotated = cv2.resize(
            annotated, (STREAM_WIDTH, round(h * STREAM_WIDTH / w)),
            interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


def encode_thumb_jpeg(annotated):
    """The event still shot's thumbnail (issue #90): the annotated frame at
    ~THUMB_WIDTH, JPEG-encoded. Returns bytes, or None if the encode fails."""
    h, w = annotated.shape[:2]
    if w > THUMB_WIDTH:
        annotated = cv2.resize(
            annotated, (THUMB_WIDTH, round(h * THUMB_WIDTH / w)),
            interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else None


def mint_frame_id(session_id, ts, kind, n):
    """The id tying an event to its still shot (issue #90): session + compact
    timestamp + kind + a per-session counter (two events can fire on the same
    frame -- a squirrel and a turkey arriving together -- and "same second,
    same kind" isn't impossible either). Filesystem-safe BY CONSTRUCTION:
    session ids are %Y%m%d_%H%M%S, kinds are lowercase identifiers, and the
    ISO timestamp's separators reduce to [T:-] -- stripped here. The archiver
    still sanitizes independently (never trust the wire), but nothing this
    mints should ever trip it."""
    stamp = ts.replace("-", "").replace(":", "")
    return f"{session_id}_{stamp}_{kind}_{n:04d}"


def next_stream_part(jpeg, seq, last_seq):
    """One tick of the /stream generator's decision: send only frames the
    worker hasn't already sent to this client (issue #49 -- the stream used to
    re-send the last frame at TARGET_FPS forever, so a stood-down station still
    pushed ~26MB/s of identical frozen frames). Returns (part_or_None, last_seq
    to carry forward). A new client passes last_seq=-1, so its first tick sends
    the current frame immediately -- a tab opened during stand-down shows the
    last frame, not a broken image."""
    if jpeg is None or seq == last_seq:
        return None, last_seq
    return mjpeg_frame(jpeg), seq


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
        self.jpeg = None            # latest annotated frame, JPEG bytes (full-res, /snapshot)
        self.stream_jpeg = None     # same frame downscaled for /stream (issue #49)
        self.seq = 0                # bumps per published frame; /stream sends only on change
        self.counts = {}            # live per-class counts
        self.tracks = []            # live tracks: [{track_id, species, conf, box}]
        self.fps = 0.0
        self.signal = True          # is the source currently delivering frames?
        self.provenance = {}        # what the source is connected to (issue #74)
        self.churn = None           # tracker churn metrics, None for trackerless sources


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
        self._frame_seq = 0      # per-session counter baked into frame_ids
        self._loop_times = deque(maxlen=30)   # wall-clock interval between loops
        self._prev_loop = None
        # The species-level event debounce, now shared logic in perception.py
        # (issue #74 Phase 0.5 -- the offline fixture runner replays the exact
        # same machine). The constants stay module-level here so tests can
        # monkeypatch them; they're read at worker construction, which happens
        # at lifespan startup, after any patching.
        self.presence = perception.SpeciesPresence(ARRIVE_AFTER, DEPART_AFTER)
        self._prov_logged = False   # one [provenance] line, once frames flow
        self._churn_at = 0.0        # last provenance/churn refresh (~1/s is plenty)
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

    def _frame_event(self, ts, kind, details):
        """An event that gets a still shot (issue #90): mint the frame_id and
        ride it in `details` -- so SQLite archives the id (per the no-blobs
        rule) and the bus event carries it to the narrator -- then record and
        publish as usual. The JPEG bytes go out later in the same loop pass,
        once the stream copy is encoded (the caller collects the returned id):
        the event fires before the frame is encoded, and the still must be the
        frame the event fired on, not the previous loop's."""
        self._frame_seq += 1
        frame_id = mint_frame_id(self.state.session_id, ts, kind, self._frame_seq)
        self._event(ts, kind, {**details, "frame_id": frame_id})
        return frame_id

    def _publish_frames(self, frame_ids, annotated, stream_jpeg):
        """The still-shot bytes for every event this loop fired: the annotated
        stream-downscaled JPEG (already encoded for /stream -- near-zero extra
        cost) as `full`, plus a ~THUMB_WIDTH thumbnail. Fire-and-forget, same
        ethos as events: a dropped frame (broker down, encode failure) is a
        moment nobody archived -- the event row still exists, frame_id and
        all -- never a lost record."""
        if not frame_ids or stream_jpeg is None:
            return
        thumb = encode_thumb_jpeg(annotated)
        for frame_id in frame_ids:
            self.publisher.publish_bytes(bus.frame_topic(frame_id, "full"),
                                         stream_jpeg)
            if thumb is not None:
                self.publisher.publish_bytes(bus.frame_topic(frame_id, "thumb"),
                                             thumb)

    def _refresh_diagnostics(self, now):
        """Pull the source's provenance + churn metrics into SharedState about
        once a second (issue #74, Phase 0), and log the provenance ONCE when
        the native resolution is first known -- the startup line that settles
        the which-stream/which-imgsz question for good. getattr-defensive:
        test fakes are duck-typed with only read()/close()."""
        if now - self._churn_at < 1.0:
            return
        self._churn_at = now
        prov = getattr(self.source, "provenance", dict)()
        fps = self.state.fps or TARGET_FPS
        churn = getattr(self.source, "metrics", lambda fps: None)(fps)
        with self.state.lock:
            self.state.provenance = prov
            self.state.churn = churn
        if not self._prov_logged and prov.get("resolution"):
            res = prov["resolution"]
            print(f"[provenance] source={prov.get('source')} url={prov.get('url')} "
                  f"native={res[0]}x{res[1]} imgsz={prov.get('imgsz')} "
                  f"quantize={prov.get('quantize')} model={prov.get('model')} "
                  f"classes={prov.get('classes')}")
            self._prov_logged = True

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

            # Everything COUNTED counts matched tracks only -- a coasting ghost
            # is a briefly-lost track (often the just-re-minted twin of a live
            # one), not another animal. Coasting boxes still draw and still ride
            # /state's `tracks` list; they just don't tally.
            present = [d for d in dets if not d.coasting]

            # Arrivals and departures -- the narrator's bread and butter.
            # Species-level and debounced: perception.SpeciesPresence.
            # Each carries a frame_id (issue #90); the still-shot bytes
            # publish below, once this frame's stream copy is encoded.
            now = time.time()
            frame_ids = []
            for kind, details in self.presence.observe(
                    Counter(d.species for d in present), now):
                frame_ids.append(self._frame_event(ts, kind, details))

            # Crowd moment: enough animals at once, and cooled down since the last.
            if len(present) >= self.control.crowd_threshold and now - self._last_crowd >= CROWD_COOLDOWN:
                frame_ids.append(self._frame_event(
                    ts, "crowd_snapshot",
                    {"total": len(present),
                     "counts": dict(Counter(d.species for d in present))}))
                self._last_crowd = now

            self._refresh_diagnostics(now)

            annotated = annotate(frame, dets)
            self._record(annotated, ts)
            ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
            stream_jpeg = encode_stream_jpeg(annotated)
            self._publish_frames(frame_ids, annotated, stream_jpeg)

            counts = dict(Counter(d.species for d in present))
            tracks = [{"track_id": d.track_id, "species": d.species,
                       "conf": round(d.conf, 3), "box": list(d.box),
                       "coasting": d.coasting} for d in dets]
            with self.state.lock:
                self.state.signal = True
                if ok:
                    self.state.jpeg = buf.tobytes()
                if stream_jpeg is not None:
                    self.state.stream_jpeg = stream_jpeg
                    self.state.seq += 1
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
            "totals": storage.species_totals(
                conn, session_id, min_frames=perception.CENSUS_AFTER_FRAMES),
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
            provenance = dict(state.provenance)
            churn = state.churn
        return {
            "session_id": session_id,
            "running": control.running,
            "recording": control.recording,
            "crowd_threshold": control.crowd_threshold,
            "species": SPECIES,
            "live": live,
            # Issue #74, Phase 0: what the source is connected to (stream,
            # native resolution, imgsz, model, classes) and the tracker churn
            # metrics all Phase 2 changes are graded against. churn is None
            # for trackerless sources (synthetic).
            "provenance": provenance,
            "churn": churn,
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
        # never notices the client left). Sends the downscaled copy, and only
        # when the worker has published a NEW frame -- see next_stream_part.
        async def gen():
            last_seq = -1
            while not await request.is_disconnected():
                with state.lock:
                    jpeg, seq = state.stream_jpeg, state.seq
                part, last_seq = next_stream_part(jpeg, seq, last_seq)
                if part is not None:
                    yield part
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
            census = storage.census_by_day(
                conn, days=days, today=today,
                min_frames=perception.CENSUS_AFTER_FRAMES)
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
            hours = storage.day_hours(
                conn, day, min_frames=perception.CENSUS_AFTER_FRAMES)
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
