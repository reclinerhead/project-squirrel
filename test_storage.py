# Tests for storage.py -- the daemon's SQLite layer. Pure logic, stdlib only,
# so these run fast in CI without ultralytics/opencv. Every DB is in-memory.

from vision import storage


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
    n_baselines = len(storage.BASELINE_RUNS)
    first = storage.seed_training_runs(conn)
    second = storage.seed_training_runs(conn)
    assert first == n_baselines    # every baseline inserted on the first pass
    assert second == 0             # already present, nothing re-inserted
    assert conn.execute("SELECT COUNT(*) FROM training_runs").fetchone()[0] == n_baselines


def test_training_runs_ordered_and_parsed():
    conn = fresh()
    storage.seed_training_runs(conn)
    runs = storage.training_runs(conn)
    # Ordered best mAP50 first; train-16 (0.936) still tops the seeded set.
    assert [r["run_name"] for r in runs][:3] == ["train-16", "train-15", "train-18"]
    assert runs[0]["metrics"]["chipmunk"]["r"] == 0.837                # JSON parsed back


def test_census_by_day_buckets_and_pads():
    conn = fresh()
    # Two squirrels + a turkey on the 5th (separate tracks), one squirrel
    # fragment re-seen on the 6th under a new session, silence on the 7th.
    storage.upsert_sighting(conn, "s1", 1, "squirrel", "2026-07-05T09:00:00", 0.8)
    storage.upsert_sighting(conn, "s1", 2, "squirrel", "2026-07-05T10:30:00", 0.7)
    storage.upsert_sighting(conn, "s1", 3, "turkey", "2026-07-05T11:00:00", 0.9)
    storage.upsert_sighting(conn, "s2", 1, "squirrel", "2026-07-06T08:00:00", 0.6)
    days = storage.census_by_day(conn, days=3, today="2026-07-07")
    assert [d["date"] for d in days] == ["2026-07-05", "2026-07-06", "2026-07-07"]
    assert days[0]["counts"] == {"squirrel": 2, "turkey": 1}
    assert days[1]["counts"] == {"squirrel": 1}
    assert days[2]["counts"] == {}                    # a quiet day is still a day


def test_census_by_day_counts_first_seen_day_only():
    conn = fresh()
    # A track that arrived on the 5th and lingered into the 6th counts once,
    # on its arrival day -- last_seen advancing must not double-count it.
    storage.upsert_sighting(conn, "s1", 9, "squirrel", "2026-07-05T23:50:00", 0.8)
    storage.upsert_sighting(conn, "s1", 9, "squirrel", "2026-07-06T00:10:00", 0.8)
    days = storage.census_by_day(conn, days=2, today="2026-07-06")
    assert days[0]["counts"] == {"squirrel": 1}
    assert days[1]["counts"] == {}


def test_census_by_day_ignores_out_of_window():
    conn = fresh()
    storage.upsert_sighting(conn, "s1", 1, "squirrel", "2026-06-01T09:00:00", 0.8)
    days = storage.census_by_day(conn, days=7, today="2026-07-07")
    assert all(d["counts"] == {} for d in days)


def test_day_hours_buckets_by_arrival_hour():
    conn = fresh()
    storage.upsert_sighting(conn, "s1", 1, "squirrel", "2026-07-05T09:05:00", 0.8)
    storage.upsert_sighting(conn, "s1", 2, "squirrel", "2026-07-05T09:40:00", 0.7)
    storage.upsert_sighting(conn, "s1", 3, "turkey", "2026-07-05T17:20:00", 0.9)
    storage.upsert_sighting(conn, "s1", 4, "squirrel", "2026-07-06T09:00:00", 0.8)  # other day
    hours = storage.day_hours(conn, "2026-07-05")
    assert hours == {9: {"squirrel": 2}, 17: {"turkey": 1}}


# --- census tenure (issue #24) -------------------------------------------------
# One-blink junk tracks and NMS-free duplicate fragments live in `sightings`
# as honest raw rows, but min_frames keeps them out of every visitor count --
# including retroactively, since filtering happens at query time.

def seen_for(conn, tid, n, species="squirrel", ts="2026-07-05T10:00:00"):
    for _ in range(n):
        storage.upsert_sighting(conn, "s1", tid, species, ts, 0.8)


def test_species_totals_min_frames_drops_one_blink_tracks():
    conn = fresh()
    seen_for(conn, 1, n=30)                   # a real visit
    seen_for(conn, 2, n=3)                    # junk fragment
    seen_for(conn, 3, n=1, species="turkey")  # one-frame blink
    assert storage.species_totals(conn, min_frames=30) == {"squirrel": 1}
    assert storage.species_totals(conn) == {"squirrel": 2, "turkey": 1}  # raw default


def test_census_by_day_min_frames_cleans_history():
    conn = fresh()
    seen_for(conn, 1, n=30, ts="2026-07-05T09:00:00")
    seen_for(conn, 2, n=2, ts="2026-07-05T09:00:05")
    days = storage.census_by_day(conn, days=1, today="2026-07-05", min_frames=30)
    assert days[0]["counts"] == {"squirrel": 1}


def test_day_hours_min_frames_matches_the_census():
    conn = fresh()
    seen_for(conn, 1, n=30, ts="2026-07-05T09:05:00")
    seen_for(conn, 2, n=4, ts="2026-07-05T09:06:00")
    assert storage.day_hours(conn, "2026-07-05", min_frames=30) == {9: {"squirrel": 1}}
