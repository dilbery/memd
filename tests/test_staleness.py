import datetime

from memd.staleness import as_of, is_stale, label, normalize_volatility
from memd.store import dump_note, parse_text
from memd.normalize import normalize_fact

TODAY = datetime.date(2026, 9, 27)


def test_as_of_prefers_verified_then_observed_then_last_title_date():
    assert as_of({"verified_at": "2026-09-01", "observed_at": "2026-08-01"}) == datetime.date(2026, 9, 1)
    assert as_of({"observed_at": datetime.datetime(2026, 8, 1, 9)}) == datetime.date(2026, 8, 1)
    assert as_of({"title": "moved 2026-07-01, fixed 2026-07-09"}) == datetime.date(2026, 7, 9)
    assert as_of({"verified_at": "garbage", "slug": "x-2026-13-40", "title": "t"}) is None


def test_only_state_and_volatile_notes_go_stale():
    old = {"slug": "x-2026-08-01"}
    assert not is_stale(old, TODAY)
    assert is_stale({**old, "volatility": "state"}, TODAY)
    assert not is_stale({**old, "volatility": "durable"}, TODAY)
    assert not is_stale({"slug": "x-2026-09-20", "volatility": "volatile"}, TODAY)
    assert is_stale({"slug": "x-2026-09-19", "volatility": "ephemeral"}, TODAY)
    assert "57 days ago" in label({**old, "volatility": "current"}, TODAY)


def test_volatility_round_trips_and_bad_values_are_dropped():
    assert normalize_volatility(" Temporary ") == "volatile"
    assert normalize_volatility("sometimes") is None
    assert normalize_fact({"body": "b", "volatility": "STATE"})["volatility"] == "state"
    assert "volatility" not in normalize_fact({"body": "b", "volatility": "sometimes"})
    note = parse_text("---\ntitle: T\nvolatility: state\n---\nbody\n", path="t.md")
    assert note.volatility == "state" and "volatility" not in note.metadata
    assert "volatility: state" in dump_note(note)
    assert "volatility" not in dump_note(parse_text("---\ntitle: T\n---\nbody\n", path="t.md"))
