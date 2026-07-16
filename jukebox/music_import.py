# =============================================================================
# project-squirrel -- music_import.py
#
# The pearl side of the analysis backfill (issue #136): JSONL from bluejay ->
# `tracks`. This exists because `music.db` is pearl's to write and SQLite over
# SMB is a corruption risk -- so the analyzer emits records and this ingests
# them, and the cross-machine write problem dissolves instead of being solved.
# No HTTP surface, no daemon dependency, no shared filesystem: copy a file,
# run this, done.
#
# IDEMPOTENT, BUT NOTE THE VERB. The epic says `INSERT OR IGNORE`; the schema
# put bpm/replaygain_db/dynamic_range_db on `tracks` as nullable columns, so
# the import is an UPDATE, not an insert. Same property, different verb --
# re-importing the same JSONL writes the same values and moves no row count.
#
# The update is UNCONDITIONAL on purpose: this data is derived and rebuildable,
# and a future better tempo algorithm must be able to land without a migration
# or a manual DELETE. The *analyzer* is what skips already-analyzed tracks
# (`--force` re-does them). Skipping here instead would make a re-analysis
# silently no-op -- the worst of both.
#
# An unknown id is counted and skipped, never inserted: `tracks` rows are the
# indexer's to create, and a row invented here would be a track with no
# location, no tags, and no way to play. That's the same rule as
# forget_paths() -- a track we can't find is not a track that never existed.
#
# Usage (on pearl):
#   MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db \
#     python3 -m jukebox.music_import music_analysis.jsonl
# =============================================================================

import json
import os
import sys

from jukebox import music_catalog


# --- pure: record shaping ------------------------------------------------------

def parse_records(lines):
    """JSONL -> (records, skipped_lines).

    A truncated final line is the normal shape of an interrupted pass, so a bad
    line is counted and dropped rather than raising -- the whole point of this
    file format is that it survives a kill -9."""
    out, bad = [], 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(rec, dict) or not rec.get("id"):
            bad += 1
            continue
        out.append(rec)
    return out, bad


def latest_by_id(records):
    """Last record wins per id. A --force re-analysis appends rather than
    rewriting, so the same id legitimately appears twice and the newer
    measurement is the one that should land."""
    out = {}
    for r in records:
        out[r["id"]] = r
    return out


def split_results(records):
    """(measurements, failures). A record carrying `error` is a track that
    could not be analyzed -- it belongs in needs_attention, not in bpm."""
    ok, bad = [], []
    for r in records:
        (bad if r.get("error") else ok).append(r)
    return ok, bad


# --- I/O: the thin half --------------------------------------------------------

def apply_measurements(conn, records):
    """UPDATE the analysis columns. Returns (updated, unknown)."""
    updated = unknown = 0
    for r in records:
        cur = conn.execute(
            "UPDATE tracks SET bpm = ?, replaygain_db = ?, "
            "dynamic_range_db = ? WHERE id = ?",
            (r.get("bpm"), r.get("replaygain_db"), r.get("dynamic_range_db"),
             r["id"]))
        if cur.rowcount:
            updated += 1
        else:
            unknown += 1
    conn.commit()
    return updated, unknown


def apply_failures(conn, records):
    """Park what wouldn't analyze in needs_attention -- a queryable number
    rather than a silent drop, and the GUI already has a place to surface it."""
    marked = unknown = 0
    for r in records:
        cur = conn.execute(
            "UPDATE tracks SET needs_attention = ? WHERE id = ?",
            ("analysis: " + str(r.get("error", ""))[:200], r["id"]))
        if cur.rowcount:
            marked += 1
        else:
            unknown += 1
    conn.commit()
    return marked, unknown


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        raise SystemExit("[import] usage: python3 -m jukebox.music_import "
                         "<analysis.jsonl>")
    path = argv[0]
    if not os.path.isfile(path):
        raise SystemExit("[import] no such file: %s" % path)
    db = music_catalog.db_path()
    if not os.path.isfile(db):
        raise SystemExit("[import] MERLE_MUSIC_DB does not exist: %s -- the "
                         "indexer owns creating the catalog." % db)

    with open(path, "r", encoding="utf-8") as fh:
        records, bad_lines = parse_records(fh)
    deduped = list(latest_by_id(records).values())
    ok, failed = split_results(deduped)

    print("[import] %s -> %s" % (path, db))
    print("[import] %d lines parsed (%d unparseable), %d unique ids"
          % (len(records), bad_lines, len(deduped)))

    conn = music_catalog.connect(db)
    before = conn.execute("SELECT COUNT(*) AS n FROM tracks "
                          "WHERE bpm IS NOT NULL").fetchone()["n"]
    updated, unknown_ok = apply_measurements(conn, ok)
    marked, unknown_bad = apply_failures(conn, failed)
    after = conn.execute("SELECT COUNT(*) AS n FROM tracks "
                         "WHERE bpm IS NOT NULL").fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM tracks").fetchone()["n"]

    print("[import] measurements applied : %d" % updated)
    print("[import] failures parked      : %d" % marked)
    if unknown_ok or unknown_bad:
        print("[import] ids not in catalog   : %d (skipped -- the indexer owns "
              "creating rows)" % (unknown_ok + unknown_bad))
    print("[import] bpm coverage: %d -> %d of %d tracks (%.1f%%)"
          % (before, after, total, 100.0 * after / max(1, total)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
