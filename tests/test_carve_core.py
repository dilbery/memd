"""core_only carve: a TINY always-on core (importance >= CORE_MIN) with everything
else recalled on demand — the design's 'stop loading the whole index' lever."""
from memd.carve import CORE_MIN_IMPORTANCE, select_core
from memd.store import Note


def _n(slug, imp):
    return Note(title=slug, slug=slug, path=slug + ".md", body="body", importance=imp, description="d")


def test_core_only_admits_only_high_importance():
    notes = [_n("rule", 5), _n("identity", 5), _n("proj", 3), _n("ops", 4), _n("ref", 3)]
    core = select_core(notes, 24985, core_only=True)
    assert {n.slug for n in core} == {"rule", "identity"}  # only importance >= 5
    assert CORE_MIN_IMPORTANCE == 5


def test_budget_fill_default_unchanged():
    notes = [_n("a", 5), _n("b", 3), _n("c", 3)]
    core = select_core(notes, 24985, core_only=False)
    assert len(core) == 3  # budget-fill still admits all when they fit
