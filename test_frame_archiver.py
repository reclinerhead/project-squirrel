# Tests for frame_archiver.py -- the pure logic only: topic -> filename
# mapping (with the path-traversal guard), retention selection (injected
# clock/listing), and env parsing. The MQTT plumbing and the live service on
# pearl are desk-tested per the testing policy.

import pytest

import bus
import frame_archiver


# --- topic -> filename (the traversal guard) ----------------------------------

def test_full_and_thumb_map_to_the_route_readable_names():
    # The names the MCC's /frames route reads: <id>.jpg and <id>.thumb.jpg.
    fid = "20260714_081500_20260714T081530_arrival_0007"
    assert frame_archiver.frame_filename(
        bus.frame_topic(fid, "full")) == f"{fid}.jpg"
    assert frame_archiver.frame_filename(
        bus.frame_topic(fid, "thumb")) == f"{fid}.thumb.jpg"


@pytest.mark.parametrize("topic", [
    "driveway/events",                        # foreign topic
    "narration/lines",
    "driveway/frames/id/medium",              # unknown variant
    "driveway/frames/a/b/full",               # extra path segment
    "driveway/frames/../full",                # traversal as the id
    "driveway/frames/..\\evil/thumb",         # windows separator
    "driveway/frames/a.b/full",               # dots never appear in minted ids
    "driveway/frames/%2e%2e/full",            # encoded traversal
    "driveway/frames/sneaky.jpg.exe/thumb",
    "driveway/frames//full",                  # empty id
])
def test_hostile_and_foreign_topics_yield_no_filename(topic):
    assert frame_archiver.frame_filename(topic) is None


# --- retention selection -------------------------------------------------------

def test_prune_selects_only_files_past_the_window():
    day = 86400.0
    now = 100 * day
    files = [("old.jpg", now - 15 * day),
             ("old.thumb.jpg", now - 14.5 * day),
             ("fresh.jpg", now - 2 * day),
             ("boundary.jpg", now - 14 * day)]   # exactly at the cutoff: kept
    doomed = frame_archiver.prune_selection(files, now, days=14.0)
    assert doomed == ["old.jpg", "old.thumb.jpg"]


def test_prune_with_nothing_old_selects_nothing():
    assert frame_archiver.prune_selection(
        [("a.jpg", 1000.0)], now=1000.0, days=14.0) == []


def test_prune_removes_files_on_disk(tmp_path):
    day = 86400.0
    now = 100 * day
    import os
    old = tmp_path / "old.jpg"
    fresh = tmp_path / "fresh.jpg"
    stray = tmp_path / "notes.txt"   # non-jpg: never the archiver's to delete
    for p in (old, fresh, stray):
        p.write_bytes(b"x")
    os.utime(old, (now - 20 * day, now - 20 * day))
    os.utime(fresh, (now - day, now - day))
    os.utime(stray, (now - 20 * day, now - 20 * day))
    frame_archiver.prune(str(tmp_path), days=14.0, now=now)
    assert not old.exists()
    assert fresh.exists()
    assert stray.exists()


def test_prune_survives_a_missing_folder():
    frame_archiver.prune("no_such_dir_anywhere", days=14.0, now=0.0)


# --- env parsing ----------------------------------------------------------------

def test_keep_days_default_and_override(monkeypatch):
    monkeypatch.delenv("MERLE_FRAMES_KEEP_DAYS", raising=False)
    assert frame_archiver.keep_days() == 14.0
    monkeypatch.setenv("MERLE_FRAMES_KEEP_DAYS", "30")
    assert frame_archiver.keep_days() == 30.0


def test_keep_days_malformed_fails_loudly(monkeypatch):
    monkeypatch.setenv("MERLE_FRAMES_KEEP_DAYS", "a fortnight")
    with pytest.raises(ValueError):
        frame_archiver.keep_days()


def test_frames_dir_default_and_override(monkeypatch):
    monkeypatch.delenv("MERLE_FRAMES_DIR", raising=False)
    assert frame_archiver.frames_dir() == "frames"
    monkeypatch.setenv("MERLE_FRAMES_DIR", "/mnt/nas/frames")
    assert frame_archiver.frames_dir() == "/mnt/nas/frames"
