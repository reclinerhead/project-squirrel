# =============================================================================
# project-squirrel -- storage.py
#
# The Merle daemon's persistent memory: a single SQLite file (stdlib sqlite3, no
# server, no extra deps -- copy the file to back it up, runs identically on a
# mini-PC or Jetson later). The daemon owns this DB; the MCC never touches it
# directly, it asks the daemon's API.
#
# Design rule from the epic: images and clips stay on the filesystem; this DB
# stores only metadata and paths. So `events` records "a hard frame was saved at
# path X", not the frame itself.
#
# Timestamps are ISO-8601 strings passed IN by the caller (never generated here)
# -- that keeps every function pure and deterministic for tests, and lets the
# perception loop stamp events with the same clock it uses everywhere else.
# =============================================================================

import json
import sqlite3

SCHEMA = """
-- One row per distinct animal the tracker followed. A ByteTrack id only means
-- something within a single daemon run (ids reset on restart), so a sighting is
-- keyed by (session_id, track_id), not track_id alone.
CREATE TABLE IF NOT EXISTS sightings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT    NOT NULL,   -- daemon-run identifier (e.g. start timestamp)
    track_id   INTEGER NOT NULL,   -- ByteTrack id within that run
    species    TEXT    NOT NULL,   -- voted class over the track's life
    first_seen TEXT    NOT NULL,   -- ISO-8601
    last_seen  TEXT    NOT NULL,
    frames     INTEGER NOT NULL DEFAULT 1,   -- how many frames it was matched in
    max_conf   REAL    NOT NULL DEFAULT 0,
    UNIQUE(session_id, track_id)
);

-- Notable moments worth remembering: hard frame saved, crowd snapshot, clip
-- recorded, etc. `kind` classifies; `details` is a free-form JSON blob (paths,
-- counts, whatever that kind needs) so new event types don't need schema changes.
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,     -- ISO-8601
    kind    TEXT NOT NULL,
    details TEXT               -- JSON, may be NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- One row per training round, so the dashboard can show metrics improving over
-- time. Headline numbers are columns; full per-class detail rides in `metrics`
-- JSON. run_name is the PK, so re-seeding is idempotent (INSERT OR IGNORE).
CREATE TABLE IF NOT EXISTS training_runs (
    run_name  TEXT PRIMARY KEY,
    map50     REAL,
    recall    REAL,
    map50_95  REAL,
    val_split TEXT,      -- which valid set produced these numbers (same-ruler rule)
    notes     TEXT,
    metrics   TEXT       -- JSON: per-class P/R/mAP
);
"""

# Baseline eval numbers, same-ruler on the 0705 valid split (see TechnicalGuide).
# Seeded so the dashboard's training-history panel isn't empty on day one and the
# train-15 -> train-16 improvement is on record.
BASELINE_RUNS = [
    {
        "run_name": "train-15", "map50": 0.913, "recall": 0.856, "map50_95": 0.651,
        "val_split": "0705", "notes": "Pre hard-frame batch. Re-validated on the 0705 split for a same-ruler compare.",
        "metrics": {
            "chipmunk": {"p": 1.0, "r": 0.781, "map50": 0.904, "map50_95": 0.523},
            "squirrel": {"p": 0.912, "r": 0.887, "map50": 0.937, "map50_95": 0.718},
            "turkey": {"p": 0.927, "r": 0.900, "map50": 0.898, "map50_95": 0.712},
        },
    },
    {
        "run_name": "train-16", "map50": 0.936, "recall": 0.887, "map50_95": 0.667,
        "val_split": "0705", "notes": "Added ~55 reviewed hard frames (100% to train). Last 3-class model.",
        "metrics": {
            "chipmunk": {"p": 0.921, "r": 0.837, "map50": 0.929, "map50_95": 0.540},
            "squirrel": {"p": 0.944, "r": 0.924, "map50": 0.949, "map50_95": 0.734},
            "turkey": {"p": 0.923, "r": 0.929, "map50": 0.929, "map50_95": 0.726},
        },
    },
    {
        # First 2-class model (chipmunk retired to the rover era). NOT same-ruler
        # vs train-16: different valid split (0707) AND class set, so this starts
        # a fresh baseline rather than continuing train-16's numbers.
        "run_name": "train-18", "map50": 0.864, "recall": 0.838, "map50_95": 0.673,
        "val_split": "0707", "notes": "First 2-class (squirrel/turkey). Deployed as models/current.pt. Turkey read is thin (30 instances) -- next data pivot.",
        "metrics": {
            "squirrel": {"p": 0.889, "r": 0.876, "map50": 0.908, "map50_95": 0.691},
            "turkey": {"p": 0.865, "r": 0.800, "map50": 0.820, "map50_95": 0.655},
        },
    },
]


def connect(path):
    """Open (creating if needed) the Merle DB and ensure the schema exists.
    `path` may be ":memory:" for tests. check_same_thread=False because the
    daemon's perception thread and FastAPI request threads share one connection.
    """
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets the daemon's perception thread write while request threads read
    # without "database is locked" -- a real on-disk DB only. (:memory: has no
    # WAL and would just report "memory", so skip it there.)
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_sighting(conn, session_id, track_id, species, ts, conf):
    """Record (or update) one tracked animal. First call inserts; later calls for
    the same (session_id, track_id) bump the frame count, advance last_seen, keep
    the running max confidence, and adopt the latest voted species."""
    conn.execute(
        """
        INSERT INTO sightings (session_id, track_id, species, first_seen, last_seen, frames, max_conf)
        VALUES (?, ?, ?, ?, ?, 1, ?)
        ON CONFLICT(session_id, track_id) DO UPDATE SET
            species   = excluded.species,
            last_seen = excluded.last_seen,
            frames    = frames + 1,
            max_conf  = MAX(max_conf, excluded.max_conf)
        """,
        (session_id, track_id, species, ts, ts, conf),
    )
    conn.commit()


def record_event(conn, ts, kind, details=None):
    """Log a notable moment. `details` is any JSON-serializable dict (or None)."""
    conn.execute(
        "INSERT INTO events (ts, kind, details) VALUES (?, ?, ?)",
        (ts, kind, json.dumps(details) if details is not None else None),
    )
    conn.commit()


def seed_training_runs(conn, runs=BASELINE_RUNS):
    """Insert baseline training-run rows. Idempotent -- run_name is the PK and
    INSERT OR IGNORE skips rows already present. Returns the number inserted."""
    inserted = 0
    for r in runs:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO training_runs
                (run_name, map50, recall, map50_95, val_split, notes, metrics)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (r["run_name"], r.get("map50"), r.get("recall"), r.get("map50_95"),
             r.get("val_split"), r.get("notes"), json.dumps(r.get("metrics"))),
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


def species_totals(conn, session_id=None, min_frames=1):
    """Distinct-animal counts per species -- {"squirrel": 12, "chipmunk": 3}.
    Scoped to one run when session_id is given, else across all history.
    `min_frames` is the census tenure (issue #24): rows below it -- one-blink
    junk tracks, NMS-free duplicate fragments -- stay recorded in `sightings`
    but don't count as visitors. Filtered at query time so history cleans up
    retroactively and the raw record stays raw."""
    if session_id is None:
        rows = conn.execute(
            "SELECT species, COUNT(*) AS n FROM sightings WHERE frames >= ? GROUP BY species",
            (min_frames,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT species, COUNT(*) AS n FROM sightings"
            " WHERE session_id = ? AND frames >= ? GROUP BY species",
            (session_id, min_frames),
        ).fetchall()
    return {row["species"]: row["n"] for row in rows}


def census_by_day(conn, days=14, today=None, min_frames=1):
    """Distinct-animal counts per species per DAY, for the history panel --
    [{"date": "2026-07-06", "counts": {"squirrel": 9, "turkey": 2}}, ...],
    oldest first, one entry per day in the window even when nothing visited
    (an empty day is real data: nobody came). `today` is an ISO date string
    supplied by the caller (never generated here -- storage stays clock-free
    for tests); the window is the `days` days ending on it. A sighting counts
    on the day its track FIRST appeared, tallied per (session_id, track_id)
    row -- with the same `min_frames` census tenure as species_totals."""
    from datetime import date, timedelta   # stdlib, deterministic

    end = date.fromisoformat(today)
    window = [(end - timedelta(days=d)).isoformat() for d in range(days - 1, -1, -1)]
    rows = conn.execute(
        """
        SELECT substr(first_seen, 1, 10) AS day, species, COUNT(*) AS n
        FROM sightings
        WHERE day >= ? AND frames >= ?
        GROUP BY day, species
        """,
        (window[0], min_frames),
    ).fetchall()
    by_day = {d: {} for d in window}
    for r in rows:
        if r["day"] in by_day:
            by_day[r["day"]][r["species"]] = r["n"]
    return [{"date": d, "counts": by_day[d]} for d in window]


def day_hours(conn, day, min_frames=1):
    """Hourly activity for one day -- {hour: {species: n}} with only the hours
    that saw arrivals (0-23 keys as ints). Same distinct-track counting and
    `min_frames` tenure as census_by_day, bucketed by the hour the track
    first appeared."""
    rows = conn.execute(
        """
        SELECT CAST(substr(first_seen, 12, 2) AS INTEGER) AS hour,
               species, COUNT(*) AS n
        FROM sightings
        WHERE substr(first_seen, 1, 10) = ? AND frames >= ?
        GROUP BY hour, species
        """,
        (day, min_frames),
    ).fetchall()
    hours = {}
    for r in rows:
        hours.setdefault(r["hour"], {})[r["species"]] = r["n"]
    return hours


def recent_events(conn, limit=50):
    """Most recent events, newest first, with `details` parsed back to a dict."""
    rows = conn.execute(
        "SELECT ts, kind, details FROM events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        {"ts": r["ts"], "kind": r["kind"],
         "details": json.loads(r["details"]) if r["details"] else None}
        for r in rows
    ]


def training_runs(conn):
    """All training runs, best mAP50 first, with `metrics` parsed back to a dict."""
    rows = conn.execute(
        "SELECT * FROM training_runs ORDER BY map50 DESC"
    ).fetchall()
    return [
        {**dict(r), "metrics": json.loads(r["metrics"]) if r["metrics"] else None}
        for r in rows
    ]
