import subprocess

import httpx
import pytest

import memd.recall as recall_mod
import memd.index as index_mod
from memd.config import Config
from memd.embed import CanaryError
from memd.recall import recall
from memd.index import open_db, build_index


def _fake_embed_corpus(texts, cfg):
    return [[float(len(t) % 5) + 1.0] * 768 for t in texts]


def _prep(config, monkeypatch):
    """Build a real index with a fake embedder, return nothing (db on disk)."""
    monkeypatch.setattr(index_mod, "embed", _fake_embed_corpus)
    db = open_db(config.db)
    build_index(db, config)
    db.close()


# ---------------------------------------------------------------------------
# Representative-corpus helpers (fix-group 2 read-robustness).
#
# The shared `git_clone`/`config` fixtures seed CLEAN <slug>.md files WITH an
# explicit `slug:` frontmatter key — which hides any bug where slug-from-title
# diverges from the underscore filename. A corpus migrated from older tooling is
# the opposite: underscore filenames (project_foo_bar.md) carrying LEGACY
# name:/description: frontmatter and NO slug: field, so the slug
# (= slugify(title)) never matches the filename stem. These helpers build that
# real shape so the new tests drive the genuine save()/recall() code path.
# ---------------------------------------------------------------------------

# underscore filename, legacy name:/description:, NO slug: field, H1 title.
_REAL_NOTE_A = """\
---
name: project-lemonade-vulkan-tuning
description: legacy note — lemonade vulkan tuning on gpuhost
type: project
metadata:
  node_type: memory
  source: hermes
---
# Lemonade Vulkan Tuning

Lemonade tuning on gpuhost uses the Vulkan backend with a larger physical
batch size as the sweet spot; the main model runs under speculative decoding.
"""

_REAL_NOTE_B = """\
---
name: project-trackr-docker-host
description: Trackr dev docker containers run on svcuser@10.10.1.11
type: project
metadata:
  node_type: memory
---
# Trackr Docker Host

All Trackr docker ops target 10.10.1.11 (vmhost), never the gpuhost box.
"""

# a CORE note (importance>=4) shaped like the real corpus: underscore filename,
# legacy frontmatter, importance set, NO slug: field.
_REAL_CORE = """\
---
name: feedback-repo-hosting-policy
description: ALL new repos go to Forgejo only, never GitHub
type: feedback
importance: 5
metadata:
  node_type: memory
---
# Repo Hosting Policy

ALL new repos go to Forgejo only (svcuser/ on 10.10.1.10); never GitHub.
"""


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _real_corpus_clone(tmp_path):
    """A git clone seeded with REAL-corpus-shaped notes (underscore filenames,
    legacy frontmatter, no slug: field)."""
    repo = tmp_path / "realclone"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@memd")
    _git(repo, "config", "user.name", "memd-test")
    for name, content in [
        ("project_lemonade_vulkan_tuning.md", _REAL_NOTE_A),
        ("project_trackr_docker_host.md", _REAL_NOTE_B),
        ("feedback_repos_to_forgejo.md", _REAL_CORE),
    ]:
        (repo / name).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed real-shaped notes")
    return repo


def _real_config(clone, tmp_path, monkeypatch):
    monkeypatch.setenv("MEMD_CLONE", str(clone))
    monkeypatch.setenv("MEMD_DB", str(tmp_path / "real-memd.db"))
    monkeypatch.setenv("MEMD_PROFILE", "amber")
    monkeypatch.setenv("MEMD_TOKEN", "test-token")
    return Config.from_env()


def test_core_set_always_prepended(config, monkeypatch):
    _prep(config, monkeypatch)
    # all arms down: embed None, rerank None
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    out = recall("anything", profile="amber", k=8, cfg=config)
    slugs = [n.slug for n in out]
    assert "repo-hosting-policy" in slugs  # importance 5 core note, never empty
    assert out  # never empty


def test_full_path_uses_rerank_order(config, monkeypatch):
    _prep(config, monkeypatch)
    monkeypatch.setattr(
        recall_mod, "embed_with_deadline", lambda *a, **k: [1.0] * 768
    )

    def fake_rerank(query, candidates, top_n, ms, *, cfg):
        # force vmhost note to the top regardless of vector/BM25 order
        ordered = sorted(
            candidates, key=lambda c: c["slug"] != "vmhost-proxmox-vm"
        )
        return ordered[:top_n]

    monkeypatch.setattr(recall_mod, "rerank", fake_rerank)
    out = recall("proxmox vm", profile="amber", k=8, cfg=config)
    non_core = [n for n in out if n.importance < 4]
    assert non_core and non_core[0].slug == "vmhost-proxmox-vm"


def test_rerank_down_embed_ok_uses_vector_order(config, monkeypatch):
    _prep(config, monkeypatch)
    monkeypatch.setattr(
        recall_mod, "embed_with_deadline", lambda *a, **k: [1.0] * 768
    )
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    out = recall("lemonade tuning", profile="amber", k=8, cfg=config)
    # vector arm preserved (not collapsed to BM25); still returns results
    assert any(n.slug == "gpuhost-inference-tuning" for n in out)


def test_embed_down_uses_bm25(config, monkeypatch):
    _prep(config, monkeypatch)
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    # a keyword unique to one note must still retrieve it via FTS5
    out = recall("Vulkan", profile="amber", k=8, cfg=config)
    assert any(n.slug == "gpuhost-inference-tuning" for n in out)


def test_never_returns_empty_when_all_down(config, monkeypatch):
    _prep(config, monkeypatch)
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    out = recall("zzz-nomatch-zzz", profile="amber", k=8, cfg=config)
    assert out  # core set is the floor


def test_stale_head_schedules_refresh_without_blocking(config, git_clone, monkeypatch):
    _prep(config, monkeypatch)
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)
    # Add a new note + commit so clone HEAD advances past index HEAD.
    (git_clone / "fresh-note.md").write_text(
        "---\ntitle: Fresh Note\nhost: gpuhost\nimportance: 2\n---\n"
        "unique-token-xyzzy here\n"
    )
    import subprocess
    subprocess.run(["git", "-C", str(git_clone), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(git_clone), "commit", "-q", "-m", "add fresh"],
        check=True,
    )
    scheduled = []
    monkeypatch.setattr(recall_mod, "request_refresh", lambda cfg: scheduled.append(cfg))
    monkeypatch.setattr(index_mod, "embed", lambda *a: pytest.fail("recall used bulk embedding"))
    out = recall("unique-token-xyzzy", profile="amber", k=8, cfg=config)
    assert scheduled == [config]
    assert any(n.slug == "repo-hosting-policy" for n in out)
    assert not any(n.slug == "fresh-note" for n in out)


# ---------------------------------------------------------------------------
# (2a) MUST-FIX: a stale-HEAD reindex whose embed() raises (backend down) must NOT
# blow recall() up. recall must degrade on the existing slightly-stale index
# and the normal ladder — never raise — because it must NEVER block a turn.
# Driven over the REAL-shaped corpus (underscore files, no slug: frontmatter).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boom",
    [
        httpx.ConnectError("embed backend refused"),
        httpx.ReadTimeout("embed backend slow"),
        CanaryError("embed dim 0 != 768"),
    ],
)
def test_stale_head_reindex_embed_failure_does_not_raise(
    tmp_path, monkeypatch, boom
):
    clone = _real_corpus_clone(tmp_path)
    cfg = _real_config(clone, tmp_path, monkeypatch)

    # Build the index ONCE at the current HEAD with a working fake embedder, so
    # there is a populated (but about-to-go-stale) index + core set on disk.
    monkeypatch.setattr(index_mod, "embed", _fake_embed_corpus)
    db = open_db(cfg.db)
    build_index(db, cfg)
    index_mod.set_head_in_index(db, _git(clone, "rev-parse", "HEAD"))
    db.close()

    # Advance the clone HEAD past the index HEAD so recall() takes the
    # stale-HEAD reindex branch on the read path.
    (clone / "project_new_fact.md").write_text(
        "---\nname: project-new-fact\ndescription: a freshly committed fact\n"
        "type: project\n---\n# New Fact\n\nsome newly committed body text here.\n"
    )
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "add new fact")

    # The embed backend is DOWN: the reindex embed call raises (ConnectError/Timeout/Canary).
    def boom_embed(texts, cfg):
        raise boom

    monkeypatch.setattr(index_mod, "embed", boom_embed)
    # Vector + rerank arms also down so we exercise the bare BM25/core ladder;
    # the whole point is recall must still return the core set, not raise.
    monkeypatch.setattr(recall_mod, "embed_with_deadline", lambda *a, **k: None)
    monkeypatch.setattr(recall_mod, "rerank", lambda *a, **k: None)

    # BEFORE the fix this RAISES (reindex re-raises the embed failure);
    # AFTER the fix recall swallows it and degrades on the stale index.
    out = recall("vulkan tuning", profile="amber", k=8, cfg=cfg)

    slugs = [n.slug for n in out]
    # core set (importance 5) is the floor — its slug is slugify(title), which on
    # this real-shaped corpus does NOT equal the underscore filename.
    assert "repo-hosting-policy" in slugs
    assert out  # never empty, never raised


# ---------------------------------------------------------------------------
# (2b) LOW: a malformed rerank response that repeats a candidate index yields a
# duplicate Note in the tail. recall must dedup the ranked slugs (first
# occurrence wins) so the same note never appears twice.
# ---------------------------------------------------------------------------


def test_rerank_duplicate_index_does_not_duplicate_note(
    tmp_path, monkeypatch
):
    clone = _real_corpus_clone(tmp_path)
    cfg = _real_config(clone, tmp_path, monkeypatch)
    monkeypatch.setattr(index_mod, "embed", _fake_embed_corpus)
    db = open_db(cfg.db)
    build_index(db, cfg)
    index_mod.set_head_in_index(db, _git(clone, "rev-parse", "HEAD"))
    db.close()

    monkeypatch.setattr(
        recall_mod, "embed_with_deadline", lambda *a, **k: [1.0] * 768
    )

    # A buggy rerank returns the SAME candidate twice (repeated index mapped
    # back to one slug). Real rerank() range-checks indices but can legitimately
    # repeat one if the upstream response duplicates it, so recall must dedup.
    def dup_rerank(query, candidates, top_n, ms, *, cfg):
        if not candidates:
            return []
        first = candidates[0]
        return [first, first]  # duplicate slug in the ranked tail

    monkeypatch.setattr(recall_mod, "rerank", dup_rerank)

    out = recall("trackr docker host", profile="amber", k=8, cfg=cfg)
    slugs = [n.slug for n in out]
    # no slug appears twice — the duplicate ranked entry was collapsed.
    assert len(slugs) == len(set(slugs)), f"duplicate notes returned: {slugs}"
