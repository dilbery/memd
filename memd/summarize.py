"""Propose "where things stand now" summary notes, one per topic cluster (PROPOSE-ONLY).

The weakest recall category is "what is the current X": a topic accumulates
many dated notes with near-identical vocabulary, and the answer is the newest
fact, often inside a long note. This job groups such notes, asks a chat model
(memd.llm) for the current state with every claim citing a source slug, and
writes the result as ordinary notes (``kind: summary``) to a REVIEW BRANCH of
the clone. Nothing reaches the store until a human merges that branch, exactly
like reflect's tidy report; unlike reflect it never checks the branch out, so
the shared working tree and save()'s HEAD are never touched.

Clustering is deterministic and bounded:
  * candidates are live notes: not superseded, not retracted, not summaries;
  * topic keys are a note's tags plus its slug stem (the slug without dates and
    filler words, first two words); a key shared by too much of the corpus says
    nothing about topic and is ignored;
  * two notes sharing a key are linked when their titles or opening bodies are
    lexically similar (the token rule dedup.py uses); linked components are
    clusters;
  * a cluster is worth summarising with MIN_DATED dated notes and at least one
    changeable-state note (volatility state/volatile, see memd.staleness).

A summary records its sources' git blobs. It is current while those blobs are
unchanged and no new note has joined its cluster; otherwise it is stale and a
refreshed version is proposed at the same path, so re-runs update rather than
duplicate. An unchanged proposal already waiting on the review branch is reused
without another model call.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from memd.config import Config
from memd.dedup import _TOKEN
from memd.llm import LLMError, chat, enabled
from memd.slug import slugify
from memd.staleness import as_of
from memd.store import (RETRACTED, Note, StoreUnavailable, clone_lock, dump_note, list_notes,
                        parse_text)

SUMMARY_KIND = "summary"
GENERATOR = "mem-summarize"
DEFAULT_BRANCH = "memd/summaries"   # MEMD_SUMMARY_BRANCH overrides

MIN_DATED = 3            # dated notes a cluster needs before a summary is worth it
MAX_CLUSTERS = 10        # proposals (model calls) per run
MAX_NOTES = 12           # newest notes per cluster sent to the model
NOTE_CHARS = 3000        # body characters per note in the prompt
KEY_MAX_NOTES = 60       # a topic key on more notes than this is not a topic
KEY_MAX_SHARE = 0.2      # ... nor is one on more than this share of a corpus >= 20
LINK_SIMILARITY = 0.2    # Jaccard of title or opening-body tokens to link two notes
OPENING_CHARS = 1500
MAX_CLAIMS = 12
MAX_HISTORY = 12
CLAIM_CHARS = 500
# Importance 4 would put every summary in recall's always-on core index; 3 keeps
# them competing on relevance like any note. --importance raises it.
DEFAULT_IMPORTANCE = 3

_FILLER = frozenset({
    "a", "an", "and", "the", "of", "for", "on", "in", "to", "at", "with",
    "project", "feedback", "reference", "user", "note", "notes", "log",
    "status", "update", "updates", "current", "latest", "state", "now",
    "summary", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
    "oct", "nov", "dec",
})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FENCE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL)


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


@dataclass
class Cluster:
    key: str                     # stable topic key, "tag:<t>" or "stem:<a-b>"
    notes: list[Note]            # newest first, at most MAX_NOTES
    tags: list[str] = field(default_factory=list)

    @property
    def topic(self) -> str:
        return self.key.split(":", 1)[-1].replace("-", " ").replace("_", " ")

    @property
    def slugs(self) -> list[str]:
        return [n.slug for n in self.notes]


def is_summary(note: Note) -> bool:
    return note.metadata.get("kind") == SUMMARY_KIND


def note_date(note: Note) -> dt.date | None:
    try:
        return as_of(note.to_dict())
    except Exception:
        return None


def is_changeable(note: Note) -> bool:
    return note.volatility in ("state", "volatile")


def slug_stem(slug: str) -> str | None:
    """The slug's first two content words, without dates or filler; None if too short."""
    words = [w for w in re.split(r"[-_]+", slug.lower())
             if w and not w.isdigit() and w not in _FILLER]
    return "-".join(words[:2]) if len(words) >= 2 else None


def topic_keys(note: Note) -> set[str]:
    keys = {f"tag:{t}" for t in (slugify(str(tag)) for tag in note.tags) if t and t != SUMMARY_KIND}
    stem = slug_stem(note.slug)
    if stem:
        keys.add(f"stem:{stem}")
    return keys


def _words(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN.findall(text) if len(t) > 1}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _newest_first(notes: list[Note]) -> list[Note]:
    # Dated notes newest first, undated after them; slug breaks ties so the
    # order never depends on filesystem enumeration.
    return sorted(notes, key=lambda n: (note_date(n) is None,
                                        -(note_date(n) or dt.date.min).toordinal(), n.slug))


def eligible(notes: list[Note]) -> list[Note]:
    return sorted((n for n in notes
                   if not n.superseded_by and n.slug != RETRACTED and not is_summary(n)),
                  key=lambda n: n.slug)


def cluster_notes(notes: list[Note], *, min_dated: int = MIN_DATED,
                  max_notes: int = MAX_NOTES) -> list[Cluster]:
    """Deterministic topic clusters worth summarising, newest topic first."""
    live = eligible(notes)
    keys = {n.slug: topic_keys(n) for n in live}
    members: dict[str, list[int]] = {}
    for i, n in enumerate(live):
        for k in sorted(keys[n.slug]):
            members.setdefault(k, []).append(i)
    cap = KEY_MAX_NOTES
    if len(live) >= 20:
        cap = min(cap, max(2, int(KEY_MAX_SHARE * len(live))))
    usable = {k: idx for k, idx in members.items() if 2 <= len(idx) <= cap}

    titles = [_words(f"{n.title} {' '.join(map(str, n.tags))}") for n in live]
    openings = [_words(n.body[:OPENING_CHARS]) for n in live]
    parent = list(range(len(live)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for k in sorted(usable):
        idx = usable[k]
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                i, j = idx[a], idx[b]
                if (_jaccard(titles[i], titles[j]) >= LINK_SIMILARITY
                        or _jaccard(openings[i], openings[j]) >= LINK_SIMILARITY):
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        parent[max(ri, rj)] = min(ri, rj)

    groups: dict[int, list[Note]] = {}
    for i, n in enumerate(live):
        groups.setdefault(find(i), []).append(n)

    clusters: list[Cluster] = []
    for group in groups.values():
        if len(group) < 2:
            continue
        dated = [n for n in group if note_date(n) is not None]
        if len(dated) < min_dated or not any(is_changeable(n) for n in group):
            continue
        counts = Counter(k for n in group for k in keys[n.slug] if k in usable)
        # The key most members share names the topic; ties go to the slug stem
        # (two words, usually more specific than a tag), then alphabetical, so
        # the key is stable across runs.
        key = min(counts, key=lambda k: (-counts[k], not k.startswith("stem:"), k))
        tag_counts = Counter(t for n in group for t in {slugify(str(x)) for x in n.tags} if t)
        tags = sorted(t for t, c in tag_counts.items() if 2 * c >= len(group) and t != SUMMARY_KIND)[:5]
        clusters.append(Cluster(key=key, notes=_newest_first(group)[:max_notes], tags=tags))
    clusters.sort(key=lambda c: (-(note_date(c.notes[0]) or dt.date.min).toordinal(), c.key))
    return clusters


# ---------------------------------------------------------------------------
# Plan: new, stale or current, against the summaries already in the store
# ---------------------------------------------------------------------------


@dataclass
class Plan:
    cluster: Cluster
    status: str                  # new | stale | current
    existing: Note | None = None
    reasons: list[str] = field(default_factory=list)


def _sources(summary: Note) -> list[str]:
    raw = summary.metadata.get("sources") or []
    return [str(s) for s in raw] if isinstance(raw, list) else []


def _revisions(summary: Note) -> dict[str, str]:
    raw = summary.metadata.get("source_revisions") or {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def stale_reasons(summary: Note, by_slug: dict[str, Note],
                  cluster: Cluster | None = None) -> list[str]:
    """Why a summary no longer reflects its sources; empty when it is current."""
    reasons: list[str] = []
    recorded = _revisions(summary)
    for slug in _sources(summary) or sorted(recorded):
        note = by_slug.get(slug)
        if note is None:
            reasons.append(f"source {slug} no longer exists")
        elif note.superseded_by:
            reasons.append(f"source {slug} was superseded by {note.superseded_by}")
        elif recorded.get(slug) != note.git_blob:
            reasons.append(f"source {slug} changed")
    if cluster is not None:
        known = set(_sources(summary)) | set(recorded)
        reasons += [f"new note {s}" for s in cluster.slugs if s not in known]
    return reasons


def _follow(slug: str, by_slug: dict[str, Note]) -> Note | None:
    """The live note a (possibly superseded) slug now resolves to."""
    seen: set[str] = set()
    note = by_slug.get(slug)
    while note is not None and note.superseded_by and note.slug not in seen:
        seen.add(note.slug)
        note = by_slug.get(note.superseded_by)
    return note if note is not None and not note.superseded_by else None


def plan(notes: list[Note], *, min_dated: int = MIN_DATED,
         max_notes: int = MAX_NOTES) -> list[Plan]:
    by_slug = {n.slug: n for n in notes}
    summaries = sorted((n for n in notes if is_summary(n) and not n.superseded_by),
                       key=lambda n: n.slug)
    unmatched = list(summaries)
    plans: list[Plan] = []
    for cluster in cluster_notes(notes, min_dated=min_dated, max_notes=max_notes):
        existing = next((s for s in unmatched if s.metadata.get("cluster") == cluster.key), None)
        if existing is None:
            # A cluster whose topic key drifted (a new tag became the majority)
            # still maps to the summary that already covers most of it.
            members = set(cluster.slugs)
            scored = [(len(members & set(_sources(s))) / max(1, len(_sources(s))), s)
                      for s in unmatched]
            scored = [(r, s) for r, s in scored if r >= 0.5]
            if scored:
                existing = min(scored, key=lambda rs: (-rs[0], rs[1].slug))[1]
        if existing is None:
            plans.append(Plan(cluster, "new"))
            continue
        unmatched.remove(existing)
        reasons = stale_reasons(existing, by_slug, cluster)
        plans.append(Plan(cluster, "stale" if reasons else "current", existing, reasons))

    # A summary whose cluster no longer qualifies is still checked: when its
    # sources moved on it is re-proposed over what they resolve to now.
    for summary in unmatched:
        reasons = stale_reasons(summary, by_slug)
        if not reasons:
            continue
        live = {n.slug: n for n in (_follow(s, by_slug) for s in _sources(summary)) if n is not None}
        if len(live) < 2:
            plans.append(Plan(Cluster(str(summary.metadata.get("cluster") or f"stem:{summary.slug}"), []),
                              "orphaned", summary, reasons + ["too few live sources to re-summarise"]))
            continue
        cluster = Cluster(key=str(summary.metadata.get("cluster") or f"stem:{summary.slug}"),
                          notes=_newest_first(list(live.values()))[:max_notes],
                          tags=[t for t in summary.tags if t != SUMMARY_KIND])
        plans.append(Plan(cluster, "stale", summary, reasons))
    return plans


# ---------------------------------------------------------------------------
# Prompt and model output
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You maintain a shared memory store of Markdown notes. You are given several \
dated notes about one topic, newest first. Work out where things stand NOW.

Rules:
- Newer notes override older ones. When notes disagree, the newest dated note wins.
- State only facts found in the notes. Do not guess or add advice.
- Every claim cites the slug(s) of the note(s) it comes from, exactly as given \
in square brackets.
- "current" lists the present state, most important first, one fact per claim.
- "history" lists the earlier states or changes that led here, oldest first.
- "description" is one sentence stating the current state.

Answer with JSON only, no prose and no code fence:
{"description": "...", \
"current": [{"claim": "...", "sources": ["slug"]}], \
"history": [{"date": "YYYY-MM-DD", "event": "...", "sources": ["slug"]}]}
"""


def build_messages(cluster: Cluster, *, today: dt.date,
                   note_chars: int = NOTE_CHARS) -> list[dict]:
    parts = [f"Topic: {cluster.topic}", f"Today: {today.isoformat()}", ""]
    for n in cluster.notes:
        d = note_date(n)
        body = n.body if len(n.body) <= note_chars else n.body[:note_chars] + "\n[... truncated]"
        parts += [
            f"### [{n.slug}] {n.title}",
            f"date: {d.isoformat() if d else 'undated'}; volatility: {n.volatility or 'unlabelled'}",
            "",
            body,
            "",
        ]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts).rstrip() + "\n"},
    ]


class SummaryParseError(ValueError):
    """The model's reply could not be turned into a cited summary."""


@dataclass
class Summary:
    description: str
    current: list[tuple[str, list[str]]]
    history: list[tuple[str, str, list[str]]]      # (date or "", event, sources)


def _json_object(text: str) -> dict:
    text = text.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise SummaryParseError("reply contains no JSON object") from None
        try:
            data = json.loads(text[start:end + 1])
        except ValueError as e:
            raise SummaryParseError(f"reply is not valid JSON: {e}") from None
    if not isinstance(data, dict):
        raise SummaryParseError("reply JSON is not an object")
    return data


def _one_line(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _cited(item: dict, allowed: set[str]) -> list[str]:
    raw = item.get("sources", item.get("source", []))
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for s in raw:
        if isinstance(s, str):
            s = s.strip().strip("[]`'\" ")
            if s in allowed and s not in out:
                out.append(s)
    return out


def parse_summary(text: str, allowed: list[str] | set[str]) -> Summary:
    """Validate a model reply. Claims citing no known source are dropped."""
    allowed = set(allowed)
    data = _json_object(text)
    current: list[tuple[str, list[str]]] = []
    raw_current = data.get("current")
    for item in raw_current if isinstance(raw_current, list) else []:
        if not isinstance(item, dict):
            continue
        claim, cites = _one_line(item.get("claim"), CLAIM_CHARS), _cited(item, allowed)
        if claim and cites:
            current.append((claim, cites))
        if len(current) >= MAX_CLAIMS:
            break
    if not current:
        raise SummaryParseError("reply has no current-state claim citing a known source")
    history: list[tuple[str, str, list[str]]] = []
    raw_history = data.get("history")
    for item in raw_history if isinstance(raw_history, list) else []:
        if not isinstance(item, dict):
            continue
        event, cites = _one_line(item.get("event"), CLAIM_CHARS), _cited(item, allowed)
        date = item.get("date") if isinstance(item.get("date"), str) and _DATE.match(item["date"]) else ""
        if event and cites:
            history.append((date, event, cites))
        if len(history) >= MAX_HISTORY:
            break
    description = _one_line(data.get("description"), 200) or _one_line(current[0][0], 200)
    return Summary(description=description, current=current, history=history)


# ---------------------------------------------------------------------------
# The summary note
# ---------------------------------------------------------------------------


def _cite(slugs: list[str]) -> str:
    return "(" + ", ".join(f"`{s}`" for s in slugs) + ")"


def summary_slug(cluster: Cluster, taken: set[str]) -> str:
    base = "current-state-" + (slugify(cluster.topic) or "topic")
    slug, n = base, 1
    while slug in taken:
        n += 1
        slug = f"{base}-{n}"
    return slug


def _rel(clone: Path, path: str) -> str:
    p = Path(path)
    return p.relative_to(clone).as_posix() if p.is_absolute() else p.as_posix()


def _new_path(clone: Path, slug: str) -> str:
    """A new summary's file name: the store codec's name for its slug."""
    from memd.codec import codec_for
    return codec_for(clone).filename(slug)


def render_note(cluster: Cluster, summary: Summary, *, clone: Path, existing: Note | None,
                taken: set[str], model: str, importance: int = DEFAULT_IMPORTANCE) -> Note:
    newest = max((d for d in map(note_date, cluster.notes) if d), default=None)
    slug = existing.slug if existing else summary_slug(cluster, taken)
    hosts = {n.host for n in cluster.notes}
    profiles = Counter(n.profile for n in cluster.notes)
    lines = [
        f"_As of {newest.isoformat() if newest else 'unknown'}: the current state of "
        f"{cluster.topic}, summarised from {len(cluster.notes)} notes. "
        "Each line cites its source; read the source before acting on a detail._",
        "",
        "## Now",
        "",
        *[f"- {claim} {_cite(cites)}" for claim, cites in summary.current],
    ]
    if summary.history:
        lines += ["", "## History", ""]
        lines += [f"- {d + ': ' if d else ''}{event} {_cite(cites)}"
                  for d, event, cites in summary.history]
    lines += ["", "## Sources", ""]
    for n in cluster.notes:
        d = note_date(n)
        lines.append(f"- `{n.slug}` {n.title}{f' ({d.isoformat()})' if d else ''}")
    metadata = {
        "kind": SUMMARY_KIND,
        "cluster": cluster.key,
        "as_of": newest.isoformat() if newest else None,
        "sources": cluster.slugs,
        "source_revisions": {n.slug: n.git_blob for n in cluster.notes},
        "generated_by": GENERATOR,
    }
    if model:
        metadata["model"] = model
    return Note(
        title=f"Current state: {cluster.topic}",
        slug=slug,
        path=_rel(clone, existing.path) if existing else _new_path(clone, slug),
        body="\n".join(lines),
        profile=profiles.most_common(1)[0][0] if profiles else "amber",
        host=hosts.pop() if len(hosts) == 1 else "any",
        importance=importance,
        tags=[SUMMARY_KIND, *[t for t in cluster.tags if t != SUMMARY_KIND]],
        grounding="unverified-remote",
        description=summary.description,
        observed_at=newest.isoformat() if newest else None,
        volatility="state",
        metadata={k: v for k, v in metadata.items() if v is not None},
    )


# ---------------------------------------------------------------------------
# Review branch (never the working tree)
# ---------------------------------------------------------------------------


def _git(clone: Path, *args: str, env: dict | None = None, input: str | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(clone), *args], check=check, capture_output=True,
                          text=True, env=env, input=input, timeout=30)


def branch_name() -> str:
    return os.environ.get("MEMD_SUMMARY_BRANCH", "").strip() or DEFAULT_BRANCH


def _blob_bytes(clone: Path, spec: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(clone), "show", spec], capture_output=True, timeout=30)
    return r.stdout if r.returncode == 0 else None


def pending_text(clone: Path, branch: str, path: str) -> str | None:
    """A note as it stands on the review branch (decoded by the store codec), or None."""
    from memd.codec import codec_for
    data = _blob_bytes(clone, f"refs/heads/{branch}:{path}")
    if data is None:
        return None
    try:
        return codec_for(clone).decode(path, data)[0]
    except (StoreUnavailable, UnicodeDecodeError):
        return None


def write_proposals(clone: Path, files: dict[str, str], *, branch: str, message: str) -> str:
    """Commit ``files`` on top of HEAD as ``branch``; returns the commit sha.

    Built with a throwaway index (read-tree/update-index/write-tree/commit-tree),
    so the clone's checkout, index and HEAD are untouched and a concurrent
    save() sees nothing. The branch is replaced wholesale each run: it always
    holds exactly this run's proposals over the current store. An identical
    proposal on the same base is left as it is.

    Each text is stored through the store's codec, so an encrypted store's
    review branch holds envelopes and a generic commit message too.
    """
    from memd.codec import codec_for
    codec = codec_for(clone)
    message = codec.commit_message(message, proposal=True)
    with clone_lock(clone):
        base = _git(clone, "rev-parse", "HEAD").stdout.strip()
        with tempfile.TemporaryDirectory(prefix="memd-summarize-") as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            _git(clone, "read-tree", base, env=env)
            for path, text in sorted(files.items()):
                blob = None
                if codec.encrypted:
                    # Reuse an identical pending envelope: a fresh nonce would
                    # otherwise rebuild an unchanged branch on every run.
                    spec = f"refs/heads/{branch}:{path}"
                    prior = _blob_bytes(clone, spec)
                    if prior is not None and codec.unchanged(path, prior, text):
                        blob = _git(clone, "rev-parse", spec).stdout.strip()
                if blob is None:
                    blob = subprocess.run(
                        ["git", "-C", str(clone), "hash-object", "-w", "--stdin"],
                        check=True, capture_output=True, timeout=30,
                        input=codec.encode(path, text)).stdout.decode().strip()
                _git(clone, "update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
            tree = _git(clone, "write-tree", env=env).stdout.strip()
        current = _git(clone, "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}", check=False)
        if current.returncode == 0:
            sha = current.stdout.strip()
            same = _git(clone, "show", "-s", "--format=%T %P", sha).stdout.split()
            if same == [tree, base]:
                return sha
        sha = _git(clone, "commit-tree", tree, "-p", base, "-m", message).stdout.strip()
        _git(clone, "update-ref", f"refs/heads/{branch}", sha)
        return sha


def push_branch(clone: Path, branch: str) -> None:
    _git(clone, "push", "--force", "origin", f"refs/heads/{branch}:refs/heads/{branch}")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    plan: Plan
    note: Note | None = None
    text: str | None = None
    error: str | None = None
    reused: bool = False


def run(cfg: Config, *, dry_run: bool = False, max_clusters: int = MAX_CLUSTERS,
        max_notes: int = MAX_NOTES, min_dated: int = MIN_DATED,
        importance: int = DEFAULT_IMPORTANCE, branch: str | None = None,
        today: dt.date | None = None) -> dict:
    """Plan, summarise and (unless dry_run) commit proposals to the review branch."""
    clone = Path(cfg.clone)
    branch = branch or branch_name()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    notes = list_notes(clone)
    plans = plan(notes, min_dated=min_dated, max_notes=max_notes)
    taken = {n.slug for n in notes}
    outcomes: list[Outcome] = []
    todo = [p for p in plans if p.status in ("new", "stale")][:max(0, max_clusters)]
    for p in plans:
        if p not in todo:
            outcomes.append(Outcome(p))
            continue
        out = Outcome(p)
        outcomes.append(out)
        path = _rel(clone, p.existing.path) if p.existing else _new_path(clone, summary_slug(p.cluster, taken))
        revisions = {n.slug: n.git_blob for n in p.cluster.notes}
        waiting = pending_text(clone, branch, path)
        if waiting is not None:
            prior = parse_text(waiting, path=path)
            if (is_summary(prior) and _revisions(prior) == revisions
                    and prior.metadata.get("cluster") == p.cluster.key):
                out.note, out.text, out.reused = prior, waiting, True
                taken.add(prior.slug)
                continue
        if not enabled(cfg):
            out.error = "chat model off (set MEMD_LLM_URL)"
            continue
        try:
            reply = chat(build_messages(p.cluster, today=today), cfg=cfg,
                         max_tokens=1500, temperature=0.0)
            summary = parse_summary(reply, p.cluster.slugs)
        except (LLMError, SummaryParseError) as e:
            out.error = str(e)
            continue
        out.note = render_note(p.cluster, summary, clone=clone, existing=p.existing,
                               taken=taken, model=cfg.llm_model, importance=importance)
        out.text = dump_note(out.note)
        taken.add(out.note.slug)

    proposals = {o.note.path: o.text for o in outcomes if o.note is not None and o.text}
    commit = None
    if proposals and not dry_run:
        commit = write_proposals(
            clone, proposals, branch=branch,
            message=f"summarize: propose {len(proposals)} current-state summaries {today.isoformat()}",
        )
    return {
        "branch": branch if commit else None,
        "commit": commit,
        "dry_run": dry_run,
        "proposed": len(proposals),
        "clusters": [{
            "key": o.plan.cluster.key,
            "status": o.plan.status,
            "summary": (o.note.slug if o.note else o.plan.existing.slug if o.plan.existing else None),
            "path": o.note.path if o.note else None,
            "sources": o.plan.cluster.slugs,
            "reasons": o.plan.reasons,
            "reused": o.reused,
            "error": o.error,
            "deferred": o.plan.status in ("new", "stale") and o.plan not in todo,
        } for o in outcomes],
        "_texts": {o.note.path: o.text for o in outcomes if o.note is not None and o.text},
    }


def _print_report(report: dict, *, show_text: bool) -> None:
    for c in report["clusters"]:
        line = f"{c['status']:<8} {c['key']}"
        if c["summary"]:
            line += f" -> {c['summary']}"
        if c["reused"]:
            line += " (unchanged proposal reused)"
        if c["deferred"]:
            line += " (deferred: over --max-clusters)"
        if c["error"]:
            line += f" SKIPPED: {c['error']}"
        print(line)
        for r in c["reasons"]:
            print(f"    - {r}")
        print(f"    sources: {', '.join(c['sources'])}")
    if show_text:
        for path, text in report["_texts"].items():
            print(f"\n===== {path} =====\n{text}", end="")
    if report["dry_run"]:
        print(f"\ndry-run: {report['proposed']} proposal(s), nothing written.")
    elif report["commit"]:
        print(f"\nproposed {report['proposed']} summary note(s) on branch {report['branch']} "
              f"({report['commit'][:12]}). Review, then merge it into the store's branch to approve.")
    else:
        print("\nnothing to propose.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mem-summarize",
        description="Propose current-state summary notes on a review branch (never the store).")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and proposed notes; write nothing")
    ap.add_argument("--json", action="store_true", help="emit the report as JSON")
    ap.add_argument("--max-clusters", type=int, default=MAX_CLUSTERS)
    ap.add_argument("--max-notes", type=int, default=MAX_NOTES)
    ap.add_argument("--min-dated", type=int, default=MIN_DATED)
    ap.add_argument("--importance", type=int, default=DEFAULT_IMPORTANCE, choices=range(1, 6))
    ap.add_argument("--branch", default=None, help=f"review branch (default {DEFAULT_BRANCH})")
    ap.add_argument("--push", action="store_true", help="push the review branch to origin")
    ap.add_argument("--pr", action="store_true",
                    help="open a Forgejo pull request for the branch (implies --push; see reflect)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    cfg = Config.from_env()
    if cfg.clone is None:
        print("mem-summarize: no clone configured (MEMD_CLONE / MEMD_PROFILE)", file=sys.stderr)
        return 2
    if not args.dry_run and not enabled(cfg):
        print("mem-summarize: MEMD_LLM_URL is not set; use --dry-run to see the plan",
              file=sys.stderr)
        return 2
    report = run(cfg, dry_run=args.dry_run, max_clusters=args.max_clusters,
                 max_notes=args.max_notes, min_dated=args.min_dated,
                 importance=args.importance, branch=args.branch)
    if report["commit"] and (args.push or args.pr):
        push_branch(Path(cfg.clone), report["branch"])
        if args.pr:
            from memd.reflect import open_review_pr
            os.environ.setdefault("MEMD_CLONE", str(cfg.clone))
            report["pr"] = open_review_pr(
                report["branch"],
                title=f"summarize: {report['proposed']} current-state summaries",
                body="PROPOSE-ONLY. Each summary cites its source notes; check the claims "
                     "against them, then merge to approve.",
            )
    if args.json:
        print(json.dumps({k: v for k, v in report.items() if k != "_texts"}))
    else:
        _print_report(report, show_text=args.dry_run)
        if report.get("pr"):
            print(f"pull request: {report['pr']}")
    if args.dry_run and not enabled(cfg):
        return 0   # a plan-only dry run has nothing to fail
    attempted = [c for c in report["clusters"] if c["status"] in ("new", "stale") and not c["deferred"]]
    return 1 if attempted and all(c["error"] for c in attempted) else 0


if __name__ == "__main__":
    raise SystemExit(main())
