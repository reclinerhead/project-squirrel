# Tests for merle_daemon.py -- the FastAPI daemon, driven by the synthetic frame
# source against an in-memory DB. No camera, no model: this is exactly the
# surface the MCC talks to, verified end to end. Entering the TestClient context
# manager runs the app's lifespan, which starts the worker thread.

import re
import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

import bus
import merle_daemon
import perception
import storage
from frames import SyntheticFrameSource, Detection


class _FakePublisher:
    """Stands in for bus.EventPublisher: records every publish so tests can
    assert on bus traffic without a broker (and CI never opens a socket)."""

    def __init__(self):
        self.messages = []   # [(topic, payload_dict), ...]
        self.raw = []        # [(topic, bytes), ...] -- the frame topics (#90)

    def publish(self, topic, payload):
        self.messages.append((topic, payload))

    def publish_bytes(self, topic, payload):
        self.raw.append((topic, payload))


def _app(source, publisher=None):
    return merle_daemon.create_app(source, storage.connect(":memory:"),
                                   publisher=publisher or _FakePublisher())


@pytest.fixture
def client():
    app = _app(SyntheticFrameSource())
    with TestClient(app) as c:
        _wait_for_first_frame(c)
        yield c


def _wait_for_first_frame(c, tries=100):
    """The worker needs a beat to produce the first frame after startup."""
    for _ in range(tries):
        if c.get("/state").json()["live"]["counts"]:
            return
        time.sleep(0.02)
    raise AssertionError("worker never produced a frame")


def test_state_shape_and_live_counts(client):
    body = client.get("/state").json()
    for key in ("session_id", "running", "crowd_threshold", "species", "live", "totals", "recent_events"):
        assert key in body
    # The class roster rides along so the dashboard can render a fixed row per
    # class (stable panel geometry) even for species not currently counted.
    assert body["species"] == ["squirrel", "turkey"]
    # The synthetic source always has two squirrels on screen.
    assert body["live"]["counts"].get("squirrel") == 2
    assert body["running"] is True


def test_state_carries_provenance_and_churn(client):
    # Issue #74, Phase 0: /state answers "what is the daemon actually watching"
    # (never a mystery again) and carries the tracker churn metrics -- honestly
    # None in the trackerless synthetic world.
    body = client.get("/state").json()
    prov = body["provenance"]
    assert prov["source"] == "synthetic"
    assert prov["resolution"] == [1280, 720]
    assert prov["model"] is None
    assert body["churn"] is None


def test_snapshot_returns_jpeg(client):
    r = client.get("/snapshot")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8"        # JPEG SOI marker


def test_mjpeg_frame_format():
    # The stream body is a sequence of these parts. Tested directly rather than
    # over the live endpoint: an infinite multipart/x-mixed-replace generator
    # can't be consumed cleanly through Starlette's TestClient (it blocks). The
    # live stream is proven end-to-end by running uvicorn (see the PR).
    part = merle_daemon.mjpeg_frame(b"\xff\xd8JPEGBYTES\xff\xd9")
    assert part.startswith(b"--frame\r\nContent-Type: image/jpeg\r\n\r\n")
    assert part.endswith(b"\r\n")
    assert b"\xff\xd8JPEGBYTES\xff\xd9" in part


def test_stream_route_registered(client):
    assert "/stream" in {r.path for r in client.app.routes}


def _decode(jpeg):
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def test_encode_stream_jpeg_downscales_wide_frames():
    # A camera-native 4K frame comes back 1080p (issue #49: 4K JPEGs at 15fps
    # were ~26MB/s through the MCC proxy), aspect preserved.
    jpeg = merle_daemon.encode_stream_jpeg(np.zeros((2160, 3840, 3), np.uint8))
    assert _decode(jpeg).shape[:2] == (1080, 1920)


def test_encode_stream_jpeg_leaves_narrow_frames_alone():
    # Frames already at or under STREAM_WIDTH (the synthetic source, a future
    # substream) must not be upscaled.
    jpeg = merle_daemon.encode_stream_jpeg(np.zeros((720, 1280, 3), np.uint8))
    assert _decode(jpeg).shape[:2] == (720, 1280)


def test_snapshot_stays_full_resolution(client):
    # The downscale is /stream-only: /snapshot keeps the source-native frame
    # (1280x720 from the synthetic source).
    r = client.get("/snapshot")
    assert _decode(r.content).shape[:2] == (720, 1280)


def test_worker_publishes_stream_jpeg_and_advancing_seq(client):
    shared = client.app.state.shared
    with shared.lock:
        assert shared.stream_jpeg is not None
        seq = shared.seq
    assert seq > 0
    # seq keeps advancing while frames flow -- this is what the /stream
    # generator gates on.
    for _ in range(100):
        with shared.lock:
            if shared.seq > seq:
                break
        time.sleep(0.02)
    with shared.lock:
        assert shared.seq > seq


def test_next_stream_part_sends_only_new_frames():
    jpeg = b"\xff\xd8JPEG\xff\xd9"
    # No frame published yet: nothing to send.
    assert merle_daemon.next_stream_part(None, 0, -1) == (None, -1)
    # Fresh client (last_seq=-1) gets the current frame immediately, even if
    # the worker is stood down and seq is stale -- last frame, not broken image.
    part, last = merle_daemon.next_stream_part(jpeg, 7, -1)
    assert part == merle_daemon.mjpeg_frame(jpeg)
    assert last == 7
    # Same seq again (stand-down, or ticking faster than the worker): silence.
    assert merle_daemon.next_stream_part(jpeg, 7, 7) == (None, 7)
    # Worker publishes a new frame: it goes out.
    part, last = merle_daemon.next_stream_part(jpeg, 8, 7)
    assert part is not None
    assert last == 8


def test_control_stop_and_start(client):
    assert client.post("/control", json={"action": "stop"}).json()["running"] is False
    assert client.post("/control", json={"action": "start"}).json()["running"] is True


def test_control_set_crowd_threshold(client):
    r = client.post("/control", json={"action": "set_crowd_threshold", "value": 8})
    assert r.json()["crowd_threshold"] == 8
    # invalid values are rejected
    assert client.post("/control", json={"action": "set_crowd_threshold", "value": 0}).status_code == 422


def test_control_rejects_unknown_action(client):
    assert client.post("/control", json={"action": "explode"}).status_code == 422


def _tenured_sighting(conn, session_id, tid, species, ts):
    """A sighting with enough matched frames to count as a census visitor
    (the /state and /history endpoints apply the tenure filter, issue #24)."""
    for _ in range(perception.CENSUS_AFTER_FRAMES):
        storage.upsert_sighting(conn, session_id, tid, species, ts, 0.8)


def test_sightings_persist_to_db():
    conn = storage.connect(":memory:")
    app = merle_daemon.create_app(SyntheticFrameSource(), conn,
                                  publisher=_FakePublisher())
    with TestClient(app) as c:
        _wait_for_first_frame(c)
        # Let the worker bank a few frames of the synthetic squirrels.
        time.sleep(0.4)
        totals = c.get("/state").json()["totals"]
    rows = conn.execute(
        "SELECT DISTINCT track_id FROM sightings WHERE species = 'squirrel'"
    ).fetchall()
    assert len(rows) == 2                      # two distinct tracks recorded raw
    # ...but a few frames is below the census tenure, so they aren't
    # /state visitors yet -- brand-new tracks must earn their count.
    assert totals == {}


def test_crowd_event_recorded_when_threshold_low():
    # Threshold below the synthetic animal count -> a crowd_snapshot event fires.
    app = _app(SyntheticFrameSource())
    with TestClient(app) as c:
        c.post("/control", json={"action": "set_crowd_threshold", "value": 2})
        deadline = time.time() + 2.0
        kinds = []
        while time.time() < deadline:
            kinds = [e["kind"] for e in c.get("/state").json()["recent_events"]]
            if "crowd_snapshot" in kinds:
                break
            time.sleep(0.05)
        assert "crowd_snapshot" in kinds


class _FlakySource:
    """Camera 'down' for the first few reads (returns None), then recovers --
    stands in for an RTSP stream that dropped and reconnected."""

    def __init__(self, drop=3):
        self._syn = SyntheticFrameSource()
        self._drop = drop
        self.reads = 0

    def read(self):
        self.reads += 1
        if self.reads <= self._drop:
            return None, []
        return self._syn.read()

    def close(self):
        pass


def test_worker_survives_dropped_frames():
    # A dropped read must NOT kill the perception loop (the bug: it used to
    # `break`, freezing the feed until a full restart). The worker should flag
    # no-signal, keep polling, and recover once frames return.
    app = _app(_FlakySource(drop=4))
    with TestClient(app) as c:
        deadline = time.time() + 4
        recovered = False
        while time.time() < deadline:
            live = c.get("/state").json()["live"]
            if live["counts"].get("squirrel"):   # frames flowing again
                assert live["signal"] is True
                recovered = True
                break
            time.sleep(0.05)
        assert recovered, "worker died on dropped frames instead of recovering"


class _VisitSource:
    """A chipmunk that shows up for a stretch and leaves -- the minimal
    arrival/departure story, without waiting out the synthetic source's
    90-frame visit cycle in real time."""

    def __init__(self, visit_reads=10):
        self._visit_reads = visit_reads
        self._syn = SyntheticFrameSource()
        self.reads = 0

    def read(self):
        self.reads += 1
        frame, dets = self._syn.read()
        dets = [d for d in dets if d.species != "chipmunk"]
        if self.reads <= self._visit_reads:
            dets.append(Detection(99, "chipmunk", (100, 100, 150, 140), 0.6))
        return frame, dets

    def close(self):
        pass


def test_arrival_and_departure_events(monkeypatch):
    # A species count that holds -> arrival after the debounce; gone past the
    # departure window -> departure with the visit duration. Both must reach
    # SQLite (recent_events) AND the bus (driveway/events) with the same content.
    monkeypatch.setattr(merle_daemon, "ARRIVE_AFTER", 0.05)
    monkeypatch.setattr(merle_daemon, "DEPART_AFTER", 0.15)
    fake = _FakePublisher()
    app = _app(_VisitSource(), publisher=fake)
    with TestClient(app) as c:
        deadline = time.time() + 3.0
        events = []
        while time.time() < deadline:
            events = c.get("/state").json()["recent_events"]
            if any(e["kind"] == "departure" for e in events):
                break
            time.sleep(0.05)

    arrivals = [e for e in events if e["kind"] == "arrival"]
    departures = [e for e in events if e["kind"] == "departure"]
    assert {a["details"]["species"] for a in arrivals} == {"squirrel", "chipmunk"}
    # Events are species-level: the two ever-present squirrels are ONE arrival.
    squirrel_arrival = next(a for a in arrivals if a["details"]["species"] == "squirrel")
    assert squirrel_arrival["details"]["count"] == 2
    assert [d["details"]["species"] for d in departures] == ["chipmunk"]
    assert departures[0]["details"]["count"] == 0
    assert departures[0]["details"]["duration_s"] >= 0

    bus_events = [p for t, p in fake.messages if t == bus.EVENTS_TOPIC]
    assert {e["kind"] for e in bus_events} >= {"arrival", "departure"}
    # The bus payload carries the same shape the archive does.
    bus_departure = next(e for e in bus_events if e["kind"] == "departure")
    assert bus_departure["details"]["species"] == "chipmunk"

    # Issue #90: every arrival/departure carries a frame_id, in SQLite and on
    # the bus alike, and the still-shot bytes went out on the frame topics --
    # full (the stream-downscaled annotated JPEG) and thumb, both real JPEGs.
    for e in arrivals + departures + bus_events:
        if e["kind"] in ("arrival", "departure"):
            assert e["details"]["frame_id"]
    frame_id = bus_departure["details"]["frame_id"]
    published = dict(fake.raw)
    for variant in ("full", "thumb"):
        jpeg = published[bus.frame_topic(frame_id, variant)]
        assert jpeg[:2] == b"\xff\xd8", f"{variant} is not a JPEG"
    # frame_ids are unique across the run's events.
    ids = [e["details"]["frame_id"] for e in bus_events
           if e["kind"] in ("arrival", "departure")]
    assert len(ids) == len(set(ids))


def test_mint_frame_id_is_filesystem_safe_and_disambiguated():
    # Derived from session + timestamp + kind (issue #90), filesystem-safe by
    # construction: the ISO timestamp's separators are stripped, and a counter
    # disambiguates events fired on the same frame (a squirrel and a turkey
    # arriving together yield two ids).
    fid = merle_daemon.mint_frame_id(
        "20260714_081500", "2026-07-14T08:15:30", "arrival", 7)
    assert fid == "20260714_081500_20260714T081530_arrival_0007"
    assert re.fullmatch(r"[A-Za-z0-9_]+", fid)
    other = merle_daemon.mint_frame_id(
        "20260714_081500", "2026-07-14T08:15:30", "arrival", 8)
    assert other != fid


def test_encode_thumb_jpeg_downscales_to_thumb_width():
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    thumb = merle_daemon.encode_thumb_jpeg(frame)
    decoded = cv2.imdecode(np.frombuffer(thumb, np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape[1] == merle_daemon.THUMB_WIDTH
    # Aspect preserved: 1280x720 -> 320x180.
    assert decoded.shape[0] == 180
    # A frame already narrower than the thumb width is left alone, not blown up.
    small = np.zeros((90, 160, 3), dtype=np.uint8)
    decoded_small = cv2.imdecode(
        np.frombuffer(merle_daemon.encode_thumb_jpeg(small), np.uint8),
        cv2.IMREAD_COLOR)
    assert decoded_small.shape[:2] == (90, 160)


class _ChurnSource:
    """One squirrel that never leaves, but whose track id gets re-minted after
    short detection gaps -- exactly the ByteTrack churn seen on the real
    driveway (stationary feeder flickers out, comes back as a 'new' animal)."""

    def __init__(self):
        self.reads = 0
        self._syn = SyntheticFrameSource()

    def read(self):
        self.reads += 1
        frame, _ = self._syn.read()
        cycle = self.reads % 7
        if cycle in (5, 6):
            return frame, []   # detection gap: the tracker loses it here
        tid = 100 + self.reads // 7   # ...and re-acquires it as a NEW id
        return frame, [Detection(tid, "squirrel", (100, 100, 190, 164), 0.7)]

    def close(self):
        pass


def test_id_churn_produces_no_phantom_events(monkeypatch):
    # THE regression test for the event spam: id churn on a squirrel that never
    # leaves must produce exactly one arrival and zero departures. The count
    # dips 1 -> 0 for two frames per cycle, far shorter than DEPART_AFTER.
    monkeypatch.setattr(merle_daemon, "ARRIVE_AFTER", 0.05)
    monkeypatch.setattr(merle_daemon, "DEPART_AFTER", 1.0)
    fake = _FakePublisher()
    app = _app(_ChurnSource(), publisher=fake)
    with TestClient(app) as c:
        time.sleep(1.5)   # ~3 churn cycles at the worker's 15fps pace
        events = c.get("/state").json()["recent_events"]

    arrivals = [e for e in events if e["kind"] == "arrival"]
    departures = [e for e in events if e["kind"] == "departure"]
    assert len(arrivals) == 1, f"churn minted extra arrivals: {arrivals}"
    assert departures == [], f"churn manufactured departures: {departures}"


def test_coasting_track_never_arrives():
    # A coasting ghost is a briefly-lost track, not a new animal: it must not
    # fire an arrival until it actually re-matches.
    class _CoastingSource(SyntheticFrameSource):
        def read(self):
            frame, dets = super().read()
            dets.append(Detection(50, "turkey", (10, 10, 60, 60), 0.3, coasting=True))
            return frame, dets

    fake = _FakePublisher()
    app = _app(_CoastingSource(), publisher=fake)
    with TestClient(app) as c:
        _wait_for_first_frame(c)
        time.sleep(0.3)
        events = c.get("/state").json()["recent_events"]
    assert not any(e["kind"] == "arrival" and e["details"]["species"] == "turkey"
                   for e in events)


def test_synthetic_source_is_deterministic():
    # Same frame index -> same boxes, so tests over the source are stable.
    a, b = SyntheticFrameSource(), SyntheticFrameSource()
    for _ in range(5):
        fa, da = a.read()
        fb, db = b.read()
        assert [d.box for d in da] == [d.box for d in db]


def test_history_endpoint_shape_and_seeded_runs():
    conn = storage.connect(":memory:")
    # Arrivals on two known days (yesterday + today), plus one out-of-window.
    from datetime import date, timedelta
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    long_ago = (date.today() - timedelta(days=40)).isoformat()
    _tenured_sighting(conn, "s1", 1, "squirrel", f"{yesterday}T09:00:00")
    _tenured_sighting(conn, "s1", 2, "turkey", f"{yesterday}T10:00:00")
    _tenured_sighting(conn, "s2", 1, "squirrel", f"{today}T08:00:00")
    _tenured_sighting(conn, "s0", 1, "squirrel", f"{long_ago}T08:00:00")
    app = merle_daemon.create_app(SyntheticFrameSource(), conn,
                                  publisher=_FakePublisher())
    with TestClient(app) as c:
        body = c.get("/history?days=7").json()

    census = body["census"]
    assert len(census) == 7
    assert census[-1]["date"] == today                 # window ends today
    assert census[-2]["counts"] == {"squirrel": 1, "turkey": 1}
    assert census[-1]["counts"] == {"squirrel": 1}
    assert all(d["counts"] == {} for d in census[:-2])  # long_ago outside window
    assert len(body["hard_frames"]) == 7               # padded to match
    runs = {r["run_name"] for r in body["training_runs"]}
    assert {"train-15", "train-16", "train-18"} <= runs


def test_history_days_is_clamped():
    app = _app(SyntheticFrameSource())
    with TestClient(app) as c:
        assert len(c.get("/history?days=5000").json()["census"]) == 90
        assert len(c.get("/history?days=-3").json()["census"]) == 1


def test_history_day_hourly_buckets():
    conn = storage.connect(":memory:")
    _tenured_sighting(conn, "s1", 1, "squirrel", "2026-07-05T09:05:00")
    _tenured_sighting(conn, "s1", 2, "turkey", "2026-07-05T17:20:00")
    app = merle_daemon.create_app(SyntheticFrameSource(), conn,
                                  publisher=_FakePublisher())
    with TestClient(app) as c:
        body = c.get("/history/day?day=2026-07-05").json()
        assert body["hours"] == {"9": {"squirrel": 1}, "17": {"turkey": 1}}
        assert c.get("/history/day?day=not-a-date").status_code == 422


def test_hard_frames_by_day_counts_mtimes(tmp_path):
    import os
    from datetime import date, timedelta
    today = date.today()
    # Two banked frames yesterday, one today, plus a sidecar .txt (not counted).
    for name, days_ago in [("a.jpg", 1), ("b.jpg", 1), ("c.jpg", 0)]:
        f = tmp_path / name
        f.write_bytes(b"jpeg")
        mtime = time.mktime((today - timedelta(days=days_ago)).timetuple())
        os.utime(f, (mtime, mtime))
    (tmp_path / "a.txt").write_text("0 0.5 0.5 0.1 0.1")

    trend = merle_daemon.hard_frames_by_day(3, today.isoformat(), folder=str(tmp_path))
    assert [d["n"] for d in trend] == [0, 2, 1]


def test_hard_frames_by_day_survives_missing_folder():
    trend = merle_daemon.hard_frames_by_day(2, "2026-07-07", folder="does/not/exist")
    assert trend == [{"date": "2026-07-06", "n": 0}, {"date": "2026-07-07", "n": 0}]
