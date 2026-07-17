# =============================================================================
# project-squirrel -- test_music_playlist.py
#
# The playlist engine (issue #139) -- the most test-worthy code in the epic,
# per the epic itself. Everything here runs the pure functions with injected
# candidates, clocks, and seeded RNGs; the one section that touches SQLite
# uses a :memory: catalog to prove the daemon's candidate WHERE clause -- the
# strong-down ban and the cooldown are enforced by the QUERY, and observation
# is not proof.
#
# NOTE: this file must be on .github/workflows/tests.yml's pytest line by
# hand. CI enumerates test files and has no pytest.ini/testpaths fallback.
# =============================================================================

import random

import pytest

from jukebox import music_catalog as mc
from jukebox import music_daemon as md
from jukebox import music_playlist as mp

NOW = 1_800_000_000.0
HOUR = 3600.0


def track(tid, artist="Artist %s", bpm=120.0, gain=-8.0, dr=6.0, year=1992,
          genre="Electronic", rating=None, last_played_at=None):
    """One synthetic candidate. Distinct artists by default so the spacing
    rule stays out of tests that aren't about it."""
    return {
        "id": tid,
        "artist": artist % tid if "%s" in artist else artist,
        "bpm": bpm, "replaygain_db": gain, "dynamic_range_db": dr,
        "year": year, "genre": genre, "rating": rating,
        "last_played_at": last_played_at,
    }


def seed_for(t):
    return {"track": dict(t)}


# --- octave folding and the lattice clamp ---------------------------------------

def test_half_and_double_time_score_like_an_exact_match():
    """Half time and double time are the same groove: 60 and 240 BPM fold
    onto a 120 target exactly, while 10% off is genuinely elsewhere."""
    assert mp.tempo_penalty(120.0, 120.0) == 0.0
    assert mp.tempo_penalty(240.0, 120.0) == 0.0
    assert mp.tempo_penalty(60.0, 120.0) == 0.0
    off = mp.tempo_penalty(132.0, 120.0)
    assert off > 0.0
    assert off > mp.tempo_penalty(240.0, 120.0)


def test_ten_percent_off_costs_about_three():
    """The issue's calibration point: 10% off ~ 3.0 penalty at weight 22."""
    assert mp.tempo_penalty(132.0, 120.0) == pytest.approx(3.0, abs=0.15)


def test_distance_below_one_lattice_bin_is_a_dead_tie():
    """librosa reports tempo on a ~4-5% lattice; distance under one bin is
    quantization noise, not music, and must not be scored."""
    assert mp.tempo_penalty(112.3, 112.3 * 2 ** 0.02) == 0.0
    assert mp.tempo_penalty(112.3, 112.3 * 2 ** 0.05) > 0.0


def test_missing_tempo_is_neutral():
    assert mp.tempo_penalty(None, 120.0) == 0.0
    assert mp.tempo_penalty(120.0, None) == 0.0
    assert mp.tempo_penalty(0, 120.0) == 0.0


# --- genre families --------------------------------------------------------------

def test_the_feral_taxonomy_maps_to_head_tokens():
    assert mp.genre_head("ELECTRONICA\\DUBSTEP") == "electronica"
    assert mp.genre_head("Hip-Hop/Rap") == "hip-hop"
    assert mp.genre_head("Rap & Hip-Hop") == "rap & hip-hop"
    assert mp.genre_head("  Rock  ") == "rock"
    assert mp.genre_head(None) is None
    assert mp.genre_head("") is None


def test_cluster_kinship_beats_string_equality():
    """ELECTRONICA\\DUBSTEP and Electronic share no exact tag, but they are
    family -- the lesson that fixed Queensryche at #2 for a Stereo MC's
    seed."""
    assert mp.genre_affinity("ELECTRONICA\\DUBSTEP",
                             "Electronic") == mp.SAME_CLUSTER_BONUS
    assert mp.genre_affinity("Hip-Hop/Rap",
                             "ELECTRONICA\\DUBSTEP") == mp.SAME_CLUSTER_BONUS


def test_same_head_token_is_the_strongest_signal():
    assert mp.genre_affinity("Electronic", "electronic") == mp.SAME_HEAD_BONUS
    assert mp.SAME_HEAD_BONUS < mp.SAME_CLUSTER_BONUS  # more negative


def test_wrong_planet_gets_no_bonus():
    assert mp.genre_affinity("Rock\\Classic", "Electronic") == 0.0
    assert mp.genre_affinity("FLAC", "Electronic") == 0.0


def test_a_missing_genre_is_neutral_never_fatal():
    assert mp.genre_affinity(None, "Electronic") == 0.0
    assert mp.genre_affinity("Electronic", None) == 0.0
    t = track("b:1")
    t["genre"] = None
    t["year"] = None
    assert isinstance(mp.score_track(t, {"bpm": 120.0}), float)


# --- ratings are rules, and the engine shines with zero of them ------------------

def test_ratings_shade_the_ranking_in_thumb_order():
    """+2 outranks +1 outranks unrated outranks -1, all else equal."""
    target = {"bpm": 120.0, "replaygain_db": -8.0,
              "dynamic_range_db": 6.0, "year": 1992, "genre": "Electronic"}
    scores = {r: mp.score_track(track("b:1", rating=r), target)
              for r in (2, 1, None, -1)}
    assert scores[2] < scores[1] < scores[None] < scores[-1]


def test_an_empty_ratings_table_is_a_full_quality_queue():
    """Today's actual state: ~0 ratings rows. Candidates with no rating key
    at all must queue identically to candidates explicitly unrated -- 'no
    opinions yet' is not an error path."""
    bare = []
    for i in range(30):
        t = track("b:%02d" % i, bpm=115.0 + i)
        del t["rating"]
        bare.append(t)
    rated_none = [dict(t, rating=None) for t in bare]
    seed = seed_for(track("b:seed", bpm=120.0))
    q1 = mp.build_queue(bare, seed, 10, random.Random(7), NOW)
    q2 = mp.build_queue(rated_none, seed, 10, random.Random(7), NOW)
    assert len(q1) == 10
    assert [t["id"] for t in q1] == [t["id"] for t in q2]


# --- the strong-down hard filter: enforced by the query, proven here -------------

@pytest.fixture
def catalog():
    """A :memory: catalog with 12 analyzed, located tracks -- the daemon's
    candidate query runs against the real schema, not a stand-in."""
    conn = mc.connect(":memory:")
    for i in range(12):
        tid = "b:%02d" % i
        mc.upsert_track(conn, {
            "id": tid, "title": "Track %d" % i, "artist": "Artist %d" % i,
            "album": "Album", "year": 1990 + i, "genre": "Electronic",
            "duration_s": 200.0, "format": "m4a", "bpm": 110.0 + i,
            "replaygain_db": -8.0, "dynamic_range_db": 6.0,
            "indexed_at": 1000})
        mc.upsert_file(conn, {
            "path": "/mnt/music/%s.m4a" % tid, "track_id": tid,
            "size": 1000, "mtime": 100, "seen_at": 1000})
    return conn


def test_a_strong_down_track_is_absent_from_the_candidate_set(catalog):
    mc.rate(catalog, "b:03", mc.RATING_STRONG_DOWN, int(NOW))
    ids = {t["id"] for t in md.queue_candidates(catalog, NOW)}
    assert "b:03" not in ids
    assert len(ids) == 11


def test_a_strong_down_track_cannot_appear_in_any_queue(catalog):
    """Asserted over many seeded runs, not observed once. The ban is the
    query's; the engine never even hears about the track."""
    mc.rate(catalog, "b:03", mc.RATING_STRONG_DOWN, int(NOW))
    candidates = md.queue_candidates(catalog, NOW)
    seed = seed_for(track("b:seed", bpm=113.0))
    for i in range(50):
        queue = mp.build_queue(candidates, seed, 8, random.Random(i), NOW)
        assert "b:03" not in [t["id"] for t in queue]


def test_every_other_rating_stays_in_the_running(catalog):
    for tid, value in (("b:01", -1), ("b:02", 1), ("b:04", 2)):
        mc.rate(catalog, tid, value, int(NOW))
    ids = {t["id"] for t in md.queue_candidates(catalog, NOW)}
    assert {"b:01", "b:02", "b:04"} <= ids


def test_the_query_requires_analysis_and_a_location(catalog):
    """bpm IS NOT NULL and a track_files row: the engine scores mood axes,
    and the daemon must never queue a track it cannot stream."""
    mc.upsert_track(catalog, {
        "id": "b:nobpm", "title": "Unanalyzed", "artist": "Z",
        "format": "m4a", "indexed_at": 1000})
    mc.upsert_file(catalog, {"path": "/mnt/music/nobpm.m4a",
                             "track_id": "b:nobpm", "size": 1, "mtime": 1,
                             "seen_at": 1000})
    mc.upsert_track(catalog, {
        "id": "b:nofile", "title": "Ghost", "artist": "Z", "bpm": 120.0,
        "format": "m4a", "indexed_at": 1000})
    ids = {t["id"] for t in md.queue_candidates(catalog, NOW)}
    assert "b:nobpm" not in ids
    assert "b:nofile" not in ids


def test_the_cooldown_lives_in_the_query_too(catalog):
    mc.record_play(catalog, "b:05", int(NOW - 1 * HOUR), mc.PLAY_COMPLETED)
    mc.record_play(catalog, "b:06", int(NOW - 25 * HOUR), mc.PLAY_COMPLETED)
    ids = {t["id"] for t in md.queue_candidates(catalog, NOW)}
    assert "b:05" not in ids  # inside the 24h window
    assert "b:06" in ids      # aged out


# --- anti-repetition (pure, injected clock) --------------------------------------

def test_the_cooldown_window_turns_on_the_injected_clock():
    assert mp.too_soon(NOW - 23 * HOUR, NOW)
    assert not mp.too_soon(NOW - 25 * HOUR, NOW)
    assert not mp.too_soon(None, NOW)  # never played


def test_a_recently_played_candidate_is_excluded_an_aged_one_is_not():
    cands = [track("b:recent", last_played_at=NOW - 23 * HOUR),
             track("b:aged", last_played_at=NOW - 25 * HOUR),
             track("b:never")]
    queue = mp.build_queue(cands, seed_for(track("b:seed")), 10,
                           random.Random(1), NOW)
    ids = [t["id"] for t in queue]
    assert "b:recent" not in ids
    assert set(ids) == {"b:aged", "b:never"}


def test_no_artist_twice_within_the_spacing_window():
    cands = [track("b:%02d" % i, artist="Band %d" % (i % 8))
             for i in range(32)]
    queue = mp.build_queue(cands, seed_for(track("b:seed")), 20,
                           random.Random(3), NOW)
    assert len(queue) == 20
    artists = [t["artist"] for t in queue]
    for i in range(len(artists) - mp.ARTIST_SPACING + 1):
        window = artists[i:i + mp.ARTIST_SPACING]
        assert len(window) == len(set(window)), window


def test_a_library_too_small_for_the_spacing_runs_dry_honestly():
    """Two artists cannot fill six spaced slots: the queue comes back short,
    never with a repeat."""
    cands = [track("b:%02d" % i, artist="Band %d" % (i % 2))
             for i in range(12)]
    queue = mp.build_queue(cands, seed_for(track("b:seed")), 10,
                           random.Random(5), NOW)
    assert len(queue) == 2
    assert len({t["artist"] for t in queue}) == 2


def test_a_refill_never_repeats_what_the_queue_already_holds():
    cands = [track("b:%02d" % i) for i in range(40)]
    seed = seed_for(track("b:seed"))
    first = mp.build_queue(cands, seed, 10, random.Random(11), NOW)
    held = [t["id"] for t in first]
    refill = mp.build_queue(cands, seed, 10, random.Random(12), NOW,
                            exclude_ids=held)
    assert not set(held) & {t["id"] for t in refill}


# --- seed resolution --------------------------------------------------------------

def test_a_track_seed_is_that_tracks_features_and_excludes_itself():
    row = track("b:seed", bpm=112.3, gain=-0.8, dr=1.2, year=1990,
                genre="Electronic")
    target, exclude = mp.resolve_seed({"track": row}, [])
    assert target == {"bpm": 112.3, "replaygain_db": -0.8,
                      "dynamic_range_db": 1.2, "year": 1990,
                      "genre": "Electronic"}
    assert exclude == {"b:seed"}


def test_an_artist_seed_is_the_median_not_the_mean():
    """One 287-BPM outlier must not drag the target -- medians throughout."""
    cands = [track("b:1", artist="Stereo MC's", bpm=100.0, year=1990),
             track("b:2", artist="Stereo MC's", bpm=104.0, year=1992),
             track("b:3", artist="Stereo MC's", bpm=287.1, year=2001),
             track("b:4", artist="Someone Else", bpm=180.0)]
    target, exclude = mp.resolve_seed({"artist": "Stereo MC's"}, cands)
    assert target["bpm"] == 104.0
    assert target["year"] == 1992
    assert target["genre"] == "electronic"
    assert exclude == set()


def test_an_artist_seed_matches_case_insensitively():
    cands = [track("b:1", artist="Stereo MC's")]
    target, _ = mp.resolve_seed({"artist": "stereo mc's"}, cands)
    assert target is not None


def test_an_unknown_artist_is_an_empty_queue_not_a_crash():
    cands = [track("b:%02d" % i) for i in range(5)]
    queue = mp.build_queue(cands, {"artist": "Nobody At All"}, 10,
                           random.Random(1), NOW)
    assert queue == []


def test_the_named_target_seam_passes_through():
    """The mood/weather shape (Phase 5's plug point): a prebuilt feature
    point rides through untouched."""
    point = {"bpm": 95.0, "replaygain_db": -6.0, "dynamic_range_db": 8.0,
             "year": 1975, "genre": "rock"}
    target, exclude = mp.resolve_seed({"target": point}, [])
    assert target == point
    assert exclude == set()


def test_garbage_seeds_resolve_to_nothing():
    for junk in (None, "seed", 7, {}, {"weird": 1}, {"track": "b:1"}):
        target, exclude = mp.resolve_seed(junk, [])
        assert target is None
        assert exclude == set()
    assert mp.build_queue([track("b:1")], {"weird": 1}, 5,
                          random.Random(1), NOW) == []


# --- determinism with variety ------------------------------------------------------

def spread_candidates():
    """60 candidates fanned across tempo so the ranking has real shape."""
    return [track("b:%02d" % i, bpm=100.0 + i * 1.7, year=1985 + i % 20)
            for i in range(60)]


def test_same_inputs_and_rng_seed_give_a_byte_identical_queue():
    cands = spread_candidates()
    seed = seed_for(track("b:seed", bpm=120.0))
    a = mp.build_queue(cands, seed, 12, random.Random(42), NOW)
    b = mp.build_queue(cands, seed, 12, random.Random(42), NOW)
    assert [t["id"] for t in a] == [t["id"] for t in b]


def test_a_different_rng_seed_varies_the_queue_within_the_top_k():
    """Two Tuesdays differ, but every pick still comes from the quality band:
    slot i samples the best TOP_K still-eligible candidates, so nothing can
    sit deeper than TOP_K + n - 1 in the global ranking."""
    cands = spread_candidates()
    seed = seed_for(track("b:seed", bpm=120.0))
    n = 10
    a = mp.build_queue(cands, seed, n, random.Random(1), NOW)
    b = mp.build_queue(cands, seed, n, random.Random(2), NOW)
    ids_a, ids_b = [t["id"] for t in a], [t["id"] for t in b]
    assert ids_a != ids_b  # variety
    target, _ = mp.resolve_seed(seed, cands)
    band = {t["id"] for _, t in
            mp.rank_candidates(cands, target)[:mp.TOP_K + n - 1]}
    assert set(ids_a) <= band
    assert set(ids_b) <= band


def test_ranking_ties_break_on_id_not_input_order():
    """Scores tie constantly (the lattice); a ranking that depended on how
    the daemon happened to order rows would make determinism a lie."""
    cands = [track("b:b"), track("b:a"), track("b:c")]
    target = {"bpm": 120.0}
    ranked = [t["id"] for _, t in mp.rank_candidates(cands, target)]
    assert ranked == ["b:a", "b:b", "b:c"]
    assert ranked == [t["id"] for _, t in
                      mp.rank_candidates(list(reversed(cands)), target)]
