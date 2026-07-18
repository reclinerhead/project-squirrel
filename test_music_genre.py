# Tests for jukebox/music_genre.py (issue #163): rules parsing/validation,
# the file->table sync, precedence, inheritance arithmetic, provenance
# protection, and the pass's idempotence. Pure logic against :memory:
# catalogs -- no rules file on disk except the one test that pins the SHIPPED
# genre_rules.yaml as valid (the typo canary: a bad edit fails CI before it
# half-normalizes a catalog on pearl).

import pytest

from jukebox import music_catalog as mc
from jukebox import music_genre as mg
from jukebox import music_playlist as mp

# A minimal valid rules file. Raw strings with backslashes stay single-quoted
# (the real file's convention); the Python source uses raw strings so the
# backslash reaches YAML intact.
MINI_RULES = r"""
vocabulary: [Rock, Electronic, Folk]
map:
  Rock:
    - Rock
    - 'Rock\Hard'
  Electronic:
    - Electronica
  Folk:
    - Folk
artist_overrides: {}
clusters:
  - [Rock, Electronic]
tuning:
  inherit_threshold: 0.6
  triage:
    - 'Pop\Rock'
"""


def rules(text=MINI_RULES):
    return mg.parse_rules(text)


def catalog():
    return mc.connect(":memory:")


def add_track(conn, tid, genre=None, artist="A", album_artist=None,
              norm=None, norm_source=None):
    mc.upsert_track(conn, {"id": tid, "title": tid, "artist": artist,
                           "album_artist": album_artist, "album": "Al",
                           "genre": genre, "indexed_at": 1})
    if norm is not None or norm_source is not None:
        conn.execute("UPDATE tracks SET genre_norm = ?, genre_norm_source = ? "
                     "WHERE id = ?", (norm, norm_source, tid))
    conn.commit()


def norm_of(conn, tid):
    r = conn.execute("SELECT genre_norm, genre_norm_source FROM tracks "
                     "WHERE id = ?", (tid,)).fetchone()
    return (r["genre_norm"], r["genre_norm_source"])


# --- parsing and validation -------------------------------------------------------

def test_a_valid_file_flattens_to_the_lookup_direction():
    r = rules()
    assert r["map"]["Rock\\Hard"] == "Rock"
    assert r["map"]["Electronica"] == "Electronic"
    assert r["inherit_threshold"] == 0.6
    assert r["triage"] == {"Pop\\Rock"}
    assert r["clusters"] == (frozenset({"Rock", "Electronic"}),)


def test_a_map_target_outside_the_vocabulary_is_rejected():
    with pytest.raises(mg.RulesError, match="not in vocabulary"):
        rules(MINI_RULES.replace("  Folk:\n    - Folk",
                                 "  Jazz:\n    - Folk"))


def test_a_raw_string_maps_once():
    with pytest.raises(mg.RulesError, match="maps to both"):
        rules(MINI_RULES.replace("  Folk:\n    - Folk",
                                 "  Folk:\n    - Folk\n    - Rock"))


def test_unknown_and_missing_sections_are_rejected():
    with pytest.raises(mg.RulesError, match="unknown section"):
        rules(MINI_RULES + "\nextras: {}\n")
    with pytest.raises(mg.RulesError, match="missing section"):
        rules(MINI_RULES.replace("artist_overrides: {}", ""))


def test_a_string_is_mapped_or_deferred_never_both():
    with pytest.raises(mg.RulesError, match="never both"):
        rules(MINI_RULES.replace("- 'Pop\\Rock'", "- Rock"))


def test_a_minority_threshold_is_a_lie():
    with pytest.raises(mg.RulesError, match="inherit_threshold"):
        rules(MINI_RULES.replace("inherit_threshold: 0.6",
                                 "inherit_threshold: 0.4"))


def test_a_cluster_member_outside_the_vocabulary_is_rejected():
    with pytest.raises(mg.RulesError, match="not in vocabulary"):
        rules(MINI_RULES.replace("[Rock, Electronic]", "[Rock, Jazz]"))


def test_an_override_target_outside_the_vocabulary_is_rejected():
    with pytest.raises(mg.RulesError, match="not in vocabulary"):
        rules(MINI_RULES.replace("artist_overrides: {}",
                                 "artist_overrides: {Enya: New Age}"))


def test_the_shipped_rules_file_is_valid():
    """The typo canary: the repo's live ruleset must always load. A broken
    edit fails HERE, in CI, not on pearl mid-pass."""
    r = mg.load_rules(mg.DEFAULT_RULES_PATH)
    assert len(r["vocabulary"]) == 22
    # 178 distinct raw strings measured in the live catalog (2026-07-18):
    # 173 mapped + the 5 triage junk drawers. Verified complete against the
    # DB at ship time; new strings surface via the pass's unmapped report.
    assert len(r["map"]) == 173
    assert r["triage"] == {"Pop\\Rock", "Pop/Rock", "Styles", "Popular",
                           "Other"}


def test_engine_clusters_speak_head_tokens():
    """'R&B/Soul' arrives at the engine as genre_head 'r&b' -- a family
    listing the display form would silently never match it."""
    r = rules(MINI_RULES.replace("vocabulary: [Rock, Electronic, Folk]",
                                 "vocabulary: [Rock, Electronic, Folk, R&B/Soul]")
              .replace("[Rock, Electronic]", "[Electronic, R&B/Soul]"))
    assert mg.engine_clusters(r) == (frozenset({"electronic", "r&b"}),)


# --- inheritance arithmetic -------------------------------------------------------

def test_majority_at_the_threshold_inherits_below_does_not():
    at = mg.artist_majorities(
        [("A", "Rock")] * 3 + [("A", "Electronic")] * 2, 0.6)
    assert at == {"A": "Rock"}  # 3 of 5 = exactly 0.6
    below = mg.artist_majorities(
        [("A", "Rock")] * 2 + [("A", "Electronic")] * 2, 0.6)
    assert below == {}


def test_majority_ties_do_not_depend_on_row_order():
    votes = [("A", "Rock"), ("A", "Electronic")]
    assert mg.artist_majorities(votes, 0.5) == \
        mg.artist_majorities(list(reversed(votes)), 0.5)


def test_precedence_override_beats_map_beats_inheritance():
    rulemap = {"Electronica": "Electronic"}
    overrides = {"A": "Folk"}
    majorities = {"B": "Rock"}
    assert mg.expected_norm("Electronica", "A", rulemap, overrides,
                            majorities) == ("Folk", "mapped")
    assert mg.expected_norm("Electronica", "B", rulemap, {}, majorities) \
        == ("Electronic", "mapped")
    assert mg.expected_norm("Weird", "B", rulemap, {}, majorities) \
        == ("Rock", "inherited")
    assert mg.expected_norm("Weird", "C", rulemap, {}, majorities) \
        == (None, None)


# --- the file -> table sync -------------------------------------------------------

def test_sync_prunes_stale_file_rows_but_never_external_ones():
    conn = catalog()
    conn.execute("INSERT INTO genre_map VALUES ('folk rock', 'Folk', "
                 "'external')")
    mg.sync_rules(conn, rules())
    rows = {r["raw"]: (r["canonical"], r["source"]) for r in
            conn.execute("SELECT * FROM genre_map")}
    assert rows["Electronica"] == ("Electronic", "file")
    assert rows["folk rock"] == ("Folk", "external")
    # An edit that drops Folk from the map prunes its file row only.
    edited = rules(MINI_RULES.replace("  Folk:\n    - Folk\n", ""))
    mg.sync_rules(conn, edited)
    rows = {r["raw"]: r["source"] for r in
            conn.execute("SELECT * FROM genre_map")}
    assert "Folk" not in rows
    assert rows["folk rock"] == "external"


# --- the pass ---------------------------------------------------------------------

def test_normalize_maps_inherits_and_leaves_honest_nulls():
    conn = catalog()
    add_track(conn, "b:1", genre="Rock\\Hard", artist="A")
    add_track(conn, "b:2", genre="Electronica", artist="B")
    # B's second track has junk -- but B is 100% Electronic where mapped.
    add_track(conn, "b:3", genre="Weird Tag", artist="B")
    # C has nothing mapped anywhere: honest NULL.
    add_track(conn, "b:4", genre="Also Weird", artist="C")
    add_track(conn, "b:5", genre=None, artist="C")
    report = mg.normalize(conn, rules())
    assert norm_of(conn, "b:1") == ("Rock", "mapped")
    assert norm_of(conn, "b:2") == ("Electronic", "mapped")
    assert norm_of(conn, "b:3") == ("Electronic", "inherited")
    assert norm_of(conn, "b:4") == (None, None)
    assert norm_of(conn, "b:5") == (None, None)
    assert report["written"] == 3
    assert report["unmapped"] == {"Weird Tag": 1, "Also Weird": 1}


def test_triage_strings_stay_null_and_off_the_report():
    conn = catalog()
    add_track(conn, "b:1", genre="Pop\\Rock", artist="A")
    report = mg.normalize(conn, rules())
    assert norm_of(conn, "b:1") == (None, None)
    assert report["unmapped"] == {}


def test_the_album_artist_is_the_inheritance_identity():
    """Compilation guests must not fragment the majority -- the artist key
    is COALESCE(album_artist, artist), every album query's rule."""
    conn = catalog()
    add_track(conn, "b:1", genre="Electronica", artist="Guest One",
              album_artist="Buddha Bar")
    add_track(conn, "b:2", genre="Electronica", artist="Guest Two",
              album_artist="Buddha Bar")
    add_track(conn, "b:3", genre="Oddball", artist="Guest Three",
              album_artist="Buddha Bar")
    mg.normalize(conn, rules())
    assert norm_of(conn, "b:3") == ("Electronic", "inherited")


def test_an_owner_row_survives_every_re_run():
    conn = catalog()
    add_track(conn, "b:1", genre="Electronica", artist="A",
              norm="Folk", norm_source="owner")
    report = mg.normalize(conn, rules())
    assert norm_of(conn, "b:1") == ("Folk", "owner")
    assert report["owner_kept"] == 1
    assert report["written"] == 0


def test_an_external_row_yields_only_to_an_explicit_rule():
    conn = catalog()
    # No rule reaches this track: the backfill's value survives.
    add_track(conn, "b:1", genre="Mystery", artist="A",
              norm="Folk", norm_source="external")
    mg.normalize(conn, rules())
    assert norm_of(conn, "b:1") == ("Folk", "external")
    # A rule now maps its raw tag: the explicit rule wins.
    edited = rules(MINI_RULES.replace("  Electronic:\n    - Electronica",
                                      "  Electronic:\n    - Electronica\n"
                                      "    - Mystery"))
    mg.normalize(conn, edited)
    assert norm_of(conn, "b:1") == ("Electronic", "mapped")


def test_the_same_run_twice_writes_nothing():
    conn = catalog()
    add_track(conn, "b:1", genre="Rock", artist="A")
    add_track(conn, "b:2", genre="Weird", artist="A")
    assert mg.normalize(conn, rules())["written"] == 2
    assert mg.normalize(conn, rules())["written"] == 0


def test_a_rules_edit_remaps_non_owner_rows():
    """The owner requirement verbatim: tweak the file, re-run, the catalog
    follows -- no separate backfill tool."""
    conn = catalog()
    add_track(conn, "b:1", genre="Electronica", artist="A")
    mg.normalize(conn, rules())
    assert norm_of(conn, "b:1") == ("Electronic", "mapped")
    edited = rules(MINI_RULES.replace("  Electronic:\n    - Electronica",
                                      "  Electronic: []\n")
                   .replace("  Folk:\n    - Folk",
                            "  Folk:\n    - Folk\n    - Electronica"))
    mg.normalize(conn, edited)
    assert norm_of(conn, "b:1") == ("Folk", "mapped")


def test_dry_run_reports_and_writes_nothing():
    conn = catalog()
    add_track(conn, "b:1", genre="Rock", artist="A")
    report = mg.normalize(conn, rules(), dry_run=True)
    assert report["would_write"] == 1 and report["written"] == 0
    assert norm_of(conn, "b:1") == (None, None)
    # Not even the rules sync lands -- a dry run previews with zero writes.
    assert conn.execute("SELECT COUNT(*) FROM genre_map").fetchone()[0] == 0


def test_a_reindex_upsert_does_not_wipe_the_normalization():
    """genre_norm is not in TRACK_COLUMNS, so the indexer's tag-refresh
    upsert can't touch it -- the analysis-columns rule extended."""
    conn = catalog()
    add_track(conn, "b:1", genre="Rock", artist="A")
    mg.normalize(conn, rules())
    mc.upsert_track(conn, {"id": "b:1", "title": "Retagged", "artist": "A",
                           "album": "Al", "genre": "Rock", "indexed_at": 2})
    assert norm_of(conn, "b:1") == ("Rock", "mapped")


# --- the engine consumes injected families ----------------------------------------

def test_injected_clusters_replace_the_default_families():
    fam = (frozenset({"metal", "rock"}),)
    assert mp.genre_affinity("Metal", "Rock", fam) == mp.SAME_CLUSTER_BONUS
    assert mp.genre_affinity("Metal", "Rock") == 0.0  # default: no kinship
