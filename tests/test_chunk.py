"""memd.chunk: deterministic, Markdown-aware, overlapping chunks of a note body."""
import random

import pytest

from memd import chunk as C
from memd.chunk import Chunk, chunk_body, embed_text


def _para(tag: str, words: int) -> str:
    return " ".join(f"{tag}{i}" for i in range(words))


def _assert_well_formed(body: str, chunks: list[Chunk]) -> None:
    assert chunks, "chunk_body must never return an empty list"
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))
    for prev, cur in zip(chunks, chunks[1:]):
        assert prev.start < cur.start, "chunks must advance"
        assert prev.end <= cur.end
        assert cur.start <= prev.end or not body[prev.end:cur.start].strip(), \
            "text between chunks must be whitespace only"
    for c in chunks:
        assert 0 <= c.start <= c.end <= len(body)
        assert c.end - c.start <= C.MAX_CHARS + C.OVERLAP_CHARS
        text = body[c.start:c.end]
        assert text == text.strip() or not text.strip()


def test_blank_and_short_bodies_are_one_chunk():
    assert chunk_body("") == [Chunk(0, 0, 0)]
    assert chunk_body("\n  \n") == [Chunk(0, 0, 4)]
    body = "Trackr host 10.10.1.11 is a VM; autostart OFF.\n"
    assert chunk_body(body) == [Chunk(0, 0, len(body.rstrip()))]


def test_chunking_is_deterministic():
    body = "\n\n".join(_para(f"p{i}x", 60) for i in range(12))
    assert chunk_body(body) == chunk_body(body)


def test_paragraphs_pack_towards_the_target_and_never_past_the_maximum():
    body = "\n\n".join(_para(f"p{i}x", 40) for i in range(20))   # ~250-char paragraphs
    chunks = chunk_body(body)
    _assert_well_formed(body, chunks)
    assert len(chunks) > 3
    for c in chunks[:-1]:
        # Each chunk (before overlap) reaches the target unless the next paragraph would overflow.
        assert c.end - c.start >= C.TARGET_CHARS - 300


def test_a_heading_starts_a_chunk_once_the_current_one_is_substantial():
    body = (f"# Host\n\n{_para('intro', 90)}\n\n## Current state\n\n{_para('now', 30)}"
            f"\n\n## History\n\n{_para('old', 30)}")
    chunks = chunk_body(body)
    _assert_well_formed(body, chunks)
    texts = [body[c.start:c.end] for c in chunks]
    assert texts[1].startswith("## Current state")
    assert chunks[1].context == ""        # the heading is inside the chunk itself
    # A short section does not force a cut: History joins Current state.
    assert len(chunks) == 2 and "## History" in texts[1]


def test_small_sections_merge_instead_of_making_tiny_chunks():
    body = "\n\n".join(f"## S{i}\n\nshort fact {i}" for i in range(10))
    chunks = chunk_body(body)
    assert len(chunks) == 1


def test_a_continuation_chunk_carries_its_heading_path_and_overlaps():
    body = "# Server\n\n## Current state\n\n" + "\n\n".join(_para(f"f{i}x", 40) for i in range(10))
    chunks = chunk_body(body)
    _assert_well_formed(body, chunks)
    assert len(chunks) >= 2
    later = chunks[1]
    assert later.context == "Server > Current state"
    assert later.start < chunks[0].end, "a chunk continuing a section overlaps the previous one"
    assert chunks[0].end - later.start <= C.OVERLAP_CHARS
    # The overlap begins on a word boundary.
    assert body[later.start - 1].isspace()


def test_a_chunk_opening_at_a_heading_does_not_overlap():
    body = f"{_para('a', 130)}\n\n## Next\n\n{_para('b', 20)}"
    chunks = chunk_body(body)
    assert len(chunks) == 2
    assert body[chunks[1].start:].startswith("## Next")
    assert chunks[1].start > chunks[0].end


def test_heading_path_resets_at_a_same_or_higher_level():
    body = (f"# A\n\n## B\n\n{_para('b', 90)}\n\n# C\n\n"
            + "\n\n".join(_para(f"c{i}x", 40) for i in range(8)))
    contexts = [c.context for c in chunk_body(body)]
    assert "C" in contexts and "A > B > C" not in contexts and "A > C" not in contexts


def test_fenced_code_is_not_split_on_blank_lines_or_hash_lines():
    code = "```bash\n# not a heading\n\necho one\n\necho two\n```"
    body = f"{_para('a', 80)}\n\n{code}\n\n{_para('b', 10)}"
    chunks = chunk_body(body)
    _assert_well_formed(body, chunks)
    start = body.index(code)
    holding = [c for c in chunks if c.start <= start and start + len(code) <= c.end]
    assert holding, "the fenced block must sit whole inside one chunk"
    assert all(c.context != "not a heading" for c in chunks)


def test_an_oversized_block_splits_on_lines_then_words_then_hard():
    lines = "\n".join(_para(f"l{i}x", 20) for i in range(40))      # one block, many lines
    chunks = chunk_body(lines)
    _assert_well_formed(lines, chunks)
    assert len(chunks) > 1
    for c in chunks[1:]:
        # a line start, possibly pulled back by overlap to another line start
        assert c.start == 0 or lines[c.start - 1] == "\n"

    words = _para("w", 800)                                          # one enormous line
    chunks = chunk_body(words)
    _assert_well_formed(words, chunks)
    assert all(words[c.start - 1] == " " for c in chunks[1:])

    solid = "x" * 3000                                               # no boundary at all
    assert [(c.start, c.end) for c in chunk_body(solid)] == [(0, 1200), (1200, 2400), (2400, 3000)]


def test_chunk_count_is_capped(monkeypatch):
    monkeypatch.setattr(C, "MAX_CHUNKS", 3)
    body = "\n\n".join(_para(f"p{i}x", 60) for i in range(40))
    assert len(chunk_body(body)) == 3


def test_every_word_lands_in_some_chunk():
    rng = random.Random(7)
    for trial in range(30):
        parts = []
        for i in range(rng.randint(1, 25)):
            kind = rng.random()
            if kind < 0.2:
                parts.append("#" * rng.randint(1, 4) + f" heading{trial}x{i}")
            elif kind < 0.3:
                parts.append(f"```\ncode{trial}x{i}\n\n# fenced{trial}x{i}\n```")
            else:
                parts.append(_para(f"t{trial}x{i}w", rng.randint(1, 300)))
        body = "\n\n".join(parts)
        chunks = chunk_body(body)
        _assert_well_formed(body, chunks)
        covered = "".join(body[c.start:c.end] + "\n" for c in chunks)
        for word in body.split():
            assert word in covered, (trial, word)


def test_embed_text_prefixes_title_and_heading_path():
    body = "# Server\n\n## Current state\n\n" + "\n\n".join(_para(f"f{i}x", 40) for i in range(10))
    first, second = chunk_body(body)[:2]
    assert embed_text("vmhost services", body, first) == \
        "vmhost services\n\n" + body[first.start:first.end]
    assert embed_text("vmhost services", body, second) == \
        "vmhost services\nServer > Current state\n\n" + body[second.start:second.end]
    assert embed_text("", body, first) == body[first.start:first.end]
    assert embed_text(None, "x", Chunk(0, 0, 1)) == "x"


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_offsets_index_the_original_body(newline):
    body = newline.join(["## Top", "", _para("a", 200), "", "## Bottom", "", _para("b", 200)])
    for c in chunk_body(body):
        text = body[c.start:c.end]
        assert text.strip() == text
        assert not text.endswith("\r")
