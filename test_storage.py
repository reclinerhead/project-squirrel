# Tests for storage.py -- the daemon's SQLite layer. Pure logic, stdlib only,
# so these run fast in CI without ultralytics/opencv. Every DB is in-memory.

import storage


def fresh():
    return storage.connect(":memory:")


def test_connect_creates_schema():
    conn = fresh()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"sightings", "events", "training_runs"} <= tables


def test_sighting_insert_then_accumulate():
    conn = fresh()
    storage.upsert_sighting(conn, "s1", 4, "squirrel", "2026-07-05T10:00:00", 0.42)
    storage.upsert_sighting(conn, "s1", 4, "squirrel", "2026-07-05T10:00:01", 0.88)
    row = conn.execute("SELECT * FROM sightings WHERE session_id='s1' AND track_id=4").fetchone()
    assert row["frames"] == 2                     # second call bumped the count
    assert row["first_seen"] == "2026-07-05T10:00:00"
    assert row["last_seen"] == "2026-07-05T10:00:01"   # advanced
    assert row["max_conf"] == 0.88                # kept the higher confidence


def test_sighting_species_revote_updates():
    conn = fresh()
    storage.upsert_sighting(conn, "s1", 7, "chipmunk", "2026-07-05T10:00:00", 0.3)
    storage.upsert_sighting(conn, "s1", 7, "squirrel", "2026-07-05T10:00:01", 0.3)
    row = conn.execute("SELECT species FROM sightings WHERE track_id=7").fetchone()
    assert row["species"] == "squirrel"           # latest vote wins


def test_same_track_id_different_sessions_are_distinct():
    conn = fresh()
    storage.upsert_sighting(conn, "s1", 4, "squirrel", "2026-07-05T10:00:00", 0.5)
    storage.upsert_sighting(conn, "s2", 4, "turkey", "2026-07-05T11:00:00", 0.5)
    n = conn.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    assert n == 2                                 # track 4 in two runs = two rows


def test_event_json_round_trip():
    conn = fresh()
    storage.record_event(conn, "2026-07-05T10:00:00", "hard_frame_saved",
                         {"path": "hard_frames/hard_x.jpg", "boxes": 4})
    storage.record_event(conn, "2026-07-05T10:00:05", "crowd_snapshot", None)
    events = storage.recent_events(conn)
    assert len(events) == 2
    assert events[0]["kind"] == "crowd_snapshot"  # newest first
    assert events[0]["details"] is None
    assert events[1]["details"] == {"path": "hard_frames/hard_x.jpg", "boxes": 4}


def test_species_totals_counts_distinct_animals():
    conn = fresh()
    # three squirrels (each seen twice) + one chipmunk
    for tid in (1, 2, 3):
        storage.upsert_sighting(conn, "s1", tid, "squirrel", "2026-07-05T10:00:00", 0.5)
        storage.upsert_sighting(conn, "s1", tid, "squirrel", "2026-07-05T10:00:01", 0.5)
    storage.upsert_sighting(conn, "s1", 9, "chipmunk", "2026-07-05T10:00:00", 0.5)
    assert storage.species_totals(conn) == {"squirrel": 3, "chipmunk": 1}


def test_species_totals_scoped_by_session():
    conn = fresh()
    storage.upsert_sighting(conn, "s1", 1, "squirrel", "2026-07-05T10:00:00", 0.5)
    storage.upsert_sighting(conn, "s2", 1, "turkey", "2026-07-05T11:00:00", 0.5)
    assert storage.species_totals(conn, session_id="s2") == {"turkey": 1}


def test_seed_training_runs_is_idempotent():
    conn = fresh()
    first = storage.seed_training_runs(conn)
    second = storage.seed_training_runs(conn)
    assert first == 2          # train-15 + train-16
    assert second == 0         # already present, nothing re-inserted
    assert conn.execute("SELECT COUNT(*) FROM training_runs").fetchone()[0] == 2


def test_training_runs_ordered_and_parsed():
    conn = fresh()
    storage.seed_training_runs(conn)
    runs = storage.training_runs(conn)
    assert [r["run_name"] for r in runs] == ["train-16", "train-15"]   # best mAP50 first
    assert runs[0]["metrics"]["chipmunk"]["r"] == 0.837                # JSON parsed back
