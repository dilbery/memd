import asyncio
import json

import pytest

import memd.mcp as mcp


class _FakeNote:
    def __init__(self, slug, title, body):
        self.slug = slug
        self.title = title
        self.body = body
        self.profile = "amber"
        self.host = "gpuhost"
        self.importance = 4

    def to_dict(self):
        return {
            "slug": self.slug,
            "title": self.title,
            "body": self.body,
            "profile": self.profile,
            "host": self.host,
            "importance": self.importance,
        }


class _FakeSaveResult:
    def __init__(self, slug, action):
        self.slug = slug
        self.action = action

    def to_dict(self):
        return {"slug": self.slug, "action": self.action}


def test_list_tools_exposes_recall_and_save():
    tools = asyncio.run(mcp.list_tools())
    names = sorted(t.name for t in tools)
    assert names == ["ask", "propose", "publish", "read", "recall", "save", "timeline"], names
    # The canonical names must still be advertised and described first.
    recall = next(t for t in tools if t.name == "recall")
    assert "query" in recall.input_schema["properties"]
    save = next(t for t in tools if t.name == "save")
    assert {"title", "body"}.issubset(set(save.input_schema["properties"]))


def test_advertised_schemas_require_nothing():
    """`required` MUST stay empty on both tools.

    The client validates against this schema and refuses to dispatch on a
    mismatch, so a non-empty `required` reinstates an earlier data-loss bug:
    agent saves were rejected locally with "title is required" and never
    reached the server, where the normalisation lives. This assertion is the
    guard on that -- do not "tighten" it back.
    """
    tools = asyncio.run(mcp.list_tools())
    for t in tools:
        assert t.input_schema["required"] == [], t.name


def test_advertised_schemas_include_the_aliases():
    """Aliases must be advertised, not merely tolerated server-side.

    A client-side validator that rejects unknown properties would otherwise
    still refuse `{"slug":..., "content":...}` before dispatch.
    """
    tools = asyncio.run(mcp.list_tools())
    save = next(t for t in tools if t.name == "save").input_schema["properties"]
    assert {"content", "text", "slug", "name", "fact", "note"}.issubset(set(save))
    recall = next(t for t in tools if t.name == "recall").input_schema["properties"]
    assert {"q", "question", "topic", "search", "text"}.issubset(set(recall))


def test_call_recall_routes_to_core(monkeypatch):
    seen = {}

    def fake_recall(query, profile="amber", k=8, *, cfg=None):
        seen["query"] = query
        seen["profile"] = profile
        seen["k"] = k
        seen["cfg"] = cfg
        return [_FakeNote("slug-a", "Note A", "body a"), _FakeNote("slug-b", "Note B", "body b")]

    monkeypatch.setattr(mcp, "_core_recall", fake_recall)
    out = asyncio.run(mcp.call_tool("recall", {"query": "gpu thrash", "k": 3}))
    text = out.content[0].text
    assert seen["query"] == "gpu thrash"
    assert seen["profile"] == "amber"
    assert seen["k"] == 3
    # wiring fix: the required keyword-only cfg is now built and threaded through.
    assert seen["cfg"] is not None
    # recall now returns the rendered markdown block, not a JSON dump -- dumping
    # every note in full cost ~43k tokens per call. These fakes are importance 4,
    # so they are core notes and render as compact index lines. Order must
    # survive rendering.
    assert "- **slug-a** -- body a" in text
    assert "- **slug-b** -- body b" in text
    assert text.index("slug-a") < text.index("slug-b")


def test_call_save_routes_to_core_and_returns_result(monkeypatch):
    captured = {}

    def fake_save(fact, profile="amber", *, cfg=None):
        captured["fact"] = fact
        captured["profile"] = profile
        captured["cfg"] = cfg
        return _FakeSaveResult("new-fact", "created")

    monkeypatch.setattr(mcp, "_core_save", fake_save)
    out = asyncio.run(
        mcp.call_tool("save", {"title": "New Fact", "body": "the body", "host": "gpuhost"})
    )
    payload = json.loads(out.content[0].text)
    assert captured["fact"]["title"] == "New Fact"
    assert captured["fact"]["body"] == "the body"
    assert captured["fact"]["host"] == "gpuhost"
    # wiring fix: the required keyword-only cfg is now built and threaded through.
    assert captured["cfg"] is not None
    assert payload == {"slug": "new-fact", "action": "created"}


def test_call_unknown_tool_errors():
    out = asyncio.run(mcp.call_tool("delete_everything", {}))
    assert "unknown tool" in out.content[0].text.lower()


# --------------------------------------------------------------------------
# End-to-end alias tolerance through call_tool.
#
# These are the shapes agent clients actually sent when the save was rejected
# and the note was lost. They must now reach the core.
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "arguments,want_title,want_body",
    [
        ({"slug": "agent-notes-gpuhost", "content": "agent 1.2.3 detail."},
         "agent-notes-gpuhost", "agent 1.2.3 detail."),
        ({"content": "The enclosure moved to a rear port."},
         "The enclosure moved to a rear port", "The enclosure moved to a rear port."),
        ({"slug": "ha-mqtt-discovery", "text": "object_id does not pin entity_id."},
         "ha-mqtt-discovery", "object_id does not pin entity_id."),
        ({"body": "Install the microSD card."},
         "Install the microSD card", "Install the microSD card."),
    ],
)
def test_call_save_accepts_the_shapes_that_used_to_be_rejected(
    monkeypatch, arguments, want_title, want_body
):
    captured = {}

    def fake_save(fact, profile="amber", *, cfg=None):
        captured["fact"] = fact
        return _FakeSaveResult("s", "created")

    monkeypatch.setattr(mcp, "_core_save", fake_save)
    out = asyncio.run(mcp.call_tool("save", arguments))
    assert json.loads(out.content[0].text) == {"slug": "s", "action": "created"}
    assert captured["fact"]["title"] == want_title
    assert captured["fact"]["body"] == want_body


def test_call_save_never_leaks_profile_into_the_fact(monkeypatch):
    """profile selects the clone; it must not be written as a note field."""
    captured = {}

    def fake_save(fact, profile="amber", *, cfg=None):
        captured["fact"] = fact
        captured["profile"] = profile
        return _FakeSaveResult("s", "created")

    monkeypatch.setattr(mcp, "_core_save", fake_save)
    asyncio.run(mcp.call_tool("save", {"content": "b", "profile": "cobalt"}))
    assert captured["profile"] == "cobalt"
    assert "profile" not in captured["fact"]


def test_call_save_with_no_content_returns_actionable_error(monkeypatch):
    """The one unrecoverable case must name the keys it got, not just fail."""
    def boom(*a, **k):  # pragma: no cover - must never be reached
        raise AssertionError("core save must not run on an empty payload")

    monkeypatch.setattr(mcp, "_core_save", boom)
    out = asyncio.run(mcp.call_tool("save", {"nonsense": 1}))
    err = json.loads(out.content[0].text)["error"]
    assert "body/content/text" in err
    assert "nonsense" in err


def test_call_recall_accepts_query_aliases_and_no_query(monkeypatch):
    seen = {}

    def fake_recall(query, profile="amber", k=8, *, cfg=None):
        seen["query"] = query
        seen["k"] = k
        return []

    monkeypatch.setattr(mcp, "_core_recall", fake_recall)

    asyncio.run(mcp.call_tool("recall", {"q": "gpu thrash"}))
    assert seen["query"] == "gpu thrash"

    # No query at all returns the core set rather than raising KeyError.
    asyncio.run(mcp.call_tool("recall", {}))
    assert seen["query"] == ""

    # k is coerced and clamped, so a string or an absurd value cannot blow up.
    asyncio.run(mcp.call_tool("recall", {"query": "x", "k": "3"}))
    assert seen["k"] == 3
    asyncio.run(mcp.call_tool("recall", {"query": "x", "k": 9999}))
    assert seen["k"] == 50


def test_scalar_fields_advertise_the_types_the_normaliser_accepts():
    """A strict advertised type re-creates the bug at a different key.

    normalize_fact() coerces `importance: "4"` and `tags: "a, b"`, but the
    validator runs FIRST -- a live save was rejected with
    `'memd, agent, ...' is not of type 'array'` after the alias keys were widened
    but these scalar types were left alone. Advertise every shape the
    normaliser actually handles.
    """
    tools = asyncio.run(mcp.list_tools())
    save = next(t for t in tools if t.name == "save").input_schema["properties"]
    assert set(save["tags"]["type"]) == {"array", "string"}
    assert set(save["importance"]["type"]) == {"integer", "string"}
    recall = next(t for t in tools if t.name == "recall").input_schema["properties"]
    assert set(recall["k"]["type"]) == {"integer", "string"}
