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
# weather/current report adds one dry factual sentence to the event summary,
# and a rolling memory of the narrator's own recent lines gives the model
# variety ("don't repeat yourself") and continuity (running-show callbacks).
#
#   python narrator.py --persona personas/marlin.yaml
#
# Bus contract (topics in bus.py):
#   subscribes  driveway/events           the daemon's live event stream
#   subscribes  weather/current           retained latest conditions -> prompt
#                                         context (LLM tier only, never a
#                                         speaking trigger)
#   publishes   narration/lines           {ts, narrator, voice, text, event_kind}
#   presence    narrators/<id>/status     "online"/"offline", retained; "offline"
#                                         is the MQTT Last Will, so a crash flips
#                                         the dashboard lamp without any cleanup
#
# LLM tier config (env, following the MERLE_MQTT convention):
#   MERLE_OLLAMA        Ollama "host" or "host:port" (port defaults to 11434).
#                       UNSET = LLM tier off; templates carry the show.
#   MERLE_OLLAMA_MODEL  model name (default: qwen2.5:14b)
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
import random
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
}

# How inherently remark-worthy each event kind is (0..1). Scaled by the
# persona's chattiness and compared to its interest_threshold -- so the same
# event can be worth a line to one narrator and beneath another's notice.
INTEREST = {
    "crowd_snapshot": 0.9,
    "arrival": 0.7,
    "departure": 0.5,
    "clip_recorded": 0.2,
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


def human_duration(seconds):
    """Visit lengths as a narrator would say them, not as floats."""
    if seconds < 1:
        return "a blink"
    if seconds < 90:
        return f"about {round(seconds)} seconds"
    if seconds < 5400:
        return f"about {round(seconds / 60)} minutes"
    return f"about {seconds / 3600:.1f} hours"


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
        "station": bible.get("station", "the driveway"),
        "seed_pile": bible.get("seed_pile", "the seed pile"),
        "big_chonk": (bible.get("legends") or {}).get("big_chonk", "Big Chonk"),
    }


# --- Tier 2: LLM narration via Ollama ----------------------------------------

OLLAMA_DEFAULT_PORT = 11434
OLLAMA_DEFAULT_MODEL = "gemma3:12b"
# Generation blocks the paho network loop (on_message), so the timeout must
# stay under the MQTT keepalive (60s). Events arriving mid-generation queue up
# and mostly fall to the cooldown gate afterwards -- which is the pacing we
# wanted anyway. Desk-tested: ~8s warm, ~15s with a cold model load, so 30s
# gives cold starts headroom without risking the bus connection.
OLLAMA_TIMEOUT_S = 30

# Output rules live in code, not the persona file, so every persona gets them
# and persona files stay pure character.
LINE_RULES = (
    "Deliver exactly ONE on-air line of one to three sentences. Spoken words "
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


def event_summary(event, bible, weather=None, now=None):
    """A factual one-line account of what just happened -- the LLM's raw
    material, deliberately dry so all the flavor comes from the persona.
    This is the hook for extra bus context: facts (like the weather sentence,
    issue #26) get appended here and the prompt shape doesn't change."""
    f = template_fields(event, bible)
    kind = event.get("kind")
    if kind == "arrival":
        summary = f"A {f['species']} has just arrived at {f['station']}."
    elif kind == "departure":
        summary = f"The {f['species']} has just left after a visit of {f['duration']}."
    elif kind == "crowd_snapshot":
        summary = f"There are now {f['total']} animals out on the pavement at once."
    elif kind == "clip_recorded":
        summary = "A video clip of the recent activity was just saved to the archive."
    else:
        summary = f"Something just happened out there (event: {f['kind']})."
    species = (event.get("details") or {}).get("species")
    lore = (bible.get("species_lore") or {}).get(species)
    if lore:
        summary += f" Local lore about this species: {lore}."
    clause = weather_sentence(weather, time.time() if now is None else now)
    if clause:
        summary += f" {clause}"
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
# the memory; the show has dead air far longer than a restart, and MQTT
# retains only one message per topic, so the in-process deque is the honest
# simple design (no persistence, no bus-history mechanism).
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


def build_user_prompt(event, bible, memory=(), now=None, weather=None):
    """The full user prompt: optional memory block, then the factual event
    summary (with its optional weather sentence), then the cue. With no
    memory and no weather this is byte-identical to the pre-#26/#28 prompt."""
    now = time.time() if now is None else now
    block = memory_block(memory, now)
    summary = event_summary(event, bible, weather=weather, now=now)
    body = f"{block}\n\n{summary}" if block else summary
    return f"{body}\n\nYour on-air line:"


def sanitize_line(text):
    """LLM output -> one speakable line, or None if unusable. Collapses
    whitespace/newlines and strips the wrapping quotes and markdown bold the
    model sometimes adds despite the rules."""
    line = " ".join((text or "").replace("**", "").split())
    line = line.strip('"“” ')
    return line or None


class Ollama:
    """Tier-2 narration: one blocking, non-streaming completion per line
    (the bus contract is one JSON object per spoken line, and the dashboard
    TTS speaks whole lines -- nothing downstream could use a token stream).
    narrate() returns a clean line or None; it never raises, because a dead
    LLM must degrade to templates, not kill the narrator."""

    def __init__(self, host, port, model):
        self.url = f"http://{host}:{port}/api/generate"
        self.model = model

    def narrate(self, persona, bible, event, memory=(), now=None, weather=None):
        payload = {
            "model": self.model,
            "system": build_system_prompt(persona, bible),
            "prompt": build_user_prompt(event, bible, memory, now, weather),
            "stream": False,
            # Driveway events can be an hour apart; without keep_alive every
            # line would pay the cold model load (~2x latency, desk-tested).
            "keep_alive": "30m",
            # num_predict caps a model that ignores the length rule; the
            # temperature keeps repeat events from producing repeat lines.
            "options": {"num_predict": 120, "temperature": 0.9},
        }
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_S) as resp:
                reply = json.load(resp)
        except Exception as e:   # any failure at all -> the template tier
            print(f"[ollama] {type(e).__name__}: {e} -- falling back to template")
            return None
        return sanitize_line(reply.get("response"))


def generate(persona, bible, event, rng, ollama=None,
             memory=(), now=None, weather=None, last_template=None):
    """Tier 2 (LLM) when an Ollama client is provided and healthy; Tier-1
    templates otherwise. The one place that decides which prose tier speaks.
    Memory and weather ride the LLM prompt only -- templates stay v1, except
    that a caller-held last_template dict (kind -> index) buys the cheap
    variety: re-roll once when the same template comes up twice running."""
    if ollama is not None:
        line = ollama.narrate(persona, bible, event,
                              memory=memory, now=now, weather=weather)
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

    def wants(self, event, now):
        return worth_speaking(event, self.persona, now, self.last_spoke_at)

    def speak(self, event, now):
        self.last_spoke_at = now
        text = generate(self.persona, self.bible, event, self.rng, self.ollama,
                        memory=self.memory, now=now, weather=self.latest_weather,
                        last_template=self.last_template)
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

    status_topic = bus.narrator_status_topic(persona["mqtt_id"])
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=persona["mqtt_id"])
    # Last Will: if this process dies without saying goodbye, the broker flips
    # the retained status to offline for us -- presence lives on the bus.
    client.will_set(status_topic, "offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties):
        c.publish(status_topic, "online", retain=True)
        c.subscribe(bus.EVENTS_TOPIC)
        # Retained, so the latest report arrives immediately on (re)connect.
        # Weather is prompt context only -- never a reason to speak.
        c.subscribe(bus.WEATHER_CURRENT_TOPIC)
        print(f"[{persona['name']}] on the air, listening to {bus.EVENTS_TOPIC}")

    def on_message(c, userdata, msg):
        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return   # not our JSON, not our problem
        if msg.topic == bus.WEATHER_CURRENT_TOPIC:
            for narrator in producer.roster:
                narrator.latest_weather = payload
            return
        event = payload
        now = time.time()
        for narrator in producer.cast(event, now):
            line = narrator.speak(event, now)
            c.publish(bus.NARRATION_TOPIC, json.dumps(line))
            print(f"[{line['narrator']}] {line['text']}")

    client.on_connect = on_connect
    client.on_message = on_message

    host, port = bus.broker_address()
    # connect() (not connect_async): a narrator with no bus has no job, so fail
    # loudly at launch. Once up, loop_forever() auto-reconnects through broker
    # restarts (on_connect re-publishes presence and re-subscribes each time).
    client.connect(host, port)
    try:
        client.loop_forever()
    except KeyboardInterrupt:
        # Graceful sign-off: publish offline ourselves (a clean disconnect
        # suppresses the Last Will -- that's for crashes).
        client.publish(status_topic, "offline", retain=True)
        client.disconnect()
        print(f"\n[{persona['name']}] signing off.")


if __name__ == "__main__":
    main()
