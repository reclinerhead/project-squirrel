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
           "details": {"species": "chipmunk", "count": 1}}
DEPARTURE = {"ts": "2026-07-06T10:01:00", "kind": "departure",
             "details": {"species": "chipmunk", "count": 0, "duration_s": 62.0}}
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


def test_departure_without_duration_never_says_a_blink():
    # "One of them left" departures (count still > 0) carry no duration; the
    # line must not claim the visit lasted "a blink".
    partial = {"kind": "departure", "details": {"species": "squirrel", "count": 1}}
    rng = random.Random(0)
    for _ in range(10):
        line = narrator.generate(PERSONA, BIBLE, partial, rng)
        assert "a blink" not in line


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


# --- the LLM tier (pure logic only; the HTTP call is desk-tested live) --------

def test_ollama_address_unset_means_tier_off(monkeypatch):
    monkeypatch.delenv("MERLE_OLLAMA", raising=False)
    assert narrator.ollama_address() is None


def test_ollama_address_default_port(monkeypatch):
    monkeypatch.setenv("MERLE_OLLAMA", "192.168.1.79")
    assert narrator.ollama_address() == ("192.168.1.79", 11434)


def test_ollama_address_explicit_port(monkeypatch):
    monkeypatch.setenv("MERLE_OLLAMA", "192.168.1.79:8080")
    assert narrator.ollama_address() == ("192.168.1.79", 8080)


def test_event_summary_states_the_facts():
    assert "chipmunk" in narrator.event_summary(ARRIVAL, BIBLE)
    assert "about 62 seconds" in narrator.event_summary(DEPARTURE, BIBLE)
    assert "6" in narrator.event_summary(CROWD, BIBLE)
    assert "meteor_strike" in narrator.event_summary({"kind": "meteor_strike"}, BIBLE)


def test_event_summary_carries_species_lore():
    bible = {**BIBLE, "species_lore": {"chipmunk": "always up to something"}}
    assert "always up to something" in narrator.event_summary(ARRIVAL, bible)
    # ...but only for the species that actually showed up.
    assert "always up to something" not in narrator.event_summary(CROWD, bible)


def test_system_prompt_has_persona_canon_and_rules():
    persona = {**PERSONA, "personality_prompt": "You are the TEST VOICE."}
    prompt = narrator.build_system_prompt(persona, BIBLE)
    assert "TEST VOICE" in prompt                # persona
    assert "maple stump" in prompt               # bible canon
    assert "Big Chonk" in prompt                 # legends
    assert narrator.LINE_RULES in prompt         # output rules


def test_system_prompt_survives_a_promptless_persona():
    assert "Test" in narrator.build_system_prompt(PERSONA, {})


@pytest.mark.parametrize("raw,expected", [
    ('"And there he goes!"', "And there he goes!"),
    ("A line\nwith   newlines\nand runs of spaces", "A line with newlines and runs of spaces"),
    ("**Majestic.** Truly.", "Majestic. Truly."),
    ("", None),
    (None, None),
    ('"  "', None),
])
def test_sanitize_line(raw, expected):
    assert narrator.sanitize_line(raw) == expected


class StubOllama:
    def __init__(self, line):
        self.line = line
        self.context = None   # kwargs of the last narrate() call

    def narrate(self, persona, bible, event, **context):
        # Snapshot the memory CONTENTS at call time -- the deque keeps living
        # after the call, and what matters is what the prompt actually saw.
        self.context = {**context, "memory": list(context.get("memory") or ())}
        return self.line


def test_generate_prefers_the_llm_line():
    rng = random.Random(0)
    line = narrator.generate(PERSONA, BIBLE, ARRIVAL, rng, ollama=StubOllama("Astounding!"))
    assert line == "Astounding!"


def test_generate_falls_back_when_the_llm_fails():
    # A narrate() returning None (unreachable, timeout, garbage) must yield a
    # normal template line -- the show never goes silent.
    rng = random.Random(0)
    line = narrator.generate(PERSONA, BIBLE, ARRIVAL, rng, ollama=StubOllama(None))
    assert "chipmunk" in line


def test_narrator_speaks_the_llm_line_on_the_bus_shape():
    n = narrator.Narrator(PERSONA, BIBLE, rng=random.Random(0),
                          ollama=StubOllama("Magnificent beast."))
    line = n.speak(ARRIVAL, now=100.0)
    assert line["text"] == "Magnificent beast."
    assert line["event_kind"] == "arrival"       # bus contract unchanged


# --- weather context (issue #26) ----------------------------------------------

NOW = 1_752_000_000.0   # any fixed epoch; weather ts values are relative to it

def weather_report(**overrides):
    """A fresh weather/current payload (5 minutes old against NOW)."""
    return {"ts": NOW - 300, "temp_f": 78.2, "wind_mph": 5.0,
            "condition": "Clouds", "description": "overcast clouds",
            **overrides}


def test_weather_sentence_states_the_conditions():
    sentence = narrator.weather_sentence(weather_report(), now=NOW)
    assert sentence == "It is 78F with a light breeze under overcast clouds."


@pytest.mark.parametrize("mph,phrase", [
    (0.4, "calm air"),
    (5.0, "a light breeze"),
    (12.0, "a steady breeze"),
    (20.0, "a strong wind"),
    (40.0, "a howling wind"),
])
def test_weather_sentence_speaks_wind_in_words(mph, phrase):
    assert phrase in narrator.weather_sentence(weather_report(wind_mph=mph), now=NOW)


def test_weather_sentence_survives_missing_fields():
    # Wind and description are optional; temperature is the price of entry.
    assert narrator.weather_sentence(
        weather_report(wind_mph=None, description=None), now=NOW) == "It is 78F."
    assert narrator.weather_sentence(weather_report(temp_f=None), now=NOW) is None


def test_weather_sentence_staleness_cutoff():
    # A report exactly at the threshold still speaks; one past it is silence.
    at = weather_report(ts=NOW - narrator.WEATHER_STALE_S)
    past = weather_report(ts=NOW - narrator.WEATHER_STALE_S - 1)
    assert narrator.weather_sentence(at, now=NOW) is not None
    assert narrator.weather_sentence(past, now=NOW) is None
    assert narrator.weather_sentence(weather_report(ts=None), now=NOW) is None
    assert narrator.weather_sentence(None, now=NOW) is None


def test_prompt_carries_fresh_weather_as_conditions_paragraph():
    # The weather rides its own labeled paragraph between the event summary
    # and the cue, guidance attached -- a bare sentence buried in the summary
    # was context the model ignored (desk-tested).
    prompt = narrator.build_user_prompt(ARRIVAL, BIBLE, weather=weather_report(), now=NOW)
    assert ("Current conditions at the station: It is 78F with a light breeze "
            "under overcast clouds. " + narrator.WEATHER_GUIDANCE) in prompt
    assert prompt.index("has just arrived") < prompt.index("78F")
    assert prompt.endswith("Your on-air line:")


def test_prompt_without_weather_is_byte_identical():
    # No weather service, or a stale report: exactly the weatherless prompt.
    plain = narrator.build_user_prompt(ARRIVAL, BIBLE, now=NOW)
    stale = weather_report(ts=NOW - narrator.WEATHER_STALE_S - 1)
    assert narrator.build_user_prompt(ARRIVAL, BIBLE, weather=None, now=NOW) == plain
    assert narrator.build_user_prompt(ARRIVAL, BIBLE, weather=stale, now=NOW) == plain
    assert "conditions" not in plain


# --- recent-lines memory (issue #28) -------------------------------------------

def test_memory_caps_and_evicts_oldest():
    n = narrator.Narrator(PERSONA, BIBLE, rng=random.Random(0))
    times = [100.0 * i for i in range(1, 13)]   # 12 spoken lines, cooldown apart
    for t in times:
        n.speak(ARRIVAL, now=t)
    assert len(n.memory) == narrator.MEMORY_LINES == 10
    assert [ts for ts, _, _ in n.memory] == times[2:]   # oldest two evicted


def test_speak_records_exactly_what_it_publishes():
    n = narrator.Narrator(PERSONA, BIBLE, rng=random.Random(0),
                          ollama=StubOllama("A fine specimen."))
    line = n.speak(ARRIVAL, now=100.0)
    assert list(n.memory) == [(100.0, line["event_kind"], line["text"])]


def test_prompt_with_memory_carries_lines_and_ages():
    memory = [(NOW - 1200, "arrival", "Here in the dappled light..."),
              (NOW - 120, "arrival", "Ah, if these grey squirrels could speak...")]
    prompt = narrator.build_user_prompt(ARRIVAL, BIBLE, memory=memory, now=NOW)
    assert "- [about 20 minutes ago, arrival] Here in the dappled light..." in prompt
    assert "- [about 2 minutes ago, arrival] Ah, if these grey squirrels" in prompt
    # Oldest first, above the event summary, guidance riding the block header.
    assert prompt.index("dappled") < prompt.index("grey squirrels") \
        < prompt.index("has just arrived")
    assert "Do not reuse" in prompt
    assert prompt.endswith("Your on-air line:")


def test_prompt_with_empty_memory_is_byte_identical_to_today():
    prompt = narrator.build_user_prompt(ARRIVAL, BIBLE, memory=(), now=NOW)
    assert prompt == f"{narrator.event_summary(ARRIVAL, BIBLE)}\n\nYour on-air line:"


def test_narrate_receives_memory_and_weather():
    # The wiring: speak() hands its own memory and the cached weather report
    # through generate() to the LLM call.
    stub = StubOllama("Magnificent.")
    n = narrator.Narrator(PERSONA, BIBLE, rng=random.Random(0), ollama=stub)
    n.latest_weather = weather_report()
    n.speak(ARRIVAL, now=100.0)
    first_memory = list(n.memory)
    n.speak(ARRIVAL, now=200.0)
    assert stub.context["weather"] == weather_report()
    assert stub.context["now"] == 200.0
    assert list(stub.context["memory"]) == first_memory   # memory BEFORE this line


def test_llm_failure_leaves_only_the_spoken_fallback_in_memory():
    # The failed call itself must not pollute memory -- but the template line
    # that replaced it DID go on air, so it counts.
    n = narrator.Narrator(PERSONA, BIBLE, rng=random.Random(0),
                          ollama=StubOllama(None))
    line = n.speak(ARRIVAL, now=100.0)
    assert "chipmunk" in line["text"]   # template fallback spoke
    assert list(n.memory) == [(100.0, "arrival", line["text"])]


class ScriptedRng:
    """randrange() plays back a script -- rigs the template picker."""
    def __init__(self, picks):
        self.picks = list(picks)

    def randrange(self, n):
        return self.picks.pop(0)


def test_template_reroll_dodges_an_immediate_repeat():
    last = {}
    first = narrator.generate(PERSONA, BIBLE, ARRIVAL, ScriptedRng([0]),
                              last_template=last)
    again = narrator.generate(PERSONA, BIBLE, ARRIVAL, ScriptedRng([0, 1]),
                              last_template=last)
    assert first != again          # the 0 re-rolled to 1
    assert last["arrival"] == 1


def test_template_reroll_lets_a_second_repeat_stand():
    # One re-roll only: if the dice insist, the repeat airs.
    last = {"arrival": 0}
    line = narrator.generate(PERSONA, BIBLE, ARRIVAL, ScriptedRng([0, 0]),
                             last_template=last)
    assert line == narrator.TEMPLATES["arrival"][0].format(
        **narrator.template_fields(ARRIVAL, BIBLE))


def test_template_reroll_skips_single_template_kinds():
    # A one-template kind never re-rolls (ScriptedRng would raise on a second
    # draw). The fallback tuple has exactly one entry.
    last = {"meteor_strike": 0}
    event = {"kind": "meteor_strike"}
    line = narrator.generate(PERSONA, BIBLE, event, ScriptedRng([0]),
                             last_template=last)
    assert "meteor_strike" in line


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


# --- the field journal window (issue #58) -------------------------------------

def journal_line(i):
    return {"ts": f"2026-07-06T10:00:{i:02d}", "narrator": "Test", "voice": "",
            "text": f"line {i}", "event_kind": "arrival"}


def test_roll_journal_appends_in_order():
    window = narrator.roll_journal([journal_line(0)], journal_line(1))
    assert [l["text"] for l in window] == ["line 0", "line 1"]


def test_roll_journal_caps_at_the_limit_dropping_oldest():
    window = []
    for i in range(narrator.JOURNAL_LINES + 3):
        window = narrator.roll_journal(window, journal_line(i))
    assert len(window) == narrator.JOURNAL_LINES
    assert window[0]["text"] == "line 3"      # the three oldest fell off
    assert window[-1]["text"] == f"line {narrator.JOURNAL_LINES + 2}"


def test_journal_round_trips_through_the_file(tmp_path):
    path = str(tmp_path / "journal.json")
    window = [journal_line(i) for i in range(3)]
    narrator.save_journal(path, window)
    assert narrator.load_journal(path) == window


def test_missing_journal_file_starts_clean(tmp_path):
    assert narrator.load_journal(str(tmp_path / "nope.json")) == []


def test_corrupt_journal_file_starts_clean(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text("{not json", encoding="utf-8")
    assert narrator.load_journal(str(path)) == []


def test_non_list_journal_file_starts_clean(tmp_path):
    path = tmp_path / "journal.json"
    path.write_text('{"lines": []}', encoding="utf-8")   # the BUS shape, not the file shape
    assert narrator.load_journal(str(path)) == []
