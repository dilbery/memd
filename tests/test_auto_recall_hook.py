import json

import httpx
import pytest
import respx

import memd.hooks.auto_recall as ar
from memd.config import DEFAULT_EMBED_URL, DEFAULT_RERANK_URL


class _Note:
    def __init__(self, slug, body):
        self.slug = slug
        self.body = body

    def to_dict(self):
        return {"slug": self.slug, "body": self.body}


def test_auto_recall_uses_small_fixed_top_n(monkeypatch):
    seen = {}

    def fake_recall(query, profile="amber", k=8, *, cfg=None, include_core=True):
        seen["k"] = k
        seen["query"] = query
        return [_Note(f"s{i}", f"body {i}") for i in range(20)]

    monkeypatch.setattr(ar, "_core_recall", fake_recall)
    out = ar.build_context({"prompt": "how is gpu thrash fixed"})
    # additive cap: at most AUTO_TOP_N notes injected
    assert seen["k"] == ar.AUTO_TOP_N
    block = out["hookSpecificOutput"]["additionalContext"]
    assert block.count("### ") <= ar.AUTO_TOP_N


def test_auto_recall_injection_is_size_bounded(monkeypatch):
    big = "x" * 50000

    def fake_recall(query, profile="amber", k=8, *, cfg=None, include_core=True):
        return [_Note(f"s{i}", big) for i in range(ar.AUTO_TOP_N)]

    monkeypatch.setattr(ar, "_core_recall", fake_recall)
    out = ar.build_context({"prompt": "anything"})
    block = out["hookSpecificOutput"]["additionalContext"]
    assert len(block) <= ar.MAX_INJECT_CHARS


def test_auto_recall_degrades_to_grep_when_core_raises(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("index locked / memd down")

    (tmp_path / "n.md").write_text(
        "---\ntitle: T\nslug: n\n---\nthrash recovery procedure\n", encoding="utf-8"
    )
    monkeypatch.setattr(ar, "_core_recall", boom)
    monkeypatch.setenv("MEMD_FALLBACK_CHECKOUT", str(tmp_path))
    out = ar.build_context({"prompt": "thrash"})
    block = out["hookSpecificOutput"]["additionalContext"]
    assert "thrash recovery" in block


def test_auto_recall_never_blocks_and_returns_neutral_on_total_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(ar, "_core_recall", boom)
    # No fallback checkout configured -> grep returns nothing -> neutral, no crash
    monkeypatch.delenv("MEMD_FALLBACK_CHECKOUT", raising=False)
    out = ar.build_context({"prompt": "anything"})
    assert out["hookSpecificOutput"]["additionalContext"] == ""


def test_main_reads_stdin_and_writes_json(monkeypatch, capsys):
    def fake_recall(query, profile="amber", k=8, *, cfg=None, include_core=True):
        return [_Note("s1", "recalled body")]

    monkeypatch.setattr(ar, "_core_recall", fake_recall)
    monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps({"prompt": "q"})))
    rc = ar.main()
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "recalled body" in out["hookSpecificOutput"]["additionalContext"]


class _StdinStub:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


# ---------------------------------------------------------------------------
# REAL-path hook test (NO core-seam monkeypatch). The tests above stub
# ar._core_recall with a fake exposing .to_dict() and ignoring `cfg`, which
# masked the same two bugs as the MCP path:
#   (a) the real Note had no to_dict(), and
#   (b) build_context called _core_recall WITHOUT the keyword-only `cfg`.
# This drives the REAL recall() against the `config` fixture's temp clone+db,
# faking ONLY the embedding and rerank HTTP endpoints. Fails before, passes
# after.
# ---------------------------------------------------------------------------

# Derived from the config defaults on purpose: these were once hardcoded to an
# embedding server address and silently went stale when the default endpoint
# moved, so respx stopped intercepting and the test asserted on a real call.
_EMBED_URL = f"{DEFAULT_EMBED_URL}/v1/embeddings"
_RERANK_URL = f"{DEFAULT_RERANK_URL}/api/v1/reranking"


def _embed_response(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    texts = payload["input"]
    if isinstance(texts, str):
        texts = [texts]
    data = [{"embedding": [float(len(t) % 7) + 1.0] * 768} for t in texts]
    return httpx.Response(200, json={"data": data})


@respx.mock
def test_auto_recall_real_pipeline_injects_core(config, monkeypatch):
    respx.post(_EMBED_URL).mock(side_effect=_embed_response)
    respx.post(_RERANK_URL).mock(return_value=httpx.Response(503))  # fall to BM25

    out = ar.build_context({"prompt": "Forgejo repos policy"})
    block = out["hookSpecificOutput"]["additionalContext"]
    # real recall() returned real Notes; .to_dict() serialized them and the
    # core-set floor body made it into the injected block.
    assert "Forgejo" in block


@respx.mock
def test_auto_recall_real_pipeline_excludes_superseded(config, git_clone, monkeypatch):
    """FIX GROUP 3(b): a superseded note must never be auto-injected via the hook."""
    import subprocess

    respx.post(_EMBED_URL).mock(side_effect=_embed_response)
    respx.post(_RERANK_URL).mock(return_value=httpx.Response(503))  # fall to BM25

    (git_clone / "stale-hint.md").write_text(
        "---\ntitle: Stale Hint\nslug: stale-hint\nprofile: amber\nhost: gpuhost\n"
        "importance: 2\nsuperseded_by: fresh-hint\ntags: []\ngrounding: ok\n---\n"
        "qqstaletoken outdated guidance that should never be injected\n"
    )
    subprocess.run(["git", "-C", str(git_clone), "add", "-A"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(git_clone), "commit", "-q", "-m", "stale"],
                   check=True, capture_output=True, text=True)

    from memd.index import open_db, build_index
    db = open_db(config.db)
    build_index(db, config)
    db.close()

    out = ar.build_context({"prompt": "qqstaletoken outdated guidance"})
    block = out["hookSpecificOutput"]["additionalContext"]
    assert "qqstaletoken" not in block, "superseded note auto-injected via the hook"
