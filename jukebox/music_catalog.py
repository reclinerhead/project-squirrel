# =============================================================================
# project-squirrel -- music_catalog.py
#
# The music catalog (issue #120, Phase 0 of #115): our own store for a ~27k
# track library that lives on the NAS and is never written to. The audio files
# are an IMMUTABLE INPUT -- tags are read once at index time and every field we
# derive afterwards lands here, never back in the file. That makes the catalog
# rebuildable and disposable, and lets metadata iterate fearlessly against a
# 612 GB library we can't afford to damage. The share is mounted read-only, so
# this is enforced by the mount rather than by discipline.
#
# Why its own file and not merle.db: weather_archive.py:22-29 is the precedent
# and the reasoning transfers unchanged. `merle.db` lives on bluejay next to
# the daemon, and daemon-down is the STEADY STATE. Music has nothing to do with
# the driveway and must not inherit its uptime. music.db lives on pearl, next
# to the writer, the way the weather archive does.
#
# Why NOT on the NAS, where the files are: SQLite's locking is unreliable on
# SMB/CIFS and its own documentation warns against network filesystems. That's
# a corruption risk, not a performance note.
#
# IDENTITY IS A HASH OF THE AUDIO STREAM, NOT OF THE FILE.
#
# Files get moved and renamed, so a path is a location, never an identity.
# But hashing whole file BYTES has a subtler failure that we measured rather
# than assumed: this library is iTunes-managed (the share carries .itl and
# 2,241 .itc2 files), so tag rewrites are a live risk, not a hypothetical. A
# retag of one m4a and one mp3 changed the whole-file hash both times and left
# the audio-stream hash byte-identical. A whole-file hash would therefore mint
# a NEW identity on every tag edit and orphan that track's ratings and play
# history -- the exact failure the content hash exists to prevent.
#
# Rejected: whole file bytes. Trivial to implement, and wrong for this library
# specifically. The cost of the choice we made is container-aware parsing per
# format, which music_index.py owns.
#
# Rejected: SHA-256. This is content-addressing for identity, not a signature;
# there is no adversary. blake2b-128 is stdlib, collision-resistant far beyond
# what 26,590 tracks need, and runs at GB/s -- while the library reads at
# 99.8 MB/s over gigabit SMB. The hash is free either way, so this was never a
# performance decision.
#
# TIMESTAMPS ARE UNIX EPOCH SECONDS, following weather.db and departing from
# storage.py's ISO-8601 TEXT. The repo deliberately disagrees with itself here,
# so this file states its side: ratings and play history are ranged and
# compared far more than they are read by a human, and epoch is what a chart
# consumes. One convention per store, written down.
#
# Config (env, the MERLE_WEATHER_DB convention):
#   MERLE_MUSIC_DB   the catalog's path (default: music.db, relative to the
#                    process's WorkingDirectory). Any MCC-side reader must be
#                    given the SAME FILE as an ABSOLUTE path and must have no
#                    default -- see mcc/app/weather/history/route.ts:20-25 for
#                    why a relative default quietly names a file nothing writes.
#
# SCHEMA EVOLUTION IS BUILT NOW, BEFORE IT'S NEEDED. `CREATE TABLE IF NOT
# EXISTS` does not alter an existing file. This phase creates `ratings` and
# `play_history` UNUSED, specifically so they accumulate for months before
# Phase 3/4 read them -- which is exactly the situation where a schema change
# lands on a table holding irreplaceable data. The catalog is rebuildable from
# the NAS; ratings and play history are not. They are to music what weather.db
# is to the station: "the one irreplaceable file the whole stack owns"
# (Servers/Pearl.md). Hence MIGRATIONS below, and hence backups.
# =============================================================================

import os
import re
import sqlite3

DEFAULT_DB_PATH = "music.db"

# Bumped whenever MIGRATIONS grows. A fresh file gets SCHEMA (already current)
# and is stamped straight to this; an existing file replays only the steps
# above its stored PRAGMA user_version.
SCHEMA_VERSION = 1

# The four-level thumbs (#115's feedback model). Phase 3 reads these as RULES
# -- strong-down is a hard filter applied BEFORE candidate selection, not an
# instruction a model is trusted to honor -- and Phase 4 reads the same rows as
# EVIDENCE. Spelled out here because the store is what both layers share.
RATING_STRONG_DOWN = -2
RATING_DOWN = -1
RATING_UP = 1
RATING_STRONG_UP = 2
RATING_VALUES = (RATING_STRONG_DOWN, RATING_DOWN, RATING_UP, RATING_STRONG_UP)

# How a play ended. Implicit feedback is the one input that cannot be
# backfilled or bought, so play_history exists from Phase 0 even though nothing
# writes it until Phase 2 -- the moment something plays, it starts counting.
PLAY_COMPLETED = "completed"
PLAY_SKIPPED = "skipped"

# `id` is the audio-stream hash: stable across a tag edit AND across a move.
# Everything that matters -- ratings, history, Phase 1's analysis -- hangs off
# it. Tags are a snapshot taken at index time, never re-read from the file at
# playback. `needs_attention` is Phase 1's bucket, created now so a file we
# can't read is a queryable number rather than a silent drop.
#
# Locations live in `track_files`, NOT here, because the hash is the identity
# and a 26k-track library rips the same recording twice (an album and a
# greatest-hits). One `tracks` row, N `track_files` rows: duplicates collapse
# to the one thing they actually are, and rating a track rates the recording
# rather than whichever copy happened to play. Folding path/size/mtime into
# `tracks` instead would make a duplicate either a spurious second identity or
# a row whose path flip-flops between copies on every index pass.
SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id           TEXT PRIMARY KEY,
    title        TEXT,
    artist       TEXT,
    album        TEXT,
    album_artist TEXT,
    track_no     INTEGER,
    disc_no      INTEGER,
    year         INTEGER,
    genre        TEXT,
    duration_s   REAL,
    format       TEXT,
    bitrate      INTEGER,
    samplerate   INTEGER,
    channels     INTEGER,
    bpm              REAL,
    replaygain_db    REAL,
    dynamic_range_db REAL,
    needs_attention TEXT,
    indexed_at   INTEGER
);

CREATE TABLE IF NOT EXISTS track_files (
    path         TEXT PRIMARY KEY,
    track_id     TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        INTEGER NOT NULL,
    audio_offset INTEGER,
    audio_length INTEGER,
    seen_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_track_files_track ON track_files(track_id);

CREATE TABLE IF NOT EXISTS artists (
    name    TEXT PRIMARY KEY,
    bio     TEXT,
    bio_src TEXT,
    fetched_at INTEGER
);

CREATE TABLE IF NOT EXISTS ratings (
    track_id TEXT PRIMARY KEY,
    value    INTEGER NOT NULL,
    rated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS play_history (
    id        INTEGER PRIMARY KEY,
    track_id  TEXT NOT NULL,
    played_at INTEGER NOT NULL,
    outcome   TEXT NOT NULL,
    seconds   REAL,
    output    TEXT
);
CREATE INDEX IF NOT EXISTS idx_play_history_track ON play_history(track_id);
CREATE INDEX IF NOT EXISTS idx_play_history_at ON play_history(played_at);
"""

# Ordered, append-only. Index N runs to reach user_version N+1, so a file at
# version 2 replays MIGRATIONS[2:] and nothing else. NEVER edit or reorder a
# landed entry -- a file in the wild has already run it. To change the schema:
# append a step here, bump SCHEMA_VERSION, and mirror the end state into SCHEMA
# so a fresh file skips the replay entirely.
#
# Empty at version 1: SCHEMA *is* version 1, and there is no deployed file
# older than it. The seam is here so the first real column addition is a
# one-line append against months of accumulated ratings, rather than an
# emergency discovered on the day it's needed.
MIGRATIONS = ()


def db_path():
    """MERLE_MUSIC_DB: unset OR blank means the default, relative to the
    process's WorkingDirectory (the MERLE_WEATHER_DB convention). A path that
    can't be opened raises in connect() at startup rather than failing quietly
    on the first write -- never run half-configured while looking healthy."""
    return os.environ.get("MERLE_MUSIC_DB", "").strip() or DEFAULT_DB_PATH


# --- pure: version arithmetic, row shaping ------------------------------------

# The id allowlist, frame_archiver's genre (never trust the wire): every id
# the indexer mints is a prefix + hex ("b:1fbc...", "f:...", "x:..."), so this
# is generous already. Anything else -- path separators, dots, spaces -- is
# rejected BEFORE the catalog is asked, so a hostile id dies here rather than
# meeting the filesystem, and a typo'd one gets a 404 instead of a traversal.
TRACK_ID_RE = re.compile(r"[A-Za-z0-9_:-]+")


def valid_track_id(track_id):
    """Whether a wire-supplied track id is even the right SHAPE. Purely a
    syntax check -- existence is the catalog's question, asked after."""
    return bool(track_id) and TRACK_ID_RE.fullmatch(track_id) is not None

def pending_migrations(current, migrations=MIGRATIONS, target=SCHEMA_VERSION):
    """The migration steps a file at `current` still owes, as (version, sql)
    pairs where `version` is what user_version becomes once that step lands.
    Pure so the arithmetic is testable without a file on disk.

    A file NEWER than we understand returns nothing rather than raising: an
    older build opening a newer catalog should read what it can, not refuse to
    start. A downgrade that actually breaks would break at the SQL, loudly."""
    if current >= target:
        return []
    return [(i + 1, migrations[i]) for i in range(current, min(target, len(migrations)))]


def track_row(track):
    """A parsed track -> its INSERT params in TRACK_COLUMNS order. .get()
    throughout: `album_artist` is ~68% populated and `genre`/`year` ~90%, so a
    missing tag maps to NULL rather than crashing the pass. A gap is real data
    (the file didn't say), which is the history_point() rule."""
    return tuple(track.get(c) for c in TRACK_COLUMNS)


TRACK_COLUMNS = (
    "id", "title", "artist", "album", "album_artist", "track_no", "disc_no",
    "year", "genre", "duration_s", "format", "bitrate", "samplerate",
    "channels", "bpm", "replaygain_db", "dynamic_range_db",
    "needs_attention", "indexed_at",
)

FILE_COLUMNS = (
    "path", "track_id", "size", "mtime", "audio_offset", "audio_length",
    "seen_at",
)


def norm_tag(value):
    """Trim and collapse a tag to None when it says nothing. iTunes writes
    empty strings and whitespace where a tag is absent; storing "" and NULL as
    two different kinds of missing would make every later `WHERE artist IS NULL`
    quietly wrong."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def norm_int(value):
    """Tag ints arrive as "7", "7/12", 7, or junk. Returns None rather than
    raising -- a malformed track number is a cosmetic loss, never a reason to
    fail a 26k-file pass (weather.py:773-777's ethos)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    head = text.split("/")[0].strip()
    try:
        return int(head)
    except ValueError:
        return None


def cache_is_valid(cached, size, mtime):
    """Whether a cached (path, size, mtime) entry still describes the file on
    disk, i.e. whether we may SKIP re-hashing it. This is what makes a re-index
    cost minutes instead of 1.74 hours, so it is the hottest decision in the
    pass.

    MEASURED TRAP: a retag can leave the file SIZE completely unchanged --
    mutagen padded within the existing tag space and only mtime moved. A cache
    keyed on size alone would call that file unchanged, skip it forever, and
    keep serving a stale span. Both fields are load-bearing; neither is
    redundant. (Re-hashing is cheap insurance anyway: the audio hash comes back
    identical, so the track keeps its id and only the span is refreshed.)"""
    if cached is None:
        return False
    return cached["size"] == size and cached["mtime"] == mtime


def moved_files(seen_paths, known_paths):
    """Paths the catalog knows that this pass did NOT see -- candidates for a
    move or a deletion. Pure (injected sets) so the boundary is testable.

    Deliberately NOT a delete list on its own: a share that fails to mount
    presents as "every path vanished", and acting on that would wipe the
    catalog's locations over a bad mount. Pair it with prune_is_safe()."""
    return sorted(set(known_paths) - set(seen_paths))


# A pass that saw less than this share of what the catalog knows is not
# trusted to prune. Half is deliberately generous: a real library reorganize
# might legitimately move a lot of files at once, while the failure this
# guards -- a share that mounted empty or half -- shows up as a number far
# below it. The point is to catch a catastrophe, not to police normal churn.
PRUNE_FLOOR = 0.5


def prune_is_safe(seen_count, known_count, floor=PRUNE_FLOOR):
    """Whether this pass saw enough of the library to be trusted to delete
    locations. Pure so the arithmetic is testable without a mount.

    This exists because the indexer cannot tell "the files moved" from "the
    share isn't there" -- both look like paths that stopped existing. A first
    run (known_count == 0) has nothing to prune and is trivially safe."""
    if known_count == 0:
        return True
    return (seen_count / known_count) >= floor


# --- I/O: the thin half -------------------------------------------------------

def connect(path):
    """Open (creating if needed) the catalog and ensure the schema is current.
    `path` may be ":memory:" for tests. weather_archive.py's connection
    handling, same reasons: WAL so an MCC route can read while the indexer
    writes without "database is locked", and an idempotent schema so a fresh
    pearl and a five-year-old file take the same path."""
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()
    return conn


def migrate(conn):
    """Bring an existing file up to SCHEMA_VERSION, returning the versions
    applied. A brand-new file already matches SCHEMA, so this only stamps it.

    The stamp is what makes the seam work: without it every file would sit at
    user_version 0 forever and the first real migration would replay against a
    schema that already had the column."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    applied = []
    for version, sql in pending_migrations(current):
        conn.executescript(sql)
        # PRAGMA won't take a bound parameter; version is ours, never input.
        conn.execute("PRAGMA user_version=%d" % version)
        applied.append(version)
    if not applied and current < SCHEMA_VERSION:
        conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
    conn.commit()
    return applied


def upsert_track(conn, track):
    """Insert or refresh one track, keyed on the audio hash. Tags are refreshed
    on re-index (a retag SHOULD update what we display) but the id, and
    therefore every rating and play-history row hanging off it, survives
    untouched. That is the whole point of hashing the audio and not the file.

    Phase 1's columns are excluded from the update: an indexer re-run must not
    wipe hours of BPM/ReplayGain analysis just because it re-read the tags."""
    cols = ", ".join(TRACK_COLUMNS)
    marks = ", ".join("?" * len(TRACK_COLUMNS))
    keep = {"id", "bpm", "replaygain_db", "dynamic_range_db"}
    sets = ", ".join("%s=excluded.%s" % (c, c)
                     for c in TRACK_COLUMNS if c not in keep)
    # f-string SQL is safe here: TRACK_COLUMNS is a module constant, never input.
    conn.execute(
        f"INSERT INTO tracks ({cols}) VALUES ({marks}) "
        f"ON CONFLICT(id) DO UPDATE SET {sets}",
        track_row(track))
    return track["id"]


def upsert_file(conn, entry):
    """Record where a track currently lives. `path` is the PK, so re-seeing a
    file is an update, and a MOVED file is an INSERT at its new path whose
    track_id already exists -- never a new track. The stale row is cleaned by
    forget_paths(), not here."""
    cols = ", ".join(FILE_COLUMNS)
    marks = ", ".join("?" * len(FILE_COLUMNS))
    sets = ", ".join("%s=excluded.%s" % (c, c)
                     for c in FILE_COLUMNS if c != "path")
    conn.execute(
        f"INSERT INTO track_files ({cols}) VALUES ({marks}) "
        f"ON CONFLICT(path) DO UPDATE SET {sets}",
        tuple(entry.get(c) for c in FILE_COLUMNS))


def file_cache(conn):
    """Every known (path -> size, mtime, track_id) in one read. The pass holds
    this in memory rather than issuing 26,590 point queries: the whole table is
    ~2 MB and one scan beats 26k round trips against a WAL file by orders of
    magnitude."""
    rows = conn.execute(
        "SELECT path, size, mtime, track_id, audio_offset, audio_length "
        "FROM track_files")
    return {r["path"]: dict(r) for r in rows}


def forget_paths(conn, paths):
    """Drop locations that no longer exist. Only ever removes from
    `track_files` -- the `tracks` row, its ratings, and its history stay, since
    a track we can't currently find is not a track that never existed. A file
    restored from a backup re-links to the same id on the next pass."""
    if not paths:
        return 0
    marks = ", ".join("?" * len(paths))
    cur = conn.execute(
        f"DELETE FROM track_files WHERE path IN ({marks})", tuple(paths))
    conn.commit()
    return cur.rowcount


def rate(conn, track_id, value, at):
    """Record a thumb. One rating per track -- a re-rate replaces, it doesn't
    append -- so `ratings` says what the listener thinks NOW. The history of
    opinion changes is not something Phase 3 or 4 asked for.

    Nothing calls this in Phase 0. It exists so the table is real and writable
    from the day the GUI can reach it."""
    if value not in RATING_VALUES:
        raise ValueError("rating must be one of %r, got %r"
                         % (RATING_VALUES, value))
    conn.execute(
        "INSERT INTO ratings (track_id, value, rated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(track_id) DO UPDATE SET value=excluded.value, "
        "rated_at=excluded.rated_at", (track_id, value, at))
    conn.commit()


def record_play(conn, track_id, played_at, outcome, seconds=None, output=None):
    """Append one play event. Append-only and never updated: a skip at 0:12 and
    a completion of the same track an hour later are two facts, not a
    correction of one.

    Nothing calls this in Phase 0 either -- Phase 2 does, the moment anything
    plays. It's here now because implicit feedback is the one input that cannot
    be backfilled."""
    conn.execute(
        "INSERT INTO play_history (track_id, played_at, outcome, seconds, "
        "output) VALUES (?, ?, ?, ?, ?)",
        (track_id, played_at, outcome, seconds, output))
    conn.commit()


def track_info(conn, track_id):
    """The tracks row for one id, as a dict -- what the daemon needs for the
    DIDL metadata and the capability check (title, artist, format,
    duration_s). None when the id isn't in the catalog: the caller turns that
    into a 404, not an exception, because an unknown id is a wrong URL, not a
    broken daemon."""
    row = conn.execute(
        "SELECT id, title, artist, album, duration_s, format "
        "FROM tracks WHERE id = ?", (track_id,)).fetchone()
    return dict(row) if row else None


def file_for_track(conn, track_id):
    """The location to stream for one track, as a dict (path, size, mtime).
    A track can live at several paths (the album rip and the greatest-hits rip
    collapse to one id -- the identity design working); which copy streams is
    musically irrelevant, so pick deterministically (lowest path) rather than
    letting SQLite's row order decide differently on different days."""
    row = conn.execute(
        "SELECT path, size, mtime FROM track_files WHERE track_id = ? "
        "ORDER BY path LIMIT 1", (track_id,)).fetchone()
    return dict(row) if row else None


def counts(conn):
    """Row counts per table -- what the indexer prints when it finishes, and
    what the acceptance criteria are read against."""
    out = {}
    for table in ("tracks", "track_files", "artists", "ratings",
                  "play_history"):
        out[table] = conn.execute(
            "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    return out
