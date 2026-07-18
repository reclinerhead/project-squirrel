# =============================================================================
# project-squirrel -- music_genre.py
#
# The catalog normalization pass: genres (issue #163) and artist identity
# (issue #152), one job, one command, one chain position for future
# ingestion.
#
# THE ARTIST STAGE (#152) runs first: the library tags one band under
# several casings (`Panic! at the Disco` / `Panic! At the Disco` -- 26
# fold-collisions measured across 748 identities), and since an artist IS
# its name string, every casing split into its own browse card, page, and
# stranded art row. The stage folds each track's artist identity
# (COALESCE(album_artist, artist), trimmed + Unicode-lowercased), tallies
# casing frequency, and writes the winner to `tracks.artist_norm` -- the
# #163 materialization move applied to a second column, so every consumer
# reads a dumb column and NO folding exists downstream (three lowercase
# implementations that must never drift was the rejected alternative).
# Tie-break, deterministic: most tracks, then a casing seen in album_artist
# beats one only seen in track artist, then lexicographic. The rules file's
# `artist_display` section is the taste veto (`GWAR`, not `Gwar`) and the
# pin against a future import flipping a winner. The fold is str.lower(),
# NOT casefold(): simple lowercase is what a TS twin could implement
# identically if ever needed; casefold's extra mappings couldn't be.
#
# THE GENRE STAGE (#163): the library's 178 feral iTunes
# genre strings -> a small canonical vocabulary, per track, in
# `tracks.genre_norm`. It keys its artist arithmetic (overrides, majority
# inheritance) on the artist stage's output, so case-split artists tally
# as one. THE RULES ARE DATA -- genre_rules.yaml holds the
# vocabulary, the string map, artist overrides, the playlist-affinity
# families, and the tuning knobs. Edit the file, re-run this module: that IS
# the "reprocess under my new rules" job. A second deployment customizes by
# pointing at its own rules file, never by touching code.
#
# WORKLIST-DRIVEN, IDEMPOTENT, STANDALONE -- the reusability rule (#153, the
# owner requirement every enrichment pass honors): the pass recomputes the
# EXPECTED (genre_norm, source) for every track and writes only where the
# stored pair differs, so a re-run after ingesting five albums touches
# exactly the new rows, a rules edit remaps everything the edit reaches, and
# the same run twice writes nothing.
#
# RAW `tracks.genre` IS NEVER TOUCHED. It is provenance -- the indexer keeps
# writing it verbatim on every pass, files on the NAS are never tag-written
# (the ro mount enforces it), and every mapping decision stays reversible
# forever because the input is still sitting right there.
#
# PRECEDENCE, highest first (expected_norm):
#   owner        a hand-set row (genre_norm_source='owner') -- this pass's
#                UPDATE never matches one; the art store's rule, in SQL.
#   override     artist_overrides in the rules file.
#   map          the string map (via the genre_map table, which also carries
#                the future bulk-metadata pass's 'external' alias rows --
#                one table, one vocabulary, every entry point funnels
#                through it).
#   inherited    the artist-majority guess: a track whose raw tag maps
#                nowhere takes its artist's genre when >= inherit_threshold
#                of the artist's MAPPED tracks agree.
#   NULL         honest ignorance. The UI shows Uncategorized; the
#                bulk-metadata backfill owns filling it later.
#
# UNMAPPED STRINGS NEVER GUESS AND NEVER LEAK: the pass ends with a report of
# distinct raw values it couldn't place (minus the rules file's declared
# `triage` junk drawers, which are known and deferred). The fix is one rules
# line + a re-run. Surfaces read only genre_norm, so an unmapped tag has no
# UI existence until deliberately mapped -- the "no weird tags ever again"
# enforcement (issue #163).
#
# FILE -> TABLE SYNC IS ONE-DIRECTIONAL: the file's map upserts into
# genre_map with source='file' and stale file-sourced rows are pruned;
# 'external' rows are never touched by the sync. Human rules live in the
# file, runtime-discovered aliases live in the table, and they cannot fight.
# Nothing else in the system parses YAML -- consumers and the normalize step
# read the table.
#
# VALIDATION FAILS LOUDLY BEFORE ANY WRITE: every map/override/cluster
# member must be in `vocabulary`, a raw string maps once, unknown sections
# are rejected. A typo'd rules file must not half-normalize a catalog.
#
# Config (env):
#   MERLE_MUSIC_GENRE_RULES  the rules file (default: genre_rules.yaml next
#                            to this module -- the repo copy is the owner's
#                            live ruleset; other deployments point here).
#   MERLE_MUSIC_DB           the catalog -- see music_catalog.py.
#
# Usage (on pearl):
#   MERLE_MUSIC_DB=/home/todd/project-squirrel/music.db \
#       venv/bin/python -m jukebox.music_genre [--rules PATH] [--dry-run]
# =============================================================================

import argparse
import os
import sys
from collections import Counter

from jukebox import music_catalog

RULES_SECTIONS = {"vocabulary", "map", "artist_overrides", "clusters",
                  "tuning"}
OPTIONAL_SECTIONS = {"artist_display"}
TUNING_KEYS = {"inherit_threshold", "triage"}

DEFAULT_RULES_PATH = os.path.join(os.path.dirname(__file__),
                                  "genre_rules.yaml")


def rules_path():
    """MERLE_MUSIC_GENRE_RULES, or the repo file beside this module. A repo
    default is safe here where art_root() refused one: the file ships in git
    and IS the deployment's ruleset; an env override is for deployments whose
    rules live elsewhere."""
    return os.environ.get("MERLE_MUSIC_GENRE_RULES", "").strip() \
        or DEFAULT_RULES_PATH


# --- pure: rules parsing and validation (unit-tested) ---------------------------

class RulesError(ValueError):
    """A rules file that must not be applied. Every message names what and
    where -- the file is hand-edited, so the error IS the user interface."""


def fold_artist(name):
    """An artist name -> its case-insensitive identity key: trimmed, Unicode
    simple lowercase. str.lower(), NOT casefold(), on purpose (#152): simple
    lowercase is what a TS twin could implement identically if one is ever
    needed; casefold's extra mappings (ss for sharp s) could not be -- and
    none of the measured collisions want them. Internal whitespace is kept:
    broader name normalization is explicitly out of #152's scope."""
    return name.strip().lower()


def parse_rules(text):
    """YAML text -> validated rules dict:
        {vocabulary: [tag...], map: {raw: canonical}, artist_overrides:
         {artist: canonical}, clusters: [frozenset(tag...)...],
         inherit_threshold: float, triage: set(raw...)}
    The file's map is canonical -> [raws] (groups read well, and a duplicated
    raw is detectable in code where YAML's silent duplicate-key merge would
    eat a raw->canonical shape); this flattens it to the lookup direction.
    Raises RulesError on anything that must not be applied."""
    import yaml  # lazy, the estimate_bpm() move: pure callers need no dep
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RulesError("rules file is not valid YAML: %s" % e)
    if not isinstance(data, dict):
        raise RulesError("rules file must be a mapping of sections, got %s"
                         % type(data).__name__)

    unknown = set(data) - RULES_SECTIONS - OPTIONAL_SECTIONS
    if unknown:
        raise RulesError("unknown section(s): %s -- the loader rejects what "
                         "it would silently ignore" % ", ".join(sorted(unknown)))
    missing = RULES_SECTIONS - set(data)
    if missing:
        raise RulesError("missing section(s): %s" % ", ".join(sorted(missing)))

    vocab = data["vocabulary"]
    if not isinstance(vocab, list) or not vocab or \
            not all(isinstance(v, str) and v.strip() for v in vocab):
        raise RulesError("vocabulary must be a non-empty list of tag names")
    if len(set(vocab)) != len(vocab):
        dupes = sorted(v for v, n in Counter(vocab).items() if n > 1)
        raise RulesError("vocabulary lists %s more than once" % ", ".join(dupes))
    vocab_set = set(vocab)

    raw_map = {}
    if not isinstance(data["map"], dict):
        raise RulesError("map must be canonical tag -> list of raw strings")
    for canonical, raws in data["map"].items():
        if canonical not in vocab_set:
            raise RulesError("map target %r is not in vocabulary" % canonical)
        if not isinstance(raws, list):
            raise RulesError("map[%r] must be a list of raw strings" % canonical)
        for raw in raws:
            if not isinstance(raw, str) or not raw.strip():
                raise RulesError("map[%r] holds a non-string entry: %r"
                                 % (canonical, raw))
            if raw in raw_map:
                raise RulesError("raw string %r maps to both %r and %r -- a "
                                 "raw maps once" % (raw, raw_map[raw], canonical))
            raw_map[raw] = canonical

    overrides = data["artist_overrides"]
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, dict):
        raise RulesError("artist_overrides must be artist -> canonical tag")
    seen_folds = set()
    for artist, canonical in overrides.items():
        if canonical not in vocab_set:
            raise RulesError("artist_overrides[%r] -> %r is not in vocabulary"
                             % (artist, canonical))
        if fold_artist(artist) in seen_folds:
            raise RulesError("artist_overrides lists %r twice (matching is "
                             "case-insensitive since #152)" % artist)
        seen_folds.add(fold_artist(artist))

    clusters = data["clusters"]
    if not isinstance(clusters, list):
        raise RulesError("clusters must be a list of tag lists")
    families = []
    for i, members in enumerate(clusters):
        if not isinstance(members, list) or len(members) < 2:
            raise RulesError("clusters[%d] must list at least two tags" % i)
        bad = [m for m in members if m not in vocab_set]
        if bad:
            raise RulesError("clusters[%d] names %s -- not in vocabulary"
                             % (i, ", ".join(map(repr, bad))))
        families.append(frozenset(members))

    tuning = data["tuning"]
    if not isinstance(tuning, dict) or set(tuning) - TUNING_KEYS:
        raise RulesError("tuning allows exactly %s" % sorted(TUNING_KEYS))
    threshold = tuning.get("inherit_threshold")
    if not isinstance(threshold, (int, float)) or not 0.5 < threshold <= 1.0:
        raise RulesError("tuning.inherit_threshold must be in (0.5, 1.0] -- "
                         "below a majority, 'majority' is a lie")
    triage = tuning.get("triage") or []
    if not isinstance(triage, list) or \
            not all(isinstance(t, str) for t in triage):
        raise RulesError("tuning.triage must be a list of raw strings")
    overlap = sorted(set(triage) & set(raw_map))
    if overlap:
        raise RulesError("triage strings also appear in map: %s -- a string "
                         "is mapped or deferred, never both"
                         % ", ".join(map(repr, overlap)))

    display = data.get("artist_display") or {}
    if not isinstance(display, dict):
        raise RulesError("artist_display must be artist -> display casing")
    display_by_fold = {}
    for key, want in display.items():
        if not isinstance(key, str) or not isinstance(want, str) \
                or not key.strip() or not want.strip():
            raise RulesError("artist_display entries must be non-empty "
                             "strings, got %r: %r" % (key, want))
        if fold_artist(key) != fold_artist(want):
            raise RulesError("artist_display[%r] -> %r is a DIFFERENT artist "
                             "-- this section picks a casing, never renames"
                             % (key, want))
        if fold_artist(key) in display_by_fold:
            raise RulesError("artist_display lists %r twice (case-"
                             "insensitively)" % key)
        display_by_fold[fold_artist(key)] = want.strip()

    return {
        "vocabulary": list(vocab),
        "map": raw_map,
        "artist_overrides": dict(overrides),
        "clusters": tuple(families),
        "inherit_threshold": float(threshold),
        "triage": set(triage),
        "artist_display": display_by_fold,
    }


def load_rules(path):
    """parse_rules() over a file. Missing file raises RulesError too -- the
    caller decides whether that's fatal (the pass: yes) or a fallback (the
    daemon: engine defaults)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return parse_rules(fh.read())
    except OSError as e:
        raise RulesError("cannot read rules file %s: %s" % (path, e))


def engine_clusters(rules):
    """The rules' families in the playlist engine's shape: frozensets of
    HEAD TOKENS (music_playlist.genre_head's output), because the engine
    heads every tag before comparing -- 'R&B/Soul' arrives as 'r&b' and a
    family listing the display form would silently never match it."""
    from jukebox import music_playlist
    return tuple(frozenset(music_playlist.genre_head(m) for m in members)
                 for members in rules["clusters"])


def artist_identity(artist, album_artist):
    """One track's artist identity, pre-normalization: the album artist when
    there is one, else the track artist -- the COALESCE every album-shaped
    query speaks, so a compilation's per-track guests don't fragment
    anything. Trimmed for display; 'Unknown Artist' is the nameless tail's
    display fallback, same as the GUI's."""
    for name in (album_artist, artist):
        if name is not None and str(name).strip():
            return str(name).strip()
    return "Unknown Artist"


def canonical_artists(pairs, display_overrides=None):
    """[(artist, album_artist)] per track -> {fold: canonical display name}.
    Most-frequent casing wins; ties break deterministically (#152, and the
    catalog has a live one -- Run DMC 12 tracks vs Run Dmc 12): a casing
    seen in album_artist beats one only seen in track artist, then
    lexicographic by code point. display_overrides ({fold: display}, the
    rules file's taste veto) replaces the winner outright."""
    votes = {}
    in_album_artist = set()
    for artist, album_artist in pairs:
        name = artist_identity(artist, album_artist)
        fold = fold_artist(name)
        votes.setdefault(fold, Counter())[name] += 1
        if album_artist is not None and str(album_artist).strip():
            in_album_artist.add(name)
    out = {}
    for fold, casings in votes.items():
        out[fold] = min(casings,
                        key=lambda name: (-casings[name],
                                          name not in in_album_artist, name))
    if display_overrides:
        for fold, want in display_overrides.items():
            if fold in out:
                out[fold] = want
    return out


def artist_majorities(pairs, threshold):
    """[(artist_key, canonical)] for every track that MAPPED -> the artists
    whose mapped tracks agree on one genre at >= threshold, as {artist:
    canonical}. This is what an unmapped track inherits. Pure and injected
    (no SQL) so the threshold arithmetic is testable to the bone."""
    by_artist = {}
    for artist, canonical in pairs:
        if artist is None:
            continue
        by_artist.setdefault(artist, Counter())[canonical] += 1
    out = {}
    for artist, votes in by_artist.items():
        total = sum(votes.values())
        # max over sorted names: Counter breaks count ties by insertion
        # order, and the verdict must not depend on row order (the
        # resolve_seed lesson).
        best = max(sorted(votes), key=lambda g: votes[g])
        if votes[best] / total >= threshold:
            out[artist] = best
    return out


def expected_norm(raw, artist_key, rulemap, overrides, majorities):
    """One track's expected (genre_norm, genre_norm_source), by precedence:
    artist override > string map > artist majority > (None, None). The
    'owner' level isn't here -- it's the UPDATE's WHERE clause, structural
    like the art store's, not a branch someone could forget.

    artist_key is the FOLD of the canonical identity since #152, and
    overrides/majorities are fold-keyed -- case-split artists answer as
    one."""
    if artist_key in overrides:
        return overrides[artist_key], music_catalog.GENRE_MAPPED
    if raw is not None and raw in rulemap:
        return rulemap[raw], music_catalog.GENRE_MAPPED
    if artist_key in majorities:
        return majorities[artist_key], music_catalog.GENRE_INHERITED
    return None, None


# --- I/O: the thin half ---------------------------------------------------------

def sync_rules(conn, rules):
    """The file's string map -> genre_map rows (source='file'), stale
    file-sourced rows pruned. One direction, and only the file's lane:
    'external' rows (the bulk-metadata pass's future alias writes) survive
    untouched. Returns (upserted, pruned)."""
    rows = list(rules["map"].items())
    if rows:
        conn.executemany(
            "INSERT INTO genre_map (raw, canonical, source) "
            "VALUES (?, ?, 'file') "
            "ON CONFLICT(raw) DO UPDATE SET canonical=excluded.canonical, "
            "source='file'",
            rows)
        marks = ", ".join("?" * len(rows))
        pruned = conn.execute(
            f"DELETE FROM genre_map WHERE source = 'file' "
            f"AND raw NOT IN ({marks})",
            tuple(raw for raw, _ in rows)).rowcount
    else:  # a rules file with no string map prunes the file lane entirely
        pruned = conn.execute(
            "DELETE FROM genre_map WHERE source = 'file'").rowcount
    conn.commit()
    return len(rows), pruned


def table_rulemap(conn):
    """genre_map as {raw: canonical} -- ALL sources. The table is what the
    normalize step reads (the file already synced into it), so an external
    alias row added between runs participates without this module knowing
    the bulk-metadata pass exists."""
    return {r["raw"]: r["canonical"]
            for r in conn.execute("SELECT raw, canonical FROM genre_map")}


def normalize(conn, rules, dry_run=False):
    """The pass: sync the rules, recompute every track's expected pair,
    write the diffs (never over 'owner'), report. Returns the report dict
    the CLI prints -- callers (tests, a future ingestion pipeline) get
    numbers, not stdout.

    dry_run writes NOTHING -- not even the rules sync -- so it previews a
    rules edit against a live catalog with zero side effects; the rulemap is
    composed in memory the way the sync would land it (file wins over an
    external alias for the same raw, matching the upsert)."""
    if dry_run:
        upserted = pruned = 0
        external = {r["raw"]: r["canonical"] for r in conn.execute(
            "SELECT raw, canonical FROM genre_map WHERE source = 'external'")}
        rulemap = {**external, **rules["map"]}
    else:
        upserted, pruned = sync_rules(conn, rules)
        rulemap = table_rulemap(conn)
    overrides = rules["artist_overrides"]

    tracks = conn.execute(
        "SELECT id, genre, artist, album_artist, artist_norm, "
        "genre_norm, genre_norm_source FROM tracks").fetchall()

    # --- artist stage (#152): the canonical identity every later step keys on
    canon = canonical_artists(
        [(t["artist"], t["album_artist"]) for t in tracks],
        rules["artist_display"])
    folds = {}
    artist_updates = []
    for t in tracks:
        name = artist_identity(t["artist"], t["album_artist"])
        fold = fold_artist(name)
        folds.setdefault(fold, set()).add(name)
        want_artist = canon[fold]
        if t["artist_norm"] != want_artist:
            artist_updates.append((want_artist, t["id"]))
    collapsed = sum(1 for names in folds.values() if len(names) > 1)
    if artist_updates and not dry_run:
        conn.executemany(
            "UPDATE tracks SET artist_norm = ? WHERE id = ?", artist_updates)
        conn.commit()

    # --- genre stage (#163), fold-keyed so case-split artists tally as one
    overrides_f = {fold_artist(name): tag for name, tag in overrides.items()}
    track_folds = [fold_artist(artist_identity(t["artist"],
                                               t["album_artist"]))
                   for t in tracks]
    mapped_pairs = []
    for t, fold in zip(tracks, track_folds):
        if fold in overrides_f:
            mapped_pairs.append((fold, overrides_f[fold]))
        elif t["genre"] is not None and t["genre"] in rulemap:
            mapped_pairs.append((fold, rulemap[t["genre"]]))
    majorities = artist_majorities(mapped_pairs, rules["inherit_threshold"])

    updates = []
    owner_kept = 0
    unmapped = Counter()
    tally = Counter()
    for t, fold in zip(tracks, track_folds):
        want, source = expected_norm(t["genre"], fold, rulemap,
                                     overrides_f, majorities)
        # A raw string with no rule is reported even when inheritance
        # covered this track -- the STRING is what wants a rules line, and
        # the next track wearing it may have no majority to lean on.
        if t["genre"] is not None and t["genre"] not in rulemap \
                and t["genre"] not in rules["triage"]:
            unmapped[t["genre"]] += 1
        current = t["genre_norm_source"]
        if current == music_catalog.GENRE_OWNER:
            # The owner's hand-set value survives every automated re-run.
            tally[current] += 1
            if (t["genre_norm"], current) != (want, source):
                owner_kept += 1
            continue
        if current == music_catalog.GENRE_EXTERNAL \
                and source != music_catalog.GENRE_MAPPED:
            # A per-track backfill value (the future bulk-metadata pass)
            # outranks an inheritance guess and honest ignorance -- only an
            # explicit rule (map or override) replaces it.
            tally[current] += 1
            continue
        tally[source or "null"] += 1
        if (t["genre_norm"], t["genre_norm_source"]) == (want, source):
            continue
        updates.append((want, source, t["id"]))

    if updates and not dry_run:
        # The owner guard rides the UPDATE itself even though the loop
        # skipped those rows -- an owner row set between the read and the
        # write must survive, and structural rules don't trust timing.
        conn.executemany(
            "UPDATE tracks SET genre_norm = ?, genre_norm_source = ? "
            "WHERE id = ? AND (genre_norm_source IS NULL "
            "OR genre_norm_source != '%s')" % music_catalog.GENRE_OWNER,
            updates)
        conn.commit()

    return {
        "total": len(tracks),
        "written": 0 if dry_run else len(updates),
        "would_write": len(updates) if dry_run else 0,
        "owner_kept": owner_kept,
        "tally": dict(tally),
        "unmapped": dict(unmapped),
        "rules_upserted": upserted,
        "rules_pruned": pruned,
        "artist_written": 0 if dry_run else len(artist_updates),
        "artist_would_write": len(artist_updates) if dry_run else 0,
        "artists_collapsed": collapsed,
        "artist_identities": len(folds),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="python3 -m jukebox.music_genre",
        description="Normalize tracks.artist_norm and tracks.genre_norm "
                    "from the rules file (issues #152 / #163).")
    ap.add_argument("--rules", default=None,
                    help="rules file (default: MERLE_MUSIC_GENRE_RULES or "
                         "the repo's genre_rules.yaml)")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change, write nothing")
    args = ap.parse_args(argv)

    path = args.rules or rules_path()
    rules = load_rules(path)  # RulesError is fatal here, before any write
    db = music_catalog.db_path()
    if not os.path.isfile(db):
        raise SystemExit("[genre] MERLE_MUSIC_DB does not exist: %s -- the "
                         "indexer owns creating the catalog." % db)

    print("[genre] rules: %s (%d tags, %d mapped strings, %d overrides)"
          % (path, len(rules["vocabulary"]), len(rules["map"]),
             len(rules["artist_overrides"])))
    conn = music_catalog.connect(db)
    report = normalize(conn, rules, dry_run=args.dry_run)

    print("[genre] rules synced: %d upserted, %d stale pruned"
          % (report["rules_upserted"], report["rules_pruned"]))
    verb = "would write" if args.dry_run else "written"
    print("[genre] artists: %d identities, %d case-collapsed -- "
          "artist_norm %s on %d tracks"
          % (report["artist_identities"], report["artists_collapsed"],
             verb, report["artist_would_write"] or report["artist_written"]))
    print("[genre] %s: %d of %d tracks (owner rows kept: %d)"
          % (verb, report["would_write"] or report["written"],
             report["total"], report["owner_kept"]))
    t = report["tally"]
    print("[genre] coverage: %d mapped, %d inherited, %d external, "
          "%d owner, %d NULL"
          % (t.get("mapped", 0), t.get("inherited", 0),
             t.get("external", 0), t.get("owner", 0), t.get("null", 0)))
    if report["unmapped"]:
        print("[genre] UNMAPPED -- these raw tags have no rule and no "
              "majority; add a map line (or triage them) and re-run:")
        for raw, n in sorted(report["unmapped"].items(),
                             key=lambda kv: -kv[1]):
            print("[genre]   %5d  %r" % (n, raw))
    else:
        print("[genre] no unmapped raw tags -- the vocabulary is closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
