"""Carve the highest-importance/load-bearing index into a budget-fitting core.

render_core stays < MEMORY_BUDGET_BYTES; render_full holds the complete index.
"""
from __future__ import annotations

from memd.store import index_line, is_archived

_CORE_HEADER = (
    "# MEMORY.md\n\n"
    "Carved core index (highest-value, load-bearing). Full index in MEMORY-full.md.\n\n"
)
_FULL_HEADER = "# MEMORY-full.md\n\nComplete note index (no byte budget).\n\n"


CORE_MIN_IMPORTANCE = 5  # core_only mode admits only notes at/above this


def _sort_key(note):
    # high importance first; then shorter lines (more facts fit); then stable slug
    return (-int(note.importance), len(index_line(note)), note.slug)


def select_core(notes: list, budget: int, *, core_only: bool = False) -> list:
    """Pick the notes for the always-loaded MEMORY.md.

    Default (budget-fill): greedily admit highest-priority notes while the
    rendered core stays < budget.
    core_only=True: admit ONLY notes with importance >= CORE_MIN_IMPORTANCE — a
    TINY always-on core (identity + behavioral rules + active safety traps);
    everything else is recalled on demand. Still budget-capped defensively.
    Notes archived by review (memd.forget) are never core.
    """
    ordered = sorted((n for n in notes if not is_archived(n)), key=_sort_key)
    chosen: list = []
    size = len(_CORE_HEADER.encode("utf-8"))
    for note in ordered:
        if core_only and int(note.importance) < CORE_MIN_IMPORTANCE:
            continue
        line_bytes = len((index_line(note) + "\n").encode("utf-8"))
        if size + line_bytes >= budget:
            continue  # skip this one; a shorter later line may still fit
        chosen.append(note)
        size += line_bytes
    return chosen


def render_core(notes: list) -> str:
    body = "".join(index_line(n) + "\n" for n in notes)
    return _CORE_HEADER + body


def render_full(notes: list) -> str:
    ordered = sorted(notes, key=lambda n: n.path)
    body = "".join(index_line(n) + "\n" for n in ordered)
    return _FULL_HEADER + body
