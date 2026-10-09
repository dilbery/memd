"""REAL-path MCP wiring tests (NO core-seam monkeypatch).

The other suite (test_mcp.py) monkeypatches mcp._core_recall / mcp._core_save
with fakes that happen to expose .to_dict() and ignore `cfg`. That masked two
shipped bugs:

  (a) the real Note / SaveResult had no to_dict() -> AttributeError in mcp.py,
  (b) mcp.call_tool invoked _core_recall/_core_save WITHOUT the required
      keyword-only `cfg` -> TypeError.

These tests drive the REAL recall()/save() through mcp.call_tool against a
temp git clone + sqlite db (the `config` fixture sets MEMD_CLONE / MEMD_DB /
MEMD_PROFILE / MEMD_TOKEN, so Config.from_env() inside mcp resolves to them).
Only the embedding and reranking HTTP endpoints are faked via respx; the core
seam is untouched. Each test fails before the fix and
passes after.
"""
from memd.config import DEFAULT_EMBED_URL, DEFAULT_RERANK_PATH, DEFAULT_RERANK_URL
import asyncio
import json

import httpx
import respx

import memd.index as index_mod
import memd.mcp as mcp
import memd.save as save_mod

# Derived from the config defaults so they cannot go stale: a hard-coded URL
# silently stops intercepting when the default backend moves.
EMBED_URL = f"{DEFAULT_EMBED_URL}/v1/embeddings"
RERANK_URL = f"{DEFAULT_RERANK_URL.rstrip('/')}{DEFAULT_RERANK_PATH}"


def _embed_response(request: httpx.Request) -> httpx.Response:
    """Deterministic 768-dim embeddings, one per input text."""
    payload = json.loads(request.content)
    texts = payload["input"]
    if isinstance(texts, str):
        texts = [texts]
    data = [{"embedding": [float(len(t) % 7) + 1.0] * 768} for t in texts]
    return httpx.Response(200, json={"data": data})


def _no_push(clone):
    return None  # never reach the network on the save() git path


@respx.mock
def test_mcp_recall_drives_real_core_and_serializes(config, monkeypatch):
    from memd.refresh import ensure_lexical
    ensure_lexical(config)
    # Fake ONLY the embeddings + rerank HTTP. No _core_recall stub.
    respx.post(EMBED_URL).mock(side_effect=_embed_response)
    # Let rerank return None so the pipeline falls down to BM25 order (still real).
    respx.post(RERANK_URL).mock(return_value=httpx.Response(503))

    out = asyncio.run(mcp.call_tool("recall", {"query": "Forgejo repos", "k": 5}))
    text = out.content[0].text
    # core set floor is always present, and survives rendering.
    assert "repo-hosting-policy" in text

    # render() reads importance/description/body off each dict, so the serialized
    # rows must still be the full real Note contract, not a fake subset. Assert
    # that on the rows themselves -- the rendered block no longer exposes them.
    notes = mcp._core_recall("Forgejo repos", profile="amber", k=5,
                             cfg=mcp._cfg_for("amber"))
    first = notes[0].to_dict()
    for key in ("slug", "title", "body", "profile", "host", "importance", "grounding"):
        assert key in first, f"missing {key} in serialized note: {first}"


@respx.mock
def test_mcp_recall_never_returns_a_superseded_note(config, git_clone, monkeypatch):
    """FIX GROUP 3(b): a superseded note must never leak through the MCP surface."""
    import subprocess

    respx.post(EMBED_URL).mock(side_effect=_embed_response)
    respx.post(RERANK_URL).mock(return_value=httpx.Response(503))  # fall to BM25

    # Commit a superseded note carrying a rare token that would otherwise win.
    (git_clone / "ghost-note.md").write_text(
        "---\ntitle: Ghost Note\nslug: ghost-note\nprofile: amber\nhost: gpuhost\n"
        "importance: 2\nsuperseded_by: live-replacement\ntags: []\ngrounding: ok\n"
        "---\nzzghosttoken unique only in the superseded ghost note\n"
    )
    subprocess.run(["git", "-C", str(git_clone), "add", "-A"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(git_clone), "commit", "-q", "-m", "ghost"],
                   check=True, capture_output=True, text=True)

    from memd.index import open_db, build_index
    db = open_db(config.db)
    build_index(db, config)
    db.close()

    out = asyncio.run(
        mcp.call_tool("recall", {"query": "zzghosttoken unique", "k": 8})
    )
    text = out.content[0].text
    assert "ghost-note" not in text, "superseded note leaked through MCP recall"
    # The rare token lived only in the superseded body; its absence proves the
    # body did not leak either, not merely that the slug was omitted.
    assert "zzghosttoken" not in text


@respx.mock
def test_mcp_save_drives_real_core_and_serializes(config, monkeypatch):
    # build the index so the BM25 dedup query in save() has a real db.
    monkeypatch.setattr(index_mod, "embed",
                        lambda texts, cfg: [[1.0] * 768 for _ in texts])
    monkeypatch.setattr(save_mod, "_pull_rebase_push", _no_push)
    from memd.index import open_db, build_index
    db = open_db(config.db)
    build_index(db, config)
    db.close()

    out = asyncio.run(
        mcp.call_tool(
            "save",
            {"title": "MCP Real Fact", "body": "a fresh durable fact", "host": "vmhost"},
        )
    )
    payload = json.loads(out.content[0].text)
    # real SaveResult.to_dict() round-trips the service fields.
    assert payload["slug"] == "mcp-real-fact"
    assert payload["action"] == "created"
    assert "grounding" in payload
    assert "flagged_for_review" in payload
    # the note actually landed in the real clone.
    from memd.store import read_note
    n = read_note(config.clone, "mcp-real-fact")
    assert n is not None and n.host == "vmhost"
