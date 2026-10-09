import json

from memd.cli_sweep import run_sweep, main
from memd.store import parse_note


def test_sweep_reports_dedup_and_grounding(corpus_dir):
    report = run_sweep(corpus_dir, write=False,
                       host_checker=lambda kind, target: False)
    # the two trackr-docker-host fixture notes are a duplicate pair (BM25, distinct slugs)
    dup_slugs = {s for pair in report["duplicates"] for s in pair}
    assert "trackr-docker-host-10-10-1-11" in dup_slugs
    assert "project-trackr-docker-host-10-10-1-11" in dup_slugs
    # vmhost login-reset note backfills to vmhost -> unverified-remote
    by_path = {r["path"]: r for r in report["notes"]}
    reset = by_path["project_trackr_login_reset.md"]
    assert reset["host"] == "vmhost"
    assert reset["grounding"] == "unverified-remote"


def test_sweep_dry_run_does_not_modify_files(corpus_dir):
    before = (corpus_dir / "project_trackr_login_reset.md").read_text()
    run_sweep(corpus_dir, write=False, host_checker=lambda k, t: False)
    after = (corpus_dir / "project_trackr_login_reset.md").read_text()
    assert before == after


def test_sweep_write_backfills_host_frontmatter(corpus_dir):
    run_sweep(corpus_dir, write=True, host_checker=lambda k, t: False)
    note = parse_note(corpus_dir / "project_trackr_login_reset.md")
    assert note.host == "vmhost"           # host backfilled and persisted
    assert note.grounding == "unverified-remote"


def test_main_dry_run_exits_zero_and_prints_json(corpus_dir, capsys):
    rc = main([str(corpus_dir), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "duplicates" in data and "notes" in data
