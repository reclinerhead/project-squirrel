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

def test_rtsp_url_builds_and_redacts(monkeypatch, tmp_path):
    # delenv the override and point MERLE_FEEDS at a file that isn't there:
    # a box with no registry at all (#270). The direct-camera form must be
    # exactly what it always was.
    monkeypatch.delenv("MERLE_RTSP_URL", raising=False)
    monkeypatch.setenv("MERLE_FEEDS", str(tmp_path / "no-such-feeds.yml"))
    monkeypatch.setenv("MERLE_RTSP_PASS", "sekrit")
    monkeypatch.setenv("MERLE_RTSP_HOST", "10.0.0.5")
    monkeypatch.delenv("MERLE_RTSP_USER", raising=False)
    url, redacted = frames.rtsp_url()
    assert url == "rtsp://admin:sekrit@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"
    # The redacted twin (what logs and /state carry) never leaks the password
    # -- and pins the MAIN stream (subtype=0) where anyone can read it.
    assert redacted == "rtsp://admin:***@10.0.0.5:554/cam/realmonitor?channel=1&subtype=0"
    assert "sekrit" not in redacted


def test_rtsp_url_requires_the_password(monkeypatch, tmp_path):
    monkeypatch.delenv("MERLE_RTSP_URL", raising=False)
    monkeypatch.setenv("MERLE_FEEDS", str(tmp_path / "no-such-feeds.yml"))
    monkeypatch.delenv("MERLE_RTSP_PASS", raising=False)
    with pytest.raises(RuntimeError, match="MERLE_RTSP_PASS"):
        frames.rtsp_url()


def test_rtsp_url_reads_the_registry(monkeypatch, tmp_path):
    # The normal posture since #270: no env override, the feed registry
    # supplies the naturalist feed's URL -- Frigate's credential-free
    # restream, so the redacted twin is honestly the URL itself.
    registry = tmp_path / "feeds.yml"
    registry.write_text(
        "feeds:\n"
        "  house-rear:\n"
        "    kind: rtsp\n"
        "    url: rtsp://pearl:8554/house-rear\n"
        "    naturalist: true\n")
    monkeypatch.delenv("MERLE_RTSP_URL", raising=False)
    monkeypatch.setenv("MERLE_FEEDS", str(registry))
    url, redacted = frames.rtsp_url()
    assert url == redacted == "rtsp://pearl:8554/house-rear"


def test_rtsp_url_malformed_registry_fails_loud(monkeypatch, tmp_path):
    # A registry that EXISTS but is broken must raise, never quietly fall
    # back to a direct camera session -- that would break the one-client
    # rule (#247) on a config typo.
    registry = tmp_path / "feeds.yml"
    registry.write_text(
        "feeds:\n"
        "  house-rear:\n"
        "    kind: rtsp\n"          # kind rtsp with no url: malformed
        "    naturalist: true\n")
    monkeypatch.delenv("MERLE_RTSP_URL", raising=False)
    monkeypatch.setenv("MERLE_FEEDS", str(registry))
    monkeypatch.setenv("MERLE_RTSP_PASS", "sekrit")   # must NOT be reached
    with pytest.raises(RuntimeError, match="needs a non-empty 'url'"):
        frames.rtsp_url()


def test_rtsp_url_override_is_the_restream(monkeypatch):
    # Issue #247: MERLE_RTSP_URL points the daemon at Frigate's go2rtc
    # restream -- used verbatim, no password required (a restream URL carries
    # no credentials), and the redacted twin is honestly the URL itself, so
    # /state shows exactly what the daemon is watching. Since #270 the
    # override outranks the registry -- explicit env beats file config.
    monkeypatch.setenv("MERLE_RTSP_URL", "rtsp://pearl:8554/house-rear")
    monkeypatch.delenv("MERLE_RTSP_PASS", raising=False)
    url, redacted = frames.rtsp_url()
    assert url == redacted == "rtsp://pearl:8554/house-rear"


def test_rtsp_url_override_still_redacts_embedded_creds(monkeypatch):
    # A direct camera URL pasted into the override must not leak either --
    # the fail-safe lives in the redactor, not in trusting the operator.
    monkeypatch.setenv("MERLE_RTSP_URL",
                       "rtsp://admin:sekrit@10.0.0.5:554/cam?channel=1")
    url, redacted = frames.rtsp_url()
    assert "sekrit" in url
    assert redacted == "rtsp://admin:***@10.0.0.5:554/cam?channel=1"
    assert "sekrit" not in redacted


def test_rover_url_default_and_override(monkeypatch):
    # The rover feed (issue #236): the Waveshare app's MJPEG endpoint by
    # default, overridable as one whole URL. No credentials, so the redacted
    # twin is the URL itself -- same (url, redacted) shape as rtsp_url().
    monkeypatch.delenv("MERLE_ROVER_STREAM", raising=False)
    assert frames.rover_url() == ("http://merle:5000/video_feed",
                                  "http://merle:5000/video_feed")
    monkeypatch.setenv("MERLE_ROVER_STREAM", "http://10.0.0.9:8554/feed")
    url, redacted = frames.rover_url()
    assert url == redacted == "http://10.0.0.9:8554/feed"


def test_synthetic_provenance_is_honest_about_what_it_lacks():
    prov = SyntheticFrameSource().provenance()
    assert prov["source"] == "synthetic"
    assert prov["resolution"] == [1280, 720]
    assert prov["model"] is None            # no model, no imgsz -- honestly null
    assert prov["imgsz"] is None


def test_sources_without_a_tracker_report_no_churn():
    assert SyntheticFrameSource().metrics() is None
