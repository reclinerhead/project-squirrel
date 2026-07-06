# =============================================================================
# project-squirrel -- narrator.py
#
# v1 of the scene narrator (issue #9): ONE voice, subscribed to the live event
# bus, publishing spoken-style observations. The point of v1 is PACING, not
# prose -- silence is most of the show, so the pacing gate matters more than
# the words, and the words are Tier-1 templates (Mad-Libs fill-ins). A future
# issue swaps an LLM into generate() and nothing else changes.
#
#   python narrator.py --persona personas/marlin.yaml
#
# Bus contract (topics in bus.py):
#   subscribes  driveway/events           the daemon's live event stream
#   publishes   narration/lines           {ts, narrator, voice, text, event_kind}
#   presence    narrators/<id>/status     "online"/"offline", retained; "offline"
#                                         is the MQTT Last Will, so a crash flips
#                                         the dashboard lamp without any cleanup
#
# The narrator never plays audio itself -- it publishes; a consumer (the MCC
# dashboard's TTS, someday a speaker on the porch) does the speaking.
#
# The producer/orchestrator is EMBEDDED here for now (Producer below), shaped
# around a roster even though the roster is one voice -- see the class docstring.
# =============================================================================

import argparse
import json
import random
import time
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
    return {
        "kind": event.get("kind", "something"),
        "species": details.get("species", "critter"),
        "duration": human_duration(details.get("duration_s", 0)),
        "total": details.get("total", "several"),
        "station": bible.get("station", "the driveway"),
        "seed_pile": bible.get("seed_pile", "the seed pile"),
        "big_chonk": (bible.get("legends") or {}).get("big_chonk", "Big Chonk"),
    }


def generate(persona, bible, event, rng):
    """Tier 1: template narration. This is the single swap point for the future
    LLM tier (persona["personality_prompt"] is already waiting for it)."""
    templates = TEMPLATES.get(event.get("kind"), FALLBACK_TEMPLATES)
    return rng.choice(templates).format(**template_fields(event, bible))


class Narrator:
    """One voice: a persona, the shared bible, and its own pacing state."""

    def __init__(self, persona, bible, rng=None):
        self.persona = persona
        self.bible = bible
        self.rng = rng or random.Random()
        self.last_spoke_at = float("-inf")   # never spoken -> first event is fair game

    def wants(self, event, now):
        return worth_speaking(event, self.persona, now, self.last_spoke_at)

    def speak(self, event, now):
        self.last_spoke_at = now
        return {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "narrator": self.persona["name"],
            "mqtt_id": self.persona["mqtt_id"],
            "voice": self.persona["tts_voice"],
            "event_kind": event.get("kind"),
            "text": generate(self.persona, self.bible, event, self.rng),
        }


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
    producer = Producer([Narrator(persona, bible)])

    status_topic = bus.narrator_status_topic(persona["mqtt_id"])
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=persona["mqtt_id"])
    # Last Will: if this process dies without saying goodbye, the broker flips
    # the retained status to offline for us -- presence lives on the bus.
    client.will_set(status_topic, "offline", retain=True)

    def on_connect(c, userdata, flags, reason_code, properties):
        c.publish(status_topic, "online", retain=True)
        c.subscribe(bus.EVENTS_TOPIC)
        print(f"[{persona['name']}] on the air, listening to {bus.EVENTS_TOPIC}")

    def on_message(c, userdata, msg):
        try:
            event = json.loads(msg.payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return   # not our JSON, not our problem
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
