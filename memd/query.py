"""Query normalization and distillation for memd.

The FTS5 tokenizer treats characters like '-_.:/@' as part of words.
This causes mismatch between indexed tokens (e.g. 'gpuhost.') and query
terms ('gpuhost'). This module normalizes both sides by stripping boundary
tokenchars while preserving inner structure (URLs, paths, versions).
"""
from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass

TOKENCHARS = "-_.:/@"
MAX_DF_TOKENS = 48          # distill() looks at the first 48 distinct query tokens

STOPWORDS: frozenset[str] = frozenset({
    "i", "me", "my", "myself", "we", "our", "ours", "ourselves", "you",
    "your", "yours", "yourself", "yourselves", "he", "him", "his", "himself",
    "she", "her", "hers", "herself", "it", "its", "itself", "they", "them",
    "their", "theirs", "themselves", "what", "which", "who", "whom", "this",
    "that", "these", "those", "am", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "having", "do", "does", "did", "doing",
    "a", "an", "the", "and", "but", "if", "or", "because", "as", "until",
    "while", "of", "at", "by", "for", "with", "about", "against", "between",
    "into", "through", "during", "before", "after", "above", "below", "to",
    "from", "up", "down", "in", "out", "on", "off", "over", "under", "again",
    "further", "then", "once", "here", "there", "when", "where", "why", "how",
    "all", "any", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than", "too",
    "very", "s", "t", "can", "will", "just", "don", "should", "now",
    "dont", "im", "ive", "its", "thats", "whats", "lets", "youre", "cant",
    "wont", "isnt", "didnt", "doesnt", "shant", "couldnt", "wouldnt",
    "shouldnt", "hadnt", "hasnt", "havent", "arent", "wasnt", "werent",
    "ill", "id", "youve", "theyre", "theres", "heres", "also", "via", "etc",
    "e.g", "i.e", "vs", "per", "one", "two", "yes", "got",
})

FRAME_WORDS: frozenset[str] = frozenset({
    "user", "users", "want", "wants", "wanted", "know", "knew", "need", "needs",
    "tell", "show", "find", "help", "remind", "please", "ok", "okay", "hey",
    "hi", "thanks", "thank", "can", "could", "would", "should", "might",
    "like", "just", "get", "got", "make", "see", "look", "looking", "think",
    "also", "still", "really", "thing", "things", "stuff", "anything",
    "something", "everything", "gonna", "wanna", "yeah", "yep", "nah", "sure",
    "good", "great", "nice", "cool", "bit", "lot", "lots", "way", "sort",
    "kind", "maybe", "actually", "basically", "probably", "now", "then",
    "again", "before", "after", "today", "currently", "current", "latest",
    "new", "old", "use", "used", "using", "work", "working", "works", "check",
    "try", "tried", "ask", "asked", "said", "say", "question", "answer",
    "info", "information", "details", "context", "memory", "memories", "note",
    "notes", "recall", "remember", "what", "which", "who", "how", "why",
    "when", "where", "whats"
})

# A maximal run of FTS token characters: unicode61 word chars plus TOKENCHARS.
# FTS5 indexes each such run as ONE token, so stripping TOKENCHARS from the ends
# of every run makes the indexed token and the query token agree.
_RUN = re.compile(r"[\w\-.:/@]+")
_APOSTROPHES = str.maketrans("", "", "'\u2019")


def normalize(text: str) -> str:
    """Strip boundary TOKENCHARS from every token run, keeping inner ones.

    'gpuhost.' -> 'gpuhost', 'memd:' -> 'memd', '--ctx-size' -> 'ctx-size',
    '10.10.1.11' and 'bge-reranker-v2-m3' are unchanged. Idempotent.
    """
    if not text:
        return text
    return _RUN.sub(lambda m: m.group(0).strip(TOKENCHARS), text)


def _raw_tokens(text: str) -> set[str]:
    """Lowercased normalised token runs, without stopword/frame filtering."""
    if not text:
        return set()
    return {m.group(0) for m in _RUN.finditer(normalize(text.translate(_APOSTROPHES)).lower())}


def is_identifier(token: str) -> bool:
    """Exact-lookup shaped: inner TOKENCHARS, letters+digits mixed, or a 3+ digit number."""
    if len(token) >= 3 and token.isdigit():
        return True
    if any(c in TOKENCHARS for c in token[1:-1]):
        return True
    return any(c.isalpha() for c in token) and any(c.isdigit() for c in token)


def tokens(text: str) -> list[str]:
    """Filtered, normalised, lowercased, de-duplicated query tokens in order."""
    if not text:
        return []
    result: list[str] = []
    framing: list[str] = []
    for m in _RUN.finditer(normalize(text.translate(_APOSTROPHES)).lower()):
        tok = m.group(0)
        if len(tok) < 2:
            continue
        if tok in STOPWORDS or tok in FRAME_WORDS:
            if tok in FRAME_WORDS and tok not in framing:
                framing.append(tok)
            continue
        if tok not in result:
            result.append(tok)
    # A query made only of framing words is still a query.
    return result or framing


def document_frequency(db: sqlite3.Connection, term: str) -> int:
    """Get document frequency for a term using FTS5 match expression."""
    if not term:
        return 0
    expr = match_expr([term])
    if not expr:
        return 0
    try:
        cursor = db.execute(
            "SELECT count(*) FROM fts_notes WHERE fts_notes MATCH ?", (expr,)
        )
        row = cursor.fetchone()
        return row[0] if row else 0
    except sqlite3.Error:
        return 0


def match_expr(terms: list[str] | tuple[str, ...]) -> str:
    """Build FTS5 OR expression from terms. Empty if no terms."""
    if not terms:
        return ""
    quoted = []
    for t in terms:
        # Escape internal quotes by doubling
        escaped = t.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " OR ".join(quoted)


@dataclass(frozen=True)
class Distilled:
    """Result of query distillation."""
    terms: tuple[str, ...]
    idf: dict[str, float]
    n_docs: int
    dropped_saturated: tuple[str, ...]
    dropped_absent: tuple[str, ...]


def distill(
    db: sqlite3.Connection,
    query: str,
    *,
    max_terms: int = 12,
    saturation: float = 0.3
) -> Distilled:
    """Distill query terms by document frequency, dropping saturated/absent terms."""
    # One FTS count per token: bound the work for a pasted log or diff.
    toks = tokens(query)[:MAX_DF_TOKENS]
    
    if not toks:
        try:
            cursor = db.execute(
                "SELECT count(*) FROM notes WHERE superseded_by IS NULL"
            )
            row = cursor.fetchone()
            n_docs = row[0] if row else 0
        except sqlite3.Error:
            n_docs = 0
        return Distilled(
            terms=(),
            idf={},
            n_docs=n_docs,
            dropped_saturated=(),
            dropped_absent=()
        )
    
    try:
        cursor = db.execute(
            "SELECT count(*) FROM notes WHERE superseded_by IS NULL"
        )
        row = cursor.fetchone()
        n_docs = row[0] if row else 0
    except sqlite3.Error:
        n_docs = 0
    
    # Compute df for each token
    df_cache: dict[str, int] = {}
    for tok in toks:
        if tok not in df_cache:
            df_cache[tok] = document_frequency(db, tok)
    
    dropped_absent = []
    dropped_saturated = []
    kept = []
    
    if n_docs == 0:
        # Treat every token as kept with idf 1.0
        terms = tuple(toks)
        idf = {t: 1.0 for t in terms}
        return Distilled(
            terms=terms,
            idf=idf,
            n_docs=0,
            dropped_saturated=(),
            dropped_absent=()
        )
    
    for tok in toks:
        df = df_cache[tok]
        if df == 0:
            dropped_absent.append(tok)
        elif df / n_docs > saturation:
            if is_identifier(tok):
                kept.append(tok)
            else:
                dropped_saturated.append(tok)
        else:
            kept.append(tok)
    
    # If nothing kept but saturated exist, keep saturated
    if not kept and dropped_saturated:
        # Sort saturated by df ascending (from cache)
        saturated_sorted = sorted(
            dropped_saturated,
            key=lambda x: df_cache[x]
        )
        kept = saturated_sorted
        dropped_saturated = []
    
    # Order kept by (df ascending, first appearance index)
    # Build map of first appearance
    first_idx = {}
    for i, tok in enumerate(toks):
        if tok not in first_idx:
            first_idx[tok] = i
    
    # Sort by df then first_idx
    kept_sorted = sorted(
        kept,
        key=lambda x: (df_cache[x], first_idx[x])
    )
    
    # Truncate
    final_terms = kept_sorted[:max_terms]
    
    # Compute IDF
    idf = {}
    for t in final_terms:
        df = df_cache[t]
        if df > 0:
            idf[t] = math.log(1 + n_docs / (df + 1))
        else:
            idf[t] = 0.0  # Shouldn't happen
    
    return Distilled(
        terms=tuple(final_terms),
        idf=idf,
        n_docs=n_docs,
        dropped_saturated=tuple(dropped_saturated),
        dropped_absent=tuple(dropped_absent)
    )


def coverage(text: str, distilled: Distilled) -> float:
    """Compute IDF-weighted coverage of distilled terms in text."""
    if not distilled.terms:
        return 0.0
    
    present = _raw_tokens(text)
    norm_lower = normalize(text).lower()
    
    total_idf = 0.0
    weighted_score = 0.0
    
    for term in distilled.terms:
        if term in distilled.idf:
            idf_val = distilled.idf[term]
            if idf_val <= 0:
                continue
            total_idf += idf_val
            if term in present:
                weighted_score += idf_val * 1.0
            elif term in norm_lower:
                weighted_score += idf_val * 0.25
            # else 0
    
    if total_idf == 0:
        return 0.0
    return weighted_score / total_idf
