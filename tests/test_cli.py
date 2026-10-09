import json

import memd.cli as cli
import memd.index as index_mod
from memd.store import Note


def _note(slug):
    return Note(
        slug=slug, path=f"{slug}.md", title=slug, profile="amber", host="gpuhost",
        importance=3, last_used=None, superseded_by=None, tags=[], grounding="ok",
        body="body", git_blob="x",
    )


def test_recall_cli_prints_json(config, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "recall", lambda query, profile, k, cfg: [_note("repo-hosting-policy")]
    )
    rc = cli.main(["recall", "forgejo", "--k", "3"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["notes"][0]["slug"] == "repo-hosting-policy"


def test_save_cli(config, monkeypatch, capsys):
    from memd.save import SaveResult
    monkeypatch.setattr(
        cli, "save",
        lambda fact, profile, cfg: SaveResult("new-fact", "created", "ok", False, "new-fact.md"),
    )
    rc = cli.main(["save", "--title", "New Fact", "--body", "hi", "--host", "gpuhost"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["slug"] == "new-fact"
    assert out["action"] == "created"


def test_doctor_ok(config, monkeypatch, capsys):
    # _probe_embed opens a real TCP connection to cfg.embed_url and swallows OSError,
    # so without this stub the test dialled the default local embed endpoint and its
    # result depended on whether an inference server happened to be running. Caught
    # by the netguard hermeticity ceiling.
    monkeypatch.setattr(cli, "_probe_embed", lambda url: None)
    monkeypatch.setattr(cli, "startup_canary", lambda cfg, fingerprint_path: None)
    monkeypatch.setattr(cli, "git_head_sha", lambda clone: "headsha")
    rc = cli.main(["doctor"])
    assert rc == 0
    assert "ok" in capsys.readouterr().out.lower()


def test_doctor_canary_failure_nonzero(config, monkeypatch, capsys):
    from memd.embed import CanaryError

    def boom(cfg, fingerprint_path):
        raise CanaryError("dim 512 != 768")

    monkeypatch.setattr(cli, "_probe_embed", lambda url: None)  # see test_doctor_ok
    monkeypatch.setattr(cli, "startup_canary", boom)
    rc = cli.main(["doctor"])
    assert rc == 1
    assert "768" in capsys.readouterr().out


def test_reindex_cli(config, monkeypatch, capsys):
    seen = {"n": 0}

    def fake_reindex(db, cfg):
        seen["n"] += 1
        return 7

    monkeypatch.setattr(cli, "reindex", fake_reindex)
    monkeypatch.setattr(cli, "git_head_sha", lambda clone: "h")
    monkeypatch.setattr(cli, "set_head_in_index", lambda db, head: None)
    rc = cli.main(["reindex"])
    assert rc == 0
    assert seen["n"] == 1
    assert "7" in capsys.readouterr().out
