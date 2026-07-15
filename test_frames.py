# Tests for frames.py's FreshestFrameReader -- the latency fix from issue #29.
# The reader's contract is what keeps the dashboard live: consumers always get
# the NEWEST frame (a backlog can never pile up between reader and consumer),
# a consumer faster than the camera never sees the same frame twice, a silent
# camera surfaces as a timeout (the daemon's "no signal" path), and a failed
# read swaps in a fresh capture. All driven by a fake capture -- no camera,
# no model, so this runs in CI alongside the other pure suites.

import time

import pytest

from vision import frames
from vision.frames import FreshestFrameReader, SyntheticFrameSource


class FakeCap:
    """Stands in for cv2.VideoCapture: serves its frames instantly, then acts
    like a broken stream (read fails after a short beat -- the beat stops an
    exhausted cap from hot-spinning the reader thread)."""

    def __init__(self, frames_to_serve=(), fail=False):
        self._frames = list(frames_to_serve)
        self._fail = fail
        self.releases = 0

    def read(self):
        if not self._fail and self._frames:
            return True, self._frames.pop(0)
        time.sleep(0.005)
        return False, None

    def release(self):
        self.releases += 1


def make_reader(cap, factory=None):
    reader = FreshestFrameReader(cap, factory or (lambda: cap))
    reader.start()
    return reader


def test_consumer_gets_newest_frame_not_a_backlog():
    reader = make_reader(FakeCap(["f1", "f2", "f3"]))
    try:
        # Wait for the reader to ingest all three (seq > 2), like a consumer
        # coming back from a stall: it must be offered f3, not queued-up f1.
        frame, seq = reader.next_frame(2, timeout=2.0)
        assert (frame, seq) == ("f3", 3)
        # A consumer starting from scratch is also handed the newest, instantly.
        frame, seq = reader.next_frame(0, timeout=0.5)
        assert (frame, seq) == ("f3", 3)
    finally:
        reader.stop()
        reader.join(timeout=2.0)


def test_same_frame_is_never_served_twice():
    reader = make_reader(FakeCap(["only"]))
    try:
        frame, seq = reader.next_frame(0, timeout=2.0)
        assert (frame, seq) == ("only", 1)
        # Caught up: asking again with the consumed seq times out instead of
        # re-serving the frame (the worker would re-run inference on it).
        frame, seq = reader.next_frame(seq, timeout=0.05)
        assert frame is None
        assert seq == 1
    finally:
        reader.stop()
        reader.join(timeout=2.0)


def test_silent_camera_times_out_as_no_signal():
    reader = make_reader(FakeCap([]))   # never produces a frame
    try:
        frame, seq = reader.next_frame(0, timeout=0.05)
        assert frame is None
        assert seq == 0                 # last_seq handed back unchanged
    finally:
        reader.stop()
        reader.join(timeout=2.0)


def test_failed_read_reopens_via_factory():
    bad = FakeCap(fail=True)
    good = FakeCap(["fresh"])
    # First-ever failure reconnects immediately (the throttle only spaces out
    # repeated attempts), so no interval patching is needed here.
    reader = make_reader(bad, factory=lambda: good)
    try:
        frame, seq = reader.next_frame(0, timeout=2.0)
        assert (frame, seq) == ("fresh", 1)
        assert bad.releases == 1        # the dead capture was let go
    finally:
        reader.stop()
        reader.join(timeout=2.0)


def test_stop_joins_promptly_and_releases_capture():
    cap = FakeCap([])
    reader = make_reader(cap)
    reader.stop()
    reader.join(timeout=2.0)
    assert not reader.is_alive()
    assert cap.releases >= 1            # run() releases on the way out


# --- provenance (issue #74, Phase 0) -------------------------------------------

def test_rtsp_url_builds_and_redacts(monkeypatch):
    monkeypatch.setenv("MERLE_RTSP_PASS", "sekrit")
    monkeypatch.setenv("MERLE_RTSP_HOST", "10.0.0.5")
    monkeypatch.delenv("MERLE_RTSP_USER", raising=False)
    url, redacted = frames.rtsp_url()
    assert url == "rtsp://admin:sekrit@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"
    # The redacted twin (what logs and /state carry) never leaks the password
    # -- and pins the MAIN stream (subtype=0) where anyone can read it.
    assert redacted == "rtsp://admin:***@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"
    assert "sekrit" not in redacted


def test_rtsp_url_requires_the_password(monkeypatch):
    monkeypatch.delenv("MERLE_RTSP_PASS", raising=False)
    with pytest.raises(RuntimeError, match="MERLE_RTSP_PASS"):
        frames.rtsp_url()


def test_synthetic_provenance_is_honest_about_what_it_lacks():
    prov = SyntheticFrameSource().provenance()
    assert prov["source"] == "synthetic"
    assert prov["resolution"] == [1280, 720]
    assert prov["model"] is None            # no model, no imgsz -- honestly null
    assert prov["imgsz"] is None


def test_sources_without_a_tracker_report_no_churn():
    assert SyntheticFrameSource().metrics() is None
