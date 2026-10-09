"""Regression: store.py must parse the REAL legacy corpus, which has notes whose
`description:` value contains an unquoted colon-space (PyYAML rejects those as a
nested mapping). Found when mem-carve crashed against the live store."""
from memd.store import parse_text


def test_unquoted_colon_in_description_does_not_crash():
    text = (
        "---\n"
        "name: no unchecked changes\n"
        "description: Before changes, verify against the live system first: check the version\n"
        "type: feedback\n"
        "metadata:\n"
        "  node_type: memory\n"
        "  originSessionId: abc\n"
        "---\n"
        "Body of the note.\n"
    )
    note = parse_text(text, path="feedback_no_unchecked.md")
    assert note.title == "no unchecked changes"
    assert "verify against the live system first: check the version" in note.description
    assert note.importance == 3
    assert note.body == "Body of the note."


def test_wellformed_yaml_still_parses_normally():
    text = (
        "---\n"
        'description: "RESOLVED 2026-06-11: quoted colon is fine"\n'
        "importance: 5\n"
        "host: gpuhost\n"
        "---\n"
        "# Real Title\n\nbody\n"
    )
    note = parse_text(text, path="x.md")
    assert note.title == "Real Title"
    assert note.importance == 5
    assert note.host == "gpuhost"
    assert note.description == "RESOLVED 2026-06-11: quoted colon is fine"
