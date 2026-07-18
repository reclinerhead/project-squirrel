# =============================================================================
# project-squirrel -- test_music_blurb.py
#
# The blurb pass's brain (issue #171): the measurement's classification
# buckets, the truncation detector, the owner's trim-to-last-sentence policy,
# and the per-album pick. All pure -- these are the functions whose
# regressions would be silent, since a wrong bucket just quietly renders (or
# hides) a paragraph and nothing fails.
#
# mutagen extraction (read_comments) is NOT covered here, the art pass's
# precedent: real tagged files prove it on pearl. The owner-row guard is
# covered against a real sqlite in test_music_catalog.py, where the other
# provenance guards live.
#
# NOTE: this file must be on .github/workflows/tests.yml's pytest line by
# hand. CI enumerates test files and has no pytest.ini/testpaths fallback.
# =============================================================================

from jukebox import music_blurb as mb


# The measurement's own example of the failure mode this issue exists for:
# 255 chars, cut mid-word. Built rather than pasted so the length is exact.
def _wall(text):
    """Pad `text` out to exactly the 255-char wall, cutting mid-word."""
    filler = " lorem ipsum dolor sit amet consectetur adipiscing elit sed"
    out = text
    while len(out) < mb.TRUNCATION_WALL:
        out += filler
    return out[:mb.TRUNCATION_WALL]


# --- classification ---------------------------------------------------------

def test_classify_real_prose_is_a_blurb():
    text = ("Five Finger Death Punch's epic third album is called American "
            "Capitalist, and it finds the band pushing their sound outward "
            "in every direction.")
    assert mb.classify_comment(text) == mb.BLURB


def test_classify_the_measured_scrap_tokens():
    """`USA` and `amrc` are 12.8% of albums -- store metadata noise that must
    never reach a page."""
    assert mb.classify_comment("USA") == mb.SCRAP
    assert mb.classify_comment("amrc") == mb.SCRAP


def test_classify_short_prose_is_a_oneliner_not_scrap():
    assert mb.classify_comment("A quiet record made in one room.") == \
        mb.ONELINER


def test_classify_rip_junk_beats_length():
    """An EAC log is long enough to pass every prose test, so the junk check
    has to run first -- this is the ordering regression that would ship a
    page full of encoder settings."""
    log = ("Exact Audio Copy V1.0 beta 3 from 29. August 2011, report for "
           "this CD, track quality 100.0 percent, all tracks accurately "
           "ripped and verified against AccurateRip.")
    assert mb.classify_comment(log) == mb.RIPJUNK


def test_classify_itunes_hex_bookkeeping_as_junk():
    """iTunNORM gain data rides the comment atom directly on m4a -- no named
    frame to skip -- and it clears the 80-char prose floor easily. Sampled
    from the real library."""
    assert mb.classify_comment(
        "000002EB 0000062C 000016F3 0000482B 000222F7 00002727 0000515D "
        "000068A6 0002BF37 00007547") == mb.RIPJUNK


def test_hex_blob_test_does_not_eat_real_prose():
    """'A decade' and similar are hex-ish words -- the anchored all-tokens
    match is what keeps this from swallowing an album description."""
    text = ("A decade of beaded, faded, accented songs about nothing in "
            "particular from a band that never once explained itself.")
    assert mb.classify_comment(text) == mb.BLURB


def test_classify_encoder_settings_are_rip_junk():
    assert mb.classify_comment(
        "FLAC 1.2.1 encoder settings -compression-level-8") == mb.RIPJUNK


def test_rip_markers_respect_word_boundaries():
    """A bare substring test would eat 'peace' via 'eac' and 'prologue' via
    'log' -- both plausible words in real album copy."""
    text = ("This is a record about peace, and its prologue is the finest "
            "thing the band has ever committed to tape anywhere.")
    assert mb.classify_comment(text) == mb.BLURB


def test_classify_empty_and_whitespace():
    assert mb.classify_comment(None) == mb.EMPTY
    assert mb.classify_comment("") == mb.EMPTY
    assert mb.classify_comment("   \n ") == mb.EMPTY


def test_classify_short_but_punctuated_is_not_scrap():
    """Three words ending in a period is a sentence, not a metadata token."""
    assert mb.classify_comment("Recorded in Memphis.") == mb.ONELINER


# --- the truncation wall ----------------------------------------------------

def test_the_255_wall_is_truncated():
    cut = _wall("An outstanding level of musicianship and a so")
    assert len(cut) == mb.TRUNCATION_WALL
    assert mb.looks_truncated(cut) is True


def test_truncation_is_detected_below_the_255_wall():
    """The regression that a length-based detector shipped: real blurbs
    sampled on pearl were cut mid-word at 105, 112 and 114 chars. Truncation
    is about how the text ends, not how long it is."""
    short_cut = ("The French twosome behind Daft Punk, Thomas Bangalter and "
                 "Guy-Manuel De Homem-Christo, get away with an awful ")
    assert len(short_cut) < mb.TRUNCATION_WALL
    assert mb.looks_truncated(short_cut) is True


def test_a_255_char_blurb_that_ends_on_a_period_was_not_truncated():
    """3% of the measured 255s ended cleanly, and trimming those would
    destroy a complete final sentence."""
    ended = _wall("x")[:mb.TRUNCATION_WALL - 1] + "."
    assert len(ended) == mb.TRUNCATION_WALL
    assert mb.looks_truncated(ended) is False


def test_a_closing_quote_still_counts_as_an_ending():
    assert mb.looks_truncated(
        'The singer called it "the only honest thing we made."') is False


def test_properly_ended_text_is_never_truncated():
    assert mb.looks_truncated("Just a note.") is False
    assert mb.looks_truncated("") is False


# --- the trim policy --------------------------------------------------------

def test_trim_keeps_whole_sentences_and_drops_the_fragment():
    text = ("The band recorded this in a barn. The result is their warmest "
            "album by some distance. It opens with a song about a so")
    got = mb.trim_to_sentence(text)
    assert got.endswith("by some distance.")
    assert "barn." in got
    assert "about a so" not in got


def test_trim_returns_none_when_no_sentence_completes():
    """A first sentence that runs past the wall leaves nothing salvageable --
    the issue is explicit that no blurb beats a fragment."""
    assert mb.trim_to_sentence(
        "This sprawling record is the sort of thing that happens when a so"
    ) is None


def test_trim_drops_a_survivor_under_the_prose_floor():
    """One short opening clause, stripped of everything it was setting up,
    reads as a mistake rather than a description."""
    assert mb.trim_to_sentence("It rules. And then the album goes on to a so") \
        is None


def test_trim_handles_a_closing_quote_after_the_period():
    text = ('The singer called it "the only honest thing we made." Then the '
            'band broke up and never spoke again about any of it. Later a so')
    got = mb.trim_to_sentence(text)
    assert got.endswith("about any of it.")


# --- describe: the whole policy in one call ---------------------------------

def test_describe_passes_untruncated_prose_through_untouched():
    text = ("A short, complete, entirely unremarkable album description that "
            "ends exactly where its author intended it to end.")
    assert mb.describe(text) == (text, False)


def test_describe_trims_a_truncated_blurb_and_flags_it():
    text = _wall("The band recorded this in a barn. The result is their "
                 "warmest album by some distance. It opens with a so")
    description, truncated = mb.describe(text)
    assert truncated is True
    assert description.endswith(".")
    assert len(description) < len(text)


def test_describe_drops_an_unsalvageable_truncation():
    assert mb.describe(_wall("This sprawling record is the sort of thing")) \
        is None


def test_describe_of_nothing_is_none():
    assert mb.describe(None) is None
    assert mb.describe("   ") is None


# --- the per-album pick -----------------------------------------------------

def test_pick_prefers_a_blurb_over_a_longer_rip_log():
    """The ranking regression that matters: a 300-char EAC log sitting on one
    track must never outrank the real description on the next one."""
    blurb = ("A perfectly ordinary album description of sufficient length to "
             "clear the prose floor comfortably.")
    log = ("Exact Audio Copy V1.0, report for this CD. " + "x" * 300)
    assert mb.pick_comment([log, blurb]) == blurb


def test_pick_rejects_one_liners_outright():
    """Measured on 400 real albums (2026-07-18): the surviving one-liners are
    store bookkeeping, not prose, and both of these would otherwise have
    rendered as an album description."""
    assert mb.pick_comment(["Amazon.com Song ID: 201982125"]) is None
    assert mb.pick_comment([">> a klangwerk release"]) is None


def test_pick_prefers_a_blurb_over_a_oneliner():
    one = "Recorded in Memphis."
    blurb = ("A perfectly ordinary album description of sufficient length to "
             "clear the prose floor comfortably.")
    assert mb.pick_comment([one, blurb]) == blurb


def test_pick_takes_the_longest_copy_of_the_same_blurb():
    """Rips are uneven -- one track's copy can be truncated harder than
    another's, and the longer one has strictly more of the text."""
    short = ("The band recorded this album in a barn somewhere in Vermont "
             "and it went well enough for th")
    longer = ("The band recorded this album in a barn somewhere in Vermont "
              "and it went well enough for them to do it again the "
              "following winter.")
    assert mb.classify_comment(short) == mb.BLURB  # both clear the floor
    assert mb.pick_comment([short, longer]) == longer


def test_pick_tie_goes_to_the_earliest():
    """Stable across runs: the worklist feeds paths sorted, so equal-length
    comments resolve to the same one every time."""
    first = ("A first album description that is comfortably long enough to "
             "clear the prose floor and be a blurb.")
    second = ("A secnd album description that is comfortably long enough to "
              "clear the prose floor and be a blurb.")
    assert len(first) == len(second)
    assert mb.classify_comment(first) == mb.BLURB
    assert mb.pick_comment([first, second]) == first


def test_pick_returns_none_when_nothing_is_usable():
    assert mb.pick_comment(["USA", "amrc", None, ""]) is None


def test_pick_of_an_empty_album_is_none():
    assert mb.pick_comment([]) is None


# --- the reusable unit ------------------------------------------------------

def test_propose_reports_no_usable_comment(monkeypatch):
    """propose_for_album is the function the GUI refresh button will call
    (issue #171). It must stay database-free so the button can preview a
    result without writing one."""
    monkeypatch.setattr(mb, "read_comments", lambda p: ["USA"])
    got = mb.propose_for_album(["/a.m4a", "/b.m4a"])
    assert got["status"] == mb.NONE
    assert got["description"] is None


def test_propose_distinguishes_dropped_from_none(monkeypatch):
    """The two ways of getting nothing are different answers to "why does
    this album have no description" -- the pass tallies them apart and the
    refresh button will want to say which one happened. It also keeps the
    raw text, so the fragment can still be shown for a human decision."""
    unsalvageable = _wall("This sprawling record is the sort of thing that")
    monkeypatch.setattr(mb, "read_comments", lambda p: [unsalvageable])
    got = mb.propose_for_album(["/a.m4a"])
    assert got["status"] == mb.DROPPED
    assert got["description"] is None
    assert got["raw"] == unsalvageable


def test_propose_sees_every_comment_in_a_file(monkeypatch):
    """A file can carry a rip log AND the store's blurb. read_comments hands
    over both so the ranking decides -- returning only the first would let
    whichever tool wrote its frame first silently win."""
    blurb = ("A perfectly ordinary album description of sufficient length to "
             "clear the prose floor comfortably.")
    monkeypatch.setattr(mb, "read_comments",
                        lambda p: ["Exact Audio Copy V1.0 log", blurb])
    assert mb.propose_for_album(["/a.mp3"])["description"] == blurb


def test_clean_comment_unescapes_html_entities():
    """Store copy carries entities verbatim -- sampled from the real library:
    "Demos,&amp; Two Live Tracks". React escapes on render, so leaving them
    in would print a literal "&amp;" on the album page."""
    assert mb.clean_comment("Demos,&amp; Two Live Tracks &#38; more") == \
        "Demos,& Two Live Tracks & more"


def test_clean_comment_collapses_nothing_to_none():
    """norm_tag's rule: "" and whitespace are the same kind of missing as an
    absent tag, so they never reach the classifier as a distinct case."""
    assert mb.clean_comment(None) is None
    assert mb.clean_comment("   ") is None
    assert mb.clean_comment("&nbsp;") is None


def test_propose_shape_is_what_set_album_note_takes(monkeypatch):
    blurb = ("A perfectly ordinary album description of sufficient length to "
             "clear the prose floor comfortably.")
    monkeypatch.setattr(mb, "read_comments", lambda p: [blurb])
    assert mb.propose_for_album(["/a.m4a"]) == {
        "status": mb.OK, "description": blurb, "raw": blurb,
        "truncated": False}
