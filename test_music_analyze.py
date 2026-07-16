# =============================================================================
# project-squirrel -- test_music_analyze.py
#
# The analysis backfill's pure half (issue #136): ebur128 parsing, the
# ReplayGain formula, path remapping across two machines' filesystems, JSONL
# resumption, and the importer's idempotency. All of it is the class CLAUDE.md
# says earns coverage -- a sign error in the gain or a wrong ebur128 capture is
# inaudible in review, survives every build, and quietly poisons every playlist
# Phase 3 ever generates.
#
# No ffmpeg, no librosa, no NAS: the I/O half (the decode, the beat tracker's
# DSP, the SMB reads) is exempt by design and verified against the real library
# in the pass itself.
#
# NOTE: this file must be on .github/workflows/tests.yml's pytest line by hand.
# CI enumerates test files and has no pytest.ini/testpaths fallback.
# =============================================================================

import json

import pytest

from jukebox import music_analyze as ma
from jukebox import music_catalog as mc
from jukebox import music_import as mi


# Real ffmpeg output, captured verbatim -- including the running readout that
# precedes the Summary, which is exactly what an unanchored regex gets wrong.
EBUR128_REAL = """\
[Parsed_ebur128_0 @ 000001d4] t: 0.0999998   TARGET:-23 LUFS    M: -25.6 S:-120.7\
     I: -70.0 LUFS       LRA:   0.0 LU
[Parsed_ebur128_0 @ 000001d4] t: 0.199999    TARGET:-23 LUFS    M: -20.1 S:-120.7\
     I: -20.5 LUFS       LRA:   0.0 LU
[Parsed_ebur128_0 @ 000001d4] Summary:

  Integrated loudness:
    I:          -8.4 LUFS
    Threshold: -18.7 LUFS

  Loudness range:
    LRA:         5.2 LU
    Threshold: -28.9 LUFS
    LRA low:   -11.9 LUFS
    LRA high:   -6.7 LUFS

  True peak:
    Peak:        0.3 dBFS
"""


# --- parse_ebur128 -------------------------------------------------------------

def test_parses_the_summary_not_the_running_readout():
    """The regression this test exists for: ebur128 prints a per-frame readout
    for the whole file before the Summary, so a first-match parse returns the
    loudness of the first 100ms (-70 LUFS here) instead of the track's."""
    i, lra, peak = ma.parse_ebur128(EBUR128_REAL)
    assert i == -8.4
    assert lra == 5.2
    assert peak == 0.3


def test_missing_summary_yields_nones_not_a_crash():
    for text in ("", None, "ffmpeg version 7.1\nno audio streams found"):
        assert ma.parse_ebur128(text) == (None, None, None)


def test_garbled_numbers_yield_nones():
    i, lra, _ = ma.parse_ebur128("  I:  NaNsense LUFS\n  LRA: ??? LU")
    assert i is None and lra is None


def test_silence_is_not_a_loudness():
    """ebur128 floors at -70 LUFS for silence. That is the absence of a
    measurement, not a very quiet one -- a -70 would become a +52 dB gain."""
    i, _, _ = ma.parse_ebur128("Summary:\n  I:  -70.0 LUFS\n  LRA: 0.0 LU")
    assert i is None


# --- ffmpeg_error --------------------------------------------------------------

FFMPEG_FAIL = """\
ffmpeg version 8.1.2-full_build Copyright (c) 2000-2026 the FFmpeg developers
  built with gcc 15.2.0 (Rev3, Built by MSYS2 project)
[mp3 @ 000002a1] Failed to read frame size: Could not seek to 1026.
[in#0 @ 000002a1] Error opening input: Invalid data found when processing input
Error opening input file /mnt/music/Genesis/The Lamb/1-02 Fly on a Windshield.mp3.
Error opening input files: Invalid data found when processing input
"""


def test_ffmpeg_error_takes_the_last_real_line_whole():
    """The regression: slicing the last N chars cut mid-word and varied with
    the filename's length, so one failure read differently on every track."""
    got = ma.ffmpeg_error(FFMPEG_FAIL)
    assert got == "Error opening input files: Invalid data found when processing input"


def test_ffmpeg_error_normalizes_the_windows_unsigned_exit_code():
    """ffmpeg returns negative AVERROR codes; Windows reports them as unsigned
    DWORDs, so the raw number surfaces as an alarming 3199971767."""
    got = ma.ffmpeg_error(FFMPEG_FAIL, 3199971767)
    assert got.startswith("ffmpeg exit -1094995529: ")


def test_ffmpeg_error_keeps_a_normal_negative_code_as_is():
    assert ma.ffmpeg_error("boom", -9).startswith("ffmpeg exit -9: ")


def test_ffmpeg_error_survives_empty_stderr():
    assert "no diagnostic" in ma.ffmpeg_error("", 1)
    assert "no diagnostic" in ma.ffmpeg_error(None, 1)


def test_ffmpeg_error_is_bounded():
    assert len(ma.ffmpeg_error("x" * 900)) <= 140


# --- replaygain_db -------------------------------------------------------------

def test_replaygain_sign_a_loud_track_gets_turned_down():
    # -8.4 LUFS is 9.6 dB hotter than the -18 reference -> negative gain.
    assert ma.replaygain_db(-8.4) == -9.6


def test_replaygain_sign_a_quiet_track_gets_turned_up():
    assert ma.replaygain_db(-23.0) == 5.0


def test_replaygain_at_reference_is_zero():
    assert ma.replaygain_db(-18.0) == 0.0


def test_replaygain_of_nothing_is_nothing():
    assert ma.replaygain_db(None) is None


@pytest.mark.parametrize("lufs,expected", [(0.0, -18.0), (-60.0, 42.0)])
def test_replaygain_absurd_inputs_still_arithmetic(lufs, expected):
    assert ma.replaygain_db(lufs) == expected


# --- remap_path ----------------------------------------------------------------

def test_remap_swaps_pearls_root_for_this_boxs():
    got = ma.remap_path("/mnt/music/Adele/21/01 Rolling in the Deep.m4a",
                        local_root=r"\\hummingbird\music")
    assert got.replace("/", "\\") == \
        r"\\hummingbird\music\Adele\21\01 Rolling in the Deep.m4a"


def test_remap_refuses_a_path_outside_the_catalog_root():
    """Better to skip the row loudly than build a nonsense path and blame the
    NAS for the resulting 'file missing'."""
    assert ma.remap_path("/etc/passwd", local_root=r"\\hummingbird\music") is None
    assert ma.remap_path("/mnt/musicXX/a.mp3", local_root=r"\\h\m") is None


def test_remap_needs_a_local_root():
    assert ma.remap_path("/mnt/music/a.mp3", local_root=None) is None
    assert ma.remap_path("", local_root=r"\\h\m") is None


def test_remap_keeps_names_with_spaces_and_punctuation():
    # Separator is os.path.join's business -- backslashes on the Windows box
    # this actually runs on, forward on CI's ubuntu. The contract is that every
    # segment survives intact, not which slash joins them.
    got = ma.remap_path("/mnt/music/AC-DC/Back in Black/01 Hells Bells.mp3",
                        local_root="/tmp/x")
    assert got.replace("\\", "/") == "/tmp/x/AC-DC/Back in Black/01 Hells Bells.mp3"


# --- done_ids / resumption -----------------------------------------------------

def test_done_ids_reads_the_jsonl():
    lines = ['{"id": "b:aaa", "bpm": 120}', '{"id": "b:bbb", "error": "decode"}']
    assert ma.done_ids(lines) == {"b:aaa", "b:bbb"}


def test_a_truncated_final_line_is_skipped_not_fatal():
    """The normal shape of a killed multi-hour pass. That track simply gets
    re-analyzed; one torn line must not refuse the whole resume."""
    lines = ['{"id": "b:aaa", "bpm": 120}', '{"id": "b:bbb", "bp']
    assert ma.done_ids(lines) == {"b:aaa"}


def test_blank_lines_and_junk_are_ignored():
    assert ma.done_ids(["", "   ", "not json at all", '{"no": "id"}']) == set()


def test_work_list_is_exactly_what_is_left():
    rows = [{"id": "b:a", "path": "/mnt/music/a.mp3"},
            {"id": "b:b", "path": "/mnt/music/b.mp3"},
            {"id": "b:c", "path": "/mnt/music/c.mp3"}]
    assert ma.work_list(rows, {"b:a"}) == [("b:b", "/mnt/music/b.mp3"),
                                           ("b:c", "/mnt/music/c.mp3")]


def test_work_list_empty_when_everything_is_done():
    rows = [{"id": "b:a", "path": "/x"}]
    assert ma.work_list(rows, {"b:a"}) == []


# --- records -------------------------------------------------------------------

def test_record_round_trips_through_jsonl():
    rec = ma.record_for("b:abc", bpm=128.5, rg=-9.6, dr=5.2, peak=0.3)
    back = json.loads(json.dumps(rec))
    assert back == {"id": "b:abc", "bpm": 128.5, "replaygain_db": -9.6,
                    "dynamic_range_db": 5.2, "true_peak_dbfs": 0.3}


def test_an_error_record_carries_no_measurements():
    rec = ma.record_for("b:abc", error="decode: boom")
    assert rec == {"id": "b:abc", "error": "decode: boom"}
    assert "bpm" not in rec


def test_eta_and_formatting():
    assert ma.eta(0, 100, 10) == 0
    assert ma.eta(50, 100, 100) == 100.0
    assert ma.fmt_hms(3661) == "1:01:01"
    assert ma.fmt_hms(-5) == "0:00:00"


# --- the importer --------------------------------------------------------------

def track(**kw):
    base = {"id": "b:abc", "title": "Safe and Sound", "artist": "Capital Cities",
            "album": "In a Tidal Wave of Mystery", "format": "m4a",
            "indexed_at": 1000}
    base.update(kw)
    return base


@pytest.fixture
def conn():
    c = mc.connect(":memory:")
    mc.upsert_track(c, track())
    mc.upsert_track(c, track(id="b:def", title="Kangaroo Court"))
    return c


def test_parse_records_drops_torn_lines():
    recs, bad = mi.parse_records(['{"id": "b:a", "bpm": 1}', '{"id": "b:b"',
                                  "", '{"bpm": 2}'])
    assert [r["id"] for r in recs] == ["b:a"]
    assert bad == 2  # the torn line and the id-less one


def test_latest_by_id_lets_a_reanalysis_win():
    """--force appends rather than rewriting, so the same id legitimately
    appears twice and the newer measurement is the one that should land."""
    recs = [{"id": "b:a", "bpm": 100}, {"id": "b:a", "bpm": 128}]
    assert mi.latest_by_id(recs)["b:a"]["bpm"] == 128


def test_split_results_separates_failures():
    ok, bad = mi.split_results([{"id": "b:a", "bpm": 1},
                                {"id": "b:b", "error": "decode"}])
    assert [r["id"] for r in ok] == ["b:a"]
    assert [r["id"] for r in bad] == ["b:b"]


def test_import_applies_measurements(conn):
    mi.apply_measurements(conn, [{"id": "b:abc", "bpm": 128.5,
                                  "replaygain_db": -9.6,
                                  "dynamic_range_db": 5.2}])
    row = conn.execute("SELECT bpm, replaygain_db, dynamic_range_db "
                       "FROM tracks WHERE id='b:abc'").fetchone()
    assert (row["bpm"], row["replaygain_db"], row["dynamic_range_db"]) == \
        (128.5, -9.6, 5.2)


def test_reimporting_the_same_jsonl_is_a_no_op(conn):
    """The acceptance criterion, as a test: run it twice, assert the row count
    doesn't move and the values are identical."""
    recs = [{"id": "b:abc", "bpm": 128.5, "replaygain_db": -9.6,
             "dynamic_range_db": 5.2}]
    mi.apply_measurements(conn, recs)
    n1 = conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
    snap1 = conn.execute("SELECT * FROM tracks ORDER BY id").fetchall()
    mi.apply_measurements(conn, recs)
    n2 = conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]
    snap2 = conn.execute("SELECT * FROM tracks ORDER BY id").fetchall()
    assert n1 == n2
    assert [tuple(r) for r in snap1] == [tuple(r) for r in snap2]


def test_import_never_invents_a_track(conn):
    """An id the catalog doesn't know is counted and skipped. A row created
    here would be a track with no location, no tags, and no way to play."""
    updated, unknown = mi.apply_measurements(conn, [{"id": "b:ghost", "bpm": 99}])
    assert (updated, unknown) == (0, 1)
    assert conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"] == 2


def test_a_failure_lands_in_needs_attention(conn):
    marked, _ = mi.apply_failures(conn, [{"id": "b:def",
                                          "error": "decode: moov atom not found"}])
    assert marked == 1
    row = conn.execute("SELECT needs_attention, bpm FROM tracks "
                       "WHERE id='b:def'").fetchone()
    assert "moov atom" in row["needs_attention"]
    assert row["bpm"] is None  # a failure is not a measurement


def test_a_later_measurement_overwrites_an_older_one(conn):
    """The importer updates unconditionally so a better tempo algorithm can
    land without a migration or a manual DELETE."""
    mi.apply_measurements(conn, [{"id": "b:abc", "bpm": 100}])
    mi.apply_measurements(conn, [{"id": "b:abc", "bpm": 128}])
    assert conn.execute("SELECT bpm FROM tracks WHERE id='b:abc'"
                        ).fetchone()["bpm"] == 128
