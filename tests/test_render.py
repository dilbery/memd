"""memd.render is canonical; the recall hook ships a copy. Assert they agree.

`clients/memd-recall-hook` must stay stdlib-only and standalone -- it runs on
machines with no memd install -- so it carries its own copy of the rendering
logic. That duplication is deliberate, but it must never drift: the whole point
is that MCP consumers and hook consumers see the same shaped memory.
"""

import importlib.util
from pathlib import Path

import pytest

from memd.render import render

HOOK = Path(__file__).resolve().parents[1] / "clients" / "memd-recall-hook"


@pytest.fixture(scope="module")
def hook():
    """Import the extension-less hook script as a module."""
    spec = importlib.util.spec_from_loader(
        "memd_recall_hook",
        importlib.machinery.SourceFileLoader("memd_recall_hook", str(HOOK)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CASES = [
    pytest.param([], id="empty"),
    pytest.param(
        [{"slug": "ssh-key", "importance": 5, "body": "use the deploy key"}],
        id="core-only",
    ),
    pytest.param(
        [{"slug": "backup", "importance": 2, "body": "runs weekly"}],
        id="relevant-only",
    ),
    pytest.param(
        [
            {"slug": "core-a", "importance": 5, "body": "a" * 400},
            {"slug": "core-b", "importance": 4, "description": "short desc"},
            {"slug": "rel-a", "importance": 2, "body": "b" * 4000},
            {"slug": "rel-b", "importance": 1, "body": "c" * 50},
        ],
        id="mixed-with-oversize-body",
    ),
    pytest.param(
        [
            {"slug": f"rel-{i}", "importance": 1, "body": f"body {i}"}
            for i in range(20)
        ],
        id="more-than-top-n",
    ),
    pytest.param(
        [
            {"title": "titled", "importance": 3, "text": "alt field names"},
            {"name": "named", "importance": 5, "content": "alt core"},
            "not-a-dict",
            {"slug": "", "body": ""},
            {"slug": "bad-importance", "importance": "xx", "body": "still renders"},
        ],
        id="malformed-and-alt-fields",
    ),
    # --- provenance (`matched`) cases: the discriminator recall() now sets. ---
    pytest.param(
        [{"slug": "hot", "importance": 5, "matched": True, "body": "full body"}],
        id="matched-high-importance-renders-full",
    ),
    pytest.param(
        [{"slug": "cold", "importance": 2, "matched": False, "body": "stubbed"}],
        id="unmatched-low-importance-renders-stub",
    ),
    pytest.param(
        [
            {"slug": "dupe", "importance": 5, "matched": False, "body": "stub side"},
            {"slug": "dupe", "importance": 5, "matched": True, "body": "full side"},
        ],
        id="same-slug-both-sides-dedups-to-full",
    ),
    pytest.param(
        [
            {"slug": "new-core", "importance": 5, "matched": False, "body": "x" * 300},
            {"slug": "new-rel", "importance": 5, "matched": True, "body": "y" * 300},
            {"slug": "legacy-core", "importance": 4, "body": "z" * 300},
            {"slug": "legacy-rel", "importance": 1, "body": "w" * 300},
        ],
        id="provenance-and-legacy-fallback-mixed",
    ),
    pytest.param(
        [{"slug": "no-body", "importance": 5, "matched": True, "body": ""}],
        id="matched-but-bodyless-is-dropped-not-stubbed",
    ),
    pytest.param(
        [
            {"slug": "driver-2020-01-01", "matched": True, "volatility": "state", "body": "old state"},
            {"slug": "plan", "matched": True, "volatility": "volatile",
             "observed_at": "2020-02-02T10:00:00Z", "body": "old plan"},
            {"slug": "pref", "matched": True, "volatility": "durable",
             "verified_at": "2020-03-03", "body": "likes fish"},
            {"slug": "undated", "matched": True, "volatility": "state", "body": "no date"},
        ],
        id="dated-and-volatile-notes",
    ),
]


@pytest.mark.parametrize("notes", CASES)
def test_package_and_hook_render_identically(hook, notes):
    assert render(notes) == hook.render(notes)


def test_core_is_one_line_and_relevant_is_full():
    """The split is the whole point: core compact, matches in full and first."""
    notes = [
        {"slug": "core-note", "importance": 5, "body": "x" * 900},
        {"slug": "match-note", "importance": 2, "body": "y" * 900},
    ]
    out = render(notes)
    assert out.index("### match-note") < out.index("### Core index")
    assert "y" * 900 in out                      # match rendered in full
    assert "x" * 900 not in out                  # core compacted
    assert "- **core-note** -- " in out


def test_budget_is_enforced():
    notes = [
        {"slug": f"n{i}", "importance": 1, "body": "z" * 1400} for i in range(60)
    ]
    out = render(notes, max_chars=5000)
    assert len(out) <= 5000 + len("\n[truncated]")
    assert out.endswith("[truncated]")


def test_oversize_body_is_capped_not_dropped():
    out = render([{"slug": "big", "importance": 1, "body": "q" * 9000}])
    assert "### big" in out
    assert "…" in out
    assert len(out) < 3000


def test_query_excerpt_reaches_tail_and_reports_exact_coordinates(hook):
    from memd.render import render_result
    body = "introductory filler " * 250 + "TAILTOKEN device password changed" + " trailing" * 200
    notes = [{"slug": "long-note", "matched": True, "body": body}]
    result = render_result(notes, query="TAILTOKEN password")
    assert "TAILTOKEN device password changed" in result["text"]
    excerpt = result["excerpts"][0]
    assert excerpt["offset"] > 1500
    assert "TAILTOKEN" in body[excerpt["offset"]:excerpt["end_offset"]]
    assert excerpt["total_chars"] == len(body) and excerpt["truncated"]
    assert "use read" in result["text"]
    assert result["text"] == hook.render(notes, query="TAILTOKEN password")


def test_core_budget_reports_omissions_and_prioritizes_explicit_pins(hook):
    from memd.render import render_result
    notes = [{"slug": f"core-{i}", "matched": False, "body": "core", "pinned": i == 19}
             for i in range(20)]
    result = render_result(notes, core_limit=3)
    assert result["returned_core"] == 3
    assert result["omitted_core"] == 17
    assert "17 core notes" in result["text"]
    assert result["text"].index("core-19") < result["text"].index("core-0")
    assert "core-3**" not in result["text"]
    assert result["text"] == hook.render(notes, core_limit=3)


def test_render_k_and_budget_never_silently_drop_requested_matches():
    from memd.render import render_result
    notes = [{"slug": f"note-{i}", "body": "body", "matched": True} for i in range(30)]
    large = render_result(notes, top_n=30, max_chars=30000)
    assert large["returned_matches"] == 30 and large["omitted_matches"] == 0
    small = render_result(notes, top_n=30, max_chars=700)
    assert small["returned_matches"] + small["omitted_matches"] == 30
    assert small["omitted_matches"] > 0 and "Omitted" in small["text"]
    assert len(small["text"]) <= 700


def test_hook_reads_shell_quoted_export_token(hook, tmp_path, monkeypatch):
    path = tmp_path / "client.env"
    path.write_text("export MEMD_TOKEN='token with spaces'\n")
    monkeypatch.setenv("MEMD_ENV_FILE", str(path))
    monkeypatch.setenv("MEMD_TOKEN", "stale")
    assert hook._resolve_token() == "token with spaces"


def test_hook_requests_canonical_context_and_preserves_empty_success(hook, monkeypatch):
    import json
    from types import SimpleNamespace
    captured = {}
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return b'{"context":"","rendering":{"omitted_core":0}}'
    def fake_open(request, **kwargs):
        captured.update(json.loads(request.data))
        return Response()
    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_open)
    result = hook.fetch_recall("a meaningful query")
    assert result["context"] == ""
    assert captured["format"] == "context"
    assert captured["max_chars"] == hook.MAX_CHARS
    assert captured["k"] == hook.TOP_N


def test_stale_state_notes_are_labelled_and_bannered():
    text = render([
        {"slug": "driver-2020-01-01", "matched": True, "volatility": "state", "body": "old state"},
        {"slug": "plan", "matched": True, "volatility": "volatile",
         "observed_at": "2020-02-02", "body": "old plan"},
        {"slug": "pref-2020-03-03", "matched": True, "volatility": "durable", "body": "likes fish"},
        {"slug": "fact-2020-04-04", "matched": True, "body": "unlabelled"},
    ])
    assert "### driver-2020-01-01  (as of 2020-01-01," in text and "may be stale" in text
    assert "### pref-2020-03-03  (as of 2020-03-03, durable)\n" in text
    assert "### fact-2020-04-04  (as of 2020-04-04)\n" in text   # a date, never a verdict
    assert "2 of these notes describe changeable state" in text


def test_fresh_or_undated_notes_get_no_banner():
    today = __import__("datetime").date.today().isoformat()
    text = render([{"slug": "now", "matched": True, "volatility": "state",
                    "verified_at": today, "body": "b"},
                   {"slug": "undated", "matched": True, "volatility": "state", "body": "b"}])
    assert "may be stale" not in text and "changeable state" not in text
    assert "### undated\n" in text


def test_hook_tells_the_agent_which_recall_id_to_pass_to_read(hook):
    rid = "0123456789abcdef"
    block = hook.with_recall_id("## Recalled memory\n\nbody", rid, max_chars=1000)
    assert block.endswith(f'read(slug, recall_id="{rid}").]')
    assert hook.with_recall_id("body", "not-an-id", max_chars=1000) == "body"
    assert hook.with_recall_id("", rid, max_chars=1000) == ""
    assert hook.with_recall_id("x" * 990, rid, max_chars=1000) == "x" * 990   # never over budget


def test_package_prompt_hook_renders_the_recall_id_within_its_cap():
    from memd.hooks import auto_recall as ar
    rid = "fedcba9876543210"
    hits = [{"slug": f"n{i}", "body": "b" * 2000} for i in range(6)]
    text = ar._render(hits, rid)
    assert len(text) <= ar.MAX_INJECT_CHARS and text.endswith(f'recall_id="{rid}").]')
    assert "recall_id" not in ar._render(hits, "<script>") and "recall_id" not in ar._render(hits)
