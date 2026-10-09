from memd.carve import select_core, render_core, render_full
from memd.config import MEMORY_BUDGET_BYTES
from memd.store import Note


def _mk(i, imp, desc_len):
    return Note(title=f"note {i}", slug=f"note-{i}", path=f"n{i}.md",
                body="x", importance=imp, description="d" * desc_len)


def test_core_render_stays_under_budget():
    # 300 fat notes; rendered core must be < the byte budget
    notes = [_mk(i, imp=(i % 5) + 1, desc_len=180) for i in range(300)]
    core = select_core(notes, MEMORY_BUDGET_BYTES)
    rendered = render_core(core)
    assert len(rendered.encode("utf-8")) < MEMORY_BUDGET_BYTES


def test_core_prefers_high_importance():
    notes = [_mk(i, imp=1, desc_len=180) for i in range(100)]
    notes.append(_mk(999, imp=5, desc_len=180))  # one critical note
    core = select_core(notes, MEMORY_BUDGET_BYTES)
    assert any(n.slug == "note-999" for n in core)


def test_full_render_includes_every_note_no_budget():
    notes = [_mk(i, imp=3, desc_len=180) for i in range(300)]
    full = render_full(notes)
    assert full.count("\n- [") == 300
    assert len(full.encode("utf-8")) > MEMORY_BUDGET_BYTES  # full is allowed to exceed


def test_empty_corpus_renders_header_only():
    assert render_core([]).startswith("# ")
    assert render_full([]).startswith("# ")
