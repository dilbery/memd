"""Tests for memd.normalize.

The payload key-sets in `test_real_production_rejections` are not invented: each
is a shape observed in an agent session where the save was rejected client-side
and the note was lost. They are the regression bar -- if any of them stops
normalising, the data-loss bug is back.
"""
import pytest

from memd.normalize import (
    BODY_KEYS,
    QUERY_KEYS,
    TITLE_KEYS,
    NormalizeError,
    derive_title,
    normalize_fact,
    normalize_recall_args,
)


# --------------------------------------------------------------------------
# The production regressions
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "payload,want_title,want_body",
    [
        # slug + content
        (
            {"slug": "cli-install-path", "content": "The CLI binary lives at ~/.local/bin/tool."},
            "cli-install-path",
            "The CLI binary lives at ~/.local/bin/tool.",
        ),
        # content only
        (
            {"content": "The external drive moved from a front to a rear port."},
            "The external drive moved from a front to a rear port",
            "The external drive moved from a front to a rear port.",
        ),
        # slug + text
        (
            {"slug": "mqtt-discovery", "text": "object_id does NOT pin the entity_id."},
            "mqtt-discovery",
            "object_id does NOT pin the entity_id.",
        ),
        # body with no title
        (
            {"body": "Install the microSD card in the front-door camera."},
            "Install the microSD card in the front-door camera",
            "Install the microSD card in the front-door camera.",
        ),
    ],
)
def test_real_production_rejections(payload, want_title, want_body):
    out = normalize_fact(payload)
    assert out["title"] == want_title
    assert out["body"] == want_body


def test_key_sets_are_disjoint_so_nothing_is_both_title_and_body():
    # normalize_fact relies on this: it looks up title and body independently
    # with no "already consumed" bookkeeping.
    assert not set(TITLE_KEYS) & set(BODY_KEYS)


# --------------------------------------------------------------------------
# normalize_fact
# --------------------------------------------------------------------------

def test_explicit_title_beats_slug():
    out = normalize_fact({"title": "Real Title", "slug": "some-slug", "body": "b"})
    assert out["title"] == "Real Title"


def test_body_alias_precedence_follows_key_order():
    out = normalize_fact({"note": "from note", "body": "from body", "title": "t"})
    assert out["body"] == "from body"


def test_title_only_payload_is_saved_not_lost():
    out = normalize_fact({"title": "  Only a title  "})
    assert out["title"] == "Only a title"
    assert out["body"] == "Only a title"


def test_values_are_stripped():
    out = normalize_fact({"title": "  T  ", "body": "  B  "})
    assert out == {"title": "T", "body": "B"}


def test_whitespace_only_values_do_not_count_as_present():
    with pytest.raises(NormalizeError):
        normalize_fact({"title": "   ", "body": "\n\t "})


def test_non_string_values_are_ignored():
    with pytest.raises(NormalizeError):
        normalize_fact({"title": 42, "body": None})


def test_payload_is_never_mutated():
    payload = {"content": "hello", "importance": "4"}
    before = dict(payload)
    normalize_fact(payload)
    assert payload == before


def test_unknown_keys_are_dropped():
    out = normalize_fact({"body": "b", "title": "t", "wat": "nope", "profile_id": 7})
    assert set(out) == {"title", "body"}


def test_error_names_the_keys_actually_received():
    with pytest.raises(NormalizeError) as exc:
        normalize_fact({"zzz": 1, "aaa": 2})
    msg = str(exc.value)
    assert "body/content/text" in msg
    assert "aaa, zzz" in msg  # sorted


def test_error_on_empty_payload_says_none():
    with pytest.raises(NormalizeError) as exc:
        normalize_fact({})
    assert "(none)" in str(exc.value)


# --- optional fields -------------------------------------------------------

def test_importance_coerced_and_clamped():
    assert normalize_fact({"body": "b", "importance": "4"})["importance"] == 4
    assert normalize_fact({"body": "b", "importance": 9})["importance"] == 5
    assert normalize_fact({"body": "b", "importance": 0})["importance"] == 1
    assert normalize_fact({"body": "b", "importance": 3.7})["importance"] == 3


def test_unparseable_importance_is_omitted_not_fatal():
    out = normalize_fact({"body": "b", "importance": "high"})
    assert "importance" not in out
    assert out["body"] == "b"


def test_tags_from_list_and_from_comma_string():
    assert normalize_fact({"body": "b", "tags": ["a", " b ", ""]})["tags"] == ["a", "b"]
    assert normalize_fact({"body": "b", "tags": "a, b ,,c"})["tags"] == ["a", "b", "c"]


def test_empty_tags_are_explicit_so_updates_can_clear_them():
    assert normalize_fact({"body": "b", "tags": []})["tags"] == []
    assert "tags" not in normalize_fact({"body": "b", "tags": 5})


def test_host_and_profile_carry_through_when_non_empty():
    out = normalize_fact({"body": "b", "host": " apphost ", "profile": " amber "})
    assert out["host"] == "apphost"
    assert out["profile"] == "amber"
    assert "host" not in normalize_fact({"body": "b", "host": "  "})


def test_conflict_is_coerced_to_bool():
    assert normalize_fact({"body": "b", "conflict": 1})["conflict"] is True
    assert normalize_fact({"body": "b", "conflict": 0})["conflict"] is False


# --------------------------------------------------------------------------
# derive_title
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "body,want",
    [
        ("", "untitled note"),
        ("   \n\t ", "untitled note"),
        ("# Heading here\nrest", "Heading here"),
        ("### Deep heading", "Deep heading"),
        ("**Bold line**", "Bold line"),
        ("__underscored__", "underscored"),
        ("- list item", "list item"),
        ("* star item", "star item"),
        ("[[wikilink-slug]]", "wikilink-slug"),
        ("\n\n  Second line is first non-empty  \nmore", "Second line is first non-empty"),
        ("Trailing punctuation...", "Trailing punctuation"),
        ("Ends with comma,", "Ends with comma"),
        ("# **Bold heading**", "Bold heading"),
    ],
)
def test_derive_title_rules(body, want):
    assert derive_title(body) == want


def test_derive_title_truncates_at_word_boundary():
    body = "word " * 30  # far over 80 chars
    out = derive_title(body)
    assert len(out) <= 80
    assert not out.endswith("wor")  # never mid-word
    assert out.startswith("word word")


def test_derive_title_hard_cuts_when_no_space_in_first_80():
    body = "x" * 200
    out = derive_title(body)
    assert out == "x" * 80


def test_derive_title_never_returns_empty():
    # a line that dissolves entirely under the stripping rules
    assert derive_title("***") == "untitled note"
    assert derive_title("...") == "untitled note"


def test_derive_title_is_used_by_normalize_fact():
    out = normalize_fact({"content": "# The Real Heading\n\nbody text"})
    assert out["title"] == "The Real Heading"


# --------------------------------------------------------------------------
# normalize_recall_args
# --------------------------------------------------------------------------

def test_recall_query_aliases():
    for key in QUERY_KEYS:
        assert normalize_recall_args({key: "gpu thrash"})["query"] == "gpu thrash"


def test_recall_missing_query_returns_empty_not_error():
    # An empty query is legal: it means "return the core set". A fumbled recall
    # must still hand back memory rather than erroring.
    assert normalize_recall_args({})["query"] == ""
    assert normalize_recall_args({"nonsense": 1})["query"] == ""


def test_recall_k_defaults_coerces_and_clamps():
    assert normalize_recall_args({"query": "q"})["k"] == 8
    assert normalize_recall_args({"query": "q", "k": "3"})["k"] == 3
    assert normalize_recall_args({"query": "q", "k": 999})["k"] == 50
    assert normalize_recall_args({"query": "q", "k": 0})["k"] == 1
    assert normalize_recall_args({"query": "q", "k": "lots"})["k"] == 8


def test_recall_profile_optional():
    assert "profile" not in normalize_recall_args({"query": "q"})
    assert normalize_recall_args({"query": "q", "profile": " amber "})["profile"] == "amber"
    assert "profile" not in normalize_recall_args({"query": "q", "profile": "  "})


def test_recall_output_shape():
    assert set(normalize_recall_args({"query": "q"})) == {"query", "k"}


def test_recall_never_raises_on_junk():
    for junk in ({}, {"k": None}, {"query": 5}, {"profile": 9}, {"q": ""}):
        out = normalize_recall_args(junk)
        assert isinstance(out["query"], str)
        assert 1 <= out["k"] <= 50


@pytest.mark.parametrize('value', ['false', ' FALSE ', 'off', 'no', '0'])
def test_false_strings_cannot_accidentally_supersede_or_pin(value):
    normalized = normalize_fact({'body': 'b', 'conflict': value, 'pinned': value})
    assert normalized['conflict'] is False
    assert normalized['pinned'] is False


@pytest.mark.parametrize('payload', [
    {'slug': '../outside'}, {'supersedes': '/other-profile/note'},
    {'expected_revision': ''}, {'expected_revision': 42}, {'conflict': 'maybe'},
])
def test_unsafe_mutation_controls_are_rejected_before_save(payload):
    with pytest.raises(NormalizeError):
        normalize_fact({'body': 'b', **payload})


def test_explicit_identity_provenance_and_clear_tags_survive_normalization():
    expected = {'title': 'Readable title', 'body': 'Evidence', 'slug': 'stable-id',
                'supersedes': 'old-id', 'expected_revision': 'abc123',
                'source': 'inspection', 'observed_at': '2026-09-08',
                'verified_at': '2026-09-09', 'pinned': True,
                'description': 'Curated summary', 'tags': []}
    assert normalize_fact(expected) == expected


def test_recall_controls_preserve_environment_defaults_unless_supplied():
    assert normalize_recall_args({}) == {'query': '', 'k': 8}
    normalized = normalize_recall_args({'q': 'backup', 'include_core': 'false',
                                       'max_chars': 200000, 'core_limit': -4,
                                       'tags': 'nas, backup', 'host': ' vmhost '})
    assert normalized == {'query': 'backup', 'k': 8, 'include_core': False,
                          'max_chars': 100000, 'core_limit': 0,
                          'tags': ['nas', 'backup'], 'host': 'vmhost'}
