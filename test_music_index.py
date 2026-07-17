# =============================================================================
# project-squirrel -- test_music_index.py
#
# The indexer's pure half (issue #120): locating the audio stream in each of
# the four containers this library actually contains. This is the code the
# whole identity decision rests on -- if a span is wrong, the id is wrong, and
# a track's ratings and play history attach to a stranger.
#
# Synthetic fixtures, deliberately: a test that needs a 30 MB ALAC file off the
# NAS isn't a test CI can run. The real containers were probed separately
# (32/32 located across 8 files per format); these lock in the parsing rules
# and the traps that probe surfaced.
#
# NOTE: this file must be on .github/workflows/tests.yml's pytest line by hand
# -- CI enumerates test files with no testpaths fallback.
# =============================================================================

import io
import os
import struct

from jukebox import music_index as mi


def fh(data):
    return io.BytesIO(data), len(data)


def atom(atype, payload):
    return struct.pack(">I4s", len(payload) + 8, atype) + payload


# --- mp4 / m4a -- 62% of the library ------------------------------------------

def test_mp4_finds_mdat():
    # ftyp is 8 + 4 = 12 bytes, so mdat's 8-byte header sits at 12 and its
    # payload starts at 20.
    data = atom(b"ftyp", b"M4A ") + atom(b"mdat", b"AUDIOAUDIO")
    f, n = fh(data)
    off, ln = mi.mp4_audio_span(f, n)
    assert (off, ln) == (20, 10)
    assert data[off:off + ln] == b"AUDIOAUDIO"


def test_mp4_finds_mdat_when_moov_comes_first():
    """iTunes writes moov before mdat on some files and after on others. A
    fixed offset would be wrong on half the library."""
    data = atom(b"ftyp", b"M4A ") + atom(b"moov", b"T" * 40) + \
        atom(b"mdat", b"AUDIO")
    f, n = fh(data)
    off, ln = mi.mp4_audio_span(f, n)
    assert (off, ln) == (12 + 48 + 8, 5)
    assert data[off:off + ln] == b"AUDIO"


def test_mp4_finds_mdat_when_moov_comes_last():
    data = atom(b"mdat", b"AUDIO") + atom(b"moov", b"T" * 40)
    f, n = fh(data)
    off, ln = mi.mp4_audio_span(f, n)
    assert data[off:off + ln] == b"AUDIO"


def test_mp4_handles_64bit_extended_size():
    """alen == 1 means a 64-bit size follows the type. Real for long ALAC:
    a 32-bit atom size caps at 4 GB."""
    payload = b"AUDIOAUDIO"
    data = (struct.pack(">I4s", 1, b"mdat") +
            struct.pack(">Q", 16 + len(payload)) + payload)
    f, n = fh(data)
    assert mi.mp4_audio_span(f, n) == (16, 10)


def test_mp4_handles_size_zero_meaning_to_eof():
    data = struct.pack(">I4s", 0, b"mdat") + b"AUDIOAUDIO"
    f, n = fh(data)
    assert mi.mp4_audio_span(f, n) == (8, 10)


def test_mp4_without_mdat_is_none_not_a_crash():
    """A file we can't parse lands in the needs-attention bucket. It never
    kills the pass."""
    f, n = fh(atom(b"ftyp", b"M4A ") + atom(b"moov", b"T" * 8))
    assert mi.mp4_audio_span(f, n) is None


def test_mp4_truncated_atom_is_none():
    f, n = fh(struct.pack(">I4s", 999999, b"mdat")[:6])
    assert mi.mp4_audio_span(f, n) is None


# --- mp3 -- 32% of the library ------------------------------------------------

def id3v2(size):
    """An ID3v2 header declaring `size` bytes of tag body, syncsafe-encoded."""
    ss = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3" + b"\x04\x00" + b"\x00" + ss + b"T" * size


def test_mp3_skips_id3v2_header():
    data = id3v2(50) + b"AUDIO"
    f, n = fh(data)
    off, ln = mi.mp3_audio_span(f, n)
    assert (off, ln) == (60, 5)
    assert data[off:off + ln] == b"AUDIO"


def test_mp3_size_is_syncsafe_not_plain_big_endian():
    """THE CLASSIC BUG. Syncsafe uses 7 bits per byte so the high bit never
    sets and can't be mistaken for an MPEG sync word. Read as a plain int, a
    tag of 200 bytes reads as 328 and the offset lands mid-audio.

    200 = 0b11001000 -> syncsafe bytes 0x01,0x48 -> plain BE would be 328."""
    data = id3v2(200) + b"AUDIO"
    f, n = fh(data)
    off, _ = mi.mp3_audio_span(f, n)
    assert off == 210          # not 338
    assert data[off:off + 5] == b"AUDIO"


def test_mp3_with_no_id3v2_starts_at_zero():
    data = b"\xff\xfb" + b"AUDIO" * 40
    f, n = fh(data)
    assert mi.mp3_audio_span(f, n) == (0, len(data))


def test_mp3_too_short_to_hold_a_header_is_none():
    """Under 10 bytes there isn't room for an ID3v2 header, let alone audio.
    Lands in the needs-attention bucket rather than crashing the pass."""
    f, n = fh(b"\xff\xfbAUD")
    assert mi.mp3_audio_span(f, n) is None


def test_mp3_trims_id3v1_trailer():
    data = id3v2(10) + b"AUDIO" + b"TAG" + b"z" * 125
    f, n = fh(data)
    off, ln = mi.mp3_audio_span(f, n)
    assert data[off:off + ln] == b"AUDIO"


def test_mp3_trims_apev2_trailer():
    body = b"q" * 40
    footer = (b"APETAGEX" + struct.pack("<I", 2000) +
              struct.pack("<I", len(body) + 32) + b"\x00" * 16)
    data = id3v2(10) + b"AUDIO" + body + footer
    f, n = fh(data)
    off, ln = mi.mp3_audio_span(f, n)
    assert data[off:off + ln] == b"AUDIO"


def test_mp3_all_tag_no_audio_is_none():
    f, n = fh(id3v2(10))
    assert mi.mp3_audio_span(f, n) is None


# --- flac -- 4%, and it carries a free identity -------------------------------

def streaminfo(md5_hex):
    body = b"\x00" * 18 + bytes.fromhex(md5_hex)
    return b"\x00" + len(body).to_bytes(3, "big") + body


def last_block():
    return b"\x81" + (4).to_bytes(3, "big") + b"\x00" * 4


def test_flac_finds_frames_and_streaminfo_md5():
    """STREAMINFO's MD5 is of the DECODED audio, so it survives not just a tag
    edit but a re-compression at a different level -- strictly stronger than
    hashing the frames, and free. Present on 8/8 real samples."""
    md5 = "0123456789abcdef" * 2
    data = b"fLaC" + streaminfo(md5) + last_block() + b"FRAMES"
    f, n = fh(data)
    span, got = mi.flac_audio_span(f, n)
    assert got == md5
    off, ln = span
    assert data[off:off + ln] == b"FRAMES"


def test_flac_zeroed_md5_is_treated_as_absent():
    """A zeroed MD5 means the encoder declined to compute it -- a valid field
    saying nothing. Treating "000..." as an identity would collapse every such
    track into one."""
    data = b"fLaC" + streaminfo("0" * 32) + last_block() + b"FRAMES"
    f, n = fh(data)
    span, got = mi.flac_audio_span(f, n)
    assert got is None
    assert span is not None      # we still hash the frames


def test_flac_bad_magic_is_none():
    f, n = fh(b"NOPEnotaflacfile")
    assert mi.flac_audio_span(f, n) == (None, None)


# --- wav -- the 1.4% tail -----------------------------------------------------

def test_wav_finds_data_chunk():
    payload = b"PCMPCMPCM!"
    data = (b"RIFF" + struct.pack("<I", 0) + b"WAVE" +
            b"fmt " + struct.pack("<I", 16) + b"f" * 16 +
            b"data" + struct.pack("<I", len(payload)) + payload)
    f, n = fh(data)
    off, ln = mi.wav_audio_span(f, n)
    assert data[off:off + ln] == payload


def test_wav_skips_odd_length_chunk_pad_byte():
    """Chunks are word-aligned: an odd-length chunk is followed by a pad byte
    that belongs to no chunk. Missing it desyncs the walk on any file with an
    odd-sized LIST/INFO block."""
    payload = b"PCM!"
    data = (b"RIFF" + struct.pack("<I", 0) + b"WAVE" +
            b"LIST" + struct.pack("<I", 3) + b"abc" + b"\x00" +
            b"data" + struct.pack("<I", len(payload)) + payload)
    f, n = fh(data)
    off, ln = mi.wav_audio_span(f, n)
    assert data[off:off + ln] == payload


def test_wav_trusts_the_file_over_a_lying_chunk_header():
    """A truncated download leaves clen describing bytes that aren't there.
    Hashing past EOF would just read short; clamping keeps the span honest."""
    data = (b"RIFF" + struct.pack("<I", 0) + b"WAVE" +
            b"data" + struct.pack("<I", 999999) + b"PCM!")
    f, n = fh(data)
    off, ln = mi.wav_audio_span(f, n)
    assert (off, ln) == (20, 4)


def test_wav_bad_magic_is_none():
    f, n = fh(b"NOPE" + b"\x00" * 20)
    assert mi.wav_audio_span(f, n) is None


# --- dispatch and hashing -----------------------------------------------------

def test_format_of_recognizes_the_library_and_rejects_the_rest():
    assert mi.format_of("/mnt/music/x.m4a") == "m4a"
    assert mi.format_of("/mnt/music/x.MP3") == "mp3"      # case-insensitive
    assert mi.format_of("/mnt/music/x.flac") == "flac"
    # iTunes bookkeeping and cover art -- 2,241 .itc2 and 400 .jpg on the real
    # share. Indexing these would invent tracks that don't exist.
    assert mi.format_of("/mnt/music/x.itc2") is None
    assert mi.format_of("/mnt/music/cover.jpg") is None
    assert mi.format_of("/mnt/music/x.itl") is None


def test_audio_span_dispatches_and_returns_md5_only_for_flac():
    f, n = fh(atom(b"mdat", b"AUDIO"))
    span, md5 = mi.audio_span(f, n, "m4a")
    assert span == (8, 5) and md5 is None


def test_hash_span_covers_exactly_the_audio_bytes():
    """The identity contract: identical audio in different tag wrappers hashes
    the same. This is the unit-level statement of the retag probe."""
    f1, _ = fh(b"TAGSTAGS" + b"AUDIO" + b"TRAILER")
    f2, _ = fh(b"D" * 40 + b"AUDIO" + b"XX")
    assert mi.hash_span(f1, 8, 5) == mi.hash_span(f2, 40, 5)


def test_hash_span_differs_on_different_audio():
    f1, _ = fh(b"AUDIO")
    f2, _ = fh(b"AUDIOX")
    assert mi.hash_span(f1, 0, 5) != mi.hash_span(f2, 0, 6)


def test_root_path_unset_or_blank_is_the_default(monkeypatch):
    monkeypatch.delenv("MERLE_MUSIC_ROOT", raising=False)
    assert mi.root_path() == mi.DEFAULT_ROOT
    monkeypatch.setenv("MERLE_MUSIC_ROOT", "  ")
    assert mi.root_path() == mi.DEFAULT_ROOT
    monkeypatch.setenv("MERLE_MUSIC_ROOT", "/srv/music")
    assert mi.root_path() == "/srv/music"


# --- the walk ------------------------------------------------------------------

def test_walk_skips_the_recycle_bin(tmp_path):
    """Regression for issue #129: the Synology share's `#recycle` bin got
    indexed on the first pass (3,096 locations of deleted tracks), which would
    have put deleted albums on the GUI's shelves. The walk must not descend
    into it -- at any depth, since the NAS keeps the bin's internal tree."""
    (tmp_path / "Artist" / "Album").mkdir(parents=True)
    (tmp_path / "Artist" / "Album" / "01 Keeper.mp3").write_bytes(b"x")
    (tmp_path / "#recycle" / "Artist" / "Album").mkdir(parents=True)
    (tmp_path / "#recycle" / "Artist" / "Album" / "02 Deleted.mp3").write_bytes(b"x")
    (tmp_path / "Artist" / "#recycle").mkdir()
    (tmp_path / "Artist" / "#recycle" / "03 Nested.mp3").write_bytes(b"x")

    found = [path for path, fmt in mi.walk(str(tmp_path))]
    assert len(found) == 1
    assert found[0].endswith("01 Keeper.mp3")


def test_walk_yields_sorted_and_typed(tmp_path):
    (tmp_path / "b.mp3").write_bytes(b"x")
    (tmp_path / "a.flac").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    got = list(mi.walk(str(tmp_path)))
    assert [os.path.basename(p) for p, _ in got] == ["a.flac", "b.mp3"]
    assert [f for _, f in got] == ["flac", "mp3"]


# --- mp4 codec probe (issue #149) ------------------------------------------------
#
# `format` alone can't say what's inside an m4a -- ALAC and lossy AAC share
# the extension and the browser output treats them oppositely. The probe
# walks moov>trak>mdia>minf>stbl>stsd for the sample-entry fourcc; a byte
# scan would misfire on the "alac" magic-cookie box and on cover art.

def stsd(fourcc):
    entry = struct.pack(">I4s", 16, fourcc) + b"\x00" * 8
    return atom(b"stsd", b"\x00" * 4 + struct.pack(">I", 1) + entry)


def moov_with(fourcc):
    return atom(b"moov", atom(b"trak", atom(b"mdia", atom(b"minf", atom(
        b"stbl", stsd(fourcc))))))


def test_codec_finds_alac():
    f, n = fh(atom(b"ftyp", b"M4A ") + moov_with(b"alac") +
              atom(b"mdat", b"AUDIO"))
    assert mi.mp4_codec(f, n) == "alac"


def test_codec_maps_mp4a_to_aac():
    f, n = fh(atom(b"ftyp", b"M4A ") + moov_with(b"mp4a") +
              atom(b"mdat", b"AUDIO"))
    assert mi.mp4_codec(f, n) == "aac"


def test_codec_passes_an_exotic_fourcc_through():
    """FairPlay relics and exotica stay honest in the catalog; the policy
    routes them to the FLAC path where an undecodable stream fails visibly."""
    f, n = fh(moov_with(b"drms"))
    assert mi.mp4_codec(f, n) == "drms"


def test_codec_finds_moov_after_mdat():
    f, n = fh(atom(b"mdat", b"AUDIO") + moov_with(b"alac"))
    assert mi.mp4_codec(f, n) == "alac"


def test_codec_without_stsd_is_none_not_a_crash():
    f, n = fh(atom(b"ftyp", b"M4A ") + atom(b"moov", atom(b"trak", b"")) +
              atom(b"mdat", b"AUDIO"))
    assert mi.mp4_codec(f, n) is None


def test_codec_on_garbage_is_none():
    f, n = fh(b"this is not an mp4 at all, not even slightly....")
    assert mi.mp4_codec(f, n) is None


def test_codec_bounded_against_a_self_containing_atom():
    """A malformed atom that claims to contain itself must not recurse
    forever -- the depth bound is the guard."""
    # moov whose payload claims another moov of the same claimed size.
    inner = struct.pack(">I4s", 16, b"moov") + b"\x00" * 8
    f, n = fh(atom(b"moov", inner))
    assert mi.mp4_codec(f, n) is None


def test_identify_carries_the_codec_for_m4a(tmp_path):
    data = atom(b"ftyp", b"M4A ") + moov_with(b"alac") + \
        atom(b"mdat", b"AUDIOAUDIO")
    p = tmp_path / "t.m4a"
    p.write_bytes(data)
    track_id, off, ln, note, codec = mi.identify(str(p), "m4a", len(data))
    assert track_id is not None and note is None
    assert codec == "alac"


def test_identify_leaves_codec_none_for_other_formats(tmp_path):
    p = tmp_path / "t.wav"
    data = b"RIFF" + b"\x00" * 4 + b"WAVE" + \
        struct.pack("<4sI", b"data", 4) + b"beep"
    p.write_bytes(data)
    track_id, off, ln, note, codec = mi.identify(str(p), "wav", len(data))
    assert track_id is not None
    assert codec is None
