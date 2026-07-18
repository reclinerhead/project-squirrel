"""Album descriptions out of the `comment` tag (issue #171).

The measurement on #115 (2026-07-16) found the comment tag holds iTunes/Amazon
store editorial copy about a specific record -- not the Last.fm artist bios the
epic assumed. ~20.6% of albums carry real prose; the rest is scrap (`USA`,
`amrc`), one-liners, EAC rip logs, or nothing. The catalog never ingested any
of it, because music_index.read_tags() uses mutagen's easy=True key set, which
excludes comments.

THE CATCH THIS PASS EXISTS TO HANDLE: these blurbs arrive truncated mid-word --
49% of them at exactly 255 chars, and (measured on pearl, not in #115) plenty
more at other lengths. The lost text is not in the files. Owner decision
(2026-07-18): trim back to the last complete sentence and show what survives;
drop it entirely if nothing complete remains. A page that ends "an outstanding
level of musicianship and a so" is worse than no blurb. The raw text is stored
alongside anyway, so changing that policy later is a re-run over album_notes
rather than another walk of 26k files.

SHAPE (see [metadata refresh] in the guide): the unit of work is
propose_for_album(paths) -- ONE album in, ONE proposal dict out, no database.
It ALWAYS returns a dict, never None; read `status` (OK / NONE / DROPPED) to
find out whether there is a description on it. main() is a thin worklist loop
over it. That order is deliberate and required: the planned "refresh this
album" button in the GUI calls the same function on one album and shows its
proposal for approval, so the button and the bulk pass can never drift into
producing different answers.
"""

import argparse
import html
import re
import sys
import time

from jukebox import music_catalog

LOG_EVERY = 200

# The iTunes tag-length wall the measurement found: exactly 255 characters,
# and only 3% of those end on punctuation. Kept as documentation of the
# dominant case -- but NOT as the truncation test. Sampling 500 real albums
# on pearl (2026-07-18) found blurbs cut mid-word at 105, 112 and 114 chars
# too ("...get away with an awful "), so a length-based detector silently
# passed them through raw. Truncation is detected by how the text ENDS, at
# any length; see looks_truncated.
TRUNCATION_WALL = 255

# iTunes' own normalization/gapless bookkeeping, which rides the comment atom
# on m4a rather than a named ID3 frame: whitespace-separated hex words and
# nothing else. Long enough to clear the prose floor, so it must be named.
ITUNES_HEX_BLOB = re.compile(r"^(?:[0-9A-Fa-f]{6,}\s*)+$")

# Prose floor. Below this a comment is a one-liner, not a description worth a
# panel -- and a trimmed survivor shorter than this is a stub, so the trim
# drops it rather than rendering a sentence and a half of orphaned context.
BLURB_MIN_CHARS = 80

# Rip-tool leavings: EAC/XLD logs and encoder settings, the 1.6% bucket. Word
# boundaries matter -- a bare "eac" substring would eat "peace", and "log"
# would eat "prologue".
RIP_MARKERS = re.compile(
    r"\b(exact audio copy|eac|xld|dbpoweramp|cdparanoia|accuraterip|lame|"
    r"flac|cuesheet|ripped by|encoded by|encoder settings|track quality)\b",
    re.IGNORECASE)

TERMINAL_PUNCT = ".!?"

# Sentence end: terminal punctuation, optionally followed by a closing quote or
# bracket, then whitespace or the end of the string. Deliberately naive about
# abbreviations ("Mr. Bungle" can cut early) -- a blurb that stops one sentence
# short reads fine, while the alternative (an abbreviation list) is a
# maintenance tax for a cosmetic gain.
SENTENCE_END = re.compile(r'[.!?]["\')\]]?(?=\s|$)')

# Classification buckets, the measurement's own names.
BLURB = "blurb"
ONELINER = "oneliner"
SCRAP = "scrap"
RIPJUNK = "ripjunk"
EMPTY = "empty"

# Which buckets are worth a page. A tuple, ordered best-first, because
# pick_comment ranks on bucket BEFORE length -- a 300-char rip log must never
# outrank the two-sentence description on the next track -- and because
# `external` prose will join it when a fetched source lands.
#
# ONE-LINERS ARE DELIBERATELY EXCLUDED. The measurement named them a bucket
# (3.5% of albums) but not what was in them; sampling 400 real albums on pearl
# found the survivors are store bookkeeping, not prose -- "Amazon.com Song ID:
# 201982125", ">> a klangwerk release". Both clear the scrap test (4+ words)
# and would have rendered as an album description. Requiring real prose costs
# a few genuine short notes and drops the coverage to the measured 20.6% blurb
# figure exactly; the alternative was an open-ended regex war against every
# store's bookkeeping format.
USABLE = (BLURB,)

# What propose_for_album concluded. OK carries a description; NONE means no
# usable comment in the files; DROPPED means there was prose but the
# truncation policy salvaged nothing whole from it.
OK = "ok"
NONE = "none"
DROPPED = "dropped"

# status -> the pass's counter name. Explicit so the constants' string values
# stay free to change without breaking main()'s tally.
TALLY_KEY = {NONE: "none", DROPPED: "dropped"}


# --- pure: classification and trimming (unit-tested in test_music_blurb.py) ---

def clean_comment(value):
    """One raw tag value -> the text everything downstream sees, or None.

    HTML-unescaped, because the store copy carries entities verbatim
    ("Demos,&amp; Two Live Tracks", sampled from the library) and React
    escapes on render -- leaving them in prints a literal "&amp;" on the
    album page. Done at read time rather than at render time so `raw` in the
    catalog is already the text a human would read: the entities are a
    transport artifact of the tag, not content worth preserving."""
    if value is None:
        return None
    return music_catalog.norm_tag(html.unescape(str(value)))


def classify_comment(text):
    """Which of the measurement's buckets one comment falls in.

    Order is load-bearing: rip junk is tested FIRST because an EAC log is long
    enough to pass every prose test below it. Scrap is short and wordless
    (`USA`, `amrc` -- 12.8% of albums, catalog-wide noise from the store's own
    metadata); one-liners are real but too thin to be a description."""
    if text is None:
        return EMPTY
    text = text.strip()
    if not text:
        return EMPTY
    if RIP_MARKERS.search(text) or ITUNES_HEX_BLOB.match(text):
        return RIPJUNK
    words = text.split()
    if len(words) <= 3 and text[-1] not in TERMINAL_PUNCT:
        return SCRAP
    if len(text) < BLURB_MIN_CHARS:
        return ONELINER
    return BLURB


def looks_truncated(text):
    """Whether this comment was cut off mid-thought.

    The test is how it ENDS, not how long it is. The first cut of this used
    `len == 255 and no terminal punctuation`, straight from the measurement --
    and sampling real albums found blurbs cut mid-word at 105, 112 and 114
    chars that sailed through untouched. Store copy that ends without terminal
    punctuation was cut, whatever its length; the 255 wall is just where it
    happens most.

    A closing quote or bracket after the punctuation still counts as an
    ending -- `("the only honest thing we made.")` is finished prose."""
    if not text:
        return False
    text = text.strip().rstrip("\"')]")
    return not text.endswith(tuple(TERMINAL_PUNCT))


def trim_to_sentence(text):
    """The owner's truncation policy: cut a mid-word blurb back to its last
    complete sentence. Returns the survivor, or None when nothing complete
    survives -- a blurb whose first sentence runs past the cut has no
    salvageable prose, and the issue is explicit that no blurb beats a
    fragment. Also drops survivors under BLURB_MIN_CHARS: one short opening
    clause stripped of everything it was setting up reads as a mistake."""
    if not text:
        return None
    text = text.strip()
    ends = [m.end() for m in SENTENCE_END.finditer(text)]
    if not ends:
        return None
    survivor = text[:ends[-1]].strip()
    if len(survivor) < BLURB_MIN_CHARS:
        return None
    return survivor


def pick_comment(comments):
    """The album's description out of its tracks' comments. The store writes
    the same album copy onto every track, so this is normally a formality --
    but rips are uneven, and a track can carry a rip log where its neighbours
    carry prose. Best bucket wins first, then longest text (the least-truncated
    copy of the same blurb), then earliest for a stable tie-break across runs,
    the largest_picture rule. Returns None when nothing is usable."""
    best = None
    for text in comments:
        if not text:
            continue
        text = text.strip()
        bucket = classify_comment(text)
        if bucket not in USABLE:
            continue
        rank = (USABLE.index(bucket), -len(text))
        if best is None or rank < best[0]:
            best = (rank, text)
    return best[1] if best else None


def describe(raw):
    """One album's raw comment -> the description a page should render, as
    (description, truncated) -- or None when this comment earns no panel.

    The whole policy in one pure function: prose that ends cleanly passes
    through untouched, a mid-word cut is trimmed back to its last full
    sentence, and a cut with no complete sentence in it is dropped."""
    # Strip BEFORE the emptiness test, not after: a whitespace-only tag is
    # falsy only once trimmed, and the naive order returns ("", False) --
    # an empty description row, which is exactly the "two kinds of missing"
    # trap norm_tag exists to prevent.
    raw = (raw or "").strip()
    if not raw:
        return None
    truncated = looks_truncated(raw)
    if not truncated:
        return (raw, False)
    survivor = trim_to_sentence(raw)
    if survivor is None:
        return None
    return (survivor, True)


# --- I/O: headers in, prose out ------------------------------------------------

def read_comments(path):
    """EVERY comment value in one file, as a list of strings. easy=True is what
    hid this field in the first place, so this opens the raw tag object and
    branches per container, exactly as music_art.embedded_pictures does. Any
    parse failure is [] -- the caller's tally, never a crash.

    All of them, not the first: a file can carry a rip log AND the store's
    blurb, and returning whichever came first would let the junk hide the
    prose. pick_comment already ranks candidates by bucket, so handing it
    everything costs nothing and makes the ordering irrelevant."""
    import mutagen
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4
    try:
        m = mutagen.File(path)
    except Exception:
        return []
    values = []
    if m is None:
        return []
    try:
        if isinstance(m, MP4):
            values = list((m.tags or {}).get("\xa9cmt", []))
        elif isinstance(m, FLAC):
            values = list(m.get("comment") or []) + \
                list(m.get("description") or [])
        else:
            tags = getattr(m, "tags", None)
            if isinstance(tags, ID3):
                for frame in tags.getall("COMM"):
                    # iTunes writes the store copy to the description-less
                    # COMM frame; its OWN bookkeeping rides "iTunNORM" /
                    # "iTunSMPB" frames -- gain and gapless data, never prose.
                    if str(getattr(frame, "desc", "")).startswith("iTun"):
                        continue
                    values.extend(frame.text)
    except Exception:
        return []
    return [t for t in (clean_comment(v) for v in values) if t]


def propose_for_album(paths):
    """THE UNIT OF WORK: one album's files in, a proposal out.

    Returns {"status", "description", "raw", "truncated"} -- what SHOULD be
    written, without touching the database. main() writes it; the future
    refresh button shows it to Todd and writes only on approval. Keeping the
    write out of here is what lets both callers share one implementation.

    `status` distinguishes the two ways of getting nothing, because they are
    different answers to a person asking why an album has no description:
    NONE means the files carry no usable comment at all, DROPPED means there
    WAS prose and the truncation policy could not salvage a whole sentence
    from it. The pass tallies them separately and the button will want to say
    which one happened."""
    candidates = []
    for p in paths:
        candidates.extend(read_comments(p))
    raw = pick_comment(candidates)
    if raw is None:
        return {"status": NONE, "description": None, "raw": None,
                "truncated": False}
    described = describe(raw)
    if described is None:
        return {"status": DROPPED, "description": None, "raw": raw,
                "truncated": True}
    description, truncated = described
    return {"status": OK, "description": description, "raw": raw,
            "truncated": truncated}


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python3 -m jukebox.music_blurb",
        description="Ingest the comment-tag album blurbs as album "
                    "descriptions.")
    ap.add_argument("--db", default=None, help="catalog path")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N albums (smoke test)")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify and report, write nothing")
    ap.add_argument("--album", default=None,
                    help="one album key -- re-propose for a single album and "
                         "print the result (what the GUI refresh button will "
                         "call in-process)")
    args = ap.parse_args(argv)

    try:
        import mutagen  # noqa: F401 -- the pass is header reads; hard dep
    except ImportError as e:
        print("[music] missing dependency (venv needs mutagen): %s" % e)
        return 1

    conn = music_catalog.connect(args.db or music_catalog.db_path())

    if args.album:
        proposal = propose_for_album(music_catalog.album_paths(
            conn, args.album))
        print("[music] %s -> %r" % (args.album, proposal))
        return 0

    work = music_catalog.albums_missing_note(conn)
    print("[music] blurb pass: %d albums on the worklist" % len(work))

    tally = {"written": 0, "dropped": 0, "none": 0, "error": 0}
    started = time.time()
    for i, (album_key, paths) in enumerate(sorted(work.items()), 1):
        if args.limit and i > args.limit:
            break
        try:
            proposal = propose_for_album(paths)
            if proposal["status"] != OK:
                # Explicit map rather than tally[status]: keying the counters
                # on the constants' string VALUES would turn a rename of
                # DROPPED into a mid-run KeyError that no test would catch.
                tally[TALLY_KEY[proposal["status"]]] += 1
                continue
            if not args.dry_run:
                music_catalog.set_album_note(
                    conn, album_key, proposal["description"], proposal["raw"],
                    music_catalog.NOTE_COMMENT, proposal["truncated"],
                    int(time.time()))
            tally["written"] += 1
        except OSError as e:
            print("[music] blurb failed, skipping: %s -- %s" % (album_key, e))
            tally["error"] += 1
        if i % LOG_EVERY == 0:
            if not args.dry_run:
                conn.commit()
            print("[music] %d/%d -- %d written, %d dropped, %d none, "
                  "%d errors" % (i, len(work), tally["written"],
                                 tally["dropped"], tally["none"],
                                 tally["error"]))
    if not args.dry_run:
        conn.commit()

    print("[music] blurb pass %sdone in %.1f min -- %d written, %d dropped "
          "(truncated past saving), %d no usable comment, %d errors"
          % ("(dry run) " if args.dry_run else "",
             (time.time() - started) / 60, tally["written"], tally["dropped"],
             tally["none"], tally["error"]))
    print("[music] catalog: %r" % (music_catalog.counts(conn),))
    return 0


if __name__ == "__main__":
    sys.exit(main())
