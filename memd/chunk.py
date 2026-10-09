"""Split a note body into overlapping, Markdown-aware chunks for the vector index.

A whole-note embedding averages every topic an umbrella note covers, so the one
fact a "current X" or paraphrased question asks about is diluted (eval/README.md).
Each chunk is embedded on its own and recall scores a note by its best chunk.

Rules, in order: a heading starts a new chunk once the current one holds
MIN_CHARS; otherwise paragraphs are packed until TARGET_CHARS, never past
MAX_CHARS. A block longer than MAX_CHARS splits on lines, then whitespace, then
hard. A chunk that continues a section (not one opening at a heading) starts up
to OVERLAP_CHARS early, on a line or word boundary, so a fact straddling a cut
is whole in one of them. Fenced code is never split at its blank lines or '#'.

Chunks are character offsets into the body, so recall can show the matched
passage without storing text twice. Pure and deterministic: the same body
always yields the same chunks, which is what lets a stored chunk's offsets be
trusted until the note's blob changes. Bump memd.index._CHUNK_VERSION whenever
the output of chunk_body or embed_text changes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_CHARS = 1000
MAX_CHARS = 1200
MIN_CHARS = 400        # a heading only cuts a chunk already this long
OVERLAP_CHARS = 150
# Bounds embedding work for a pasted log or dump; FTS still indexes the whole body.
MAX_CHUNKS = 64

_HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    start: int          # body[start:end] is the chunk's text
    end: int
    context: str = ""   # enclosing heading path when the heading is not inside the chunk


@dataclass
class _Block:
    start: int
    end: int
    opens: bool         # begins with a heading line
    context: str        # heading path in effect before the block
    heading: str = ""   # the heading the block opens with, if it opens

    @property
    def inner(self) -> str:
        """Heading path for text after this block's first line."""
        return " > ".join(p for p in (self.context, self.heading) if p)


def _lines(body: str):
    """(start, end-without-newline) for each line."""
    pos = 0
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        yield pos, pos + len(stripped)
        pos += len(line)


def _blocks(body: str) -> list[_Block]:
    """Blank-line separated blocks outside code fences; a heading starts its own block."""
    blocks: list[_Block] = []
    stack: list[tuple[int, str]] = []       # (level, heading text)
    fence: str | None = None
    cur: _Block | None = None

    def path() -> str:
        return " > ".join(text for _, text in stack)

    for start, end in _lines(body):
        line = body[start:end]
        if fence is not None:
            if cur is None:
                cur = _Block(start, end, False, path())
            cur.end = end
            m = _FENCE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not line.strip()[len(m.group(1)):].strip():
                fence = None
            continue
        if not line.strip():
            if cur is not None:
                blocks.append(cur)
                cur = None
            continue
        heading = _HEADING.match(line)
        if heading:
            if cur is not None:
                blocks.append(cur)
            level = len(heading.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            context = path()                  # the heading's own parents
            stack.append((level, heading.group(2).strip()))
            cur = _Block(start, end, True, context, heading.group(2).strip())
            continue
        m = _FENCE.match(line)
        if m:
            fence = m.group(1)
        if cur is None:
            cur = _Block(start, end, False, path())
        cur.end = end
    if cur is not None:
        blocks.append(cur)
    return blocks


def _split_long(body: str, block: _Block) -> list[_Block]:
    """Pieces of at most MAX_CHARS: whole lines where possible, then words, then hard cuts."""
    if block.end - block.start <= MAX_CHARS:
        return [block]
    pieces: list[_Block] = []
    start = block.start
    while start < block.end:
        # Leading whitespace would only inflate the piece; it stays in the body.
        while start < block.end and body[start].isspace():
            start += 1
        if start >= block.end:
            break
        limit = min(block.end, start + MAX_CHARS)
        if limit == block.end:
            cut = limit
        else:
            window = body[start:limit + 1]
            nl = window.rfind("\n")
            sp = max(window.rfind(" "), window.rfind("\t"))
            if nl > 0:
                cut = start + nl
            elif sp > 0:
                cut = start + sp
            else:
                cut = limit
        if pieces:
            # Later pieces continue the section the block opened.
            pieces.append(_Block(start, cut, False, block.inner))
        else:
            pieces.append(_Block(start, cut, block.opens, block.context, block.heading))
        start = cut
    return pieces


def _overlap_start(body: str, floor: int, start: int) -> int:
    """Move start back by up to OVERLAP_CHARS, to a line or word boundary, never below floor.

    A line start is preferred; when the only line break in reach is the one just
    before start (a long paragraph), a word start is used instead.
    """
    lo = max(floor, start - OVERLAP_CHARS)
    if lo >= start:
        return start
    window = body[lo:start]
    nl = window.find("\n")
    if nl >= 0 and window[nl + 1:].strip():
        lo += nl + 1
    elif lo > 0 and not body[lo - 1].isspace():
        sp = next((i for i, ch in enumerate(window) if ch.isspace()), -1)
        if sp < 0:
            return start
        lo += sp + 1
    while lo < start and body[lo].isspace():
        lo += 1
    return lo


def chunk_body(body: str) -> list[Chunk]:
    """The body's chunks, in order. Never empty: a blank body is one empty chunk."""
    pieces = [p for b in _blocks(body) for p in _split_long(body, b)]
    if not pieces:
        return [Chunk(0, 0, len(body))]
    groups: list[list[_Block]] = []
    for piece in pieces:
        if groups:
            cur = groups[-1]
            length = cur[-1].end - cur[0].start
            if (piece.end - cur[0].start <= MAX_CHARS
                    and not (piece.opens and length >= MIN_CHARS)
                    and length < TARGET_CHARS):
                cur.append(piece)
                continue
        groups.append([piece])
    chunks: list[Chunk] = []
    for ordinal, group in enumerate(groups[:MAX_CHUNKS]):
        first = group[0]
        start = first.start
        if ordinal and not first.opens:
            start = _overlap_start(body, chunks[-1].start + 1, start)
        context = "" if first.opens else first.context
        chunks.append(Chunk(ordinal, start, group[-1].end, context))
    return chunks


def embed_text(title: str | None, body: str, chunk: Chunk) -> str:
    """What the embedder sees for a chunk: title, heading path, then the passage."""
    head = "\n".join(p for p in ((title or "").strip(), chunk.context) if p)
    text = body[chunk.start:chunk.end]
    return f"{head}\n\n{text}" if head else text

