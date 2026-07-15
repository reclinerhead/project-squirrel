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
import struct

import music_index as mi


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
