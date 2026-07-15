# =============================================================================
# project-squirrel -- music_index.py
#
# The read-only indexer (issue #120, Phase 0 of #115): walks the NAS music
# share, works out each file's audio-stream byte span, hashes it, and upserts
# the result into music_catalog. Runs on pearl, by hand, not as a service --
# this is a one-time pass plus occasional re-runs, and a `while True` daemon
# for a job that has an end is the wrong shape.
#
# WHY PEARL AND NOT BLUEJAY, which has the faster CPU: because the pass is
# WIRE-LIMITED, not CPU-bound, and that was measured rather than assumed.
# pearl reads the share at 99.8 MB/s; bluejay at 90.2 MB/s. Both are at
# gigabit saturation (~110 MB/s practical), and blake2b hashes at GB/s -- so
# neither CPU is the constraint and pearl is in fact the faster reader. pearl
# also already has the share mounted read-only and already owns music.db, so
# it writes rows directly with no JSONL export/import hop.
#
# Rejected: bluejay + JSONL + INSERT OR IGNORE on pearl. That IS the right
# shape for Phase 1 -- beat tracking is genuinely CPU-bound and bluejay is
# several times pearl there -- but for Phase 0 it buys a slower pass and an
# extra moving part. Phase 1 builds that importer when it has a reason to.
#
# Also measured, and it closes two of #115's open questions:
#   - vers=2.0 costs nothing. The wire is the limit, so there is no throughput
#     to win by revisiting the mount version.
#   - pearl reads at wire speed, so D2's transcode cache stays on pearl and
#     hummingbird's escalation triggers remain unmet.
#
# THE AUDIO SPAN, PER FORMAT. Identity is a hash of the audio stream (see
# music_catalog.py's banner for why -- it was measured against a retag), which
# means finding where the audio actually starts and stops. Probed against 8
# real files per format, 32/32 located, with no dependency needed for the
# offsets -- stdlib struct parsing does all four:
#
#   m4a/mp4  the `mdat` atom payload. 62% of this library.
#   mp3      between the ID3v2 header (syncsafe size) and the ID3v1 `TAG` /
#            APEv2 `APETAGEX` trailers.
#   flac     after the metadata blocks. STREAMINFO also carries an MD5 of the
#            DECODED audio, present on 8/8 samples -- a free, container-
#            independent identity, used in preference to hashing where it's
#            there and non-zero.
#   wav      the `data` chunk.
#
# Measured tag overhead -- the bytes a whole-file hash would wrongly fold into
# the identity: m4a 0.12-1.34%, mp3 0.18-4.10%. Small, and fatal: a retag moves
# them and the whole-file hash changes.
#
# A file whose span we cannot locate is NOT dropped and NOT fatal
# (weather.py:773-777's ethos): it lands with `needs_attention` set, which is
# Phase 1's bucket, so it's a queryable number in the GUI rather than a mystery.
#
# NEVER WRITES TO THE SHARE. Principle 1 -- the audio files are an immutable
# input and we never write tags back. The mount is `ro`, so this is enforced
# rather than trusted, and every file here is opened "rb".
#
# Config (env):
#   MERLE_MUSIC_ROOT  the library root (default: /mnt/music)
#   MERLE_MUSIC_DB    the catalog -- see music_catalog.py
#
# Usage (on pearl):
#   python3 music_index.py                 full pass, honoring the hash cache
#   python3 music_index.py --limit 200     a sample, for a smoke test
#   python3 music_index.py --rehash        ignore the cache, re-hash everything
#   python3 music_index.py --prune         also drop locations that vanished
#   python3 music_index.py --dry-run       walk and hash, write nothing
#
# Measured on pearl against the real 612.7 GB / 26,590-file library:
#   ~61 MB/s hashing, ~56 MB/s with tag reads -> a ~3.0 h first pass.
# A raw sequential read of a sample of large files clocks 99.8 MB/s, so the
# real walk runs at ~60% of best-case streaming -- per-file open/seek overhead
# across 26k mostly-12 MB files, not the hash and not the chunk size (1/4/8 MB
# read chunks all landed within 1%, since the mount's rsize=65536 governs).
# The SECOND pass is seconds: the hash cache means a re-index re-reads nothing.
# =============================================================================

import argparse
import hashlib
import os
import struct
import sys
import time

from jukebox import music_catalog

DEFAULT_ROOT = "/mnt/music"

# What counts as a track. Everything else on the share is iTunes bookkeeping
# and cover art -- 2,241 .itc2, 400 .jpg, 11 .tmp, 10 .png, 5 .cue, 5 .log,
# 3 .itl at last count -- and indexing any of it would be inventing tracks that
# don't exist.
AUDIO_EXTS = {".m4a": "m4a", ".mp3": "mp3", ".flac": "flac", ".wav": "wav",
              ".mp4": "mp4"}

READ_CHUNK = 1 << 20

# Progress cadence. The pass is ~1.74 h, so silence for that long is
# indistinguishable from a hang.
LOG_EVERY = 250


def root_path():
    """MERLE_MUSIC_ROOT: unset or blank means the default (the MERLE_* rule --
    unset OR blank is the default, never a half-configured run)."""
    return os.environ.get("MERLE_MUSIC_ROOT", "").strip() or DEFAULT_ROOT


# --- pure: locating the audio stream ------------------------------------------
#
# Each returns (offset, length) into the file, or None if the container isn't
# what it claimed. All take an open binary file plus its size -- injected, so
# tests run against small synthetic fixtures rather than 30 MB of real music.

def mp4_audio_span(fh, size):
    """The `mdat` atom's payload. Walks top-level atoms rather than assuming
    layout: iTunes writes `moov` before `mdat` on some files and after on
    others, and a fixed offset would be wrong on half the library.

    Handles both size escapes: alen == 1 means a 64-bit size follows the type
    (real for long ALAC files -- 32 bits caps at 4 GB), alen == 0 means the
    atom runs to EOF."""
    pos = 0
    while pos < size:
        fh.seek(pos)
        hdr = fh.read(8)
        if len(hdr) < 8:
            return None
        alen, atype = struct.unpack(">I4s", hdr)
        hdr_len = 8
        if alen == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return None
            alen = struct.unpack(">Q", ext)[0]
            hdr_len = 16
        elif alen == 0:
            alen = size - pos
        if alen < hdr_len:
            return None
        if atype == b"mdat":
            return (pos + hdr_len, alen - hdr_len)
        pos += alen
    return None


def mp3_audio_span(fh, size):
    """Everything between the ID3v2 header and the ID3v1/APEv2 trailers.

    ID3v2's size is SYNCSAFE -- 7 bits per byte, so the high bit never sets and
    can't be mistaken for an MPEG sync word. Reading it as a plain big-endian
    int is the classic bug and silently lands the offset mid-tag."""
    fh.seek(0)
    head = fh.read(10)
    if len(head) < 10:
        return None
    start = 0
    if head[:3] == b"ID3":
        b = head[6:10]
        start = 10 + ((b[0] & 0x7F) << 21 | (b[1] & 0x7F) << 14 |
                      (b[2] & 0x7F) << 7 | (b[3] & 0x7F))
    end = size
    if size >= 128:
        fh.seek(size - 128)
        if fh.read(3) == b"TAG":
            end = size - 128
    if end >= 32:
        fh.seek(end - 32)
        if fh.read(8) == b"APETAGEX":
            fh.seek(end - 32 + 12)
            raw = fh.read(4)
            if len(raw) == 4:
                # The footer's size counts the body plus the footer itself.
                end = max(0, end - 32 - (struct.unpack("<I", raw)[0] - 32))
    if start >= end:
        return None
    return (start, end - start)


def flac_audio_span(fh, size):
    """After the metadata blocks, plus STREAMINFO's decoded-audio MD5 if it's
    there. Returns (span, md5_hex) -- md5 is None when absent or zeroed.

    A zeroed MD5 means the encoder declined to compute it, so it's a valid
    field saying nothing. Treating "00000..." as an identity would collapse
    every such track into one."""
    fh.seek(0)
    if fh.read(4) != b"fLaC":
        return None, None
    md5 = None
    pos = 4
    while True:
        fh.seek(pos)
        hdr = fh.read(4)
        if len(hdr) < 4:
            return None, None
        last = hdr[0] & 0x80
        btype = hdr[0] & 0x7F
        blen = int.from_bytes(hdr[1:4], "big")
        if btype == 0:
            si = fh.read(blen)
            if len(si) >= 34:
                raw = si[18:34].hex()
                if raw != "0" * 32:
                    md5 = raw
        pos += 4 + blen
        if last:
            break
        if pos >= size:
            return None, None
    if pos >= size:
        return None, md5
    return (pos, size - pos), md5


def wav_audio_span(fh, size):
    """The `data` chunk. Chunks are word-aligned, so an odd-length chunk is
    followed by a pad byte that is not part of any chunk -- skipping it is what
    keeps the walk in sync on files with an odd-sized LIST/INFO block."""
    fh.seek(0)
    if fh.read(4) != b"RIFF":
        return None
    pos = 12
    while pos < size - 8:
        fh.seek(pos)
        hdr = fh.read(8)
        if len(hdr) < 8:
            return None
        cid, clen = struct.unpack("<4sI", hdr)
        if cid == b"data":
            # Trust the file's length over the chunk header: a truncated
            # download leaves clen describing bytes that aren't there.
            return (pos + 8, min(clen, size - pos - 8))
        pos += 8 + clen + (clen & 1)
    return None


def audio_span(fh, size, fmt):
    """Dispatch on the indexed format. Returns (span, md5) so flac's free
    identity rides the same path as everyone else's."""
    if fmt in ("m4a", "mp4"):
        return mp4_audio_span(fh, size), None
    if fmt == "mp3":
        return mp3_audio_span(fh, size), None
    if fmt == "flac":
        return flac_audio_span(fh, size)
    if fmt == "wav":
        return wav_audio_span(fh, size), None
    return None, None


def format_of(path):
    """The indexed format for a path, or None if it isn't a track."""
    return AUDIO_EXTS.get(os.path.splitext(path)[1].lower())


# --- I/O: the thin half -------------------------------------------------------

def hash_span(fh, offset, length):
    """blake2b-128 over exactly the audio bytes.

    Streamed rather than slurped: a WAV in this library runs to 65 MB and
    holding whole files in memory buys nothing. The chunk size itself is not a
    tuning knob -- 1, 4, and 8 MB all measured within 1% of each other, because
    the mount's rsize=65536 is what actually governs the wire."""
    h = hashlib.blake2b(digest_size=16)
    fh.seek(offset)
    left = length
    while left > 0:
        chunk = fh.read(min(READ_CHUNK, left))
        if not chunk:
            break
        left -= len(chunk)
        h.update(chunk)
    return h.hexdigest()


def identify(path, fmt, size):
    """This file's track id, plus the span we hashed. Returns
    (id, offset, length, note) where `note` is None on success and a
    needs_attention reason otherwise.

    flac's STREAMINFO MD5 wins where present: it identifies the DECODED audio,
    so it survives not just a tag edit but a re-compression at a different
    level -- strictly stronger than hashing the frames, and free."""
    with open(path, "rb") as fh:
        span, md5 = audio_span(fh, size, fmt)
        if span is None or span[1] <= 0:
            return None, None, None, "unparsed:%s" % fmt
        offset, length = span
        if md5:
            return "f:" + md5, offset, length, None
        return "b:" + hash_span(fh, offset, length), offset, length, None


def read_tags(path, fmt):
    """Tags as a dict, or {} if we can't read them. mutagen is optional on
    purpose: it is the only non-stdlib thing this pass wants, and a box without
    it should still produce a complete, correctly-identified catalog with null
    tags rather than refusing to run."""
    try:
        import mutagen
    except ImportError:
        return {}
    try:
        m = mutagen.File(path, easy=True)
    except Exception:
        return {}
    if m is None:
        return {}

    def first(key):
        try:
            v = m.get(key)
        except Exception:
            return None
        if not v:
            return None
        return v[0] if isinstance(v, list) else v

    info = getattr(m, "info", None)
    return {
        "title": music_catalog.norm_tag(first("title")),
        "artist": music_catalog.norm_tag(first("artist")),
        "album": music_catalog.norm_tag(first("album")),
        "album_artist": music_catalog.norm_tag(first("albumartist")),
        "track_no": music_catalog.norm_int(first("tracknumber")),
        "disc_no": music_catalog.norm_int(first("discnumber")),
        "year": music_catalog.norm_int(
            (first("date") or "")[:4] if first("date") else None),
        "genre": music_catalog.norm_tag(first("genre")),
        "duration_s": getattr(info, "length", None),
        "bitrate": getattr(info, "bitrate", None),
        "samplerate": getattr(info, "sample_rate", None),
        "channels": getattr(info, "channels", None),
    }


def walk(root):
    """Every audio file under `root`, as (path, format). Sorted per directory
    so a re-run visits in the same order and its progress log is comparable to
    the last one's."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            fmt = format_of(name)
            if fmt:
                yield os.path.join(dirpath, name), fmt


def index_file(conn, path, fmt, cache, now, rehash=False, dry_run=False):
    """Index one file. Returns the outcome as a string, for the tallies.

    The cache check is what makes a re-index minutes instead of 1.74 hours --
    and it deliberately checks BOTH size and mtime, because a measured retag
    left size untouched and moved only mtime (music_catalog.cache_is_valid)."""
    try:
        st = os.stat(path)
    except OSError as e:
        print("[music] stat failed, skipping: %s -- %s" % (path, e))
        return "error"
    size, mtime = st.st_size, int(st.st_mtime)

    cached = cache.get(path)
    if not rehash and music_catalog.cache_is_valid(cached, size, mtime):
        return "cached"

    try:
        track_id, offset, length, note = identify(path, fmt, size)
    except OSError as e:
        # One unreadable file never kills a 26k-file pass.
        print("[music] read failed, skipping: %s -- %s" % (path, e))
        return "error"

    if track_id is None:
        # Can't identify it -- but a file we can't parse is a number in the
        # needs-attention bucket, not a silent drop. Keyed on path since we
        # have no audio identity for it.
        track_id = "x:" + hashlib.blake2b(
            path.encode("utf-8", "replace"), digest_size=16).hexdigest()

    if dry_run:
        return "unparsed" if note else "hashed"

    track = {"id": track_id, "format": fmt, "needs_attention": note,
             "indexed_at": now}
    track.update(read_tags(path, fmt))
    music_catalog.upsert_track(conn, track)
    music_catalog.upsert_file(conn, {
        "path": path, "track_id": track_id, "size": size, "mtime": mtime,
        "audio_offset": offset, "audio_length": length, "seen_at": now})
    return "unparsed" if note else "hashed"


def main():
    ap = argparse.ArgumentParser(description="Index the NAS music library.")
    ap.add_argument("--root", default=None, help="library root")
    ap.add_argument("--db", default=None, help="catalog path")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files")
    ap.add_argument("--rehash", action="store_true",
                    help="ignore the hash cache")
    ap.add_argument("--prune", action="store_true",
                    help="drop locations this pass didn't see (never drops "
                         "tracks, ratings, or history)")
    ap.add_argument("--dry-run", action="store_true", help="write nothing")
    args = ap.parse_args()

    root = args.root or root_path()
    db = args.db or music_catalog.db_path()

    # Fail loudly at startup rather than running half-configured: a missing
    # mount presents as an empty walk, which would look like a successful pass
    # over a library that vanished.
    if not os.path.isdir(root):
        print("[music] library root not found: %s" % root)
        return 1

    print("[music] indexing %s -> %s%s%s" %
          (root, db, " (rehash)" if args.rehash else "",
           " (dry run)" if args.dry_run else ""))

    conn = music_catalog.connect(db)
    cache = music_catalog.file_cache(conn)
    print("[music] catalog knows %d locations" % len(cache))

    tally = {"hashed": 0, "cached": 0, "unparsed": 0, "error": 0}
    seen = []
    started = time.time()
    bytes_hashed = 0

    try:
        for i, (path, fmt) in enumerate(walk(root), 1):
            if args.limit and i > args.limit:
                break
            before = tally.copy()
            outcome = index_file(conn, path, fmt, cache, int(time.time()),
                                 rehash=args.rehash, dry_run=args.dry_run)
            tally[outcome] += 1
            seen.append(path)
            if outcome in ("hashed", "unparsed") and before != tally:
                try:
                    bytes_hashed += os.path.getsize(path)
                except OSError:
                    pass
            if i % LOG_EVERY == 0:
                el = time.time() - started
                mbs = (bytes_hashed / 1048576 / el) if el > 0 else 0
                print("[music] %d files -- %d hashed, %d cached, %d unparsed, "
                      "%d errors -- %.1f MB/s" %
                      (i, tally["hashed"], tally["cached"], tally["unparsed"],
                       tally["error"], mbs))
                if not args.dry_run:
                    conn.commit()
    except KeyboardInterrupt:
        # The pass is resumable by construction -- the hash cache means a
        # restart skips everything already committed. Ctrl-C is a pause.
        print("\n[music] interrupted -- committing what we have")

    if not args.dry_run:
        conn.commit()

    gone = music_catalog.moved_files(seen, cache.keys())
    if gone and not args.dry_run:
        # A rename leaves the OLD path behind as a stale row: the file is
        # re-hashed at its new path, re-links to the same track id (which is
        # the whole point), and the location it left is still on the books.
        # Without a prune, track_files grows an orphan per move forever.
        #
        # But pruning is never automatic, because the indexer cannot tell "the
        # files moved" from "the share isn't mounted" -- both present as paths
        # that stopped existing, and the second one would wipe every location
        # the catalog has. Hence an explicit flag AND a floor.
        safe = music_catalog.prune_is_safe(len(seen), len(cache))
        if args.prune and safe:
            n = music_catalog.forget_paths(conn, gone)
            print("[music] pruned %d stale locations (tracks, ratings, and "
                  "history are untouched)" % n)
        elif args.prune and not safe:
            print("[music] REFUSING to prune: saw %d of %d known locations "
                  "(<%.0f%%). That looks like a bad mount, not a library "
                  "reorganize. Nothing deleted." %
                  (len(seen), len(cache), music_catalog.PRUNE_FLOOR * 100))
        else:
            print("[music] %d known paths not seen this pass (moved or "
                  "deleted). Re-run with --prune to drop the stale locations; "
                  "tracks and ratings are never dropped." % len(gone))
            for p in gone[:10]:
                print("[music]   gone? %s" % p)

    el = time.time() - started
    mbs = (bytes_hashed / 1048576 / el) if el > 0 else 0
    print("[music] done in %.1f min -- %d hashed, %d cached, %d unparsed, "
          "%d errors" % (el / 60, tally["hashed"], tally["cached"],
                         tally["unparsed"], tally["error"]))
    print("[music] read %.1f GB of audio at %.1f MB/s" %
          (bytes_hashed / 1073741824, mbs))
    if not args.dry_run:
        print("[music] catalog: %r" % (music_catalog.counts(conn),))
    return 0


if __name__ == "__main__":
    sys.exit(main())
