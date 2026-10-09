import subprocess
import sys

from memd.budget_lint import check_budget, main
from memd.config import MEMORY_BUDGET_BYTES


def test_check_budget_ok_under_limit(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text("x" * (MEMORY_BUDGET_BYTES - 1), encoding="utf-8")
    ok, size = check_budget(p, MEMORY_BUDGET_BYTES)
    assert ok is True
    assert size == MEMORY_BUDGET_BYTES - 1


def test_check_budget_fails_at_or_over_limit(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text("x" * MEMORY_BUDGET_BYTES, encoding="utf-8")
    ok, size = check_budget(p, MEMORY_BUDGET_BYTES)
    assert ok is False


def test_main_exits_nonzero_when_over(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text("x" * (MEMORY_BUDGET_BYTES + 100), encoding="utf-8")
    rc = main([str(p)])
    assert rc == 1


def test_main_exits_zero_when_under(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text("x" * 10, encoding="utf-8")
    rc = main([str(p)])
    assert rc == 0


def test_cli_entrypoint_returns_nonzero(tmp_path):
    p = tmp_path / "MEMORY.md"
    p.write_text("x" * (MEMORY_BUDGET_BYTES + 1), encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "memd.budget_lint", str(p)])
    assert r.returncode == 1
