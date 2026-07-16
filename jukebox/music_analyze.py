# =============================================================================
# project-squirrel -- music_analyze.py
#
# The audio-analysis backfill (issue #136, epic #115 Phase 1a): BPM,
# ReplayGain, and dynamic range for every track, computed from the audio
# because the tags don't have them (all three are under 9% in this library).
# Phase 3's playlist engine can only weigh what exists; "play me stuff like
# Capital Cities" against genre and year alone is a decade filter, not a mood
# match. This pass is where that ceiling gets raised.
#
# RUNS ON BLUEJAY, WRITES NOTHING. Beat tracking is the CPU-bound half and
# bluejay's CPU is several times pearl's -- but `music.db` is pearl's, and
# SQLite over SMB is a corruption risk, not a perf note. So this emits JSONL
# keyed by content hash and stops; `music_import.py` ingests it on pearl. No
# HTTP surface, no daemon dependency, no cross-machine write. Once the backfill
# lands, bluejay can be powered off forever and every later phase still works.
#
# ONE READ PER FILE. Phase 0 measured the library wire-limited at ~61 MB/s over
# 612.7 GB -- ~2.8 h of pure reading before any analysis costs anything. So one
# ffmpeg invocation does both jobs: the `ebur128` filter prints loudness to
# stderr AND passes the audio through, so the same decode feeds beat tracking
# on stdout. Reading twice (once for loudness, once for beats) would double the
# floor for nothing. (Rejected: librosa.load() for the decode -- it hands
# non-WAV/FLAC off to audioread, which shells out to ffmpeg anyway, so it's the
# same read with less control and no loudness.)
#
# NEVER WRITE TO THE SHARE -- principle 1, permanent. On pearl the mount
# enforces it (`cifs (ro,...)`); on bluejay the share is plain UNC with NO such
# guarantee, so it is code discipline here: every open is "rb", and ffmpeg is
# given "-" (stdout) as its output, never a path. If a read-only NAS account
# for bluejay ever exists, that turns discipline back into a wall.
#
# Config (bluejay):
#   MERLE_MUSIC_DB      the catalog to read the work list from -- a SNAPSHOT of
#                       pearl's, read-only. Never the live file over the wire.
#   MERLE_MUSIC_ROOT    where the share is on THIS box (\\hummingbird\music).
#                       The catalog holds pearl's paths (/mnt/music/...), so
#                       every path is remapped -- see remap_path().
#   MERLE_MUSIC_JSONL   where results accumulate. This file IS the resumption
#                       state, not the catalog: the snapshot won't know a track
#                       is analyzed until the import runs on pearl, which may
#                       be days later.
# =============================================================================

import concurrent.futures
import json
import os
import re
import subprocess
import sys
import threading
import time

from jukebox import music_catalog

DEFAULT_JSONL = "music_analysis.jsonl"
CATALOG_ROOT = "/mnt/music"          # what pearl's indexer recorded
BEAT_SR = 22050                      # librosa's default; plenty for tempo

# ReplayGain 2.0's reference level. RG's whole job is "how far is this track
# from the target", so the gain is target minus measured -- see replaygain_db.
RG_REFERENCE_LUFS = -18.0


# --- pure: parsing, arithmetic, work lists -------------------------------------

# ffmpeg's ebur128 summary, as printed to stderr at end of stream:
#     [Parsed_ebur128_0 @ ...] Summary:
#       Integrated loudness:
#         I:         -14.4 LUFS
#       Loudness range:
#         LRA:         7.6 LU
_I_RX = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.M)
_LRA_RX = re.compile(r"^\s*LRA:\s*(-?\d+(?:\.\d+)?)\s*LU", re.M)
_PEAK_RX = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", re.M)


def parse_ebur128(text):
    """Pull integrated loudness (LUFS) and loudness range (LU) out of ffmpeg's
    stderr. Returns Nones rather than raising when the summary is missing or
    garbled: a file that decoded badly should land in needs_attention, not take
    the pass down.

    Anchored to the LAST match on purpose. ebur128 prints a running readout for
    the whole file before the final Summary block, so an unanchored search
    would happily return the loudness of the first 100ms."""
    if not text:
        return None, None, None

    def last(rx):
        m = rx.findall(text)
        if not m:
            return None
        try:
            return float(m[-1])
        except (TypeError, ValueError):
            return None

    i = last(_I_RX)
    # ebur128 FLOORS at exactly -70.0 LUFS for silence -- it does not go below,
    # so the comparison must include the floor (`<` misses it, and -70 would
    # sail through as a +52 dB gain). Silence is the absence of a loudness, not
    # a very quiet one.
    if i is not None and i <= -70.0:
        i = None
    return i, last(_LRA_RX), last(_PEAK_RX)


def ffmpeg_error(stderr_text, returncode=None):
    """ffmpeg's stderr -> one line worth putting in needs_attention.

    The last non-empty line is where ffmpeg says what actually went wrong
    ("Invalid data found when processing input"). Slicing the last N CHARS
    instead -- the obvious shortcut -- cuts mid-word and varies with the
    filename's length, so the same failure reads differently on every track
    ("essing input", " when processing input"). This text lands in the catalog
    and the GUI shows it, so it should be a sentence.

    The exit code is normalized to signed: ffmpeg returns negative AVERROR
    codes, and Windows reports them as unsigned DWORDs, so the raw number
    surfaces as an alarming 3199971767 instead of -1094995529."""
    if returncode is not None and returncode > 2 ** 31:
        returncode -= 2 ** 32
    line = ""
    for cand in reversed((stderr_text or "").splitlines()):
        cand = cand.strip()
        # Skip ffmpeg's progress/config chatter -- it's the last real diagnostic
        # that matters, not the last thing printed.
        if cand and not cand.startswith(("frame=", "size=", "  ", "配")):
            line = cand
            break
    if not line:
        line = "no diagnostic on stderr"
    if returncode is not None:
        return "ffmpeg exit %d: %s" % (returncode, line[:140])
    return line[:140]


def replaygain_db(integrated_lufs):
    """The gain that would bring this track to the RG2 reference (-18 LUFS).
    A quiet track gets a positive gain, a loud one negative -- the sign is the
    whole point, and a sign error here is inaudible in review and wrong in
    every playlist."""
    if integrated_lufs is None:
        return None
    return round(RG_REFERENCE_LUFS - integrated_lufs, 2)


def remap_path(path, catalog_root=CATALOG_ROOT, local_root=None):
    """pearl's /mnt/music/... -> this box's \\hummingbird\\music\\...

    The catalog stores locations as pearl sees them (it is pearl's store). The
    analyzer runs elsewhere, so the prefix is swapped rather than the catalog
    being taught about a second machine's filesystem -- identity stays the
    hash, and the path stays a re-resolvable location.

    Returns None when the path isn't under the catalog root at all: better to
    skip a row loudly than to build a nonsense path and blame the NAS."""
    if not path or not local_root:
        return None
    p = path.replace("\\", "/")
    root = catalog_root.replace("\\", "/").rstrip("/")
    if not p.startswith(root + "/"):
        return None
    rest = p[len(root) + 1:]
    return os.path.join(local_root, *rest.split("/"))


def done_ids(lines):
    """Track ids already in the JSONL. A partially-written final line is the
    normal shape of an interrupted multi-hour pass -- skip it silently and let
    that track be re-analyzed, rather than letting one truncated line refuse
    the whole resume."""
    out = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        tid = rec.get("id")
        if tid:
            out.add(tid)
    return out


def work_list(rows, done, force=False):
    """(id, path) pairs still to analyze, in catalog order.

    `rows` is every candidate the catalog offers; `done` is what the JSONL
    already holds. Without --force a row whose bpm is already set is skipped by
    the QUERY (analyzed_rows below); this function's job is only the JSONL
    dedupe, so resumption works even against a snapshot that predates the last
    import."""
    out = []
    for r in rows:
        tid = r["id"] if not isinstance(r, dict) else r.get("id")
        path = r["path"] if not isinstance(r, dict) else r.get("path")
        if not force and tid in done:
            continue
        if force and tid in done:
            continue
        out.append((tid, path))
    return out


def record_for(track_id, bpm=None, rg=None, dr=None, peak=None, error=None):
    """One JSONL row. Keyed by the content hash, which is what makes the import
    idempotent and what lets one analysis serve every duplicate copy of a
    recording (Phase 0 collapses those into one `tracks` row)."""
    rec = {"id": track_id}
    if error:
        rec["error"] = error
    else:
        rec["bpm"] = bpm
        rec["replaygain_db"] = rg
        rec["dynamic_range_db"] = dr
        if peak is not None:
            rec["true_peak_dbfs"] = peak
    return rec


def eta(done, total, elapsed_s):
    """Seconds left at the current rate. A multi-hour pass with no feedback is
    indistinguishable from a hung one."""
    if done <= 0 or done >= total:
        return 0
    return (elapsed_s / done) * (total - done)


def fmt_hms(seconds):
    seconds = int(max(0, seconds))
    return "%d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)


# --- I/O: the thin half --------------------------------------------------------

def decode(path, sr=BEAT_SR, ffmpeg="ffmpeg"):
    """One ffmpeg run -> (mono float32 samples, ebur128 stderr text).

    The filter chain is the whole trick: ebur128 measures and passes the audio
    through, so the same decode that produces the loudness summary also
    produces the PCM for beat tracking. -f f32le to "-" means stdout: no
    temp file, and NOTHING is ever handed a path to write.

    communicate() reads stdout and stderr concurrently -- a naive read of one
    then the other deadlocks the moment a 4-minute track fills the pipe."""
    cmd = [
        ffmpeg, "-nostdin", "-hide_banner", "-v", "info",
        "-i", path,
        "-map", "0:a:0",
        "-af", "ebur128=peak=true,aresample=%d,aformat=sample_fmts=flt:"
               "channel_layouts=mono" % sr,
        "-f", "f32le", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    raw, err = proc.communicate()
    text = err.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(ffmpeg_error(text, proc.returncode))
    return raw, text


def estimate_bpm(raw):
    """Beat tracking -- the expensive half of this pass, and the reason it runs
    on bluejay at all. Imported lazily so the pure half stays importable (and
    CI-testable) on a box with no librosa."""
    import numpy as np
    import librosa

    y = np.frombuffer(raw, dtype=np.float32)
    if y.size < BEAT_SR:  # under a second of audio -- nothing to track
        return None
    tempo, _ = librosa.beat.beat_track(y=y, sr=BEAT_SR)
    t = float(np.atleast_1d(tempo)[0])
    return round(t, 2) if t > 0 else None


def analyze_one(track_id, path, ffmpeg="ffmpeg"):
    """One track -> one record. Never raises: an analysis failure on one track
    is logged and skipped and lands in needs_attention, never fatal to a
    25,000-track pass (weather.py:773-777's ethos, at scale)."""
    try:
        raw, err = decode(path, ffmpeg=ffmpeg)
    except Exception as e:
        return record_for(track_id, error="decode: %s" % str(e)[:180])
    try:
        i, lra, peak = parse_ebur128(err)
        bpm = estimate_bpm(raw)
    except Exception as e:
        return record_for(track_id, error="analyze: %s: %s"
                                          % (type(e).__name__, str(e)[:160]))
    if bpm is None and i is None:
        return record_for(track_id, error="no measurements (silent or empty?)")
    return record_for(track_id, bpm=bpm, rg=replaygain_db(i), dr=lra, peak=peak)


def candidate_rows(conn, force=False):
    """What the catalog offers. One row per track -- a recording rips twice in
    a 26k library (an album and a greatest-hits), and Phase 0 collapsed those
    into one id with N locations, so MIN(path) picks a copy arbitrarily and
    correctly: they are the same audio by definition of the hash."""
    sql = ("SELECT t.id AS id, MIN(f.path) AS path FROM tracks t "
           "JOIN track_files f ON f.track_id = t.id ")
    if not force:
        sql += "WHERE t.bpm IS NULL "
    sql += "GROUP BY t.id ORDER BY t.id"
    return conn.execute(sql).fetchall()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv
    if force:
        argv.remove("--force")
    limit = None
    workers = 4
    for i, a in enumerate(list(argv)):
        if a == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
        if a == "--workers" and i + 1 < len(argv):
            workers = int(argv[i + 1])

    db = music_catalog.db_path()
    local_root = os.environ.get("MERLE_MUSIC_ROOT", "").strip()
    jsonl = os.environ.get("MERLE_MUSIC_JSONL", "").strip() or DEFAULT_JSONL
    ffmpeg = os.environ.get("MERLE_FFMPEG", "").strip() or "ffmpeg"

    # Validate at startup, loudly. A pass that runs half-configured for two
    # hours and then has nothing to show is worse than one that refuses.
    if not local_root:
        raise SystemExit("[analyze] MERLE_MUSIC_ROOT is not set -- this box's "
                         "path to the share (e.g. \\\\hummingbird\\music). The "
                         "catalog holds pearl's paths; they must be remapped.")
    if not os.path.isfile(db):
        raise SystemExit("[analyze] MERLE_MUSIC_DB does not exist: %s" % db)
    try:
        subprocess.run([ffmpeg, "-version"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True)
    except Exception:
        raise SystemExit("[analyze] no ffmpeg on PATH (or MERLE_FFMPEG). It is "
                         "not optional: this library is 62%% ALAC, which "
                         "libsndfile cannot decode, and ebur128 is ffmpeg's.")

    conn = music_catalog.connect(db)
    done = set()
    if os.path.isfile(jsonl):
        with open(jsonl, "r", encoding="utf-8") as fh:
            done = done_ids(fh)
    rows = candidate_rows(conn, force=force)
    work = work_list(rows, done, force=force)
    if limit:
        work = work[:limit]

    print("[analyze] catalog: %s" % db)
    print("[analyze] share:   %s" % local_root)
    print("[analyze] results: %s (%d already done)" % (jsonl, len(done)))
    print("[analyze] %d tracks to analyze, %d workers%s"
          % (len(work), workers, " (--force)" if force else ""), flush=True)
    if not work:
        print("[analyze] nothing to do.")
        return 0

    lock = threading.Lock()
    stop = threading.Event()
    state = {"n": 0, "ok": 0, "bad": 0}
    t0 = time.time()
    fh = open(jsonl, "a", encoding="utf-8")

    def run(item):
        if stop.is_set():
            return
        tid, cat_path = item
        local = remap_path(cat_path, local_root=local_root)
        if local is None:
            rec = record_for(tid, error="path outside catalog root: %s" % cat_path)
        else:
            rec = analyze_one(tid, local, ffmpeg=ffmpeg)
        with lock:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()  # the JSONL is the resume state; buffering loses hours
            state["n"] += 1
            if rec.get("error"):
                state["bad"] += 1
                print("[analyze] !! %s -- %s" % (tid[:14], rec["error"][:90]),
                      flush=True)
            else:
                state["ok"] += 1
            n = state["n"]
            if n % 25 == 0 or n == len(work):
                el = time.time() - t0
                print("[analyze] %d/%d  ok=%d bad=%d  %.2f tr/s  elapsed %s  "
                      "eta %s" % (n, len(work), state["ok"], state["bad"],
                                  n / el, fmt_hms(el), fmt_hms(eta(n, len(work), el))),
                      flush=True)

    # NOT `with ThreadPoolExecutor(...)` + ex.map: map() submits every future
    # up front, and the with-block's __exit__ calls shutdown(wait=True), which
    # DRAINS THE WHOLE QUEUE. Ctrl-C would raise in the main thread and then
    # wait two hours for the other 23,000 tracks -- an interrupt that doesn't
    # interrupt. cancel_futures drops what hasn't started; the `stop` flag
    # turns anything already dequeued into an immediate no-op; and the ~16
    # genuinely in flight are simply re-analyzed on the next run, because the
    # JSONL is the resume state and it is flushed per record.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [ex.submit(run, item) for item in work]
        for f in concurrent.futures.as_completed(futures):
            f.result()
        ex.shutdown(wait=True)
    except KeyboardInterrupt:
        stop.set()
        ex.shutdown(wait=False, cancel_futures=True)
        print("\n[analyze] interrupted -- %d done this run. Re-run to resume; "
              "the JSONL is the state." % state["n"])
    finally:
        fh.close()

    el = time.time() - t0
    print("[analyze] done: %d ok, %d failed, %s wall, %.2f tracks/s"
          % (state["ok"], state["bad"], fmt_hms(el), state["n"] / max(el, 0.001)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
