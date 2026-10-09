"""The public synthetic recall-eval set (eval/public) and its CI stand-ins."""
from __future__ import annotations

import importlib.util
import json
import math
import shutil
import sys
from pathlib import Path

import memd.index as index_mod
import memd.recall as recall_mod
from memd.recall_eval import (
    DIM, hash_embed, install_local, load_golden, main, materialize_corpus,
)
from memd.store import parse_note

PUBLIC = Path(__file__).resolve().parents[1] / "eval" / "public"
CATEGORIES = {"identifier", "paraphrase", "agent-framed", "current-state", "multi", "real"}


def _generator():
    spec = importlib.util.spec_from_file_location("public_eval_generate", PUBLIC / "generate.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module       # dataclasses resolve annotations through it
    try:
        spec.loader.exec_module(module)
    finally:
        del sys.modules[spec.name]
    return module


def test_generator_output_is_deterministic_and_committed(tmp_path):
    gen = _generator()
    gen.write(tmp_path / "a")
    gen.write(tmp_path / "b")
    names = sorted(p.name for p in (tmp_path / "a" / "corpus").glob("*.md"))
    assert names == sorted(p.name for p in (PUBLIC / "corpus").glob("*.md"))
    for name in names:
        fresh = (tmp_path / "a" / "corpus" / name).read_bytes()
        assert fresh == (tmp_path / "b" / "corpus" / name).read_bytes()
        assert fresh == (PUBLIC / "corpus" / name).read_bytes(), \
            f"{name} differs: re-run eval/public/generate.py and commit its output"
    assert (tmp_path / "a" / "golden.jsonl").read_bytes() == (PUBLIC / "golden.jsonl").read_bytes()


def test_golden_labels_resolve_to_live_notes():
    notes = {n.slug: n for n in map(parse_note, (PUBLIC / "corpus").glob("*.md"))}
    assert 150 <= len(notes) <= 250
    rows = load_golden(PUBLIC / "golden.jsonl")
    assert 100 <= len(rows) <= 150
    assert {r["category"] for r in rows} == CATEGORIES
    for n, row in enumerate(rows, 1):
        assert row["split"] == ("test" if n % 3 == 0 else "dev")
        for gold in row["gold"]:
            note = notes.get(gold["slug"])
            assert note is not None, (row["id"], gold["slug"])
            assert note.superseded_by is None, (row["id"], gold["slug"])
    # The corpus keeps what makes recall hard: superseded notes and long umbrellas.
    assert any(n.superseded_by for n in notes.values())
    assert max(len(n.body) for n in notes.values()) > 4000


def test_hash_embedder_is_deterministic_unit_length_and_lexical():
    a = hash_embed("restic backup check on apphost")
    assert len(a) == DIM == 768
    assert a == hash_embed("restic backup check on apphost")
    assert math.isclose(sum(v * v for v in a), 1.0, rel_tol=1e-9)

    def cos(x, y):
        return sum(p * q for p, q in zip(x, y))
    assert cos(a, hash_embed("apphost restic backups")) > cos(a, hash_embed("tmux prefix on lapbox"))
    empty = hash_embed("the and of")         # stopwords only: still a unit vector
    assert math.isclose(sum(v * v for v in empty), 1.0)


def test_install_local_swaps_and_restores_backends():
    before = (index_mod.embed, recall_mod.embed_with_deadline, recall_mod.rerank)
    uninstall = install_local(embedder="hash", reranker="none")
    try:
        assert recall_mod.rerank("q", [{"slug": "a"}, {"slug": "b"}], top_n=1, cfg=None) == [{"slug": "a"}]
        assert len(recall_mod.embed_with_deadline("x", cfg=type("C", (), {"embed_dim": 768}))) == 768
    finally:
        uninstall()
    assert (index_mod.embed, recall_mod.embed_with_deadline, recall_mod.rerank) == before


def test_materialized_corpus_head_is_reproducible(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for p in sorted((PUBLIC / "corpus").glob("*.md"))[:3]:
        shutil.copyfile(p, src / p.name)
    assert materialize_corpus(src, tmp_path / "one") == materialize_corpus(src, tmp_path / "two") != ""


def test_eval_runs_end_to_end_on_a_subset_and_gates(tmp_path, monkeypatch, capsys):
    # main() points the profile at its scratch clone through the environment.
    monkeypatch.setenv("MEMD_AMBER_CLONE", str(tmp_path))
    monkeypatch.setenv("MEMD_AMBER_DB", str(tmp_path / "unused.db"))
    rows = [json.loads(line) for line in (PUBLIC / "golden.jsonl").read_text().splitlines()[:12]]
    golden = tmp_path / "golden.jsonl"
    golden.write_text("".join(json.dumps(r) + "\n" for r in rows))
    wanted = {g["slug"] for r in rows for g in r["gold"]}
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for p in sorted((PUBLIC / "corpus").glob("*.md")):
        if p.stem in wanted or len(list(corpus.iterdir())) < 40:
            shutil.copyfile(p, corpus / p.name)

    args = ["--golden", str(golden), "--corpus", str(corpus), "--embedder", "hash",
            "--rerank", "none"]
    baseline = tmp_path / "baseline.json"
    assert main(args + ["--write-baseline", str(baseline)]) == 0
    stored = json.loads(baseline.read_text())
    assert stored["setup"] == {"embedder": "hash", "rerank": "none", "recall_vectors": "chunks"}
    assert set(stored["per_query"]) == {r["id"] for r in rows}
    assert any(q["ndcg@10"] > 0 for q in stored["per_query"].values())

    summary = tmp_path / "summary.md"
    assert main(args + ["--baseline", str(baseline), "--markdown", str(summary)]) == 0
    out = capsys.readouterr().out
    assert "RECALL EVAL (all)" in out and "verdict: NEUTRAL" in out
    assert "| **all** | 12 |" in summary.read_text()

    # A baseline that ranked everything perfectly makes this run a regression.
    for q in stored["per_query"].values():
        q.update({"recall@5": 1.0, "recall@10": 1.0, "mrr@10": 1.0, "ndcg@10": 1.0, "pool": 1.0})
    baseline.write_text(json.dumps(stored))
    assert main(args + ["--baseline", str(baseline)]) == 1
    assert "verdict: FAIL" in capsys.readouterr().out
