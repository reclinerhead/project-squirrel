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
SCHEMA_VERSION = 9

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

# What's inside an m4a/mp4 container -- `format` alone cannot say (issue #149).
# The extension covers BOTH Apple Lossless and lossy iTunes-purchase AAC, and
# the browser output treats them oppositely: ALAC repacks to FLAC (lossless to
# lossless), AAC streams untouched (re-encoding a lossy source to FLAC loses
# nothing but inflates it for no reason). NULL on an m4a means "not probed
# yet", which the policy treats as ALAC -- the never-lossy default; the worst
# case is wasted bytes, never lost ones. Only m4a/mp4 rows carry a codec: for
# every other format the extension IS the codec and a second column saying so
# would be a drift risk with no question it answers.
CODEC_ALAC = "alac"
CODEC_AAC = "aac"

# Where a piece of art came from (issue #153). Provenance is load-bearing,
# not bookkeeping: `owner` rows are the listener's own choice (an uploaded
# band photo, a promoted cover) and NO automated pass may ever overwrite one
# -- the upsert enforces it, so the rule can't be forgotten at a call site.
# `derived` marks a machine's guess (the promoted-cover artist image), which
# re-runs MAY refresh.
ART_EMBEDDED = "embedded"
ART_FOLDER = "folder"
ART_DERIVED = "derived"
ART_OWNER = "owner"

# Where a normalized genre came from (issue #163) -- the art tables' provenance
# idea applied to a column. `mapped` is the rules file's string map or artist
# override; `inherited` is the artist-majority guess for tracks whose raw tag
# maps nowhere; `external` is reserved for the bulk-metadata backfill
# (MusicBrainz/Last.fm); `owner` is the listener's own hand-set value, which
# NO automated pass may overwrite -- enforced in the pass's UPDATE, same as
# set_album_art's upsert, so the rule can't be forgotten at a call site.
GENRE_MAPPED = "mapped"
GENRE_INHERITED = "inherited"
GENRE_EXTERNAL = "external"
GENRE_OWNER = "owner"

# Where an album's description came from (issue #171). Same provenance idea a
# third time, and for the same reason: `comment-tag` is the iTunes/Amazon store
# copy lifted out of the files, `external` is reserved for a richer fetched
# source (a Wikipedia album article via #170's MBIDs) that would supersede it,
# and `owner` is Todd's own text -- which no automated pass may overwrite,
# enforced in set_album_note's upsert.
NOTE_COMMENT = "comment-tag"
NOTE_EXTERNAL = "external"
NOTE_OWNER = "owner"

# Where an artist bio came from (issue #170). The provenance idea a fourth
# time, same owner rule: `wikipedia` is the CC BY-SA lead extract reached
# through the artist's MusicBrainz entity, `lastfm` the fallback where no
# Wikipedia article exists, `owner` Todd's own text -- untouchable by any
# re-fetch, enforced in set_artist_bio's upsert.
BIO_WIKIPEDIA = "wikipedia"
BIO_LASTFM = "lastfm"
BIO_OWNER = "owner"

# THE ALBUM KEY, shared verbatim with the music app. An album's identity is
# the display pair the GUI derives (lib/catalog-rows.ts albumIdOf, before its
# base64url): COALESCE'd artist + U+241F + COALESCE'd title. This SQL is that
# derivation, and music/lib/db.ts carries its twin -- the paired fixture
# tests on both sides are what keep them from drifting. U+241F (symbol for
# unit separator) because no album title contains it.
#
# artist_norm leads the artist half since #152: the pass's case-collapsed
# canonical identity, so `Gwar` and `GWAR` mint ONE key and art can't strand
# under a minority casing. NULL (pass not run) falls through to the raw
# derivation -- exactly the pre-#152 key, so a stale catalog degrades to
# case-split albums, never to broken art.
ALBUM_ARTIST_SQL = ("COALESCE(NULLIF(t.artist_norm, ''), "
                    "NULLIF(t.album_artist, ''), t.artist, 'Unknown Artist')")
ALBUM_KEY_SQL = (
    ALBUM_ARTIST_SQL +
    " || '␟' || COALESCE(NULLIF(t.album, ''), 'Unknown Album')"
)

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
    codec        TEXT,
    bitrate      INTEGER,
    samplerate   INTEGER,
    channels     INTEGER,
    bpm              REAL,
    replaygain_db    REAL,
    dynamic_range_db REAL,
    needs_attention TEXT,
    indexed_at   INTEGER,
    genre_norm        TEXT,
    genre_norm_source TEXT,
    artist_norm       TEXT
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

-- `fetched_at` marks ATTEMPTED, not succeeded (issue #170): a row with a NULL
-- bio and a timestamp is "we looked and found nothing", which is why a re-run
-- skips it and --retry-missing is the deliberate way back. `mbid` is the
-- MusicBrainz identity the resolver accepted; `bio_url` is the attribution
-- link, which Wikipedia's CC BY-SA licensing makes part of using the prose
-- properly rather than a nicety.
CREATE TABLE IF NOT EXISTS artists (
    name    TEXT PRIMARY KEY,
    bio     TEXT,
    bio_src TEXT,
    fetched_at INTEGER,
    mbid    TEXT,
    bio_url TEXT
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

CREATE TABLE IF NOT EXISTS album_art (
    album_key  TEXT PRIMARY KEY,
    art_hash   TEXT NOT NULL,
    source     TEXT NOT NULL,
    w          INTEGER,
    h          INTEGER,
    updated_at INTEGER,
    focal_y    REAL
);

CREATE TABLE IF NOT EXISTS artist_art (
    artist     TEXT PRIMARY KEY,
    art_hash   TEXT NOT NULL,
    source     TEXT NOT NULL,
    w          INTEGER,
    h          INTEGER,
    updated_at INTEGER,
    focal_y    REAL
);

CREATE TABLE IF NOT EXISTS genre_map (
    raw       TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT 'file'
);

-- Album descriptions (issue #171). `description` is what a page renders,
-- `raw` is the untouched tag text -- kept because 49% of these blurbs are
-- hard-cut at 255 chars mid-word and the trim-to-last-sentence policy throws
-- away the fragment. Storing both means a future policy change (or a diff
-- against a fetched source) is a re-run over this table, not another walk of
-- 26k files. `truncated` flags the 255-char wall so a surface can decide
-- whether to say so, and so the trim rule stays measurable after the fact.
CREATE TABLE IF NOT EXISTS album_notes (
    album_key   TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    raw         TEXT NOT NULL,
    source      TEXT NOT NULL,
    truncated   INTEGER NOT NULL DEFAULT 0,
    updated_at  INTEGER
);
"""

# Ordered, append-only. Index N runs to reach user_version N+1, so a file at
# version 2 replays MIGRATIONS[2:] and nothing else. NEVER edit or reorder a
# landed entry -- a file in the wild has already run it. To change the schema:
# append a step here, bump SCHEMA_VERSION, and mirror the end state into SCHEMA
# so a fresh file skips the replay entirely.
#
# Index i takes a file from version i to i+1 -- and version 1 was never a
# migration (SCHEMA was born at 1, files were stamped straight to it), so
# index 0 is a no-op placeholder that keeps that arithmetic honest. Deployed
# files all sit at 1 and replay only what's above them.
#
# NEW TABLES NEVER LAND HERE (issue #153's album_art/artist_art set the
# precedent): CREATE TABLE IF NOT EXISTS in SCHEMA self-applies to existing
# files on every connect. This seam is for ALTERs only -- do not cargo-cult
# a no-op step for a table addition.
MIGRATIONS = (
    "-- version 1 is SCHEMA's birth state; nothing to replay",
    # 1 -> 2 (issue #149): the codec column, the seam's first real customer
    # -- exactly the "one-line append against accumulated ratings" it was
    # built for.
    "ALTER TABLE tracks ADD COLUMN codec TEXT;",
    # 2 -> 3, 3 -> 4 (issue #159): where the art's interest lives vertically,
    # 0..1, the extraction pass's edge-density centroid; NULL = not analyzed
    # (the --focal worklist). TWO steps, one ALTER each, on purpose: migrate()
    # tolerates "duplicate column" by stamping the step done, so a two-ALTER
    # script whose first line already landed would skip its second forever.
    "ALTER TABLE album_art ADD COLUMN focal_y REAL;",
    "ALTER TABLE artist_art ADD COLUMN focal_y REAL;",
    # 4 -> 5, 5 -> 6 (issue #163): the normalized genre and its provenance.
    # Two steps, one ALTER each -- the focal_y lesson, same reason.
    "ALTER TABLE tracks ADD COLUMN genre_norm TEXT;",
    "ALTER TABLE tracks ADD COLUMN genre_norm_source TEXT;",
    # 6 -> 7 (issue #152): the canonical artist identity -- the case-collapsed
    # display name of COALESCE(album_artist, artist), written by the
    # normalization pass. Raw artist/album_artist stay untouched provenance.
    "ALTER TABLE tracks ADD COLUMN artist_norm TEXT;",
    # 7 -> 8, 8 -> 9 (issue #170): the MusicBrainz identity the bio fetcher
    # resolved, and the attribution link for the prose it fetched. Two steps,
    # one ALTER each -- the focal_y lesson, same reason.
    "ALTER TABLE artists ADD COLUMN mbid TEXT;",
    "ALTER TABLE artists ADD COLUMN bio_url TEXT;",
)


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
    "year", "genre", "duration_s", "format", "codec", "bitrate", "samplerate",
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
    # Fresh vs existing is decided BEFORE the schema lands, because it's the
    # fork the seam turns on: a fresh file gets SCHEMA (already the end
    # state) and is stamped straight to SCHEMA_VERSION -- replaying
    # migrations against it would ALTER-in columns SCHEMA already mirrors
    # and die on "duplicate column". An existing file replays only what its
    # stamp says it owes. This surfaced with the first real migration
    # (issue #149); the empty-MIGRATIONS era never exercised it.
    fresh = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE type='table' AND name='tracks'").fetchone()[0] == 0
    conn.executescript(SCHEMA)
    if fresh:
        conn.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
    else:
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
        try:
            conn.executescript(sql)
        except sqlite3.OperationalError as e:
            # "duplicate column" means the schema already matches this step
            # -- the crash window between connect()'s executescript and its
            # stamp, where DDL landed but the version didn't. The column
            # being there IS the step's end state, so stamp and move on;
            # anything else is a real failure and stays loud.
            if "duplicate column" not in str(e):
                raise
            print("[music] migration %d already applied (%s) -- stamping"
                  % (version, e))
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

    The daemon's POST /rate is the only caller (issue #135) -- validation
    lives here, next to RATING_VALUES, rather than at the route, because a
    second copy of the legal set is how the two drift apart.

    `bool` is checked explicitly: it subclasses int, so a JSON `true` off the
    wire satisfies `in RATING_VALUES` and would file itself as a thumbs-up."""
    if isinstance(value, bool) or not isinstance(value, int) \
            or value not in RATING_VALUES:
        raise ValueError("rating must be one of %r, got %r"
                         % (RATING_VALUES, value))
    conn.execute(
        "INSERT INTO ratings (track_id, value, rated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(track_id) DO UPDATE SET value=excluded.value, "
        "rated_at=excluded.rated_at", (track_id, value, at))
    conn.commit()


def unrate(conn, track_id):
    """Clear a thumb -- the control's third click (issue #135). An unrated
    track is the ABSENCE of a row, never a stored zero: `ratings` says what the
    listener thinks, and "no opinion" is not an opinion. Phase 3's filters read
    the rows that exist; a 0 would be a third thing they'd all have to know to
    ignore.

    Silent on a track that was never rated -- clearing nothing is the state the
    caller asked for."""
    conn.execute("DELETE FROM ratings WHERE track_id = ?", (track_id,))
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
        "SELECT id, title, artist, album, duration_s, format, codec "
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


def set_codec(conn, track_id, codec):
    """Record what a probe found inside one m4a/mp4 container. The backfill's
    writer (issue #149) -- one UPDATE per probed file rather than a re-index,
    because the codec lives in the header and re-reading 16k full files to
    learn 4 bytes each would turn minutes into hours."""
    conn.execute("UPDATE tracks SET codec = ? WHERE id = ?", (codec, track_id))


def tracks_missing_codec(conn):
    """(track_id, path) for every m4a/mp4 track not yet probed -- the
    backfill's worklist. Lowest path per track, file_for_track's determinism
    rule: which copy gets probed is irrelevant (same container, same codec),
    so pick the same one every run."""
    rows = conn.execute(
        "SELECT t.id AS track_id, MIN(f.path) AS path "
        "FROM tracks t JOIN track_files f ON f.track_id = t.id "
        "WHERE t.format IN ('m4a', 'mp4') AND t.codec IS NULL "
        "GROUP BY t.id ORDER BY t.id")
    return [(r["track_id"], r["path"]) for r in rows]


def set_album_art(conn, album_key, art_hash, source, w, h, at, focal_y=None):
    """Record an album's art. THE OWNER RULE LIVES IN THE SQL: an existing
    row with source='owner' is never touched -- the listener's own pick
    survives every automated re-run by construction, not by caller
    discipline (issue #153). Everything else refreshes. focal_y rides the
    row since #159 (extraction computes it inline); defaulted so pre-focal
    callers and tests stay honest about "not analyzed"."""
    conn.execute(
        "INSERT INTO album_art (album_key, art_hash, source, w, h, "
        "updated_at, focal_y) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(album_key) DO UPDATE SET "
        "art_hash=excluded.art_hash, source=excluded.source, w=excluded.w, "
        "h=excluded.h, updated_at=excluded.updated_at, "
        "focal_y=excluded.focal_y "
        "WHERE album_art.source != ?",
        (album_key, art_hash, source, w, h, at, focal_y, ART_OWNER))


def set_artist_art(conn, artist, art_hash, source, w, h, at, focal_y=None):
    """Record an artist's image, same owner rule as set_album_art."""
    conn.execute(
        "INSERT INTO artist_art (artist, art_hash, source, w, h, "
        "updated_at, focal_y) VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(artist) DO UPDATE SET "
        "art_hash=excluded.art_hash, source=excluded.source, w=excluded.w, "
        "h=excluded.h, updated_at=excluded.updated_at, "
        "focal_y=excluded.focal_y "
        "WHERE artist_art.source != ?",
        (artist, art_hash, source, w, h, at, focal_y, ART_OWNER))


def set_album_focal(conn, album_key, focal_y):
    """The --focal backfill's write: focal_y ONLY, deliberately no owner
    guard -- analysis of where the interest sits isn't a clobber of WHICH
    image the owner chose (issue #159), and an owner-override image wants
    a good crop as much as any other."""
    conn.execute("UPDATE album_art SET focal_y = ? WHERE album_key = ?",
                 (focal_y, album_key))


def set_artist_focal(conn, artist, focal_y):
    """set_album_focal's artist twin."""
    conn.execute("UPDATE artist_art SET focal_y = ? WHERE artist = ?",
                 (focal_y, artist))


def album_art_missing_focal(conn):
    """The --focal backfill worklist: rows extracted before the column
    existed (issue #159). (key, art_hash) pairs, sorted for stable logs;
    inline computation at extraction keeps this list empty from here on,
    so on a fresh install this returns nothing, ever."""
    return [(r["album_key"], r["art_hash"]) for r in conn.execute(
        "SELECT album_key, art_hash FROM album_art "
        "WHERE focal_y IS NULL ORDER BY album_key")]


def artist_art_missing_focal(conn):
    """album_art_missing_focal's artist twin."""
    return [(r["artist"], r["art_hash"]) for r in conn.execute(
        "SELECT artist, art_hash FROM artist_art "
        "WHERE focal_y IS NULL ORDER BY artist")]


def albums_missing_art(conn):
    """The art pass's worklist: {album_key: [paths]} for every album with no
    album_art row. Worklist-driven is THE reusability rule (issue #153) --
    after ingesting five new albums, this returns exactly those five, and a
    full-coverage catalog returns nothing. Paths sorted so the probe order
    (and therefore a tie on identical-size images) is stable across runs."""
    rows = conn.execute(
        f"SELECT {ALBUM_KEY_SQL} AS album_key, f.path "
        f"FROM tracks t JOIN track_files f ON f.track_id = t.id "
        f"WHERE NOT EXISTS (SELECT 1 FROM album_art aa "
        f"                  WHERE aa.album_key = {ALBUM_KEY_SQL}) "
        f"ORDER BY album_key, f.path")
    out = {}
    for r in rows:
        out.setdefault(r["album_key"], []).append(r["path"])
    return out


def artists_missing_art(conn):
    """The promotion pass's worklist: for each artist with no artist_art
    row, their albums' art candidates as (artist, album_key, art_hash, w, h,
    score) rows -- score is the album's summed thumb values, so "their
    most-rated album's cover" is data the caller just sorts (pick highest
    score, tie-break lowest album_key: deterministic across runs, the
    issue's contract)."""
    rows = conn.execute(
        f"SELECT {ALBUM_ARTIST_SQL} AS artist, "
        f"       {ALBUM_KEY_SQL} AS album_key, "
        f"       aa.art_hash, aa.w, aa.h, aa.focal_y, "
        f"       COALESCE(SUM(r.value), 0) AS score "
        f"FROM tracks t "
        f"JOIN album_art aa ON aa.album_key = {ALBUM_KEY_SQL} "
        f"LEFT JOIN ratings r ON r.track_id = t.id "
        f"WHERE NOT EXISTS (SELECT 1 FROM artist_art x WHERE x.artist = "
        f"      {ALBUM_ARTIST_SQL}) "
        f"GROUP BY artist, album_key")
    return [dict(r) for r in rows]


def set_album_note(conn, album_key, description, raw, source, truncated, at):
    """Record an album's description. Same owner rule as set_album_art, in the
    same place -- the SQL -- so a future per-album refresh button (which will
    call this from a request handler, not from the pass) cannot forget it
    (issue #171)."""
    conn.execute(
        "INSERT INTO album_notes (album_key, description, raw, source, "
        "truncated, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(album_key) DO UPDATE SET "
        "description=excluded.description, raw=excluded.raw, "
        "source=excluded.source, truncated=excluded.truncated, "
        "updated_at=excluded.updated_at "
        "WHERE album_notes.source != ?",
        (album_key, description, raw, source, int(bool(truncated)), at,
         NOTE_OWNER))


def albums_missing_note(conn):
    """The blurb pass's worklist: {album_key: [paths]} for every album with no
    album_notes row -- albums_missing_art's twin, same NOT EXISTS shape and
    same sorted paths so the comment pick is stable across runs.

    Note the deliberate consequence, inherited from the art pass: an album
    whose files carry no usable comment writes no row, so it returns here
    every run. That is ~62% of the catalog re-reading its headers each pass
    (minutes, not hours) and it is the price of not inventing a "checked,
    found nothing" row whose staleness nobody would ever invalidate."""
    rows = conn.execute(
        f"SELECT {ALBUM_KEY_SQL} AS album_key, f.path "
        f"FROM tracks t JOIN track_files f ON f.track_id = t.id "
        f"WHERE NOT EXISTS (SELECT 1 FROM album_notes an "
        f"                  WHERE an.album_key = {ALBUM_KEY_SQL}) "
        f"ORDER BY album_key, f.path")
    out = {}
    for r in rows:
        out.setdefault(r["album_key"], []).append(r["path"])
    return out


def album_paths(conn, album_key):
    """Every file backing one album, sorted -- the single-album twin of the
    worklist queries. This is what the future "refresh this album" button
    calls (issue #171): the pass and the button read the same rows through the
    same album-key derivation, so a one-off refresh can never disagree with
    what a full run would have produced."""
    rows = conn.execute(
        f"SELECT f.path FROM tracks t JOIN track_files f ON f.track_id = t.id "
        f"WHERE {ALBUM_KEY_SQL} = ? ORDER BY f.path", (album_key,))
    return [r["path"] for r in rows]


def set_artist_bio(conn, name, bio, bio_src, bio_url, mbid, at):
    """Record one artist's bio. Same owner rule as set_album_art, in the same
    place -- the SQL (issue #170). `bio` may be None: that is the
    attempted-and-found-nothing row, and it is deliberately a WRITE, because
    `fetched_at` is what keeps the next run from re-probing a known miss."""
    conn.execute(
        "INSERT INTO artists (name, bio, bio_src, fetched_at, mbid, bio_url) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET "
        "bio=excluded.bio, bio_src=excluded.bio_src, "
        "fetched_at=excluded.fetched_at, mbid=excluded.mbid, "
        "bio_url=excluded.bio_url "
        "WHERE artists.bio_src IS NOT ?",
        (name, bio, bio_src, at, mbid, bio_url, BIO_OWNER))


def artists_missing_bio(conn, retry_missing=False):
    """The bio pass's worklist: canonical artist identities with no bio row
    yet, as {name: [album titles]} -- the albums ride along because the
    resolver corroborates a name match against them (issue #170), and
    fetching them here means one query instead of one per artist.

    An artist the pass already ATTEMPTED is off the list even if it found
    nothing, which is what `fetched_at` is for; `retry_missing=True` puts the
    empty ones back on deliberately. An owner row is never on the list at
    all -- there is nothing to fetch for an artist Todd has written up."""
    where = ("WHERE NOT EXISTS (SELECT 1 FROM artists a "
             "                  WHERE a.name = " + ALBUM_ARTIST_SQL + ")")
    if retry_missing:
        # Back on the list: attempted, found nothing, not an owner row.
        where = ("WHERE NOT EXISTS (SELECT 1 FROM artists a "
                 "  WHERE a.name = " + ALBUM_ARTIST_SQL +
                 "    AND (COALESCE(a.bio, '') != '' OR a.bio_src IS ?))")
    params = (BIO_OWNER,) if retry_missing else ()
    rows = conn.execute(
        f"SELECT {ALBUM_ARTIST_SQL} AS artist, t.album AS album "
        f"FROM tracks t {where} "
        f"GROUP BY artist, album ORDER BY artist, album", params)
    out = {}
    for r in rows:
        out.setdefault(r["artist"], [])
        if r["album"]:
            out[r["artist"]].append(r["album"])
    return out


def artist_albums(conn, name):
    """One artist's album titles, sorted -- artists_missing_bio's single-item
    twin, so a per-artist refresh corroborates against exactly the same
    evidence a full run would have used (issue #170)."""
    rows = conn.execute(
        f"SELECT DISTINCT t.album AS album FROM tracks t "
        f"WHERE {ALBUM_ARTIST_SQL} = ? AND COALESCE(t.album, '') != '' "
        f"ORDER BY album", (name,))
    return [r["album"] for r in rows]


def counts(conn):
    """Row counts per table -- what the indexer prints when it finishes, and
    what the acceptance criteria are read against."""
    out = {}
    for table in ("tracks", "track_files", "artists", "ratings",
                  "play_history", "album_art", "artist_art", "genre_map",
                  "album_notes"):
        out[table] = conn.execute(
            "SELECT COUNT(*) FROM %s" % table).fetchone()[0]
    return out
