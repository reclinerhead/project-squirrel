# =============================================================================
# project-squirrel -- test_music_bio.py
#
# The bio fetcher's brain (issue #170): the accept rule, its corroboration
# evidence, the rules-file validation, and the two prose cleanups. These are
# the functions whose regressions are SILENT and expensive -- a loosened
# accept rule does not fail a build, it publishes a page that lies about a
# band.
#
# The fetch chain itself (MusicBrainz -> Wikidata -> Wikipedia) is not covered
# here, the art pass's precedent: real APIs prove it on pearl. The owner-row
# guard and the worklist arithmetic are covered against a real sqlite in
# test_music_catalog.py, where the other provenance guards live.
#
# NOTE: this file must be on .github/workflows/tests.yml's pytest line by
# hand. CI enumerates test files and has no pytest.ini/testpaths fallback.
# =============================================================================

import pytest

from jukebox import music_bio as mb


def hit(name, mbid, score=100, disambiguation=""):
    return {"name": name, "id": mbid, "score": score,
            "disambiguation": disambiguation}


RULES = {"skip": set(), "pin": {}, "tuning": {"min_score": 90}}


# --- name folding and title folding -----------------------------------------

def test_fold_collapses_case_and_whitespace():
    assert mb.fold("  The   Beatles ") == mb.fold("the beatles")


def test_fold_keeps_punctuation_distinct():
    """"AC/DC" and "ACDC" are different strings and a near-miss belongs in
    the review queue, not silently accepted."""
    assert mb.fold("AC/DC") != mb.fold("ACDC")


def test_fold_title_drops_a_leading_article():
    """The library holds both "Square Root of Minus One" and "The Square Root
    of Negative One" for one band -- two catalogs disagreeing about "The" is
    not evidence of anything."""
    assert mb.fold_title("The Wall") == mb.fold_title("Wall")
    assert mb.fold_title("A Night at the Opera") == \
        mb.fold_title("Night at the Opera")


def test_fold_title_does_not_eat_a_leading_article_mid_word():
    assert mb.fold_title("Theatre of Pain") == "theatre of pain"


def test_fold_title_folds_typographic_punctuation_to_ascii():
    """MEASURED (2026-07-18): MusicBrainz house style is curly punctuation,
    iTunes writes ASCII, and that alone sank A Tribe Called Quest's
    corroboration. An encoding difference is not a different album."""
    assert mb.fold_title("We Got It from Here… Thank You 4 Your Service") == \
        mb.fold_title("We Got It from Here... Thank You 4 Your Service")
    assert mb.fold_title("People’s Instinctive Travels") == \
        mb.fold_title("People's Instinctive Travels")
    assert mb.fold_title("Rock – Roll") == mb.fold_title("Rock - Roll")


def test_fold_title_still_requires_the_words_to_agree():
    """Punctuation folding must not turn into fuzzy matching."""
    assert mb.fold_title("Kid A") != mb.fold_title("Kid B")
    assert mb.fold_title("Low End Theory") != mb.fold_title("High End Theory")


# --- corroboration ----------------------------------------------------------

def test_album_overlap_finds_the_shared_titles():
    assert mb.album_overlap(["Moon Safari", "Talkie Walkie"],
                            ["Talkie Walkie", "Premiers Symptomes"]) == \
        {"talkie walkie"}


def test_album_overlap_is_empty_when_nothing_matches():
    assert mb.album_overlap(["Square Root of Minus One"],
                            ["Lost in Time", "Nudity"]) == set()


def test_album_overlap_ignores_blank_titles():
    assert mb.album_overlap(["", None, "Kid A"], ["Kid A", ""]) == {"kid a"}


# --- exact matching ---------------------------------------------------------

def test_exact_matches_rejects_a_fuzzy_hit_scoring_100():
    """THE measured failure (2026-07-18): MusicBrainz returns "We Are
    Scientists" at score 100 for a search for "We". Their score is Lucene
    relevance, not confidence."""
    candidates = [hit("We Are Scientists", "was-id", 100),
                  hit("WE", "we-id", 93)]
    got = mb.exact_matches("We", candidates, 90)
    assert [c["id"] for c in got] == ["we-id"]


def test_exact_matches_applies_the_score_floor():
    assert mb.exact_matches("Air", [hit("Air", "a", 80)], 90) == []


# --- the accept rule --------------------------------------------------------

def test_accepts_a_sole_name_match_that_shares_an_album():
    """The common, boring case: one name match, and the library and
    MusicBrainz agree on a record."""
    mbid, reason = mb.decide(
        "Air", [hit("Air", "air-id")], ["Moon Safari"],
        {"air-id": ["Moon Safari", "Talkie Walkie"]}, 90)
    assert mbid == "air-id"
    assert "moon safari" in reason


def test_refuses_a_sole_name_match_whose_albums_all_disagree():
    """THE "We" CASE, and the whole reason this rule exists. The only exact
    name match on MusicBrainz is a Norwegian hardrock band whose twelve
    release groups share nothing with the two albums in this library.
    Accepting it would publish a bio about the wrong band."""
    mbid, reason = mb.decide(
        "We", [hit("WE", "we-id", 93)],
        ["Square Root of Minus One", "The Square Root of Negative One"],
        {"we-id": ["Lost in Time", "Nudity", "Wonderland"]}, 90)
    assert mbid is None
    assert "release groups" in reason


def test_refuses_when_there_is_no_exact_name_match_at_all():
    mbid, reason = mb.decide(
        "We", [hit("We Are Scientists", "was-id", 100)], ["Whatever"],
        {}, 90)
    assert mbid is None
    assert "no exact name match" in reason


def test_accepts_a_sole_match_with_no_release_groups_to_check():
    """Corroboration IMPOSSIBLE is not corroboration FAILED. Refusing every
    sparsely-documented artist would empty the page for exactly the obscure
    bands a bio helps most with."""
    mbid, reason = mb.decide(
        "Obscure Duo", [hit("Obscure Duo", "od-id")], ["Some Record"],
        {"od-id": []}, 90)
    assert mbid == "od-id"
    assert "no release groups" in reason


def test_two_bands_share_a_name_and_the_albums_pick_one():
    """Genuine homonyms are common. The library's own shelf is the tiebreak
    that a relevance score can never be."""
    mbid, _ = mb.decide(
        "Nirvana",
        [hit("Nirvana", "us-id"), hit("Nirvana", "uk-id")],
        ["Nevermind"],
        {"us-id": ["Nevermind", "In Utero"],
         "uk-id": ["The Story of Simon Simopath"]}, 90)
    assert mbid == "us-id"


def test_two_corroborating_matches_are_ambiguous_not_a_coin_flip():
    mbid, reason = mb.decide(
        "Twins", [hit("Twins", "a-id"), hit("Twins", "b-id")], ["Shared"],
        {"a-id": ["Shared"], "b-id": ["Shared"]}, 90)
    assert mbid is None
    assert "ambiguous" in reason


def test_several_name_matches_none_corroborating_is_unresolved():
    mbid, reason = mb.decide(
        "Common", [hit("Common", "a"), hit("Common", "b")], ["Mine"],
        {"a": ["Theirs"], "b": ["Other"]}, 90)
    assert mbid is None
    assert "none corroborated" in reason


def test_no_candidates_at_all_is_unresolved():
    assert mb.decide("Nobody", [], ["X"], {}, 90)[0] is None


# --- prose cleanup ----------------------------------------------------------

def test_clean_wikipedia_unescapes_and_trims_section_scaffolding():
    got = mb.clean_wikipedia(
        "Deerhunter is an American band &amp; a good one.\n\n== History ==\n"
        "More text here.")
    assert got == "Deerhunter is an American band & a good one."


def test_clean_wikipedia_collapses_blank_line_runs():
    assert mb.clean_wikipedia("One.\n\n\n\nTwo.") == "One.\n\nTwo."


def test_clean_wikipedia_of_nothing_is_none():
    assert mb.clean_wikipedia(None) is None
    assert mb.clean_wikipedia("   ") is None


def test_clean_lastfm_strips_the_licence_tail_before_the_tags():
    """The tail goes first: stripping tags first would turn its anchor into
    bare words that read like part of the bio."""
    got = mb.clean_lastfm(
        '<p>A band from Leeds.</p> <a href="https://www.last.fm/music/X">'
        'Read more on Last.fm</a>. User-contributed text is available...')
    assert got == "A band from Leeds."


def test_clean_lastfm_handles_the_read_more_about_variant():
    got = mb.clean_lastfm("Prose here. Read more about X on Last.fm and stuff")
    assert got == "Prose here."


def test_clean_lastfm_unescapes_entities():
    assert mb.clean_lastfm("<p>Rock &amp; roll.</p>") == "Rock & roll."


def test_clean_lastfm_of_only_boilerplate_is_none():
    assert mb.clean_lastfm("Read more on Last.fm") is None


# --- rules file -------------------------------------------------------------

def test_parse_rules_folds_skips_and_pins():
    r = mb.parse_rules("skip:\n  - Various Artists\npin:\n  'We': abc-123\n")
    assert "various artists" in r["skip"]
    assert r["pin"]["we"] == "abc-123"
    assert r["tuning"]["min_score"] == 90


def test_parse_rules_accepts_an_empty_file():
    r = mb.parse_rules("")
    assert r["skip"] == set() and r["pin"] == {}


def test_parse_rules_rejects_an_unknown_section():
    """A typo'd section that silently did nothing would be a rule the owner
    believes is in force."""
    with pytest.raises(mb.RulesError, match="unknown"):
        mb.parse_rules("skips:\n  - X\n")


def test_parse_rules_rejects_a_non_string_pin():
    with pytest.raises(mb.RulesError, match="MBID"):
        mb.parse_rules("pin:\n  We: 42\n")


def test_parse_rules_rejects_an_out_of_range_score():
    with pytest.raises(mb.RulesError, match="min_score"):
        mb.parse_rules("tuning:\n  min_score: 500\n")


def test_parse_rules_rejects_a_list_at_the_top_level():
    with pytest.raises(mb.RulesError, match="mapping"):
        mb.parse_rules("- just\n- a list\n")


# --- the unit of work -------------------------------------------------------

def test_a_skipped_artist_never_reaches_the_network(monkeypatch):
    """Various Artists would match SOMETHING on MusicBrainz, and every one of
    those matches would be wrong."""
    def boom(*a, **k):
        raise AssertionError("must not fetch for a skipped artist")
    monkeypatch.setattr(mb, "mb_search_artist", boom)
    rules = {"skip": {"various artists"}, "pin": {},
             "tuning": {"min_score": 90}}
    got = mb.propose_for_artist("Various Artists", ["X"], rules)
    assert got["status"] == mb.SKIPPED
    assert got["bio"] is None


def test_an_unresolved_artist_carries_its_candidates(monkeypatch):
    """A review-queue entry without the options is not reviewable."""
    monkeypatch.setattr(mb, "mb_search_artist",
                        lambda n, t: [hit("We Are Scientists", "was", 100,
                                          "US indie rock band")])
    got = mb.propose_for_artist("We", ["Square Root of Minus One"], RULES)
    assert got["status"] == mb.UNRESOLVED
    assert got["bio"] is None and got["mbid"] is None
    assert got["candidates"][0]["disambiguation"] == "US indie rock band"


def test_resolved_but_articleless_is_no_prose_not_unresolved(monkeypatch):
    """Identity found, nothing written about them anywhere. That is a
    different fact from "we don't know who this is", and it is why the pass
    still writes a row -- fetched_at stops the next run re-probing."""
    monkeypatch.setattr(mb, "mb_search_artist",
                        lambda n, t: [hit("Obscure Duo", "od")])
    monkeypatch.setattr(mb, "mb_artist_detail", lambda m, t: ([], {}))
    got = mb.propose_for_artist("Obscure Duo", ["Some Record"], RULES)
    assert got["status"] == mb.NO_PROSE
    assert got["mbid"] == "od"


def test_a_pin_overrules_the_resolver_without_searching(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a pin must not run a search")
    monkeypatch.setattr(mb, "mb_search_artist", boom)
    monkeypatch.setattr(mb, "mb_artist_detail", lambda m, t: ([], {}))
    rules = {"skip": set(), "pin": {"we": "pinned-id"},
             "tuning": {"min_score": 90}}
    got = mb.propose_for_artist("We", ["Anything"], rules)
    assert got["mbid"] == "pinned-id"
    assert "pinned" in got["match"]
    # A pin is not re-corroborated -- it is the owner overruling the resolver,
    # so it reaches the prose stage even though "Anything" matches nothing.
    assert got["status"] == mb.NO_PROSE


def test_a_truncated_release_group_list_is_topped_up_before_judging(
        monkeypatch):
    """MEASURED FALSE NEGATIVE (2026-07-18): a lookup's release-groups
    subquery caps at 25, so "A Tribe Called Quest" was reported unresolved
    with "none of its 25 release groups match" while the corroborating album
    sat outside the window. Exactly the cap means probably-truncated."""
    capped = ["Filler %d" % i for i in range(mb.MB_SUBQUERY_CAP)]
    monkeypatch.setattr(mb, "mb_search_artist",
                        lambda n, t: [hit("A Tribe Called Quest", "atcq")])
    monkeypatch.setattr(mb, "mb_artist_detail", lambda m, t: (capped, {}))
    monkeypatch.setattr(mb, "mb_browse_release_groups",
                        lambda m, t: capped + ["The Low End Theory"])
    got = mb.propose_for_artist("A Tribe Called Quest",
                                ["The Low End Theory"], RULES)
    assert got["mbid"] == "atcq"
    assert "low end theory" in got["match"]


def test_a_short_release_group_list_is_never_topped_up(monkeypatch):
    """Under the cap the list IS complete, so a failed corroboration is a
    real answer -- the "We" case must not spend two extra requests
    rediscovering that."""
    def boom(*a, **k):
        raise AssertionError("must not browse an untruncated list")
    monkeypatch.setattr(mb, "mb_search_artist",
                        lambda n, t: [hit("WE", "we-id", 93)])
    monkeypatch.setattr(mb, "mb_artist_detail",
                        lambda m, t: (["Lost in Time", "Nudity"], {}))
    monkeypatch.setattr(mb, "mb_browse_release_groups", boom)
    got = mb.propose_for_artist("We", ["Square Root of Minus One"], RULES)
    assert got["status"] == mb.UNRESOLVED


def test_lastfm_is_not_consulted_when_no_key_is_set(monkeypatch):
    """The kill-switch convention: unset MERLE_LASTFM_KEY means a complete
    Wikipedia-only pass, not a degraded one."""
    def boom(*a, **k):
        raise AssertionError("must not call Last.fm without a key")
    monkeypatch.setattr(mb, "mb_search_artist",
                        lambda n, t: [hit("Obscure Duo", "od")])
    monkeypatch.setattr(mb, "mb_artist_detail", lambda m, t: ([], {}))
    monkeypatch.setattr(mb, "lastfm_bio", boom)
    assert mb.propose_for_artist("Obscure Duo", ["R"], RULES, key=None)[
        "status"] == mb.NO_PROSE


# --- throttle ---------------------------------------------------------------

def test_throttle_waits_out_the_interval():
    """MusicBrainz declines everything over 1 req/s from one IP, so the pass
    must pace itself rather than discover the limit as 503s."""
    slept, now = [], [0.0]
    t = mb.Throttle(1.1, sleep=slept.append, clock=lambda: now[0])
    t.wait()
    assert slept == []          # first call is free
    now[0] = 0.2
    t.wait()
    assert slept and abs(slept[0] - 0.9) < 1e-9


def test_throttle_does_not_sleep_when_enough_time_passed():
    slept, now = [], [0.0]
    t = mb.Throttle(1.1, sleep=slept.append, clock=lambda: now[0])
    t.wait()
    now[0] = 5.0
    t.wait()
    assert slept == []
