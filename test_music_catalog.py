# =============================================================================
# project-squirrel -- test_music_catalog.py
#
# The catalog's pure logic (issue #120): version arithmetic, tag normalization,
# the hash cache, and the identity/location contract that every later phase's
# ratings and play history hang off.
#
# All against :memory: and synthetic fixtures -- no NAS, no 612 GB, no network.
# The I/O half is verified by the real pass, not by CI.
#
# NOTE: this file must be on .github/workflows/tests.yml's pytest line by hand.
# CI enumerates test files and has no pytest.ini/testpaths fallback, so a test
# that isn't named there silently never runs and CI goes green having tested
# nothing.
# =============================================================================

import pytest

from jukebox import music_catalog as mc


@pytest.fixture
def conn():
    c = mc.connect(":memory:")
    yield c
    c.close()


def track(**kw):
    base = {"id": "b:abc", "title": "Safe and Sound", "artist": "Capital Cities",
            "album": "In a Tidal Wave of Mystery", "format": "m4a",
            "indexed_at": 1000}
    base.update(kw)
    return base


def entry(**kw):
    base = {"path": "/mnt/music/a.m4a", "track_id": "b:abc", "size": 100,
            "mtime": 200, "audio_offset": 16, "audio_length": 84,
            "seen_at": 1000}
    base.update(kw)
    return base


# --- schema and the migration seam --------------------------------------------

def test_connect_creates_all_tables(conn):
    assert set(mc.counts(conn)) == {
        "tracks", "track_files", "artists", "ratings", "play_history",
        "album_art", "artist_art", "genre_map", "album_notes"}


def test_connect_stamps_user_version(conn):
    """Without the stamp every file would sit at 0 forever and the first real
    migration would replay against a schema that already had the column."""
    assert conn.execute("PRAGMA user_version").fetchone()[0] == mc.SCHEMA_VERSION


def test_connect_is_idempotent(tmp_path):
    """A fresh pearl and a five-year-old file take the same path."""
    p = str(tmp_path / "music.db")
    c1 = mc.connect(p)
    mc.upsert_track(c1, track())
    c1.commit()
    c1.close()
    c2 = mc.connect(p)
    assert mc.counts(c2)["tracks"] == 1
    assert mc.migrate(c2) == []      # nothing pending the second time
    c2.close()


def test_pending_migrations_fresh_file():
    steps = mc.pending_migrations(0, migrations=("A", "B"), target=2)
    assert steps == [(1, "A"), (2, "B")]


def test_pending_migrations_partway():
    assert mc.pending_migrations(1, migrations=("A", "B"), target=2) == [(2, "B")]


def test_pending_migrations_current_is_noop():
    assert mc.pending_migrations(2, migrations=("A", "B"), target=2) == []


def test_pending_migrations_newer_file_does_not_raise():
    """An older build opening a newer catalog reads what it can rather than
    refusing to start."""
    assert mc.pending_migrations(5, migrations=("A", "B"), target=2) == []


def test_migrate_applies_and_stamps(tmp_path):
    p = str(tmp_path / "m.db")
    c = mc.connect(p)
    conn_ver = c.execute("PRAGMA user_version").fetchone()[0]
    assert conn_ver == mc.SCHEMA_VERSION
    # Simulate an older file that predates a new column.
    c.execute("PRAGMA user_version=0")
    steps = mc.pending_migrations(
        0, migrations=("ALTER TABLE tracks ADD COLUMN mood TEXT;",), target=1)
    for version, sql in steps:
        c.executescript(sql)
        c.execute("PRAGMA user_version=%d" % version)
    cols = {r[1] for r in c.execute("PRAGMA table_info(tracks)")}
    assert "mood" in cols
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    c.close()


def test_v1_file_gains_codec_and_keeps_its_rows(tmp_path):
    """The seam's first real customer (issue #149), replayed against the
    thing it exists to protect: a file with pre-migration data. The row
    survives, the column appears, the stamp advances."""
    import sqlite3
    p = str(tmp_path / "v1.db")
    raw = sqlite3.connect(p)
    raw.execute("CREATE TABLE tracks (id TEXT PRIMARY KEY, title TEXT, "
                "format TEXT)")
    raw.execute("INSERT INTO tracks VALUES ('b:old', 'Kept', 'm4a')")
    raw.execute("PRAGMA user_version=1")
    raw.commit()
    raw.close()
    c = mc.connect(p)
    cols = {r[1] for r in c.execute("PRAGMA table_info(tracks)")}
    assert "codec" in cols
    assert c.execute("PRAGMA user_version").fetchone()[0] == mc.SCHEMA_VERSION
    row = c.execute("SELECT title, codec FROM tracks "
                    "WHERE id = 'b:old'").fetchone()
    assert row["title"] == "Kept"
    assert row["codec"] is None  # not probed yet -- the backfill's worklist
    c.close()


def test_fresh_file_is_stamped_not_replayed(tmp_path):
    """A fresh file's SCHEMA already holds the codec column; replaying the
    ALTER against it would die on "duplicate column". connect() must stamp
    it straight to SCHEMA_VERSION instead."""
    p = str(tmp_path / "fresh.db")
    c = mc.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == mc.SCHEMA_VERSION
    cols = {r[1] for r in c.execute("PRAGMA table_info(tracks)")}
    assert "codec" in cols
    c.close()


def test_crash_window_between_ddl_and_stamp_recovers(tmp_path):
    """A file whose schema is current but whose stamp is behind (the crash
    window between connect()'s DDL and its PRAGMA) must come back up, not
    crash-loop on "duplicate column"."""
    p = str(tmp_path / "torn.db")
    c = mc.connect(p)
    c.execute("PRAGMA user_version=1")  # roll the stamp back, keep the schema
    c.commit()
    c.close()
    c2 = mc.connect(p)  # replays the ALTER, tolerates the duplicate, stamps
    assert c2.execute("PRAGMA user_version").fetchone()[0] == mc.SCHEMA_VERSION
    c2.close()


# --- the codec column (issue #149) ----------------------------------------------

def test_codec_round_trips_through_upsert_and_track_info(conn):
    mc.upsert_track(conn, track(codec="alac"))
    assert mc.track_info(conn, "b:abc")["codec"] == "alac"


def test_set_codec_updates_one_track(conn):
    mc.upsert_track(conn, track())
    assert mc.track_info(conn, "b:abc")["codec"] is None
    mc.set_codec(conn, "b:abc", "aac")
    assert mc.track_info(conn, "b:abc")["codec"] == "aac"


def test_tracks_missing_codec_is_the_backfill_worklist(conn):
    """Only unprobed m4a/mp4 tracks belong on it -- an mp3 never carries a
    codec, and a probed track is done. Lowest path per track, so which copy
    of a duplicated rip gets probed is deterministic."""
    mc.upsert_track(conn, track(id="b:m4a"))
    mc.upsert_file(conn, entry(path="/mnt/music/z.m4a", track_id="b:m4a"))
    mc.upsert_file(conn, entry(path="/mnt/music/a.m4a", track_id="b:m4a"))
    mc.upsert_track(conn, track(id="b:mp3", format="mp3"))
    mc.upsert_file(conn, entry(path="/mnt/music/c.mp3", track_id="b:mp3"))
    mc.upsert_track(conn, track(id="b:done", codec="aac"))
    mc.upsert_file(conn, entry(path="/mnt/music/d.m4a", track_id="b:done"))
    assert mc.tracks_missing_codec(conn) == [("b:m4a", "/mnt/music/a.m4a")]


# --- tag normalization --------------------------------------------------------

def test_norm_tag_blanks_become_none():
    """iTunes writes "" and whitespace where a tag is absent. Storing those and
    NULL as two kinds of missing makes every later `IS NULL` quietly wrong."""
    assert mc.norm_tag("  ") is None
    assert mc.norm_tag("") is None
    assert mc.norm_tag(None) is None


def test_norm_tag_trims():
    assert mc.norm_tag("  Capital Cities ") == "Capital Cities"


def test_norm_int_handles_the_real_shapes():
    assert mc.norm_int("7/12") == 7        # iTunes' track-of-total
    assert mc.norm_int("7") == 7
    assert mc.norm_int(7) == 7
    assert mc.norm_int(" 3 / 5 ") == 3


def test_norm_int_junk_is_none_not_an_exception():
    """A malformed track number is a cosmetic loss, never a reason to fail a
    26,590-file pass."""
    assert mc.norm_int("side A") is None
    assert mc.norm_int("") is None
    assert mc.norm_int(None) is None


def test_track_row_missing_tags_become_null():
    row = dict(zip(mc.TRACK_COLUMNS, mc.track_row({"id": "b:x"})))
    assert row["id"] == "b:x"
    assert row["album_artist"] is None   # ~68% populated in the real library
    assert row["genre"] is None


# --- the hash cache -- the hottest decision in the pass ------------------------

def test_cache_valid_when_size_and_mtime_match():
    assert mc.cache_is_valid({"size": 100, "mtime": 200}, 100, 200)


def test_cache_invalid_when_unknown():
    assert not mc.cache_is_valid(None, 100, 200)


def test_cache_invalid_when_size_changes():
    assert not mc.cache_is_valid({"size": 100, "mtime": 200}, 101, 200)


def test_cache_invalid_when_only_mtime_changes():
    """THE MEASURED TRAP. A real mutagen retag padded within the existing tag
    space: the file size did not move at all, only mtime did. A cache keyed on
    size alone would call that file unchanged and skip it forever. Both fields
    are load-bearing; neither is redundant."""
    assert not mc.cache_is_valid({"size": 100, "mtime": 200}, 100, 999)


# --- identity survives what it exists to survive -------------------------------

def test_retag_keeps_the_id_and_refreshes_the_tags(conn):
    """The whole point of hashing the audio and not the file: a tag edit
    changes what we display and never the identity."""
    mc.upsert_track(conn, track(title="Old Title"))
    mc.rate(conn, "b:abc", mc.RATING_STRONG_UP, 5000)
    mc.upsert_track(conn, track(title="New Title"))

    assert mc.counts(conn)["tracks"] == 1
    row = conn.execute("SELECT title FROM tracks WHERE id='b:abc'").fetchone()
    assert row["title"] == "New Title"
    # The rating survived, still attached.
    r = conn.execute("SELECT value FROM ratings WHERE track_id='b:abc'").fetchone()
    assert r["value"] == mc.RATING_STRONG_UP


def test_reindex_does_not_wipe_phase1_analysis(conn):
    """An indexer re-run re-reads tags. It must not erase hours of
    BPM/ReplayGain work just because it did."""
    mc.upsert_track(conn, track())
    conn.execute("UPDATE tracks SET bpm=128.5, replaygain_db=-7.2 "
                 "WHERE id='b:abc'")
    mc.upsert_track(conn, track(title="Retagged"))
    row = conn.execute(
        "SELECT bpm, replaygain_db, title FROM tracks WHERE id='b:abc'"
    ).fetchone()
    assert row["bpm"] == 128.5
    assert row["replaygain_db"] == -7.2
    assert row["title"] == "Retagged"


def test_moved_file_updates_path_and_keeps_id(conn):
    """#120's acceptance criterion, as a test: a rename is a path update, never
    a new track."""
    mc.upsert_track(conn, track())
    mc.upsert_file(conn, entry(path="/mnt/music/old.m4a"))
    mc.rate(conn, "b:abc", mc.RATING_UP, 5000)

    # Same audio hash, new location.
    mc.upsert_file(conn, entry(path="/mnt/music/new.m4a"))
    mc.forget_paths(conn, ["/mnt/music/old.m4a"])

    assert mc.counts(conn)["tracks"] == 1
    assert mc.counts(conn)["track_files"] == 1
    row = conn.execute("SELECT path, track_id FROM track_files").fetchone()
    assert row["path"] == "/mnt/music/new.m4a"
    assert row["track_id"] == "b:abc"
    assert conn.execute(
        "SELECT value FROM ratings WHERE track_id='b:abc'").fetchone()["value"] == 1


def test_duplicate_rips_collapse_to_one_track_two_locations(conn):
    """A 26k library rips the same recording twice (an album and a greatest
    hits). Identical audio IS one track -- one `tracks` row, two locations --
    so rating it rates the recording, not whichever copy happened to play."""
    mc.upsert_track(conn, track())
    mc.upsert_file(conn, entry(path="/mnt/music/album/x.m4a"))
    mc.upsert_file(conn, entry(path="/mnt/music/hits/x.m4a"))

    assert mc.counts(conn)["tracks"] == 1
    assert mc.counts(conn)["track_files"] == 2


def test_forget_paths_keeps_the_track_and_its_history(conn):
    """A track we can't currently find is not a track that never existed. A
    file restored from a backup re-links to the same id on the next pass."""
    mc.upsert_track(conn, track())
    mc.upsert_file(conn, entry())
    mc.record_play(conn, "b:abc", 6000, mc.PLAY_COMPLETED, seconds=213.0)

    mc.forget_paths(conn, ["/mnt/music/a.m4a"])

    assert mc.counts(conn)["track_files"] == 0
    assert mc.counts(conn)["tracks"] == 1
    assert mc.counts(conn)["play_history"] == 1


def test_moved_files_reports_unseen_paths():
    assert mc.moved_files(["a", "b"], ["a", "b", "c"]) == ["c"]


def test_moved_files_is_not_a_delete_list():
    """A share that fails to mount presents as "every path vanished". This
    returns that fact; prune_is_safe() is what decides whether to act on it."""
    assert mc.moved_files([], ["a", "b"]) == ["a", "b"]


# --- the prune guard: the difference between a reorganize and a disaster ------

def test_prune_safe_when_the_pass_saw_the_library():
    assert mc.prune_is_safe(26000, 26590)


def test_prune_unsafe_when_the_share_did_not_mount():
    """THE FAILURE THIS EXISTS FOR. An unmounted share walks to zero files,
    which is indistinguishable from "the user deleted everything" -- and
    acting on it would wipe every location the catalog has."""
    assert not mc.prune_is_safe(0, 26590)


def test_prune_unsafe_when_the_share_half_mounted():
    assert not mc.prune_is_safe(10000, 26590)


def test_prune_safe_on_a_first_run():
    """Nothing known means nothing to prune -- trivially safe, and must not
    divide by zero."""
    assert mc.prune_is_safe(0, 0)


def test_prune_safe_through_a_big_but_plausible_reorganize():
    """The floor is deliberately generous: moving half the library at once is
    a real thing a person does, and the guard is here to catch a catastrophe,
    not to police churn."""
    assert mc.prune_is_safe(13295, 26590)


# --- ratings and play history: created now, unused, writable -------------------

def test_ratings_and_play_history_are_writable(conn):
    """#120's acceptance criterion. Nothing writes these until Phase 2; they
    exist from Phase 0 so implicit feedback starts the moment anything plays."""
    mc.upsert_track(conn, track())
    mc.rate(conn, "b:abc", mc.RATING_STRONG_DOWN, 7000)
    mc.record_play(conn, "b:abc", 7001, mc.PLAY_SKIPPED, seconds=12.0,
                   output="denon")
    assert mc.counts(conn)["ratings"] == 1
    assert mc.counts(conn)["play_history"] == 1


def test_rerating_replaces_rather_than_appends(conn):
    """`ratings` says what the listener thinks NOW. A history of opinion
    changes is not something Phase 3 or 4 asked for."""
    mc.upsert_track(conn, track())
    mc.rate(conn, "b:abc", mc.RATING_DOWN, 7000)
    mc.rate(conn, "b:abc", mc.RATING_STRONG_UP, 8000)
    assert mc.counts(conn)["ratings"] == 1
    row = conn.execute("SELECT value, rated_at FROM ratings").fetchone()
    assert row["value"] == mc.RATING_STRONG_UP
    assert row["rated_at"] == 8000


def test_rate_rejects_a_value_outside_the_four_levels(conn):
    """The feedback model is four levels. A 5 landing here would sail straight
    through Phase 3's weighting and quietly outrank every real rating."""
    mc.upsert_track(conn, track())
    with pytest.raises(ValueError):
        mc.rate(conn, "b:abc", 5, 7000)


def test_rate_rejects_a_bool(conn):
    """bool subclasses int, so `True in RATING_VALUES` is True and a JSON
    `true` off the wire would file itself as a thumbs-up (issue #135)."""
    mc.upsert_track(conn, track())
    with pytest.raises(ValueError):
        mc.rate(conn, "b:abc", True, 7000)
    with pytest.raises(ValueError):
        mc.rate(conn, "b:abc", False, 7000)
    assert mc.counts(conn)["ratings"] == 0


def test_rate_rejects_zero(conn):
    """Zero is the CONTROL's third click, not a stored value: an unrated track
    is the absence of a row. The daemon dispatches 0 to unrate()."""
    mc.upsert_track(conn, track())
    with pytest.raises(ValueError):
        mc.rate(conn, "b:abc", 0, 7000)


def test_unrate_removes_the_row(conn):
    mc.upsert_track(conn, track())
    mc.rate(conn, "b:abc", mc.RATING_STRONG_UP, 7000)
    mc.unrate(conn, "b:abc")
    assert mc.counts(conn)["ratings"] == 0


def test_unrate_of_an_unrated_track_is_silent(conn):
    """Clearing nothing is the state the caller asked for."""
    mc.upsert_track(conn, track())
    mc.unrate(conn, "b:abc")  # must not raise
    assert mc.counts(conn)["ratings"] == 0


def test_unrate_leaves_other_tracks_alone(conn):
    mc.upsert_track(conn, track())
    mc.upsert_track(conn, track(id="b:def"))
    mc.rate(conn, "b:abc", mc.RATING_UP, 7000)
    mc.rate(conn, "b:def", mc.RATING_DOWN, 7000)
    mc.unrate(conn, "b:abc")
    rows = conn.execute("SELECT track_id FROM ratings").fetchall()
    assert [r["track_id"] for r in rows] == ["b:def"]


def test_play_history_appends_rather_than_updates(conn):
    """A skip at 0:12 and a completion an hour later are two facts, not a
    correction of one."""
    mc.upsert_track(conn, track())
    mc.record_play(conn, "b:abc", 7000, mc.PLAY_SKIPPED, seconds=12.0)
    mc.record_play(conn, "b:abc", 10600, mc.PLAY_COMPLETED, seconds=213.0)
    assert mc.counts(conn)["play_history"] == 2


# --- the daemon's reads (issue #129) -------------------------------------------

def test_valid_track_id_accepts_every_id_shape_the_indexer_mints():
    for tid in ("b:1fbc567c3e773e30b70b89b79f7e3783", "f:0a" * 16, "x:deadbeef"):
        assert mc.valid_track_id(tid)


def test_valid_track_id_rejects_traversal_and_junk():
    """The frame_archiver genre: anything shaped wrong dies at the allowlist,
    not on the filesystem. These are the actual attack shapes, not typos."""
    for tid in ("../../../etc/passwd", "b:abc/../..", "b:abc%2F..", "",
                "b:abc def", "b:abc\x00", "b:abc\n", ".", "/mnt/music/x.m4a"):
        assert not mc.valid_track_id(tid)


def test_track_info_returns_the_daemon_fields(conn):
    mc.upsert_track(conn, track(duration_s=193.0))
    info = mc.track_info(conn, "b:abc")
    assert info == {"id": "b:abc", "title": "Safe and Sound",
                    "artist": "Capital Cities",
                    "album": "In a Tidal Wave of Mystery",
                    "duration_s": 193.0, "format": "m4a", "codec": None}


def test_track_info_unknown_id_is_none_not_an_error(conn):
    assert mc.track_info(conn, "b:nope") is None


def test_file_for_track_picks_the_lowest_path_of_duplicates(conn):
    """The album rip and the greatest-hits rip share one id; which copy
    streams must not depend on row order."""
    mc.upsert_track(conn, track())
    mc.upsert_file(conn, entry(path="/mnt/music/z-greatest-hits/a.m4a"))
    mc.upsert_file(conn, entry(path="/mnt/music/album/a.m4a"))
    assert mc.file_for_track(conn, "b:abc")["path"] == "/mnt/music/album/a.m4a"


def test_file_for_track_unknown_id_is_none(conn):
    assert mc.file_for_track(conn, "b:nope") is None


# --- config -------------------------------------------------------------------

def test_db_path_unset_or_blank_is_the_default(monkeypatch):
    """The MERLE_* rule: unset OR blank means the default -- never a
    half-configured run against a file nothing writes."""
    monkeypatch.delenv("MERLE_MUSIC_DB", raising=False)
    assert mc.db_path() == mc.DEFAULT_DB_PATH
    monkeypatch.setenv("MERLE_MUSIC_DB", "   ")
    assert mc.db_path() == mc.DEFAULT_DB_PATH


def test_db_path_honors_the_env(monkeypatch):
    monkeypatch.setenv("MERLE_MUSIC_DB", "/srv/music.db")
    assert mc.db_path() == "/srv/music.db"


# --- the art store (issue #153) --------------------------------------------------

def test_album_key_sql_matches_the_gui_derivation(conn):
    """THE cross-language contract: this exact string, base64url'd, is the
    GUI's album id. music/lib/catalog-rows.test.ts pins the same fixtures
    through albumIdOf -- change one side and the paired test on the other
    is what catches you."""
    mc.upsert_track(conn, track(id="b:1", artist="Capital Cities",
                                album="In A Tidal Wave Of Mystery"))
    mc.upsert_file(conn, entry(path="/m/a.m4a", track_id="b:1"))
    # A compilation: album_artist wins over the track artist.
    mc.upsert_track(conn, track(id="b:2", artist="Some Performer",
                                album_artist="Various Artists",
                                album="Now That's Music"))
    mc.upsert_file(conn, entry(path="/m/b.m4a", track_id="b:2"))
    # The nameless tail: NULLs fall to the GUI's display fallbacks.
    mc.upsert_track(conn, track(id="b:3", artist=None, album_artist=None,
                                album=None, title="Mystery"))
    mc.upsert_file(conn, entry(path="/m/c.m4a", track_id="b:3"))
    # The canonical identity leads the key when the pass has run (#152):
    # a minority-cased tag mints the CANONICAL key, so art cannot strand.
    mc.upsert_track(conn, track(id="b:4", artist="Gwar",
                                album="Scumdogs of the Universe"))
    mc.upsert_file(conn, entry(path="/m/d.m4a", track_id="b:4"))
    conn.execute("UPDATE tracks SET artist_norm = 'GWAR' WHERE id = 'b:4'")
    keys = sorted(mc.albums_missing_art(conn))
    assert keys == [
        "Capital Cities␟In A Tidal Wave Of Mystery",
        "GWAR␟Scumdogs of the Universe",
        "Unknown Artist␟Unknown Album",
        "Various Artists␟Now That's Music",
    ]


def test_albums_missing_art_is_a_worklist_that_shrinks(conn):
    """The reusability rule in miniature: rows with art drop off, and an
    empty worklist is what makes the ingestion-era re-run a no-op."""
    mc.upsert_track(conn, track(id="b:1", album="One"))
    mc.upsert_file(conn, entry(path="/m/1.m4a", track_id="b:1"))
    mc.upsert_track(conn, track(id="b:2", album="Two"))
    mc.upsert_file(conn, entry(path="/m/2.m4a", track_id="b:2"))
    assert len(mc.albums_missing_art(conn)) == 2
    mc.set_album_art(conn, "Capital Cities␟One", "h1", mc.ART_EMBEDDED,
                     500, 500, 1000)
    work = mc.albums_missing_art(conn)
    assert list(work) == ["Capital Cities␟Two"]
    assert work["Capital Cities␟Two"] == ["/m/2.m4a"]


def test_owner_art_survives_every_automated_source(conn):
    """The provenance rule, enforced in SQL: once the listener has chosen,
    no pass -- embedded, folder, or derived -- may overwrite it."""
    mc.set_album_art(conn, "A␟X", "owners-pick", mc.ART_OWNER, 1, 1, 1000)
    for source in (mc.ART_EMBEDDED, mc.ART_FOLDER, mc.ART_DERIVED):
        mc.set_album_art(conn, "A␟X", "machine-pick", source, 2, 2, 2000)
    row = conn.execute("SELECT art_hash, source FROM album_art "
                       "WHERE album_key = 'A␟X'").fetchone()
    assert row["art_hash"] == "owners-pick"
    assert row["source"] == mc.ART_OWNER


def test_non_owner_art_refreshes(conn):
    mc.set_album_art(conn, "A␟X", "old", mc.ART_FOLDER, 1, 1, 1000)
    mc.set_album_art(conn, "A␟X", "new", mc.ART_EMBEDDED, 2, 2, 2000)
    row = conn.execute("SELECT art_hash, source FROM album_art").fetchone()
    assert (row["art_hash"], row["source"]) == ("new", mc.ART_EMBEDDED)


def test_owner_artist_art_survives_the_promotion_pass(conn):
    mc.set_artist_art(conn, "Capital Cities", "todds-photo", mc.ART_OWNER,
                      800, 600, 1000)
    mc.set_artist_art(conn, "Capital Cities", "derived-cover",
                      mc.ART_DERIVED, 500, 500, 2000)
    row = conn.execute("SELECT art_hash FROM artist_art").fetchone()
    assert row["art_hash"] == "todds-photo"


def test_albums_missing_note_is_the_blurb_worklist(conn):
    """albums_missing_art's twin (issue #171): an album with a note is off
    the list, so a re-run after ingesting new albums touches only those."""
    mc.upsert_track(conn, track(id="b:1", album="One"))
    mc.upsert_file(conn, entry(path="/m/1.m4a", track_id="b:1"))
    mc.upsert_track(conn, track(id="b:2", album="Two"))
    mc.upsert_file(conn, entry(path="/m/2.m4a", track_id="b:2"))
    assert len(mc.albums_missing_note(conn)) == 2
    mc.set_album_note(conn, "Capital Cities␟One", "A description.",
                      "A description.", mc.NOTE_COMMENT, False, 1000)
    work = mc.albums_missing_note(conn)
    assert list(work) == ["Capital Cities␟Two"]
    assert work["Capital Cities␟Two"] == ["/m/2.m4a"]


def test_owner_note_survives_every_automated_source(conn):
    """The provenance rule a third time (issue #171). This one matters more
    than its siblings: the planned refresh button writes through the same
    function from a request handler, where "the pass skipped it" reasoning
    doesn't apply -- only the SQL guard does."""
    mc.set_album_note(conn, "A␟X", "Todd's own words.", "Todd's own words.",
                      mc.NOTE_OWNER, False, 1000)
    for source in (mc.NOTE_COMMENT, mc.NOTE_EXTERNAL):
        mc.set_album_note(conn, "A␟X", "machine copy", "machine copy",
                          source, False, 2000)
    row = conn.execute("SELECT description, source FROM album_notes "
                       "WHERE album_key = 'A␟X'").fetchone()
    assert row["description"] == "Todd's own words."
    assert row["source"] == mc.NOTE_OWNER


def test_a_fetched_note_may_supersede_the_comment_tag(conn):
    """The store copy is a floor, not a ceiling -- a richer fetched source
    refreshes it, same as folder art yielding to embedded."""
    mc.set_album_note(conn, "A␟X", "store copy", "store copy",
                      mc.NOTE_COMMENT, True, 1000)
    mc.set_album_note(conn, "A␟X", "wikipedia prose", "wikipedia prose",
                      mc.NOTE_EXTERNAL, False, 2000)
    row = conn.execute("SELECT description, source, truncated "
                       "FROM album_notes").fetchone()
    assert (row["description"], row["source"]) == ("wikipedia prose",
                                                   mc.NOTE_EXTERNAL)
    assert row["truncated"] == 0


def test_album_paths_is_the_single_album_twin_of_the_worklist(conn):
    """The refresh button and the bulk pass must read the same files through
    the same album-key derivation, or a one-off refresh could disagree with
    what a full run would have produced."""
    mc.upsert_track(conn, track(id="b:1", album="One"))
    mc.upsert_file(conn, entry(path="/m/b.m4a", track_id="b:1"))
    mc.upsert_file(conn, entry(path="/m/a.m4a", track_id="b:1"))
    mc.upsert_track(conn, track(id="b:2", album="Two"))
    mc.upsert_file(conn, entry(path="/m/2.m4a", track_id="b:2"))
    assert mc.album_paths(conn, "Capital Cities␟One") == ["/m/a.m4a",
                                                          "/m/b.m4a"]
    assert mc.album_paths(conn, "Capital Cities␟Nope") == []


def test_artists_missing_art_scores_by_summed_thumbs(conn):
    """The promotion worklist carries the score; the pure pick sorts it.
    An artist with a row already -- owner or derived -- is off the list."""
    mc.upsert_track(conn, track(id="b:1", album="Loved"))
    mc.upsert_file(conn, entry(path="/m/1.m4a", track_id="b:1"))
    mc.upsert_track(conn, track(id="b:2", album="Meh"))
    mc.upsert_file(conn, entry(path="/m/2.m4a", track_id="b:2"))
    mc.set_album_art(conn, "Capital Cities␟Loved", "h-loved",
                     mc.ART_EMBEDDED, 1, 1, 1000)
    mc.set_album_art(conn, "Capital Cities␟Meh", "h-meh",
                     mc.ART_EMBEDDED, 1, 1, 1000)
    mc.rate(conn, "b:1", 2, 1000)
    rows = mc.artists_missing_art(conn)
    scores = {r["album_key"]: r["score"] for r in rows}
    assert scores == {"Capital Cities␟Loved": 2, "Capital Cities␟Meh": 0}
    mc.set_artist_art(conn, "Capital Cities", "h-loved", mc.ART_DERIVED,
                      1, 1, 1000)
    assert mc.artists_missing_art(conn) == []
