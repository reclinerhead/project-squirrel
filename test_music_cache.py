# =============================================================================
# project-squirrel -- test_music_cache.py
#
# The FLAC cache's brain (issue #149): the per-output transcode policy the
# lossless rule rides on, the eviction plan (orphans-first, then LRU -- the
# re-rip story depends on the order), the id->filename mapping, and the
# tailing reader that makes a cold click start in milliseconds. The real
# ffmpeg is exercised on pearl, not here: every transcode in this file is a
# fake runner, so CI proves the machinery without decoding a note.
#
# NOTE: this file must be on .github/workflows/tests.yml's pytest line by
# hand. CI enumerates test files and has no pytest.ini/testpaths fallback.
# =============================================================================

import os
import threading
import time

from jukebox import music_cache as mcc


# --- naming ---------------------------------------------------------------------

def test_cache_name_is_the_track_id_made_portable():
    """':' is illegal on NTFS and tests run where developers do; no indexer-
    minted id contains '_', so the mapping is collision-free."""
    assert mcc.cache_name("b:1fbc") == "b_1fbc.flac"
    assert mcc.cache_name("f:00aa") == "f_00aa.flac"
    assert mcc.part_name("b_1fbc.flac") == "b_1fbc.flac.part"


# --- the policy: who transcodes -------------------------------------------------

def test_natively_decodable_formats_stream_raw():
    for fmt in ("mp3", "flac", "wav"):
        assert mcc.needs_flac(fmt, None) is False


def test_lossy_aac_streams_raw():
    """Repacking a lossy source to FLAC loses nothing but inflates it for no
    reason -- and browsers decode AAC natively."""
    assert mcc.needs_flac("m4a", "aac") is False
    assert mcc.needs_flac("mp4", "aac") is False


def test_alac_takes_the_flac_path():
    assert mcc.needs_flac("m4a", "alac") is True
    assert mcc.needs_flac("mp4", "alac") is True


def test_unprobed_and_exotic_codecs_take_the_never_lossy_default():
    """NULL means "not probed yet"; an exotic fourcc means "not AAC". Both go
    to FLAC because that path's worst case is wasted bytes, never lost ones."""
    assert mcc.needs_flac("m4a", None) is True
    assert mcc.needs_flac("m4a", "drms") is True


def test_ffmpeg_argv_is_the_lossless_repack():
    argv = mcc.ffmpeg_argv("/mnt/music/a.m4a", "/cache/b_1.flac.part")
    assert argv[0] == "ffmpeg"
    assert "/mnt/music/a.m4a" in argv
    assert argv[-1] == "/cache/b_1.flac.part"
    assert argv[argv.index("-c:a") + 1] == "flac"  # never a lossy codec
    assert "-vn" in argv          # cover art must not become a video stream
    assert "-f" in argv           # dst ends .part, so the muxer is explicit


# --- the eviction plan ----------------------------------------------------------

def test_under_cap_with_no_orphans_deletes_nothing():
    entries = [("a.flac", 100, 10.0), ("b.flac", 100, 20.0)]
    assert mcc.plan_evictions(entries, {"a.flac", "b.flac"}, 1000, 5000.0) == []


def test_orphans_go_first_regardless_of_recency():
    """An orphan is a re-ripped or pruned track's leftover -- unreachable by
    construction, pure waste, deleted even when the cache is under cap and
    even if it was served a second ago."""
    entries = [("old.flac", 100, 10.0), ("orphan.flac", 100, 99999.0)]
    doomed = mcc.plan_evictions(entries, {"old.flac"}, 10_000, 100_000.0)
    assert doomed == ["orphan.flac"]


def test_lru_evicts_oldest_mtime_until_under_cap():
    entries = [("a.flac", 400, 30.0), ("b.flac", 400, 10.0),
               ("c.flac", 400, 20.0)]
    expected = {"a.flac", "b.flac", "c.flac"}
    # cap 800: total 1200, must shed 400 -- the oldest (b) goes, then fits.
    assert mcc.plan_evictions(entries, expected, 800, 100.0) == ["b.flac"]
    # cap 400: shed two, oldest-first order.
    assert mcc.plan_evictions(entries, expected, 400, 100.0) == \
        ["b.flac", "c.flac"]


def test_orphan_bytes_do_not_count_against_the_cap():
    """Deleting the orphan already frees its bytes -- the LRU pass must size
    only what survives, or it would evict a live track to pay for a dead one."""
    entries = [("live.flac", 400, 10.0), ("orphan.flac", 10_000, 20.0)]
    doomed = mcc.plan_evictions(entries, {"live.flac"}, 500, 100.0)
    assert doomed == ["orphan.flac"]  # live.flac fits once the orphan is gone


def test_fresh_part_is_untouchable_and_uncounted():
    """A transcode in flight is neither orphan nor evictable -- killing it
    under cache pressure would turn "cache full" into "playback breaks"."""
    now = 1000.0
    entries = [("x.flac.part", 10_000, now - 5.0), ("a.flac", 100, 10.0)]
    assert mcc.plan_evictions(entries, {"a.flac"}, 200, now) == []


def test_stale_part_is_a_crashed_transcodes_litter():
    now = 100_000.0
    entries = [("x.flac.part", 100, now - mcc.STALE_PART_S - 1)]
    assert mcc.plan_evictions(entries, set(), 10_000, now) == ["x.flac.part"]


# --- MusicCache: the transcoder harness ------------------------------------------

def write_runner(payload=b"FLACBYTES"):
    """A fake ffmpeg: writes the payload to dst and succeeds."""
    def run(src, dst):
        with open(dst, "wb") as f:
            f.write(payload)
        return True
    return run


def make_cache(tmp_path, runner, cap=1 << 30, expected=None):
    return mcc.MusicCache(str(tmp_path), cap,
                          (lambda: expected) if expected is not None
                          else set, runner=runner)


def test_ensure_transcodes_then_hits(tmp_path):
    cache = make_cache(tmp_path, write_runner(b"OUT"),
                       expected={"b_1.flac"})
    kind, job = cache.ensure("b:1", "src.m4a")
    assert kind == "job"
    assert job.done.wait(5.0)
    assert job.ok
    final = cache.path_for("b:1")
    assert os.path.isfile(final)
    assert not os.path.exists(job.part_path)  # renamed, not copied
    kind2, val2 = cache.ensure("b:1", "src.m4a")
    assert (kind2, val2) == ("file", final)   # the second click is free
    assert cache.jobs == {}                   # bookkeeping cleaned up


def test_concurrent_ensures_share_one_job(tmp_path):
    gate = threading.Event()

    def slow_runner(src, dst):
        gate.wait(5.0)
        with open(dst, "wb") as f:
            f.write(b"X")
        return True

    cache = make_cache(tmp_path, slow_runner, expected={"b_1.flac"})
    k1, j1 = cache.ensure("b:1", "src.m4a")
    k2, j2 = cache.ensure("b:1", "src.m4a")
    assert (k1, k2) == ("job", "job")
    assert j1 is j2  # two browsers, one ffmpeg
    gate.set()
    assert j1.done.wait(5.0)


def test_failed_transcode_cleans_up_and_can_retry(tmp_path):
    calls = []

    def failing_runner(src, dst):
        calls.append(src)
        with open(dst, "wb") as f:
            f.write(b"HALF")
        return False

    cache = make_cache(tmp_path, failing_runner)
    kind, job = cache.ensure("b:1", "src.m4a")
    assert job.done.wait(5.0)
    assert not job.ok
    assert not os.path.exists(job.part_path)     # no half-written litter
    assert not os.path.exists(cache.path_for("b:1"))
    kind2, job2 = cache.ensure("b:1", "src.m4a")  # a retry is a fresh job
    assert kind2 == "job"
    assert job2.done.wait(5.0)
    assert len(calls) == 2


def test_lookup_touches_the_lru_clock(tmp_path):
    cache = make_cache(tmp_path, write_runner())
    path = cache.path_for("b:1")
    with open(path, "wb") as f:
        f.write(b"X")
    os.utime(path, (1000.0, 1000.0))
    assert cache.lookup("b:1") == path
    assert os.stat(path).st_mtime > 1000.0  # the serve IS the recency signal


def test_sweep_applies_the_plan(tmp_path):
    cache = make_cache(tmp_path, write_runner(), cap=150,
                       expected={"a.flac", "b.flac"})
    for name, mtime in (("a.flac", 10.0), ("b.flac", 20.0),
                        ("orphan.flac", 30.0)):
        p = os.path.join(str(tmp_path), name)
        with open(p, "wb") as f:
            f.write(b"0" * 100)
        os.utime(p, (mtime, mtime))
    removed = cache.sweep()
    # The orphan goes as an orphan; a (oldest) goes to fit the 150 cap.
    assert removed == 2
    assert sorted(os.listdir(str(tmp_path))) == ["b.flac"]


# --- iter_growing: the tailing reader ---------------------------------------------

def stub_job(tmp_path, name="t.flac"):
    final = os.path.join(str(tmp_path), name)
    return mcc.Job(final + ".part", final)


def test_tail_reads_a_file_that_grows_then_finishes(tmp_path):
    """The cold click: ffmpeg is still writing while the browser reads. The
    generator must yield the early bytes immediately and drain the rest after
    the job signals done -- including bytes flushed between its last read and
    the signal."""
    job = stub_job(tmp_path)
    with open(job.part_path, "wb") as f:
        f.write(b"AAAA")

    def finish():
        time.sleep(0.15)
        with open(job.part_path, "ab") as f:
            f.write(b"BBBB")
        job.ok = True
        job.done.set()

    t = threading.Thread(target=finish)
    t.start()
    chunks = list(mcc.iter_growing(job, chunk=2, poll_s=0.01))
    t.join()
    assert b"".join(chunks) == b"AAAABBBB"


def test_tail_serves_the_final_file_when_the_transcode_already_won(tmp_path):
    """A fast transcode can rename before the reader's first open -- the
    generator follows to the final path rather than reporting nothing."""
    job = stub_job(tmp_path)
    with open(job.final_path, "wb") as f:
        f.write(b"DONE")
    job.ok = True
    job.done.set()
    assert b"".join(mcc.iter_growing(job, poll_s=0.01)) == b"DONE"


def test_tail_yields_nothing_for_a_job_that_died_before_first_byte(tmp_path):
    job = stub_job(tmp_path)
    job.ok = False
    job.done.set()  # failed; cleanup already removed the part
    assert list(mcc.iter_growing(job, poll_s=0.01)) == []


def test_tail_gives_up_when_no_first_byte_ever_arrives(tmp_path):
    """ffmpeg never started writing (dead mount, bad source) and the job
    never signals: the reader must time out rather than hold the browser's
    connection forever."""
    job = stub_job(tmp_path)
    started = time.monotonic()
    chunks = list(mcc.iter_growing(job, poll_s=0.01,
                                   first_byte_timeout_s=0.1))
    assert chunks == []
    assert time.monotonic() - started < 5.0
