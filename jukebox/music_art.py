# =============================================================================
# project-squirrel -- music_art.py
#
# The cover-art pass (issue #153, epic #115 GUI track): walks the catalog's
# art worklist, pulls each album's best image out of the files' own headers,
# writes content-addressed originals plus pre-generated sizes to the art
# store, and records the pick -- with provenance -- in music.db. Runs on
# pearl, by hand, minutes end to end (headers only; the codec backfill's
# 15,582 reads took 11 min INCLUDING writes, and this touches ~1,872 albums).
#
# WORKLIST-DRIVEN, IDEMPOTENT, STANDALONE -- the reusability rule (owner
# requirement, #153): the worklist is "albums with no album_art row", so a
# re-run after ingesting five new albums probes exactly those five, and a
# full-coverage catalog is a no-op in seconds. The future ingestion pipeline
# chains this pass unchanged; nothing here assumes "full library".
#
# SOURCES, in priority order (measured on this library: embedded art on ~83%
# of tracks, WMP-era Folder.jpg in 38% of dirs, union ~90% of albums):
#   1. The LARGEST embedded picture across the album's tracks -- iTunes-era
#      rips embed per-file, usually the same JPEG; largest wins because the
#      occasional odd one out is a low-res thumbnail, not different art.
#   2. Folder.jpg / Cover.jpg beside the files -- the WMP era's convention.
#   3. Nothing: no row is written, the GUI keeps its generated SVG, and the
#      album stays on the worklist -- a future re-run (or a retagged file)
#      gets another look for free.
#
# FILES ARE CONTENT-ADDRESSED: <blake2b128(original)>.orig, plus .thumb.webp
# (~160px) and .large.webp (~600px) generated HERE, at extraction time --
# the serve path does zero image work, and the immutable-cache contract
# (the URL is the hash) is what lets browsers keep every cover forever.
# The original's bytes are stored UNTOUCHED; the sized variants are lossy
# WebP, which is fine -- the lossless commandment covers audio, not pixels
# (owner confirmed, #153). thumbnail() never upscales: art smaller than a
# size ships at its real resolution rather than inventing detail.
#
# ARTIST IMAGES: none exist in this library (measured: zero), so v1 PROMOTES
# an album cover -- the artist's most-rated album (summed thumb values),
# tie-broken by lowest album_key so the pick is stable across runs. Recorded
# as source='derived', which a re-run may refresh; the owner's own pick
# (source='owner') is untouchable by construction (music_catalog's upsert).
#
# THE ALBUM KEY is minted by music_catalog.ALBUM_KEY_SQL -- the same
# derivation the GUI's albumIdOf uses (music/lib/catalog-rows.ts), U+241F
# separator and all. Paired fixture tests on both sides keep the two
# implementations honest; this module never re-derives it in Python.
#
# NEVER WRITES TO THE SHARE (principle 1; the ro mount enforces it). Writes
# go to the art store and the catalog only. One unreadable file or corrupt
# image logs and skips, never kills the pass (weather.py:773-777's ethos).
#
# Config (env):
#   MERLE_MUSIC_ART  the art store -- music-art's own tenant dir on the
#                    media-cache LV (/srv/media-cache/music-art on pearl).
#                    Required, absolute, must exist: this pass is pointless
#                    without somewhere to put the pixels. NOT the FLAC
#                    cache's dir -- its sweep deletes files it doesn't
#                    recognize, and art inside it would be eaten.
#   MERLE_MUSIC_DB   the catalog -- see music_catalog.py.
#
# Usage (on pearl):
#   MERLE_MUSIC_ART=/srv/media-cache/music-art \
#   MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db \
#       venv/bin/python -m jukebox.music_art [--limit N]
# =============================================================================

import argparse
import hashlib
import io
import os
import sys
import time

from jukebox import music_catalog

# The sized variants generated at extraction time. Grid cells render ~40-160
# CSS px, album/artist heroes ~160-600 -- one size per band, WebP because at
# these dimensions it's half the bytes of JPEG at the same eyeball.
SIZES = {"thumb": 160, "large": 600}
WEBP_QUALITY = 82

# The folder-art filenames the WMP/ripper era actually left behind, in
# preference order. Case matters on the Linux mount; both casings of the two
# common names cover what a sample of the share showed.
FOLDER_NAMES = ("Folder.jpg", "folder.jpg", "Cover.jpg", "cover.jpg",
                "Folder.png", "folder.png")

LOG_EVERY = 100


def art_root():
    """MERLE_MUSIC_ART, or None. No default on purpose -- a relative default
    would resolve against the CWD and quietly scatter images into a repo
    checkout (the MERLE_WEATHER_DB trap's genre)."""
    return os.environ.get("MERLE_MUSIC_ART", "").strip() or None


# --- pure: naming and picking (unit-tested in test_music_art.py) ----------------

def art_names(art_hash):
    """One image's three filenames. The bare hash is the identity; suffixes
    are the variants. '.orig' keeps the untouched bytes extension-less --
    the serve route sniffs the magic instead of trusting a name."""
    return (art_hash + ".orig",
            art_hash + ".thumb.webp",
            art_hash + ".large.webp")


def largest_picture(pictures):
    """The pick among an album's embedded images: most bytes wins. Ties go
    to the EARLIEST (stable across runs -- the worklist feeds paths sorted),
    and byte length beats decoding-and-comparing dimensions because a bigger
    file of the same art is the better source and a thumbnail stub is never
    the biggest thing in the list."""
    best = None
    for data in pictures:
        if data and (best is None or len(data) > len(best)):
            best = data
    return best


def promotion_pick(candidates):
    """artist -> the album art they inherit (issue #153's v1 rule): highest
    summed-thumbs score wins, ties break on LOWEST album_key so the pick is
    byte-stable across runs. Pure over the worklist rows so the whole rule
    is one sort key under test."""
    by_artist = {}
    for row in candidates:
        cur = by_artist.get(row["artist"])
        if cur is None or (-row["score"], row["album_key"]) < \
                (-cur["score"], cur["album_key"]):
            by_artist[row["artist"]] = row
    return by_artist


# --- I/O: headers in, pixels out -------------------------------------------------

def embedded_pictures(path):
    """Every embedded image in one file, as raw bytes. mutagen reads tag
    structures only -- headers, not audio -- which is why a full-library
    pass is minutes. Returns [] for formats that don't embed (wav), files
    without art, or files mutagen can't parse (the caller's tally, never a
    crash)."""
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    try:
        m = mutagen.File(path)
    except Exception:
        return []
    if m is None:
        return []
    try:
        if isinstance(m, MP4):
            return [bytes(c) for c in (m.tags or {}).get("covr", [])]
        if isinstance(m, FLAC):
            return [p.data for p in m.pictures]
        tags = getattr(m, "tags", None)
        if isinstance(tags, ID3):
            return [f.data for f in tags.getall("APIC")]
    except Exception:
        return []
    return []


def folder_picture(track_paths):
    """The WMP-era fallback: the first FOLDER_NAMES hit beside the album's
    files. One directory is enough -- multi-disc rips that split dirs keep
    the art beside disc 1, and disc 1's tracks sort first."""
    if not track_paths:
        return None
    dirpath = os.path.dirname(track_paths[0])
    for name in FOLDER_NAMES:
        p = os.path.join(dirpath, name)
        try:
            with open(p, "rb") as fh:
                return fh.read()
        except OSError:
            continue
    return None


def store_image(root, data):
    """Content-address one original and generate its sizes. Returns
    (art_hash, w, h) or None for bytes Pillow can't decode. Idempotent by
    construction: an image already in the store (same hash) writes nothing
    -- two albums sharing one JPEG share one set of files."""
    from PIL import Image
    art_hash = hashlib.blake2b(data, digest_size=16).hexdigest()
    orig, thumb, large = (os.path.join(root, n) for n in art_names(art_hash))
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
        w, h = img.size
    except Exception as e:
        print("[music] undecodable image (%d bytes): %s" % (len(data), e))
        return None
    if not os.path.exists(orig):
        tmp = orig + ".part"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, orig)  # rename-then-visible, the cache's idiom
    for name, px in ((thumb, SIZES["thumb"]), (large, SIZES["large"])):
        if os.path.exists(name):
            continue
        variant = img.convert("RGB")
        variant.thumbnail((px, px))  # never upscales
        tmp = name + ".part"
        variant.save(tmp, "WEBP", quality=WEBP_QUALITY)
        os.replace(tmp, name)
    return art_hash, w, h


def main():
    ap = argparse.ArgumentParser(description="Extract album art into the "
                                             "art store and the catalog.")
    ap.add_argument("--db", default=None, help="catalog path")
    ap.add_argument("--art", default=None, help="art store dir")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N albums (smoke test)")
    args = ap.parse_args()

    root = args.art or art_root()
    if not root or not os.path.isdir(root):
        # Fail loudly at startup: no store means nothing to do, and a typo'd
        # path discovered 1,800 albums in would be a worse conversation.
        print("[music] MERLE_MUSIC_ART is unset or not a directory: %r"
              % root)
        return 1
    try:
        import mutagen  # noqa: F401 -- the pass is header reads; hard dep
        import PIL  # noqa: F401
    except ImportError as e:
        print("[music] missing dependency (venv needs mutagen + pillow): %s"
              % e)
        return 1

    conn = music_catalog.connect(args.db or music_catalog.db_path())
    work = music_catalog.albums_missing_art(conn)
    print("[music] art pass: %d albums on the worklist" % len(work))

    tally = {"embedded": 0, "folder": 0, "none": 0, "error": 0}
    started = time.time()
    for i, (album_key, paths) in enumerate(sorted(work.items()), 1):
        if args.limit and i > args.limit:
            break
        try:
            pictures = []
            for p in paths:
                pictures.extend(embedded_pictures(p))
            data = largest_picture(pictures)
            source = music_catalog.ART_EMBEDDED
            if data is None:
                data = folder_picture(paths)
                source = music_catalog.ART_FOLDER
            if data is None:
                tally["none"] += 1
                continue
            stored = store_image(root, data)
            if stored is None:
                tally["error"] += 1
                continue
            art_hash, w, h = stored
            music_catalog.set_album_art(conn, album_key, art_hash, source,
                                        w, h, int(time.time()))
            tally[source] += 1
        except OSError as e:
            print("[music] art failed, skipping: %s -- %s" % (album_key, e))
            tally["error"] += 1
        if i % LOG_EVERY == 0:
            conn.commit()
            print("[music] %d/%d -- %d embedded, %d folder, %d none, "
                  "%d errors" % (i, len(work), tally["embedded"],
                                 tally["folder"], tally["none"],
                                 tally["error"]))
    conn.commit()

    # The promotion pass: artists inherit their best album's cover. Runs on
    # its own worklist (artists with no row), so an owner override -- or a
    # previous run's pick -- is never recomputed unless the row is gone.
    promoted = 0
    for artist, row in sorted(promotion_pick(
            music_catalog.artists_missing_art(conn)).items()):
        music_catalog.set_artist_art(conn, artist, row["art_hash"],
                                     music_catalog.ART_DERIVED,
                                     row["w"], row["h"], int(time.time()))
        promoted += 1
    conn.commit()

    print("[music] art pass done in %.1f min -- %d embedded, %d folder, "
          "%d none, %d errors; %d artists promoted"
          % ((time.time() - started) / 60, tally["embedded"],
             tally["folder"], tally["none"], tally["error"], promoted))
    print("[music] catalog: %r" % (music_catalog.counts(conn),))
    return 0


if __name__ == "__main__":
    sys.exit(main())
