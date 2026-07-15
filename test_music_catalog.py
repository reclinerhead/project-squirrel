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
        "tracks", "track_files", "artists", "ratings", "play_history"}


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


def test_play_history_appends_rather_than_updates(conn):
    """A skip at 0:12 and a completion an hour later are two facts, not a
    correction of one."""
    mc.upsert_track(conn, track())
    mc.record_play(conn, "b:abc", 7000, mc.PLAY_SKIPPED, seconds=12.0)
    mc.record_play(conn, "b:abc", 10600, mc.PLAY_COMPLETED, seconds=213.0)
    assert mc.counts(conn)["play_history"] == 2


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
