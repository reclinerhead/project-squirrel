# =============================================================================
# project-squirrel -- music_cache.py
#
# The browser output's FLAC cache (issue #149, epic #115 Phase 2b). The Denon
# gets catalog bytes untouched and needs none of this; the browser is the one
# output that can't decode the library's ALAC majority, and the owner's rule
# is absolute: NO LOSSY TRANSCODE, EVER. So ALAC repacks to FLAC -- identical
# PCM samples in a container Chrome/Edge/Firefox decode natively -- and
# everything else streams as raw bytes exactly like the Denon path.
#
# Rejected: AAC. Smaller, universally supported, and lossy -- which defeats
# the point of a library ripped lossless on purpose. Bytes are cheap; the
# bits are the product. The PR proves the repack with `ffmpeg -f md5` decoded
# -PCM equality, not a promise.
#
# THE CACHE KEY IS THE TRACK ID (the audio-stream content hash, #120), which
# makes staleness STRUCTURALLY IMPOSSIBLE: a re-ripped file has different
# audio bytes, hence a different hash, hence a different cache file. A retag
# doesn't change the hash, so it never invalidates. There is no mtime check
# and no invalidation logic here to get wrong -- an entry either corresponds
# to a catalog track or it's an orphan, and orphans are swept.
#
# CACHED FILES, NOT LIVE PIPES, because a pipe can't serve HTTP Range and no
# Range means no seeking (the epic's hard UX floor). The cold-click answer is
# the middle path: ffmpeg writes <id>.flac.part, readers TAIL THE GROWING
# FILE (iter_growing below) so playback starts within ~the first ffmpeg
# frames, and on success the part is renamed to <id>.flac -- rename-then-
# signal, so a tailing reader's fd survives on the same inode and presence of
# the final name IS the completeness contract. No sentinel files, no journal.
# The one degradation: bytes streamed mid-transcode carry ffmpeg's
# placeholder STREAMINFO (unknown duration, no seek until the track is next
# loaded); ffmpeg patches the header on disk at finalize, so every later
# serve -- including the acceptance criterion's MD5 check -- reads the
# corrected file.
#
# EVICTION IS A PLAN, THEN AN ACT. plan_evictions() is pure and tested:
# stale .part files first (a crashed ffmpeg's litter), then orphans (hash no
# longer in the catalog -- the re-rip case), then oldest-mtime LRU down to
# the cap. mtime is the recency signal ON PURPOSE: the daemon touch()es a
# file on every serve, because the mount is noatime and fs atime would lie.
#
# The cache lives on pearl's /srv/media-cache LV -- SHARED fast multimedia
# storage (issue #149's owner decision), of which music owns exactly the
# MERLE_MUSIC_CACHE subdirectory. Future tenants (Earl's bird audio #133,
# rover streams) get sibling dirs and their own retention; nothing here may
# assume it owns the volume.
#
# DEGRADES, NEVER FAILS: this module is imported by a daemon whose job is
# playback. A missing cache dir, a full partition, a failed transcode -- all
# log-and-degrade (the raw formats still stream; the cold ALAC click gets an
# honest error), never crash the daemon (weather.py:773-777's ethos).
#
# Config (env, the MERLE_* rule -- unset OR blank means the default):
#   MERLE_MUSIC_CACHE         music's subdirectory of the media-cache LV
#                             (absolute; /srv/media-cache/music on pearl).
#                             UNSET IS THE KILL SWITCH: no cache means the
#                             daemon offers no browser output at all, the
#                             MERLE_OLLAMA convention -- a bare checkout
#                             still runs the Denon untouched.
#   MERLE_MUSIC_CACHE_CAP_GB  LRU cap (default 40 -- ~1,400 ALAC-sized
#                             tracks against the LV's 48 GiB; re-budget when
#                             a second tenant lands).
# =============================================================================

import os
import subprocess
import threading
import time

# FLAC compression level for the repack. Low on purpose: this is a cache, not
# an archive -- encode speed is what the cold click feels, and the ~5% size
# difference against -5 buys nothing under an LRU cap. Lossless at every
# level; the level only trades CPU for bytes.
FFMPEG_COMPRESSION = "2"

# A .part older than this with no live job is a crashed transcode's litter.
# Generous: no real track takes an hour to repack.
STALE_PART_S = 3600.0

# How long a tailing reader sleeps when it outruns ffmpeg, and how long the
# cold click waits for the first byte before giving up. The poll is tighter
# than human perception; the timeout covers "ffmpeg never started writing"
# (bad source, dead mount) without holding a browser connection forever.
TAIL_POLL_S = 0.05
FIRST_BYTE_TIMEOUT_S = 15.0

# How many precache transcodes may run at once. Deliberately below the box's
# core count: warming the queue is a background nicety and must never starve
# a cold click (which bypasses this gate) or the rest of pearl.
PRECACHE_SLOTS = 2

DEFAULT_CAP_GB = 40

READ_CHUNK = 1 << 20


def cache_root():
    """MERLE_MUSIC_CACHE, or None -- and None means the browser output is OFF
    (the kill-switch convention). No default on purpose: a relative default
    would resolve against WorkingDirectory and quietly fill the repo checkout
    (the MERLE_WEATHER_DB trap, in reverse)."""
    return os.environ.get("MERLE_MUSIC_CACHE", "").strip() or None


def cache_cap_bytes():
    """The LRU cap in bytes. A malformed value falls to the default loudly
    rather than silently -- a typo'd cap of 0 would evict everything."""
    raw = os.environ.get("MERLE_MUSIC_CACHE_CAP_GB", "").strip()
    if not raw:
        return DEFAULT_CAP_GB << 30
    try:
        gb = float(raw)
        if gb <= 0:
            raise ValueError(raw)
    except ValueError:
        print("[music] bad MERLE_MUSIC_CACHE_CAP_GB=%r -- using %d"
              % (raw, DEFAULT_CAP_GB))
        return DEFAULT_CAP_GB << 30
    return int(gb * (1 << 30))


# --- pure: naming, policy, and the eviction plan --------------------------------

def cache_name(track_id):
    """Track id -> cache filename. The ':' in every id ("b:1f...") is illegal
    on NTFS, and tests run where developers do -- so it maps to '_', which no
    id contains (music_catalog.TRACK_ID_RE allows it, but the indexer only
    mints [bfx]:hex). Collision-free by construction, portable by choice."""
    return track_id.replace(":", "_") + ".flac"


def part_name(name):
    """The in-progress twin of a cache filename. Presence of the bare .flac
    is the completeness contract; the .part is the only thing ffmpeg writes."""
    return name + ".part"


def needs_flac(fmt, codec):
    """Whether the browser gets this track repacked to FLAC (True) or the
    file's raw bytes (False). POLICY, NOT DISCOVERY -- decided from catalog
    columns before any stream starts (epic principle 4):

      - mp3/flac/wav: raw. Browsers decode all three natively.
      - m4a/mp4 + aac: raw. Already lossy; browsers play AAC natively, and
        repacking a lossy source to FLAC loses nothing but inflates it for
        no reason.
      - m4a/mp4 + anything else: FLAC. `alac` is the library's majority; an
        unprobed NULL or an exotic fourcc takes the same path because the
        never-lossy default's worst case is wasted bytes, and a genuinely
        undecodable stream fails visibly at transcode."""
    if fmt not in ("m4a", "mp4"):
        return False
    return codec != "aac"


def ffmpeg_argv(src, dst):
    """The one transcode this repo performs. -vn drops the cover-art video
    stream (a FLAC container carrying mjpeg is exactly the kind of clever
    that breaks <audio>); -map_metadata keeps the tags -- harmless to
    browsers, kind to anyone who ever inspects the cache. -f flac is
    explicit because dst ends in .part, not .flac."""
    return ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", src,
            "-vn", "-map_metadata", "0",
            "-c:a", "flac", "-compression_level", FFMPEG_COMPRESSION,
            "-f", "flac", dst]


def plan_evictions(entries, expected_names, cap_bytes, now,
                   stale_s=STALE_PART_S):
    """What to delete, in order: stale .parts, then orphans, then LRU to the
    cap. Pure -- entries are (name, size, mtime) tuples, expected_names is
    the set of cache_name()s the catalog currently implies -- so the whole
    eviction brain is testable without a filesystem.

    Orphans go before ANY live entry regardless of age: an orphan is a
    re-ripped or pruned track's leftover, unreachable by construction (its
    hash resolves no catalog row), so it is pure waste. LRU then deletes
    oldest-mtime first until the survivors fit the cap. Fresh .parts are
    exempt from both counts -- a transcode in flight is neither orphan nor
    evictable, and killing it under a full cache would turn "cache pressure"
    into "playback breaks"."""
    doomed = []
    live = []
    for name, size, mtime in entries:
        if name.endswith(".part"):
            if now - mtime > stale_s:
                doomed.append(name)
            continue  # fresh part: in flight, untouchable, uncounted
        if name not in expected_names:
            doomed.append(name)
        else:
            live.append((name, size, mtime))
    total = sum(size for _, size, _ in live)
    for name, size, _ in sorted(live, key=lambda e: e[2]):
        if total <= cap_bytes:
            break
        doomed.append(name)
        total -= size
    return doomed


# --- I/O: the transcoder and the tailing reader ---------------------------------

class Job:
    """One in-flight transcode. `done` fires after the rename (or the
    failure cleanup), so a waiter that sees done-and-ok can trust the final
    path exists. `ok` is only meaningful after `done`."""

    def __init__(self, part_path, final_path):
        self.part_path = part_path
        self.final_path = final_path
        self.done = threading.Event()
        self.ok = False


class MusicCache:
    """The daemon's one handle on the cache directory: lookups, on-demand
    transcodes (cold clicks -- start immediately), background warms
    (precache -- gated to PRECACHE_SLOTS), and the sweep. Everything
    injectable for tests: `runner` replaces the real ffmpeg call, and
    `expected_names_fn` supplies the catalog's view at sweep time."""

    def __init__(self, root, cap_bytes, expected_names_fn, runner=None):
        self.root = root
        self.cap_bytes = cap_bytes
        self.expected_names_fn = expected_names_fn
        self.runner = runner or self._run_ffmpeg
        self.lock = threading.Lock()
        self.jobs = {}  # track_id -> Job
        self._precache_gate = threading.Semaphore(PRECACHE_SLOTS)

    # -- lookups

    def path_for(self, track_id):
        return os.path.join(self.root, cache_name(track_id))

    def lookup(self, track_id):
        """The complete cached file's path, or None. Touches on hit -- the
        serve IS the recency signal, and mtime is the LRU's clock because
        the mount is noatime."""
        path = self.path_for(track_id)
        if os.path.isfile(path):
            try:
                os.utime(path, None)
            except OSError:
                pass  # a vanished file just misses; the caller re-ensures
            return path
        return None

    # -- transcoding

    def ensure(self, track_id, src_path, background=False):
        """A complete file ("file", path), an in-flight transcode
        ("job", Job), or a refusal ("error", reason). Idempotent and
        deduplicating: two browsers cold-clicking the same track share one
        ffmpeg, and a precache racing a click is the same job seen twice.

        `background` marks a precache warm: it waits on the semaphore so a
        queue-warming burst can't starve the box, while a cold click starts
        NOW -- the listener is waiting on it."""
        with self.lock:
            hit = self.lookup(track_id)
            if hit:
                return "file", hit
            job = self.jobs.get(track_id)
            if job is not None:
                return "job", job
            final = self.path_for(track_id)
            job = Job(final + ".part", final)
            self.jobs[track_id] = job
        t = threading.Thread(target=self._work, name="music-transcode",
                             args=(track_id, src_path, job, background),
                             daemon=True)
        t.start()
        return "job", job

    def _work(self, track_id, src_path, job, background):
        if background:
            self._precache_gate.acquire()
        try:
            job.ok = self.runner(src_path, job.part_path)
            if job.ok:
                # Rename BEFORE done.set(): a tailing reader's open fd rides
                # the same inode across the rename (POSIX), and a waiter
                # released by done can trust final_path to exist.
                os.replace(job.part_path, job.final_path)
        except OSError as e:
            print("[music] transcode finalize failed: %s -- %s"
                  % (track_id, e))
            job.ok = False
        finally:
            if not job.ok:
                try:
                    os.remove(job.part_path)
                except OSError:
                    pass
            job.done.set()
            with self.lock:
                self.jobs.pop(track_id, None)
            if background:
                self._precache_gate.release()
            self.sweep()

    def _run_ffmpeg(self, src, dst):
        try:
            proc = subprocess.run(ffmpeg_argv(src, dst),
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE)
        except OSError as e:
            # ffmpeg missing entirely -- config problem, say so plainly.
            print("[music] ffmpeg unavailable: %s" % e)
            return False
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip()[-300:]
            print("[music] ffmpeg failed on %s: %s" % (src, tail))
            return False
        return True

    # -- eviction

    def sweep(self):
        """Apply plan_evictions to the directory. Called after every
        transcode and at daemon startup; failures are logged and skipped --
        a sweep that can't delete degrades to a fatter cache, never to a
        stopped daemon."""
        try:
            names = os.listdir(self.root)
        except OSError as e:
            print("[music] cache sweep: cannot list %s -- %s"
                  % (self.root, e))
            return 0
        entries = []
        for name in names:
            try:
                st = os.stat(os.path.join(self.root, name))
            except OSError:
                continue
            entries.append((name, st.st_size, st.st_mtime))
        doomed = plan_evictions(entries, self.expected_names_fn(),
                                self.cap_bytes, time.time())
        removed = 0
        for name in doomed:
            try:
                os.remove(os.path.join(self.root, name))
                removed += 1
            except OSError:
                pass
        if removed:
            print("[music] cache sweep: removed %d of %d entries"
                  % (removed, len(entries)))
        return removed


def iter_growing(job, chunk=READ_CHUNK, poll_s=TAIL_POLL_S,
                 first_byte_timeout_s=FIRST_BYTE_TIMEOUT_S):
    """Tail a transcode-in-progress as an HTTP body: yield bytes as ffmpeg
    writes them, sleep when we outrun it, finish when the job is done and
    the file is drained. This is what makes a cold click START playing in
    ~milliseconds instead of after the whole transcode.

    Served chunked (no Content-Length -- the FLAC's final size is unknowable
    until the encoder finishes), which RFC 7233 permits alongside ignoring
    Range. A seek during this first serve re-requests with Range against
    the by-then-complete file; this generator never has to honor one.

    If the job dies before the first byte, we simply end the body -- the
    <audio> element surfaces a decode error, the log has the ffmpeg tail,
    and the next click retries from scratch. After bytes have flowed a
    failure truncates, which the browser also treats as an error. Both are
    visible, neither wedges the daemon."""
    deadline = time.monotonic() + first_byte_timeout_s
    fh = None
    try:
        while fh is None:
            try:
                fh = open(job.part_path, "rb")
            except FileNotFoundError:
                if job.done.is_set():
                    # Renamed already (fast transcode) or failed: the final
                    # file has everything or the job has nothing.
                    if job.ok:
                        fh = open(job.final_path, "rb")
                        break
                    return
                if time.monotonic() > deadline:
                    print("[music] tail: no first byte from transcode")
                    return
                time.sleep(poll_s)
        while True:
            data = fh.read(chunk)
            if data:
                yield data
                continue
            if job.done.is_set():
                # One last read after done: the rename kept our inode, but
                # ffmpeg's final flush (and its header patch) may have
                # landed between our EOF and the event.
                data = fh.read(chunk)
                while data:
                    yield data
                    data = fh.read(chunk)
                return
            time.sleep(poll_s)
    finally:
        if fh is not None:
            fh.close()
