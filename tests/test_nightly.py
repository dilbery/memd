import pytest

from memd import nightly


def _fake(calls, codes=None, boom=()):
    codes = codes or {}

    def runner(step):
        def main(argv):
            calls.append((step, argv))
            if step in boom:
                raise RuntimeError("backend down")
            return codes.get(step, 0)
        return main
    return runner


def test_steps_parse_in_run_order_and_reject_unknown():
    assert nightly.parse_steps(None) == ("facts", "summarize", "health")
    assert nightly.parse_steps(" health, VERIFY ,facts") == ("facts", "verify", "health")
    with pytest.raises(ValueError, match="unknown step"):
        nightly.parse_steps("facts,reindex")


def test_flags_only_reach_steps_that_take_them(monkeypatch):
    monkeypatch.setenv("MEMD_LLM_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MEMD_LLM_MODEL", "m")
    calls = []
    nightly.run(nightly.ORDER, push=True, runner=_fake(calls))
    assert calls == [("facts", []), ("summarize", ["--push"]), ("verify", ["--push"]),
                     ("forget", ["--push"]), ("backup", []), ("drill", []), ("health", [])]
    calls.clear()
    nightly.run(nightly.ORDER, dry_run=True, push=True, runner=_fake(calls))
    assert calls == [("facts", ["--dry-run"]), ("summarize", ["--dry-run"]),
                     ("verify", ["--dry-run"]), ("forget", ["--dry-run"]), ("backup", []),
                     ("drill", []), ("health", [])]


def test_a_failing_step_does_not_stop_the_rest(monkeypatch):
    monkeypatch.setenv("MEMD_LLM_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MEMD_LLM_MODEL", "m")
    calls = []
    results = nightly.run(("facts", "summarize", "health"),
                          runner=_fake(calls, codes={"summarize": 1}, boom={"facts"}))
    assert [c[0] for c in calls] == ["facts", "summarize", "health"]
    assert [r["status"] for r in results] == ["failed", "failed", "ok"]
    assert "RuntimeError: backend down" in results[0]["detail"]


def test_summarize_is_skipped_without_a_model(monkeypatch):
    monkeypatch.delenv("MEMD_LLM_URL", raising=False)
    calls = []
    results = nightly.run(("facts", "summarize"), runner=_fake(calls))
    assert [c[0] for c in calls] == ["facts"]
    assert results[1]["status"] == "skipped" and "MEMD_LLM_URL" in results[1]["detail"]


def test_exit_status_and_env_steps(monkeypatch, capsys):
    monkeypatch.setattr(nightly, "_main", _fake([], codes={"health": 2}))
    monkeypatch.setenv("MEMD_NIGHTLY_STEPS", "health")
    assert nightly.main([]) == 1
    assert "health" in capsys.readouterr().out
    assert nightly.main(["--steps", "facts"]) == 0
    assert nightly.main(["--steps", "nope"]) == 2


@pytest.mark.parametrize("step", nightly.ORDER)
def test_every_step_cli_accepts_the_flags_it_is_given(step, monkeypatch, capsys):
    """The real CLIs must parse what the runner passes (they then stop: no store here)."""
    argv = (["--dry-run"] if step in nightly.DRY_RUN else []) + (["--push"] if step in nightly.PUSH else [])
    try:
        nightly._main(step)(argv)
    except SystemExit:
        pass
    except Exception:
        pass   # past argument parsing; failing on the absent store is fine here
    assert "unrecognized arguments" not in capsys.readouterr().err
