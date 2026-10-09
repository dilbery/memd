import subprocess

import memd.save as save_mod
import memd.index as index_mod
from memd.save import save_lint, save, SaveResult
from memd.index import open_db, build_index
from memd.store import Note, read_note


def _checker(present):
    return lambda kind, target: target in present


def test_new_note_gets_slug_and_grounding_gpuhost():
    fact = {"title": "New Desk Fact", "body": "run `pkgaudit`", "host": "gpuhost"}
    res = save_lint(fact, [], host_checker=_checker({"pkgaudit"}))
    assert isinstance(res, SaveResult)
    assert res.note.slug == "new-desk-fact"
    assert res.note.grounding == "ok"
    assert res.action == "new"
    assert res.upsert_target is None
    assert res.flagged_for_review is False


def test_gpuhost_missing_command_flags_unverified_local_but_still_writes():
    fact = {"title": "Bad Fact", "body": "use `parux`", "host": "gpuhost"}
    res = save_lint(fact, [], host_checker=_checker(set()))
    assert res.note.grounding == "unverified-local"
    assert res.action == "new"            # ADVISORY: it still writes
    assert res.flagged_for_review is True


def test_remote_fact_is_unverified_remote_and_never_flagged():
    fact = {"title": "Vmhost Reset", "body": "`docker` exec on the dev host",
            "host": "vmhost"}
    called = {"n": 0}

    def spy(kind, target):
        called["n"] += 1
        return False

    res = save_lint(fact, [], host_checker=spy)
    assert res.note.grounding == "unverified-remote"
    assert res.flagged_for_review is False
    assert called["n"] == 0               # no local check on a remote fact


def test_strong_dup_upserts_existing():
    existing = [Note(title="Trackr Docker Host", slug="trackr-docker-host",
                     path="a.md",
                     body="Trackr Docker containers on 10.10.1.11 svcuser "
                          "prometheus loki alloy SSH svcuser@10.10.1.11")]
    fact = {"title": "Trackr containers host",
            "body": "Trackr Docker containers on 10.10.1.11 svcuser prometheus "
                    "loki alloy SSH svcuser@10.10.1.11 always use 10.10.1.11",
            "host": "vmhost"}
    res = save_lint(fact, existing, host_checker=_checker(set()))
    assert res.action == "upsert"
    assert res.upsert_target == "trackr-docker-host"


def test_host_defaults_to_local_gpuhost_when_unspecified(monkeypatch):
    monkeypatch.setenv("MEMD_LOCAL_HOST", "gpuhost")  # hermetic: not this machine's hostname
    fact = {"title": "Unscoped fact", "body": "`pkgaudit` here"}  # no host key
    res = save_lint(fact, [], host_checker=_checker({"pkgaudit"}))
    assert res.note.host == "gpuhost"    # write-path default = local host
    assert res.note.grounding == "ok"


# ---------------------------------------------------------------------------
# Task 10: the memd-service save() write path (slug/dedup-upsert, host-aware
# grounding, conflict->supersede, dedicated-clone commit). Uses the real temp
# git clone; push is monkeypatched out so no network is touched.
# ---------------------------------------------------------------------------


def _no_push(clone):
    return None  # never reach the network in tests


def _fake_embed(texts, cfg):
    return [[1.0] * 768 for _ in texts]


def _git(clone, *args):
    return subprocess.run(
        ["git", "-C", str(clone), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def test_save_new_note_commits(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    db = open_db(config.db)
    build_index(db, config)
    db.close()
    res = save(
        {"title": "Brand New Fact", "body": "something fresh", "host": "vmhost"},
        profile="amber", cfg=config,
    )
    assert isinstance(res, SaveResult)
    assert res.slug == "brand-new-fact"
    assert res.action == "created"
    n = read_note(config.clone, "brand-new-fact")
    assert n is not None and n.host == "vmhost"
    # a commit landed in the dedicated clone
    log = _git(config.clone, "log", "--oneline")
    assert "brand-new-fact" in log or "Brand New Fact" in log


def test_save_strong_match_upserts(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    db = open_db(config.db)
    build_index(db, config)
    db.close()
    # body overlaps strongly with the seeded tuning note -> upsert, not new file.
    res = save(
        {"title": "gpuhost inference tuning",
         "body": "Inference tuning batch size 1024 Vulkan updated value",
         "host": "gpuhost"},
        profile="amber", cfg=config,
    )
    assert res.slug == "gpuhost-inference-tuning"
    assert res.action == "updated"
    n = read_note(config.clone, "gpuhost-inference-tuning")
    assert "updated value" in n.body


def test_grounding_gpuhost_present_command(config, monkeypatch):
    monkeypatch.setenv("MEMD_LOCAL_HOST", "gpuhost")  # hermetic: not this machine's hostname
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    monkeypatch.setattr(save_mod.shutil, "which", lambda c: "/usr/bin/" + c)
    db = open_db(config.db); build_index(db, config); db.close()
    res = save(
        {"title": "Use systemctl Here", "body": "run `systemctl status nginx`",
         "host": "gpuhost"},
        profile="amber", cfg=config,
    )
    n = read_note(config.clone, "use-systemctl-here")
    assert n.grounding == "ok"


def test_grounding_gpuhost_missing_command_flags(config, monkeypatch):
    monkeypatch.setenv("MEMD_LOCAL_HOST", "gpuhost")  # hermetic: not this machine's hostname
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    monkeypatch.setattr(save_mod.shutil, "which", lambda c: None)
    monkeypatch.setattr(save_mod.os.path, "exists", lambda p: False)
    db = open_db(config.db); build_index(db, config); db.close()
    res = save(
        {"title": "Bogus Command Note", "body": "run `frobnicate --hard`",
         "host": "gpuhost"},
        profile="amber", cfg=config,
    )
    n = read_note(config.clone, "bogus-command-note")
    assert n.grounding == "unverified-local"
    assert res.flagged_for_review is True
    # ALWAYS writes despite failed grounding
    assert n is not None


def test_grounding_remote_host_never_local_checks(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)

    def _boom(_):
        raise AssertionError("local check ran for a non-gpuhost host")

    monkeypatch.setattr(save_mod.shutil, "which", _boom)
    db = open_db(config.db); build_index(db, config); db.close()
    res = save(
        {"title": "Vmhost Remote Fact", "body": "run `whatever` on vmhost",
         "host": "vmhost"},
        profile="amber", cfg=config,
    )
    n = read_note(config.clone, "vmhost-remote-fact")
    assert n.grounding == "unverified-remote"


def test_conflict_supersedes_old_keeps_it(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _fake_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    db = open_db(config.db); build_index(db, config); db.close()
    res = save(
        {"title": "vmhost Proxmox VM",
         "body": "CONTRADICTS: onboot is now ON for VM 210",
         "host": "vmhost", "conflict": True},
        profile="amber", cfg=config,
    )
    # new note written under a fresh slug
    assert res.action == "superseded"
    new = read_note(config.clone, res.slug)
    assert new is not None
    # old note retained AND marked superseded_by the new slug
    old = read_note(config.clone, "vmhost-proxmox-vm")
    assert old is not None
    assert old.superseded_by == res.slug
    assert res.flagged_for_review is True


def _axis_embed(texts, cfg):
    # The tuning note points along axis 0, every other note along its own axis.
    return [[1.0 if i == (0 if "Vulkan" in t else 1 + n % 5) else 0.0 for i in range(768)]
            for n, t in enumerate(texts)]


def _near(axis0):
    import math
    return [axis0, math.sqrt(1 - axis0 ** 2)] + [0.0] * 766


def test_save_names_semantic_near_duplicates_without_retiring_anything(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _axis_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    db = open_db(config.db)
    build_index(db, config)
    db.close()
    monkeypatch.setattr(save_mod, "embed_with_deadline", lambda body, **kw: _near(0.98))
    res = save({"title": "Inference sweet spot", "body": "the GPU runs best at micro-batch 1024",
                "host": "vmhost"}, profile="amber", cfg=config)
    assert res.action == "created"                      # a suggestion never blocks or merges
    assert res.related[0] == "gpuhost-inference-tuning"
    assert any("Near-duplicate of gpuhost-inference-tuning" in w for w in res.warnings)
    assert read_note(config.clone, "gpuhost-inference-tuning").superseded_by is None


def test_save_related_threshold_and_embed_outage(config, monkeypatch):
    monkeypatch.setattr(index_mod, "embed", _axis_embed)
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    db = open_db(config.db)
    build_index(db, config)
    db.close()
    monkeypatch.setattr(save_mod, "embed_with_deadline", lambda body, **kw: _near(0.94))
    res = save({"title": "A related fact", "body": "something about batching", "host": "vmhost"},
               profile="amber", cfg=config)
    assert res.related == ["gpuhost-inference-tuning"]
    assert not any("Near-duplicate" in w for w in res.warnings)
    monkeypatch.setattr(save_mod, "embed_with_deadline", lambda body, **kw: None)
    res = save({"title": "Unrelated fact", "body": "the cat sat on the mat", "host": "vmhost"},
               profile="amber", cfg=config)
    assert res.saved and res.related == []
