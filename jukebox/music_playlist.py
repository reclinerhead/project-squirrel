# =============================================================================
# project-squirrel -- music_playlist.py
#
# The deterministic playlist engine (issue #139, epic #115 Phase 3): "play me
# stuff like this" as pure arithmetic over the catalog's analysis axes -- no
# LLM anywhere in this phase, by contract. If Phase 4 never happens, this
# still works; that's the epic's bar, verbatim.
#
# EVERYTHING HERE IS PURE. Candidates, the clock, and the RNG are injected;
# there is no SQL, no time.time(), no bare random. The daemon (music_daemon.py
# POST /queue) owns fetching candidates and supplying production entropy; the
# tests supply fixed seeds and frozen clocks. This is the most test-worthy
# code in the epic and it must be testable to the bone (the issue, in as many
# words).
#
# TWO MEASURED FACTS THE SCORING HONORS (epic #115 comments, 2026-07-16):
#
# 1. BPM IS LATTICE-QUANTIZED AT ~4-5% RESOLUTION. librosa's beat tracker
#    reports tempo on the discrete lattice 60*22050/(512*lag) -- 112.3, 117.5,
#    143.6 are lattice points, and the library median landing on exactly 117.5
#    is the lattice, not music. So exact BPM ties across genres are the NORMAL
#    case, tempo distance below one bin is noise (clamped to zero here), and
#    the GUI must never render "112.3" as if it were precise.
#
# 2. BECAUSE TEMPO TIES CONSTANTLY, THE TIEBREAK CARRIES THE RANKING -- and it
#    must include genre FAMILIES. The prototype's first draft used a weak
#    exact-tag bonus and put Queensryche at #2 for a Stereo MC's seed: same
#    tempo bin, same loudness-war crush, wrong planet. The taxonomy is feral
#    iTunes tags (ELECTRONICA\DUBSTEP, Rap & Hip-Hop, FLAC-as-a-genre), so
#    affinity works on the head token plus a small cluster table -- never
#    string equality, and never as a hard filter, which would exclude
#    soulmates over spelling.
#
# RATINGS ARE RULES AT THIS LAYER (#135's constants are the contract), and the
# engine must shine with zero of them -- the table has ~0 rows today, and an
# empty table is indistinguishable from "no opinions yet". Strong-down (-2)
# never reaches this module at all: the ban is enforced by the daemon's
# candidate WHERE clause, not entrusted to arithmetic (epic principle 4).
# Skip-weighting from play_history's `outcome` is deliberately Phase 4's --
# skips are EVIDENCE, and Phase 3 deals in rules.
#
# DETERMINISM WITH VARIETY, the lib/queue.ts move: same candidates + same seed
# + same RNG seed -> byte-identical queue (testable); production passes a
# fresh random.Random() and two Tuesday listens differ. Variety comes from
# weighted sampling over the top-K scored candidates, not a strict argsort.
# =============================================================================

import math
from collections import Counter
from statistics import median

from jukebox import music_catalog

# --- the scoring recipe -------------------------------------------------------
# Weights are the values tuned LIVE against the real catalog on 2026-07-16
# (epic #115: the Stereo MC's "Lost In Music" seed returning early-90s
# electronic/hip-hop nearly wall to wall is the regression baseline). Starting
# points with provenance, not sacred numbers -- retuning is a constant change
# plus a test update, which is the point of keeping the engine pure.

# Octave-folded log2 tempo distance: 10% off ~ 3.0 penalty. Dominant on
# purpose -- tempo is the axis that makes or breaks "same groove".
TEMPO_WEIGHT = 22.0

# One lattice bin in log2 tempo space (measured fact 1). Distance below this
# is quantization noise, never music, and scores as a dead tie.
TEMPO_EPS_LOG2 = 0.03

DR_WEIGHT = 0.55       # |delta dynamic range| in LU
GAIN_WEIGHT = 0.30     # |delta ReplayGain| in dB
ERA_SCALE_YEARS = 18.0 # |delta year| / this ...
ERA_CAP_YEARS = 30.0   # ... capped: 1975 vs 2020 is "different era", enough said

# Genre family affinity (measured fact 2) -- a BONUS (negative penalty), never
# a filter. Head token match beats cluster match beats nothing.
SAME_HEAD_BONUS = -2.5
SAME_CLUSTER_BONUS = -1.8

# The one measured cluster: the electronic/hip-hop family that fixed the
# Queensryche-at-#2 draft. A tuple of frozensets so the next family is an
# append, but no family is invented ahead of a seed that proves the need.
# Members are HEAD TOKENS (lowercased, first segment before \ or /).
#
# Since #163 this is the DEFAULT, not the truth: the daemon injects the
# families from genre_rules.yaml (music_genre.engine_clusters), stated over
# the canonical vocabulary, because a deployment's custom vocabulary needs
# custom families. This raw-tag set survives as the fallback for callers
# with no rules file -- and for a catalog whose genre_norm predates the pass.
GENRE_CLUSTERS = (
    frozenset({
        "electronic", "electronica", "dance", "house", "techno", "trance",
        "dubstep", "drum & bass", "breakbeat", "big beat", "downtempo",
        "trip-hop", "trip hop", "ambient", "idm", "industrial",
        "hip-hop", "hip hop", "rap", "rap & hip-hop", "r&b",
    }),
)

# Rating rules (#135's four thumbs as Phase 3 arithmetic). Strong-down is
# ABSENT deliberately: -2 is a WHERE clause in the daemon's candidate query,
# and an entry here would quietly turn the ban into a nudge. These three are
# unmeasured starting values (the table had ~0 rows when this shipped), sized
# so a strong-up outbids a same-head genre match and a down roughly cancels one.
RATING_ADJUST = {
    music_catalog.RATING_DOWN: 3.0,
    music_catalog.RATING_UP: -1.5,
    music_catalog.RATING_STRONG_UP: -3.0,
}

# --- anti-repetition ----------------------------------------------------------

# Anything played within this window is out of the running (injected clock).
# Both constants are policy a future settings surface could expose -- named
# here, not buried in the scorer.
COOLDOWN_HOURS = 24.0

# No artist twice within this many consecutive slots. The prototype's
# one-track-per-artist cap is the degenerate case of this rule.
ARTIST_SPACING = 6

# --- sampling -----------------------------------------------------------------

# Each slot samples from the best TOP_K still-eligible candidates, weighted
# exp(-(score - best) / TEMPERATURE). A strict argsort would make every
# Tuesday identical; a wider K or hotter temperature trades mood-fit for
# surprise. K=40 at ~23k candidates keeps every pick inside the prototype's
# quality band. 0.6 was tuned against the real catalog the same way the
# weights were: at 1.5, a candidate a full genre-family bonus worse still
# carried ~19% relative weight and Queensryche -- the issue's named failure
# case -- sampled back into a Stereo MC's queue at slot 6. At 0.6 it's ~1.5%.
TOP_K = 40
TEMPERATURE = 0.6


# --- genre families -----------------------------------------------------------

def genre_head(genre):
    """A feral iTunes tag -> its head token: lowercased first segment before
    any \\ or / separator, whitespace collapsed. ELECTRONICA\\DUBSTEP ->
    "electronica", Hip-Hop/Rap -> "hip-hop". None (or an empty tag) stays
    None -- a missing genre is neutral, never fatal."""
    if not genre:
        return None
    head = str(genre).replace("\\", "/").split("/")[0]
    head = " ".join(head.lower().split())
    return head or None


def genre_affinity(genre, target_genre, clusters=GENRE_CLUSTERS):
    """The bonus (negative penalty) two genre tags earn each other. Same head
    token is the strongest signal the taxonomy can give; same cluster catches
    the family resemblance exact tags miss (ELECTRONICA\\DUBSTEP vs
    Electronic). Anything else -- including a missing tag on either side --
    is zero: never a filter, never fatal. `clusters` is injected like the
    candidates and the RNG (#163: the daemon passes the rules file's
    families); the module constant is only the no-rules default."""
    a, b = genre_head(genre), genre_head(target_genre)
    if a is None or b is None:
        return 0.0
    if a == b:
        return SAME_HEAD_BONUS
    for cluster in clusters:
        if a in cluster and b in cluster:
            return SAME_CLUSTER_BONUS
    return 0.0


# --- the axes -----------------------------------------------------------------

def tempo_penalty(bpm, target_bpm):
    """Octave-folded log2 tempo distance, weighted. Half time and double time
    are the same groove (min over k in {-1, 0, 1} of |log2(bpm/target) + k|),
    and distance under one lattice bin is quantization noise scored as an
    exact tie (measured fact 1). Missing tempo on either side is neutral --
    though the daemon's candidate query requires bpm, so in production only
    the TARGET can lack one (a seed track that predates analysis)."""
    if not bpm or not target_bpm or bpm <= 0 or target_bpm <= 0:
        return 0.0
    d = math.log2(bpm / target_bpm)
    folded = min(abs(d + k) for k in (-1, 0, 1))
    if folded <= TEMPO_EPS_LOG2:
        return 0.0
    return TEMPO_WEIGHT * folded


def era_penalty(year, target_year):
    """|delta year| / 18, capped at 30 years. Gentle on purpose: era mostly
    rides along with the loudness axes anyway (#136 measured the loudness war
    decade by decade -- a known, accepted overlap)."""
    if not year or not target_year:
        return 0.0
    return min(abs(year - target_year), ERA_CAP_YEARS) / ERA_SCALE_YEARS


def _axis(value, target, weight):
    """Weighted absolute delta, neutral when either side is missing. A gap is
    real data (the file didn't say), which is the catalog's own rule."""
    if value is None or target is None:
        return 0.0
    return weight * abs(value - target)


def score_track(track, target, clusters=GENRE_CLUSTERS):
    """One candidate against the seed target: penalty-based, lower is better.
    The rating adjustment reads the four-thumb rules -- .get() with a 0.0
    default is what makes an EMPTY ratings table indistinguishable from "no
    opinions yet" (today's actual state, a design input not a blocker)."""
    score = tempo_penalty(track.get("bpm"), target.get("bpm"))
    score += _axis(track.get("dynamic_range_db"),
                   target.get("dynamic_range_db"), DR_WEIGHT)
    score += _axis(track.get("replaygain_db"),
                   target.get("replaygain_db"), GAIN_WEIGHT)
    score += era_penalty(track.get("year"), target.get("year"))
    score += genre_affinity(track.get("genre"), target.get("genre"), clusters)
    score += RATING_ADJUST.get(track.get("rating"), 0.0)
    return score


def rank_candidates(candidates, target, clusters=GENRE_CLUSTERS):
    """Every candidate scored and sorted, best first, as (score, track)
    pairs. Ties break on track id -- scores tie CONSTANTLY (the lattice), and
    a ranking that depended on input order would make determinism a lie."""
    scored = [(score_track(t, target, clusters), t) for t in candidates]
    scored.sort(key=lambda pair: (pair[0], pair[1].get("id") or ""))
    return scored


# --- seed resolution ----------------------------------------------------------

def resolve_seed(seed, candidates):
    """A flexible intent -> (target, exclude_ids). The target is a feature
    point (bpm, gain, DR, year, genre); exclude_ids are hard constraints the
    seed itself implies. Three shapes today:

      {"track": {...row...}}   that track's features; the track excludes
                               itself (radio from a song should not open by
                               replaying it).
      {"artist": "Name"}       the centroid of the artist's candidate tracks:
                               MEDIANS, not means -- one 287-BPM outlier must
                               not drag the target -- and the modal genre.
                               An unknown artist resolves to no target, which
                               build_queue turns into an empty queue, never a
                               crash.
      {"target": {...}}        a prebuilt feature point, passed through. This
                               is the mood/weather seam (narrator.py's
                               Producer-shaped-around-a-roster-of-one move):
                               the shape exists now so Phase 5's seeds plug
                               in without a rewrite. Nothing fills it yet.

    Anything else resolves to (None, empty) -- the route layer 400s garbage
    before it gets here; the pure layer just declines to queue."""
    if not isinstance(seed, dict):
        return None, set()

    if isinstance(seed.get("track"), dict):
        row = seed["track"]
        target = {axis: row.get(axis) for axis in
                  ("bpm", "replaygain_db", "dynamic_range_db", "year", "genre")}
        exclude = {row["id"]} if row.get("id") else set()
        return target, exclude

    if seed.get("artist"):
        name = str(seed["artist"]).strip().lower()
        rows = [t for t in candidates
                if (t.get("artist") or "").strip().lower() == name]
        if not rows:
            return None, set()
        target = {}
        for axis in ("bpm", "replaygain_db", "dynamic_range_db", "year"):
            values = [t[axis] for t in rows if t.get(axis) is not None]
            target[axis] = median(values) if values else None
        heads = Counter(h for h in (genre_head(t.get("genre")) for t in rows)
                        if h is not None)
        if heads:
            # max over (count, name) rather than most_common(): Counter breaks
            # count ties by insertion order, i.e. by candidate order, and the
            # target must not depend on how the daemon happened to sort rows.
            best = max(sorted(heads), key=lambda h: heads[h])
            target["genre"] = best
        else:
            target["genre"] = None
        return target, set()

    if isinstance(seed.get("target"), dict):
        return dict(seed["target"]), set()

    return None, set()


# --- anti-repetition ----------------------------------------------------------

def too_soon(last_played_at, now):
    """Whether a play at `last_played_at` (epoch seconds, None = never) is
    still inside the cooldown window at `now`. The clock is injected -- no
    time.time() anywhere in this module."""
    if last_played_at is None:
        return False
    return (now - last_played_at) < COOLDOWN_HOURS * 3600.0


# --- assembly -----------------------------------------------------------------

def _weighted_pick(pool, rng):
    """One track from a scored pool (best first), sampled with weight
    exp(-(score - best) / TEMPERATURE). rng supplies a single float in
    [0, 1) per call -- random.Random in production, a seeded one in tests."""
    best = pool[0][0]
    weights = [math.exp(-(s - best) / TEMPERATURE) for s, _ in pool]
    total = sum(weights)
    r = rng.random() * total
    acc = 0.0
    for weight, (_, track) in zip(weights, pool):
        acc += weight
        if r < acc:
            return track
    return pool[-1][1]  # float dust: r landed on the far edge


def build_queue(candidates, seed, n, rng, now, exclude_ids=(),
                clusters=GENRE_CLUSTERS):
    """The engine's whole job: an ordered list of up to n tracks like the
    seed, from the injected candidate set. Cooldown reads each candidate's
    `last_played_at` against the injected clock; `exclude_ids` is the
    caller's dedupe (the GUI passes what its queue already holds, so a refill
    never repeats it). Runs dry honestly: a library too small to satisfy the
    spacing rule yields a short queue, never a repeat."""
    target, seed_exclude = resolve_seed(seed, candidates)
    if target is None or n <= 0:
        return []
    banned = set(exclude_ids) | seed_exclude
    eligible = [t for t in candidates
                if t.get("id") not in banned
                and not too_soon(t.get("last_played_at"), now)]
    ranked = rank_candidates(eligible, target, clusters)

    picked = []
    picked_ids = set()
    while len(picked) < n:
        recent = {(t.get("artist") or "").lower()
                  for t in picked[-(ARTIST_SPACING - 1):]}
        pool = []
        for s, t in ranked:
            if t["id"] in picked_ids:
                continue
            if (t.get("artist") or "").lower() in recent:
                continue
            pool.append((s, t))
            if len(pool) == TOP_K:
                break
        if not pool:
            break
        track = _weighted_pick(pool, rng)
        picked.append(track)
        picked_ids.add(track["id"])
    return picked
