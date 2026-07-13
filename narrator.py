# =============================================================================
# project-squirrel -- narrator.py
#
# The scene narrator: ONE voice, subscribed to the live event bus, publishing
# spoken-style observations. v1 (issue #9) built the PACING -- silence is most
# of the show -- with Tier-1 template prose. Issue #20 added Tier 2: when
# MERLE_OLLAMA is set, generate() synthesizes each line with a local LLM
# (Ollama) in the persona's voice, falling back to the templates whenever the
# LLM is absent, slow, or unwell. The pacing gate and bus contract are
# identical across tiers. Issues #26/#28 enriched the LLM prompt: a fresh
# weather/current report adds a dry current-conditions paragraph, and a
# rolling memory of the narrator's own recent lines gives the model variety
# ("don't repeat yourself") and continuity (running-show callbacks).
#
# Issue #74 put an EDITOR between the wire and the talent: tracker id churn on
# a steady scene can flood driveway/events with phantom departure+arrival
# pairs, and v1 handed every one of them to the narrator -- nonstop LLM calls.
# The Editor (below) holds a species-count change until it PERSISTS (a
# departure immediately undone by an arrival was the same animal), hard-caps
# how often anything reaches the talent, and collapses whatever piled up
# during the cooldown into ONE scene_update summary. Dropped moments are fine:
# SQLite has the record; the bus is live transport.
#
# Issue #80 made the show a two-hander: a second narrator process (Jim, on
# merle) runs this same script with its own persona. A persona carrying the
# opt-in `answers_to` knob additionally subscribes to narration/lines and
# treats a colleague's line that names it (word-boundary, case-insensitive)
# as a synthetic `colleague_mention` event -- which rides the Editor's
# moments shelf like everything else, so mentions respect the narration rate
# limit. Every colleague line heard (mention or not) also feeds a small
# broadcast-context memory rendered into the LLM prompt, so the follow-up
# riffs on what was actually said instead of repeating it. Loop safety: a
# narrator ignores lines carrying its own mqtt_id, a follow-up line never
# triggers a follow-up (never reply to a reply -- what bounds the chain now
# that both narrators listen, issue #88), and a persona without `answers_to`
# never subscribes at all -- byte-identical to the one-voice era.
#
# Issue #88 split the ROLES: Jim (the field man) makes the announcements;
# Marlin carries `defer_events_to: [jim]` and stays quiet on raw events while
# Jim's retained presence reads online, speaking instead through mention
# follow-ups (studio instructions). When the field goes dark -- rover off,
# merle down -- the deferring narrator covers the announcements automatically,
# with a dry covering note in the prompt for the persona to rib.
#
#   python narrator.py --persona personas/marlin.yaml
#
# Bus contract (topics in bus.py):
#   subscribes  driveway/events           the daemon's live event stream
#   subscribes  weather/current           retained latest conditions -> prompt
#                                         context (LLM tier only, never a
#                                         speaking trigger)
#   subscribes  narration/lines           ONLY when the persona sets answers_to
#                                         (issue #80): colleague lines -> heard
#                                         memory + mention triggers
#   subscribes  narrators/+/status        ONLY when the persona sets
#                                         defer_events_to (issue #88):
#                                         colleague presence -> defer vs cover
#   publishes   narration/lines           {ts, narrator, mqtt_id, voice, text,
#                                         event_kind}
#   publishes   narration/journal/<id>    {lines: [...]} -- the field journal
#                                         window (issue #58): the last
#                                         JOURNAL_LINES spoken lines, oldest
#                                         first, RETAINED and republished whole
#                                         so a fresh dashboard tab rehydrates
#                                         from the broker (the weather/history
#                                         pattern). Persisted to a JSON file so
#                                         a narrator restart doesn't blank it.
#                                         Namespaced per narrator (issue #80)
#                                         so two narrators can't clobber each
#                                         other's retained window.
#   presence    narrators/<id>/status     "online"/"offline", retained; "offline"
#                                         is the MQTT Last Will, so a crash flips
#                                         the dashboard lamp without any cleanup
#
# LLM tier config (env, following the MERLE_MQTT convention):
#   MERLE_OLLAMA        Ollama "host" or "host:port" (port defaults to 11434).
#                       UNSET = LLM tier off; templates carry the show.
#   MERLE_OLLAMA_MODEL  model name (default: OLLAMA_DEFAULT_MODEL below)
#   MERLE_NARRATION_JOURNAL  journal file path (default: narration_journal.json,
#                       which lands in the unit's WorkingDirectory on pearl --
#                       the MERLE_WEATHER_HISTORY convention)
#   MERLE_NARRATE_STABLE_S        seconds a species-count change must hold
#                                 before it is narratable (default 20)
#   MERLE_NARRATE_MIN_INTERVAL_S  hard floor between narrations -- the LLM-call
#                                 ceiling, regardless of upstream behavior
#                                 (default 30 = at most 2 calls/min)
#
# The narrator never plays audio itself -- it publishes; a consumer (the MCC
# dashboard's TTS, someday a speaker on the porch) does the speaking.
#
# The producer/orchestrator is EMBEDDED here for now (Producer below), shaped
# around a roster even though the roster is one voice -- see the class docstring.
# =============================================================================

import argparse
import json
import os
import queue
import random
import re
import time
import urllib.request
from collections import deque
from datetime import datetime

import paho.mqtt.client as mqtt
import yaml

import bus

PERSONA_DEFAULTS = {
    "tts_voice": "",
    "cooldown_seconds": 20.0,
    "chattiness": 0.9,
    "interest_threshold": 0.4,
    # Names this narrator answers to on narration/lines (issue #80). OPT-IN:
    # absent/empty means the mention machinery is entirely off and the process
    # behaves byte-identically to the one-voice era (the #26/#28 degradation
    # convention). A tuple, not a list, so the shared default can't be mutated.
    "answers_to": (),
    # Colleague mqtt_ids this narrator defers the play-by-play to (issue #88).
    # OPT-IN, same degradation rule: absent/empty = off. When set, the
    # narrator watches those colleagues' retained presence and stays quiet on
    # raw driveway events while any of them is on the air -- the field has the
    # announcements. Mention follow-ups are never deferred (they're this
    # narrator's own beat), and when every listed colleague is dark the
    # announcements come back with a covering note in the prompt.
    "defer_events_to": (),
}

# How inherently remark-worthy each event kind is (0..1). Scaled by the
# persona's chattiness and compared to its interest_threshold -- so the same
# event can be worth a line to one narrator and beneath another's notice.
INTEREST = {
    "crowd_snapshot": 0.9,
    "arrival": 0.7,
    "departure": 0.5,
    # scene_update is the Editor's burst-collapse summary (issue #74) -- it
    # stands in for several arrivals/departures at once, so it must interest
    # any narrator an arrival would.
    "scene_update": 0.7,
    "clip_recorded": 0.2,
    # A colleague naming you on the air is the marquee trigger (issue #80) --
    # it must clear any sane persona's threshold. Rare by construction: only
    # Marlin's occasional "trusty assistant Jim" flourishes mint one.
    "colleague_mention": 0.95,
}
UNKNOWN_INTEREST = 0.3   # future event kinds: mildly interesting, never spam

# Tier-1 narration: templates per event kind, filled from the event details and
# the character bible. Deliberately plain -- persona flavor arrives with the
# LLM tier; what v1 must get right is that these fire at the right MOMENTS.
TEMPLATES = {
    "arrival": (
        "A {species} just came in over the east edge. Eyes on {seed_pile}.",
        "We've got a {species} on the pavement. Settle in.",
        "New arrival: one {species}. {station} is open for business.",
        "Here comes a {species} -- walking like it owns the place. They all do.",
    ),
    "departure": (
        "And the {species} is gone. Total visit: {duration}.",
        "The {species} has left {station}. {duration}, well spent.",
        "There goes the {species} -- {duration} and not a second wasted.",
    ),
    "crowd_snapshot": (
        "Big scene: {total} animals out there at once. Someone alert {big_chonk}.",
        "It's getting crowded -- {total} on the pavement. This is the good stuff.",
        "{total} visitors at once. {station} hasn't seen numbers like this all day.",
    ),
    "clip_recorded": (
        "For the archive: that last stretch is on tape.",
        "Clip's in the can. Posterity will thank us.",
    ),
    "scene_update": (
        "Busy stretch out there. Where things stand now: {scene}.",
        "Taking stock after the commotion: {scene}. {station} rolls on.",
        "The dust settles, and the tally reads: {scene}.",
    ),
    # Tier-1 fallback for a mention (issue #80): only a mention-listening
    # narrator ever receives this kind, so the acknowledgment can lean field-
    # correspondent without stepping on any other persona.
    "colleague_mention": (
        "Right, {colleague}. On my way -- someone has to do the field work.",
        "I heard you, {colleague}. Heading in for a closer look, as usual.",
    ),
}
FALLBACK_TEMPLATES = (
    "Something stirred out there ({kind}), and I'm choosing to find it interesting.",
)


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_persona(path):
    """Persona file merged over defaults, so a minimal persona (name + mqtt_id)
    still has working pacing knobs."""
    persona = {**PERSONA_DEFAULTS, **load_yaml(path)}
    for key in ("name", "mqtt_id"):
        if not persona.get(key):
            raise ValueError(f"persona {path} is missing required field '{key}'")
    return persona


def score_event(event):
    return INTEREST.get(event.get("kind"), UNKNOWN_INTEREST)


def worth_speaking(event, persona, now, last_spoke_at):
    """THE pacing gate -- the one place that decides silence vs a line.
    Cooldown first (a narrator mid-breath hears nothing), then scored interest
    scaled by chattiness against the persona's threshold."""
    if now - last_spoke_at < persona["cooldown_seconds"]:
        return False
    return score_event(event) * persona["chattiness"] >= persona["interest_threshold"]


# --- Colleague mentions (issue #80) -------------------------------------------
# A mention-listening narrator (persona knob `answers_to`) watches
# narration/lines for a colleague naming it and turns the hit into a synthetic
# colleague_mention event. The event then rides the Editor's moments shelf and
# the normal pacing gate -- NEVER a direct reply channel; everything rides the
# bus. Loop safety: lines carrying the narrator's own mqtt_id are ignored, and
# a persona without the knob never subscribes at all, so with one listener
# (Jim) and one non-listener (Marlin) there is no ping-pong path.

def mentions_name(text, names):
    """True when any of the names appears in text as a whole word,
    case-insensitive -- a line about 'Jimmy' is not a mention of Jim."""
    return any(re.search(rf"\b{re.escape(name)}\b", text or "", re.IGNORECASE)
               for name in names)


def colleague_mention(line, persona):
    """The synthetic colleague_mention event for a narration/lines payload,
    or None when it isn't one: knob absent/empty (feature off), the
    narrator's own line (loop safety), a follow-up line (never reply to a
    reply -- with two mention-listeners on the air since issue #88, this is
    what bounds the chain at announcement -> one follow-up instead of
    ping-ponging forever), or no name matched. A standing instruction still
    reaches the colleague through the heard-lines memory; it just can't
    trigger."""
    names = persona.get("answers_to") or ()
    if not names:
        return None
    if line.get("mqtt_id") == persona["mqtt_id"]:
        return None
    if line.get("event_kind") == "colleague_mention":
        return None
    if not mentions_name(line.get("text"), names):
        return None
    return {"ts": line.get("ts"), "kind": "colleague_mention",
            "details": {"narrator": line.get("narrator"),
                        "mqtt_id": line.get("mqtt_id"),
                        "text": line.get("text")}}


# --- Announcer roles (issue #88) -----------------------------------------------
# Two live narrators both cleared their gates on the same event and the show
# double-announced. The fix is a role split, not coordination: the field man
# (Jim) announces, and a narrator carrying `defer_events_to` (Marlin) stays
# quiet on raw events while any listed colleague's retained presence reads
# "online" -- presence is already on the bus with Last-Will semantics, so a
# dead rover flips the studio back to covering the announcements within
# seconds, no new machinery. Mention follow-ups are never deferred: reacting
# to the field report IS the studio's beat.

def defers_event(event, persona, colleagues):
    """True when this narrator should leave the event to the field: the
    defer knob is set, the event is a raw one (a mention follow-up is never
    deferred), and at least one listed colleague is on the air."""
    targets = persona.get("defer_events_to") or ()
    if not targets or event.get("kind") == "colleague_mention":
        return False
    return any(colleagues.get(t) == "online" for t in targets)


def covering_field(event, persona, colleagues):
    """True when this narrator is announcing a raw event ONLY because every
    colleague it defers to is off the air -- the cue for the prompt's
    covering note (and the coffee-break ribbing the persona hangs on it)."""
    targets = persona.get("defer_events_to") or ()
    if not targets or event.get("kind") == "colleague_mention":
        return False
    return not any(colleagues.get(t) == "online" for t in targets)


# Rides the prompt only while covering (issue #88): dry facts, no flavor --
# the persona decides whether an absent field man means worry or a pointed
# remark about coffee breaks. Absent = prompt byte-identical, as ever. Like
# the weather paragraph (#26), the fact travels with a one-line usage nudge:
# desk-tested, without it the model kept addressing the absent colleague as
# if they were at their post.
COVERING_NOTE = (
    "Note: your colleague out in the field is away from their post right "
    "now and cannot be reached, so you are covering the field watch from "
    "the studio yourself. Do not hand anything off to them; acknowledge "
    "their absence in your delivery.")


def human_duration(seconds):
    """Visit lengths as a narrator would say them, not as floats."""
    if seconds < 1:
        return "a blink"
    if seconds < 90:
        return f"about {round(seconds)} seconds"
    if seconds < 5400:
        return f"about {round(seconds / 60)} minutes"
    return f"about {seconds / 3600:.1f} hours"


def scene_phrase(counts):
    """A spoken inventory of the scene from a {species: count} dict --
    "4 squirrels and 1 turkey" -- or a quiet-pavement phrase when nothing is
    out there. Naive plural (+s) is fine for this cast: squirrels, turkeys,
    chipmunks."""
    parts = [f"{n} {sp}{'s' if n != 1 else ''}"
             for sp, n in sorted(counts.items()) if n > 0]
    if not parts:
        return "a quiet stretch of pavement"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def template_fields(event, bible):
    """Everything a template may reference, from the event + the bible. One
    function so a new template can't invent a field the tests don't check."""
    details = event.get("details") or {}
    duration_s = details.get("duration_s")
    return {
        "kind": event.get("kind", "something"),
        "species": details.get("species", "critter"),
        # departures only carry a duration when the LAST one leaves; a "one of
        # them left" event has none, and "a blink" would be a lie.
        "duration": human_duration(duration_s) if duration_s is not None
                    else "a good while",
        "total": details.get("total", "several"),
        "scene": scene_phrase(details.get("counts") or {}),
        "station": bible.get("station", "the driveway"),
        "seed_pile": bible.get("seed_pile", "the seed pile"),
        "big_chonk": (bible.get("legends") or {}).get("big_chonk", "Big Chonk"),
        # colleague_mention events carry who did the mentioning (issue #80).
        "colleague": details.get("narrator", "the studio"),
    }


# --- Tier 2: LLM narration via Ollama ----------------------------------------

OLLAMA_DEFAULT_PORT = 11434
OLLAMA_DEFAULT_MODEL = "gemma3:12b"
# Generation runs on the main pacing loop (issue #74 moved it off paho's
# network thread -- on_message only enqueues now, so a slow generation can no
# longer threaten the MQTT keepalive). Events arriving mid-generation wait in
# the queue and mostly collapse at the Editor afterwards -- which is the
# pacing we wanted anyway. Desk-tested: ~8s warm, ~15s with a cold model load,
# so 30s gives cold starts headroom without stalling the show for long.
OLLAMA_TIMEOUT_S = 30

# Output rules live in code, not the persona file, so every persona gets them
# and persona files stay pure character.
LINE_RULES = (
    "Deliver ONE or TWO (at most) on-air lines of one to three sentences. Spoken words "
    "only: no stage directions, no quotation marks, no emoji, no preamble. "
    "Never break character."
)


def ollama_address():
    """Ollama endpoint from MERLE_OLLAMA ("host" or "host:port"). OPTIONAL,
    unlike MERLE_MQTT -- unset simply means the LLM tier is off, which is both
    the kill switch and what keeps a bare dev checkout working."""
    raw = os.environ.get("MERLE_OLLAMA", "").strip()
    if not raw:
        return None
    host, _, port = raw.partition(":")
    return host, int(port) if port else OLLAMA_DEFAULT_PORT


# A weather/current report older than this is ignored: the weather service
# polls every 10 minutes, so 30 minutes is 3 missed polls -- the service is
# down or wedged, not merely between reads. A dead weather post must degrade
# to exactly the no-weather prompt, never to narrating yesterday's rain.
WEATHER_STALE_S = 30 * 60

# Wind speed -> the dry phrase the summary uses (upper bound mph, phrase).
WIND_WORDS = (
    (1, "calm air"),
    (8, "a light breeze"),
    (16, "a steady breeze"),
    (25, "a strong wind"),
    (float("inf"), "a howling wind"),
)


def weather_sentence(report, now):
    """One dry factual sentence from the retained weather/current payload, or
    None when there is no report fresh enough to speak about. Like the event
    summary itself, deliberately plain -- Marlin turns "overcast clouds" into
    Wild Kingdom weather color on his own."""
    if not report:
        return None
    ts = report.get("ts")   # unix epoch seconds, OpenWeather's own dt
    if ts is None or now - ts > WEATHER_STALE_S:
        return None
    temp = report.get("temp_f")
    if temp is None:
        return None   # a report with no temperature isn't worth a sentence
    parts = [f"It is {round(temp)}F"]
    wind = report.get("wind_mph")
    if wind is not None:
        parts.append(f"with {next(p for cap, p in WIND_WORDS if wind < cap)}")
    if report.get("description"):
        parts.append(f"under {report['description']}")
    return " ".join(parts) + "."


def event_summary(event, bible):
    """A factual one-line account of what just happened -- the LLM's raw
    material, deliberately dry so all the flavor comes from the persona.
    Extra bus context (like the weather, issue #26) rides build_user_prompt()
    as its own labeled paragraph instead of being appended here: desk-tested,
    a bare fact buried in the summary is context the model ignores."""
    f = template_fields(event, bible)
    kind = event.get("kind")
    if kind == "arrival":
        summary = f"A {f['species']} has just arrived at {f['station']}."
    elif kind == "departure":
        summary = f"The {f['species']} has just left after a visit of {f['duration']}."
    elif kind == "crowd_snapshot":
        summary = f"There are now {f['total']} animals out on the pavement at once."
    elif kind == "scene_update":
        # The Editor's burst-collapse summary (issue #74): several changes
        # landed during the narration cooldown, so the story is where the
        # scene ENDED UP, not the play-by-play that got it there.
        counts = (event.get("details") or {}).get("counts") or {}
        if counts:
            summary = (f"After a busy stretch of comings and goings, the scene "
                       f"has settled: {scene_phrase(counts)} out on the pavement.")
        else:
            summary = ("After a busy stretch of comings and goings, "
                       "the pavement has gone quiet.")
    elif kind == "clip_recorded":
        summary = "A video clip of the recent activity was just saved to the archive."
    elif kind == "colleague_mention":
        # A colleague named this narrator on the air (issue #80). Dry facts +
        # the cue to follow up; the persona provides all the flavor.
        details = event.get("details") or {}
        summary = (f"Your colleague {details.get('narrator', 'in the studio')} "
                   f"just said on air: '{details.get('text', '')}' -- "
                   "he mentioned you. Pick up the thread.")
    else:
        summary = f"Something just happened out there (event: {f['kind']})."
    species = (event.get("details") or {}).get("species")
    lore = (bible.get("species_lore") or {}).get(species)
    if lore:
        summary += f" Local lore about this species: {lore}."
    return summary


def build_system_prompt(persona, bible):
    """Persona voice + shared world canon + the output rules."""
    facts = [f"The show is set at {bible.get('station', 'the driveway')}.",
             f"The main attraction is {bible.get('seed_pile', 'the seed pile')}."]
    facts += [f"Legend: {legend}." for legend in (bible.get("legends") or {}).values()]
    personality = (persona.get("personality_prompt") or "").strip() \
        or f"You are {persona['name']}, a wildlife narrator."
    return (f"{personality}\n\n"
            "World facts you may draw on:\n"
            + "\n".join(f"- {fact}" for fact in facts)
            + f"\n\n{LINE_RULES}")


# Rolling memory of the narrator's own recent lines (issue #28), fed back into
# every LLM prompt for variety (don't repeat your own phrasing) and continuity
# (running-show callbacks). 10 lines x ~40 tokens is ~400 extra prompt tokens
# -- trivial at the current model/timeout, so no config knob. A restart blanks
# the memory; the show has dead air far longer than a restart, so the
# in-process deque stays the honest simple design. The field journal window
# below (issue #58) is deliberately NOT this: the journal is the show's
# record for the dashboard, the deque is prompt seasoning -- seeding one from
# the other would couple what the audience sees to what the model is told.
MEMORY_LINES = 10

# The variety/continuity guidance travels WITH the memory block -- not in
# LINE_RULES -- so an empty memory degrades the prompt to exactly the
# pre-memory shape and the output contract stays untouched.
MEMORY_HEADER = (
    "Your most recent on-air lines (oldest first). Do not reuse their "
    "openings, imagery, or sentence structure -- each line should sound like "
    "a different moment of the same broadcast. You may reference earlier "
    "moments for continuity -- a returning individual, a running count, a "
    "callback -- when the timing makes it plausible:"
)


def memory_block(memory, now):
    """The "recently on air" prompt section from (ts, event_kind, text)
    entries, or "" when memory is empty. Ages are rendered with the
    human_duration() vocabulary so the model reasons about time gaps in the
    same units the show already speaks."""
    if not memory:
        return ""
    lines = [f"- [{human_duration(now - ts)} ago, {kind}] {text}"
             for ts, kind, text in memory]
    return MEMORY_HEADER + "\n" + "\n".join(lines)


# Broadcast-context memory (issue #80): what the OTHER voices said recently.
# Only a mention-listening narrator ever hears anything (it's the only one
# subscribed to narration/lines), and only IT gets this block -- which is what
# makes a mention follow-up context-aware instead of redundantly re-telling
# what the colleague already told the audience. Kept smaller than the own-lines
# memory: it's a cue sheet, not a transcript. Empty block = prompt
# byte-identical to today (the memory/weather degradation convention).
HEARD_LINES = 5

HEARD_HEADER = (
    "Recently on the broadcast, other voices (oldest first) -- your "
    "colleagues' latest on-air lines. The audience already heard these: "
    "don't repeat them, build on them or answer them when it suits the "
    "moment:"
)


def heard_block(heard, now):
    """The other-voices prompt section from (ts, narrator, text) entries, or
    "" when nothing has been heard. Same age vocabulary as the memory block."""
    if not heard:
        return ""
    lines = [f"- [{human_duration(now - ts)} ago] {who}: {text}"
             for ts, who, text in heard]
    return HEARD_HEADER + "\n" + "\n".join(lines)


# Like the memory guidance, the weather usage nudge travels WITH the weather
# paragraph, so a weatherless prompt reproduces the old shape exactly.
# Desk-tested against gemma3:12b: the bare sentence appended to the event
# summary was ignored 4 generations out of 4; labeled as conditions with this
# one-line nudge it was woven in (naturally) 4 out of 4.
WEATHER_HEADER = "Current conditions at the station:"
WEATHER_GUIDANCE = "Work the weather into your commentary when it adds color."


def build_user_prompt(event, bible, memory=(), now=None, weather=None, heard=(),
                      covering=False):
    """The full user prompt: optional memory block, optional other-voices
    block, the factual event summary, an optional covering note (issue #88),
    an optional current-conditions paragraph, then the cue. With no memory,
    nothing heard, not covering, and no fresh weather this is byte-identical
    to the pre-#26/#28 prompt."""
    now = time.time() if now is None else now
    parts = []
    for block in (memory_block(memory, now), heard_block(heard, now)):
        if block:
            parts.append(block)
    parts.append(event_summary(event, bible))
    if covering:
        parts.append(COVERING_NOTE)
    clause = weather_sentence(weather, now)
    if clause:
        parts.append(f"{WEATHER_HEADER} {clause} {WEATHER_GUIDANCE}")
    parts.append("Your on-air line:")
    return "\n\n".join(parts)


def sanitize_line(text):
    """LLM output -> one speakable line, or None if unusable. Collapses
    whitespace/newlines and strips the wrapping quotes and markdown bold the
    model sometimes adds despite the rules."""
    line = " ".join((text or "").replace("**", "").split())
    line = line.strip('"“” ')
    return line or None


class Ollama:
    """One blocking, non-streaming completion per call (the bus contract is
    one JSON object per spoken line, and the dashboard TTS speaks whole lines
    -- nothing downstream could use a token stream). complete() is the generic
    core, shared with the weather post's Willard segment (issue #45);
    narrate() is the narrator-shaped wrapper. Both return None instead of
    raising, because a dead LLM must degrade (templates, a skipped segment),
    not kill the caller."""

    def __init__(self, host, port, model):
        self.url = f"http://{host}:{port}/api/generate"
        self.model = model

    def complete(self, system, prompt, num_predict=120, temperature=0.9):
        """Raw model text for one system+prompt pair, or None on any failure.
        num_predict caps a model that ignores the length rules; the
        temperature keeps repeat prompts from producing repeat prose."""
        payload = {
            "model": self.model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            # Driveway events can be an hour apart; without keep_alive every
            # call would pay the cold model load (~2x latency, desk-tested).
            "keep_alive": "30m",
            "options": {"num_predict": num_predict, "temperature": temperature},
        }
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as resp:
                reply = json.load(resp)
        except Exception as e:   # any failure at all -> the caller's fallback
            print(f"[ollama] {type(e).__name__}: {e} -- generation skipped")
            return None
        return reply.get("response")

    def narrate(self, persona, bible, event, memory=(), now=None, weather=None,
                heard=(), covering=False):
        return sanitize_line(self.complete(
            build_system_prompt(persona, bible),
            build_user_prompt(event, bible, memory, now, weather, heard,
                              covering)))


def generate(persona, bible, event, rng, ollama=None,
             memory=(), now=None, weather=None, last_template=None, heard=(),
             covering=False):
    """Tier 2 (LLM) when an Ollama client is provided and healthy; Tier-1
    templates otherwise. The one place that decides which prose tier speaks.
    Memory, weather, and heard lines ride the LLM prompt only -- templates
    stay v1, except that a caller-held last_template dict (kind -> index) buys
    the cheap variety: re-roll once when the same template comes up twice
    running."""
    if ollama is not None:
        line = ollama.narrate(persona, bible, event,
                              memory=memory, now=now, weather=weather,
                              heard=heard, covering=covering)
        if line:
            return line
    kind = event.get("kind")
    templates = TEMPLATES.get(kind, FALLBACK_TEMPLATES)
    if last_template is None:
        last_template = {}
    pick = rng.randrange(len(templates))
    if len(templates) > 1 and pick == last_template.get(kind):
        pick = rng.randrange(len(templates))   # re-roll once; a repeat may stand
    last_template[kind] = pick
    return templates[pick].format(**template_fields(event, bible))


# --- The field journal window (issue #58) ------------------------------------
# The last JOURNAL_LINES published lines, oldest first -- the dashboard's Field
# Journal, made durable the way the weather post made its 48h window durable:
# a bounded window persisted to a flat JSON file (a restart doesn't blank it)
# and published RETAINED, republished whole on every new line, so a fresh
# browser tab gets the journal straight from the broker. 50 matches the
# dashboard's JOURNAL_LIMIT; ~50 lines x ~200 chars is ~10 KB, trivial to
# republish whole. Deliberately a flat file, not SQLite -- bounded, rewritten
# whole, refetchable (the weather_history.json argument).

JOURNAL_LINES = 50
DEFAULT_JOURNAL_PATH = "narration_journal.json"


def load_journal(path):
    """The persisted journal window, or a fresh one. Missing and corrupt files
    both mean "start over" -- the journal is the show's recent record, not a
    record of record (SQLite keeps the events), so failing loudly would cost
    more than it protects."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = json.load(f)
        return lines if isinstance(lines, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_journal(path, window):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(window, f)


def roll_journal(window, line, limit=JOURNAL_LINES):
    """A new window with line appended, oldest lines dropped past the limit."""
    return (window + [line])[-limit:]


# --- The editor's desk (issue #74) --------------------------------------------
# Tracker id churn on a steady feeding scene floods driveway/events with
# phantom departure+arrival pairs (a stationary squirrel flickers out of
# detection, its track dies, a "new" one is born in place). The daemon
# debounces at its end, but the narrator must survive a noisy bus on its own:
# every event that reached the talent was a potential LLM call, and a churning
# scene turned into a nonstop generation storm on the GPU. The Editor is the
# narrator-side defense, and it is deliberately SCENE-level state (one desk
# for the whole show), not per-narrator pacing -- personas keep their own
# cooldown/interest knobs downstream, unchanged.

# A species-count change must hold this long before it is news. Phantom churn
# reads as departure-then-arrival within a few seconds; a change that survives
# 20s is an animal that actually came or went.
STABLE_AFTER_S = 20.0
# The hard floor between narrations -- the LLM-call ceiling (issue #74's
# "GPU fan" number: 30s = at most 2 generations/min no matter what the bus
# does). Independent of the persona cooldown on purpose: personas tune
# character, this caps cost.
NARRATE_MIN_INTERVAL_S = 30.0
# How often main() drains the queue and asks the Editor for news. Coarse is
# fine -- nothing downstream is latency-sensitive at under a second.
EDITOR_TICK_S = 1.0


def env_float(name, default):
    """An optional float env knob: unset/blank means the default; a malformed
    value fails at startup (the bus.py convention -- never run half-configured
    while looking healthy)."""
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


class Editor:
    """Decides what is NEWS. Sits between driveway/events and the talent:

    - HYSTERESIS: an arrival/departure only becomes narratable once the
      species' reported count has held at its new value for `stable_s`. A
      departure immediately undone by an arrival of the same species (the id-
      churn signature) settles back to the old count and cancels -- it was the
      same animal, and the explicit outcome is SILENCE, not a hedged line.
    - RATE LIMIT + BURST COLLAPSE: poll() hands out at most one event per
      `min_interval_s`. Everything that stabilized or happened during the
      cooldown collapses into ONE scene_update event summarizing where the
      scene ended up. Dropped play-by-play is fine -- SQLite has the record.

    Pure logic, injected clock (`now` everywhere), no I/O -- covered by
    test_narrator.py. ingest() takes events as they arrive; poll() is called
    on a timer and returns the one event worth handing the talent, or None.
    """

    def __init__(self, stable_s=STABLE_AFTER_S, min_interval_s=NARRATE_MIN_INTERVAL_S):
        self.stable_s = stable_s
        self.min_interval_s = min_interval_s
        # species -> {"stable": count last narrated (or settled), "latest":
        # count most recently reported, "since": when latest first diverged
        # from stable (None = no change pending), "event": the event that
        # last moved latest (kept so a lone ripened change narrates as the
        # real arrival/departure, duration_s and all)}
        self.species = {}
        # Non-presence moments (crowd_snapshot, clip_recorded, future kinds),
        # keyed by kind so a burst of the same moment keeps only the latest.
        self.moments = {}
        self.last_narrated = float("-inf")

    def ingest(self, event, now):
        details = event.get("details") or {}
        kind = event.get("kind")
        presence = kind in ("arrival", "departure") \
            and "species" in details and "count" in details
        if not presence:
            self.moments[kind] = event
            return
        st = self.species.setdefault(
            details["species"], {"stable": 0, "latest": 0, "since": None, "event": None})
        st["latest"] = details["count"]
        st["event"] = event
        if st["latest"] == st["stable"]:
            st["since"] = None   # wobbled back -- same animal, never news
        elif st["since"] is None:
            st["since"] = now    # a fresh divergence starts the clock; a
                                 # pending one keeps its original clock (the
                                 # count has differed from stable throughout)

    def _ripe(self, now):
        return [st for st in self.species.values()
                if st["since"] is not None and now - st["since"] >= self.stable_s]

    def poll(self, now):
        """The one event worth narrating right now, or None. Advances the
        stable counts (and clears the moment shelf) only when a slot is
        actually spent, so nothing stabilizes into the void."""
        if now - self.last_narrated < self.min_interval_s:
            return None
        ripe = self._ripe(now)
        moments = list(self.moments.values())
        if not ripe and not moments:
            return None
        self.last_narrated = now
        for st in ripe:
            st["stable"] = st["latest"]
            st["since"] = None
        self.moments.clear()
        if len(ripe) == 1 and not moments:
            return ripe[0]["event"]        # one real change: tell it straight
        if not ripe and len(moments) == 1:
            return moments[0]
        # Burst collapse: several things landed during the cooldown. One
        # summary of where the scene ended up -- full snapshot, including
        # species that didn't change, because "the scene" is the story.
        counts = {sp: st["stable"] for sp, st in self.species.items()
                  if st["stable"] > 0}
        return {"ts": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                "kind": "scene_update", "details": {"counts": counts}}


class Narrator:
    """One voice: a persona, the shared bible, and its own pacing state."""

    def __init__(self, persona, bible, rng=None, ollama=None):
        self.persona = persona
        self.bible = bible
        self.rng = rng or random.Random()
        self.ollama = ollama
        self.last_spoke_at = float("-inf")   # never spoken -> first event is fair game
        self.latest_weather = None           # last weather/current payload seen
        self.memory = deque(maxlen=MEMORY_LINES)   # (ts, event_kind, text) spoken
        self.last_template = {}              # kind -> last Tier-1 template index
        # Broadcast-context memory (issue #80): (ts, narrator, text) heard from
        # OTHER narrators. A plain tuple rebuilt on every hear() -- assignment
        # is atomic, so the network thread can write while the pacing loop
        # reads (the latest_weather pattern), no lock, no torn iteration.
        self.heard = ()
        # Colleague presence (issue #88): mqtt_id -> latest retained status
        # string, written from the network thread (per-key dict assignment is
        # atomic, same contract as latest_weather). Only consulted when the
        # persona carries defer_events_to.
        self.colleagues = {}

    def wants(self, event, now):
        if defers_event(event, self.persona, self.colleagues):
            return False   # the field has it -- deferring beats all pacing math
        return worth_speaking(event, self.persona, now, self.last_spoke_at)

    def hear(self, line, now):
        """A colleague's narration/lines payload -> broadcast-context memory.
        Callers filter out this narrator's own lines; everything a colleague
        says is context, mention or not."""
        entry = (now, line.get("narrator", "a colleague"), line.get("text", ""))
        self.heard = (self.heard + (entry,))[-HEARD_LINES:]

    def speak(self, event, now):
        self.last_spoke_at = now
        text = generate(self.persona, self.bible, event, self.rng, self.ollama,
                        memory=self.memory, now=now, weather=self.latest_weather,
                        last_template=self.last_template, heard=self.heard,
                        covering=covering_field(event, self.persona,
                                                self.colleagues))
        line = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "narrator": self.persona["name"],
            "mqtt_id": self.persona["mqtt_id"],
            "voice": self.persona["tts_voice"],
            "event_kind": event.get("kind"),
            "text": text,
        }
        # Memory records exactly what goes on the bus -- captured here, where
        # the payload is built, so the two can never disagree. A template
        # fallback line counts (it went on air); a failed LLM call leaves no
        # trace beyond the fallback line that replaced it.
        self.memory.append((now, line["event_kind"], text))
        return line


class Producer:
    """The producer/orchestrator, embedded in this process for v1 (promoting it
    to its own daemon is a future issue -- as is what narrators do when it's
    absent). Deliberately shaped around a ROSTER (a set of voices), not "the
    narrator": cast() picks who speaks to an event, and with one voice the rule
    is simply "I speak to everything that clears my pacing gate". Solo-beat vs
    banter-beat casting slots in here later without a rewrite."""

    def __init__(self, roster):
        self.roster = list(roster)

    def cast(self, event, now):
        return [n for n in self.roster if n.wants(event, now)]


def main():
    ap = argparse.ArgumentParser(description="Merle scene narrator v1")
    ap.add_argument("--persona", required=True, help="persona YAML, e.g. personas/marlin.yaml")
    ap.add_argument("--bible", default="character_bible.yaml",
                    help="shared world-facts YAML (default: character_bible.yaml)")
    args = ap.parse_args()

    persona = load_persona(args.persona)
    bible = load_yaml(args.bible)

    ollama = None
    addr = ollama_address()
    if addr:
        model = os.environ.get("MERLE_OLLAMA_MODEL", "").strip() or OLLAMA_DEFAULT_MODEL
        ollama = Ollama(*addr, model)
        print(f"[{persona['name']}] narration tier: LLM "
              f"({model} via {addr[0]}:{addr[1]}, templates on failure)")
    else:
        print(f"[{persona['name']}] narration tier: templates (MERLE_OLLAMA not set)")

    producer = Producer([Narrator(persona, bible, ollama=ollama)])
    editor = Editor(
        stable_s=env_float("MERLE_NARRATE_STABLE_S", STABLE_AFTER_S),
        min_interval_s=env_float("MERLE_NARRATE_MIN_INTERVAL_S", NARRATE_MIN_INTERVAL_S))
    print(f"[{persona['name']}] editor's desk: changes must hold "
          f"{editor.stable_s:g}s; at most one narration per "
          f"{editor.min_interval_s:g}s")

    journal_path = os.environ.get("MERLE_NARRATION_JOURNAL", "").strip() \
        or DEFAULT_JOURNAL_PATH
    journal = load_journal(journal_path)
    if journal:
        print(f"[{persona['name']}] journal restored: {len(journal)} lines "
              f"from {journal_path}")

    status_topic = bus.narrator_status_topic(persona["mqtt_id"])
    journal_topic = bus.narration_journal_topic(persona["mqtt_id"])
    answers_to = persona.get("answers_to") or ()
    if answers_to:
        print(f"[{persona['name']}] answering to: {', '.join(answers_to)} "
              f"(listening to {bus.NARRATION_TOPIC})")
    defer_to = persona.get("defer_events_to") or ()
    if defer_to:
        print(f"[{persona['name']}] deferring the play-by-play to: "
              f"{', '.join(defer_to)} (watching {bus.NARRATOR_STATUS_WILDCARD})")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=persona["mqtt_id"])
    # Last Will: if this process dies without saying goodbye, the broker flips
    # the retained status to offline for us -- presence lives on the bus.
    client.will_set(status_topic, "offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties):
        c.publish(status_topic, "online", retain=True)
        # The journal file, not the broker, is the window's source of truth:
        # republishing on every (re)connect heals the retained copy after a
        # broker restart (Mosquitto only keeps retained state across restarts
        # when persistence is configured -- don't depend on it).
        c.publish(journal_topic,
                  json.dumps({"lines": journal}), retain=True)
        c.subscribe(bus.EVENTS_TOPIC)
        # Retained, so the latest report arrives immediately on (re)connect.
        # Weather is prompt context only -- never a reason to speak.
        c.subscribe(bus.WEATHER_CURRENT_TOPIC)
        # Mention listening is opt-in (issue #80): a persona without
        # answers_to never subscribes. With two listeners on the air (issue
        # #88) the no-ping-pong guarantee moved into colleague_mention():
        # a follow-up line never triggers a follow-up.
        if answers_to:
            c.subscribe(bus.NARRATION_TOPIC)
        # Deferring narrators watch colleague presence (issue #88). Retained,
        # so the roster's lamps arrive the moment we subscribe.
        if defer_to:
            c.subscribe(bus.NARRATOR_STATUS_WILDCARD)
        print(f"[{persona['name']}] on the air, listening to {bus.EVENTS_TOPIC}")

    # on_message runs on paho's network thread and must stay instant (issue
    # #74): it only parses and enqueues. The pacing loop below -- the main
    # thread -- owns the Editor, the talent, and the (blocking) LLM call, so a
    # slow generation can never threaten the MQTT keepalive again. Events are
    # queued WITH their arrival time: hysteresis judges when a change was
    # reported, not when the loop got around to reading it.
    events = queue.Queue()

    def on_message(c, userdata, msg):
        # Presence first (issue #88): status payloads are raw strings, not
        # JSON (they predate the JSON topics), so they must not fall into the
        # parse below. Our own retained echo lands here too -- stored under
        # our own id, which no defer knob lists, so it's inert.
        colleague = bus.narrator_status_id(msg.topic)
        if colleague is not None:
            status = msg.payload.decode("utf-8", "replace")
            for narrator in producer.roster:
                narrator.colleagues[colleague] = status
            return
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return   # not our JSON, not our problem
        if msg.topic == bus.WEATHER_CURRENT_TOPIC:
            for narrator in producer.roster:
                narrator.latest_weather = payload
            return
        if msg.topic == bus.NARRATION_TOPIC:
            # A colleague's line (issue #80). Own lines are ignored outright
            # (loop safety); everything else feeds the broadcast-context
            # memory, and a line naming this narrator additionally mints a
            # colleague_mention event for the Editor's moments shelf -- so a
            # mention respects the narration rate limit like everything else.
            # Both steps are instant (parse + tuple rebuild), safe on paho's
            # network thread.
            if payload.get("mqtt_id") == persona["mqtt_id"] \
                    or not payload.get("text"):
                return
            now = time.time()
            for narrator in producer.roster:
                narrator.hear(payload, now)
            mention = colleague_mention(payload, persona)
            if mention:
                events.put((now, mention))
            return
        events.put((time.time(), payload))

    client.on_connect = on_connect
    client.on_message = on_message

    host, port = bus.broker_address()
    # connect() (not connect_async): a narrator with no bus has no job, so fail
    # loudly at launch. Once up, paho's loop thread auto-reconnects through
    # broker restarts (on_connect re-publishes presence and re-subscribes each
    # time).
    client.connect(host, port)
    client.loop_start()
    try:
        while True:
            time.sleep(EDITOR_TICK_S)
            while True:
                try:
                    arrived, event = events.get_nowait()
                except queue.Empty:
                    break
                editor.ingest(event, arrived)
            story = editor.poll(time.time())
            if story is None:
                continue   # the explicit no-output path: unstable or nothing new
            now = time.time()
            for narrator in producer.cast(story, now):
                line = narrator.speak(story, now)
                client.publish(bus.NARRATION_TOPIC, json.dumps(line))
                # The journal keeps exactly what went out on narration/lines --
                # file first, then the retained window, so a crash between the
                # two loses a broadcast, never the record.
                journal = roll_journal(journal, line)
                save_journal(journal_path, journal)
                client.publish(journal_topic,
                               json.dumps({"lines": journal}), retain=True)
                print(f"[{line['narrator']}] {line['text']}")
    except KeyboardInterrupt:
        # Graceful sign-off: publish offline ourselves (a clean disconnect
        # suppresses the Last Will -- that's for crashes).
        client.publish(status_topic, "offline", retain=True)
        client.loop_stop()
        client.disconnect()
        print(f"\n[{persona['name']}] signing off.")


if __name__ == "__main__":
    main()
