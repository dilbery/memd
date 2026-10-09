from pathlib import Path

from memd.cli_carve import main
from memd.config import MEMORY_BUDGET_BYTES


def _make_corpus(d: Path, n: int):
    for i in range(n):
        (d / f"project_note_{i}.md").write_text(
            f"---\ntitle: note {i}\nimportance: {(i % 5) + 1}\n---\n"
            f"# note {i}\n\n" + ("detail " * 40),
            encoding="utf-8",
        )


def test_carve_writes_both_files_core_under_budget(tmp_path):
    _make_corpus(tmp_path, 300)
    rc = main([str(tmp_path)])
    assert rc == 0
    core = (tmp_path / "MEMORY.md").read_bytes()
    full = (tmp_path / "MEMORY-full.md").read_text(encoding="utf-8")
    assert len(core) < MEMORY_BUDGET_BYTES
    assert full.count("\n- [") == 300  # full index keeps every note


def test_carve_is_idempotent(tmp_path):
    _make_corpus(tmp_path, 50)
    main([str(tmp_path)])
    first = (tmp_path / "MEMORY.md").read_bytes()
    main([str(tmp_path)])
    assert (tmp_path / "MEMORY.md").read_bytes() == first
