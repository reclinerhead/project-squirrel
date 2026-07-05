# Tests for merle_daemon.py -- the FastAPI daemon, driven by the synthetic frame
# source against an in-memory DB. No camera, no model: this is exactly the
# surface the MCC talks to, verified end to end. Entering the TestClient context
# manager runs the app's lifespan, which starts the worker thread.

import time

import pytest
from fastapi.testclient import TestClient

import merle_daemon
import storage
from frames import SyntheticFrameSource, Detection


@pytest.fixture
def client():
    app = merle_daemon.create_app(SyntheticFrameSource(), storage.connect(":memory:"))
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
    for key in ("session_id", "running", "crowd_threshold", "live", "totals", "recent_events"):
        assert key in body
    # The synthetic source always has two squirrels on screen.
    assert body["live"]["counts"].get("squirrel") == 2
    assert body["running"] is True


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


def test_sightings_persist_to_db(client):
    # Let the worker run enough frames to bank the squirrels as sightings.
    time.sleep(0.4)
    totals = client.get("/state").json()["totals"]
    assert totals.get("squirrel") == 2         # two distinct tracks recorded


def test_crowd_event_recorded_when_threshold_low():
    # Threshold below the synthetic animal count -> a crowd_snapshot event fires.
    app = merle_daemon.create_app(SyntheticFrameSource(), storage.connect(":memory:"))
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
    app = merle_daemon.create_app(_FlakySource(drop=4), storage.connect(":memory:"))
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


def test_synthetic_source_is_deterministic():
    # Same frame index -> same boxes, so tests over the source are stable.
    a, b = SyntheticFrameSource(), SyntheticFrameSource()
    for _ in range(5):
        fa, da = a.read()
        fb, db = b.read()
        assert [d.box for d in da] == [d.box for d in db]
