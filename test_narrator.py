# Tests for narrator.py -- the pure logic only (pacing gate, scoring, template
# narration, persona loading). The MQTT plumbing is I/O and is desk-tested
# against a real broker per the testing policy.

import random

import pytest

import narrator


PERSONA = {"name": "Test", "mqtt_id": "test", "tts_voice": "",
           "cooldown_seconds": 20.0, "chattiness": 0.9, "interest_threshold": 0.4}
BIBLE = {"station": "the driveway",
         "seed_pile": "the seed pile out by the maple stump",
         "legends": {"big_chonk": "Big Chonk"}}

ARRIVAL = {"ts": "2026-07-06T10:00:00", "kind": "arrival",
           "details": {"track_id": 7, "species": "chipmunk"}}
DEPARTURE = {"ts": "2026-07-06T10:01:00", "kind": "departure",
             "details": {"track_id": 7, "species": "chipmunk", "duration_s": 62.0}}
CROWD = {"ts": "2026-07-06T10:02:00", "kind": "crowd_snapshot",
         "details": {"total": 6, "counts": {"squirrel": 4, "turkey": 2}}}


# --- the pacing gate ---------------------------------------------------------

def test_interesting_event_clears_the_gate():
    assert narrator.worth_speaking(ARRIVAL, PERSONA, now=100.0, last_spoke_at=0.0)


def test_cooldown_blocks_even_a_great_event():
    assert not narrator.worth_speaking(CROWD, PERSONA, now=100.0, last_spoke_at=90.0)


def test_gate_reopens_after_cooldown():
    assert narrator.worth_speaking(CROWD, PERSONA, now=100.0, last_spoke_at=79.0)


def test_dull_event_is_beneath_notice():
    clip = {"kind": "clip_recorded", "details": {"path": "x.mp4"}}
    assert not narrator.worth_speaking(clip, PERSONA, now=100.0, last_spoke_at=0.0)


def test_low_chattiness_narrator_lets_arrivals_pass():
    quiet = {**PERSONA, "chattiness": 0.5}   # 0.7 * 0.5 = 0.35 < 0.4
    assert not narrator.worth_speaking(ARRIVAL, quiet, now=100.0, last_spoke_at=0.0)


def test_unknown_kind_scores_low_but_nonzero():
    assert narrator.score_event({"kind": "meteor_strike"}) == narrator.UNKNOWN_INTEREST


# --- template narration ------------------------------------------------------

def test_every_template_formats_cleanly():
    # No template may reference a field template_fields() doesn't provide, and
    # nothing unfilled may survive -- for every kind, against a details-free
    # event (worst case: all fields fall back).
    bare_fields = narrator.template_fields({"kind": "arrival"}, {})
    for kind, templates in narrator.TEMPLATES.items():
        for t in templates + narrator.FALLBACK_TEMPLATES:
            line = t.format(**bare_fields)
            assert "{" not in line and "}" not in line


def test_arrival_lines_name_the_species():
    rng = random.Random(0)
    for _ in range(10):
        assert "chipmunk" in narrator.generate(PERSONA, BIBLE, ARRIVAL, rng)


def test_departure_lines_carry_the_visit_duration():
    rng = random.Random(0)
    for _ in range(10):
        line = narrator.generate(PERSONA, BIBLE, DEPARTURE, rng)
        assert "about 62 seconds" in line


def test_bible_facts_reach_the_lines():
    # The seed-pile template must pull the bible's fact, not a hardcoded one.
    rng = random.Random(0)
    lines = {narrator.generate(PERSONA, BIBLE, ARRIVAL, rng) for _ in range(50)}
    assert any("maple stump" in line for line in lines)


def test_unknown_kind_falls_back_gracefully():
    rng = random.Random(0)
    line = narrator.generate(PERSONA, BIBLE, {"kind": "meteor_strike"}, rng)
    assert "meteor_strike" in line


def test_missing_details_never_crash_generation():
    rng = random.Random(0)
    for kind in list(narrator.TEMPLATES) + ["mystery"]:
        assert narrator.generate(PERSONA, {}, {"kind": kind}, rng)


# --- human_duration ----------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0.4, "a blink"),
    (45, "about 45 seconds"),
    (89, "about 89 seconds"),
    (130, "about 2 minutes"),
    (3600, "about 60 minutes"),
    (7200, "about 2.0 hours"),
])
def test_human_duration(seconds, expected):
    assert narrator.human_duration(seconds) == expected


# --- narrator + producer -----------------------------------------------------

def test_narrator_speaks_then_holds_its_tongue():
    n = narrator.Narrator(PERSONA, BIBLE, rng=random.Random(0))
    assert n.wants(ARRIVAL, now=100.0)
    line = n.speak(ARRIVAL, now=100.0)
    assert line["narrator"] == "Test"
    assert line["event_kind"] == "arrival"
    assert line["text"]
    # Cooldown now applies: the same narrator stays quiet...
    assert not n.wants(CROWD, now=105.0)
    # ...until it lapses.
    assert n.wants(CROWD, now=125.0)


def test_producer_casts_from_the_roster():
    n = narrator.Narrator(PERSONA, BIBLE, rng=random.Random(0))
    p = narrator.Producer([n])
    assert p.cast(ARRIVAL, now=100.0) == [n]
    n.speak(ARRIVAL, now=100.0)
    assert p.cast(ARRIVAL, now=101.0) == []   # everyone's cooling down -> silence


def test_load_persona_merges_defaults(tmp_path):
    f = tmp_path / "minimal.yaml"
    f.write_text("name: Ghost\nmqtt_id: ghost\n", encoding="utf-8")
    p = narrator.load_persona(str(f))
    assert p["cooldown_seconds"] == narrator.PERSONA_DEFAULTS["cooldown_seconds"]
    assert p["name"] == "Ghost"


def test_load_persona_requires_identity(tmp_path):
    f = tmp_path / "nameless.yaml"
    f.write_text("tts_voice: David\n", encoding="utf-8")
    with pytest.raises(ValueError, match="name"):
        narrator.load_persona(str(f))
