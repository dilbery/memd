# Recall eval

`python -m memd.recall_eval` measures how well `recall()` ranks notes against a
hand-labelled golden set. Run it before and after any change to retrieval,
fusion, reranking or the FTS index; it exits 1 when `--baseline` says the change
made recall worse.

## Files

The golden set and baseline describe a private note corpus, so they are kept
outside the repository (`eval/golden-*.jsonl` and `eval/baseline-*.json` are
git-ignored). A golden set is JSON Lines, one labelled query per line:

```json
{"id": "q001", "query": "why does the backup job fail on the nas", "category": "real",
 "split": "dev", "gold": [{"slug": "backup-job-nas-timeout", "grade": 2}], "why": "root cause note"}
```

- Categories used so far: `real` (verbatim prompts, what the recall hook actually
  sends), `identifier`, `paraphrase`, `agent-framed`, `current-state`, `multi`.
- Every third id is `split: test`: tune on `dev`, report `test`. A `fresh` split
  holds queries labelled after tuning and used only for validation.
- Grades: 2 = the note that answers, 1 = useful context. Label by reading the
  corpus, never by asking memd (that would bias the set toward the current ranker).
- `--baseline` names a stored per-query run of the current ranking to compare against.
- Usage-derived candidates: `mem-usage export-golden` turns logged recalls that an
  agent followed with a read into rows marked `source: "usage"` and
  `needs_review: true` (category `usage`, split one in three `test` by query hash,
  or `--split fresh`). They load as they are; `--reviewed-only` skips rows still
  marked for review. A read is evidence, not a label and is biased toward what the
  ranker already showed: confirm each slug against the corpus (and add gold the
  ranker missed), regrade, set the real category, then drop `needs_review`.

The results below come from a 171-query set (plus 90 `fresh` queries) against a
984-note private corpus.

## Public synthetic set (CI)

`eval/public/` holds a set anyone can run, and CI runs it on every push and pull
request. `generate.py` (seeded; no network, no model) writes a fictional home-lab
corpus in memd's note format, 172 notes about gpuhost, vmhost, lapbox and apphost, to
`corpus/`, and 123 labelled queries in the six categories above to `golden.jsonl`
(every third row `test`). Labels come from the generator itself: each question is
written next to the note that answers it. The corpus has the shapes that make the real
set hard: topics with 4 to 10 dated notes of near-identical wording where the newest is
the answer, long umbrella notes with one fact buried past the opening, superseded notes,
and unqueried distractors. Edit the generator, never its output, and re-run it; a test
fails when the committed files differ from what it writes.

```bash
.venv/bin/python eval/public/generate.py            # after changing the generator
.venv/bin/python -m memd.recall_eval --golden eval/public/golden.jsonl \
    --corpus eval/public/corpus --embedder hash --rerank none \
    --baseline eval/public/baseline.json            # what CI runs, ~5 s
```

`--corpus` commits the notes into a scratch clone with a fixed author and date, so its
HEAD sha is the same everywhere. A runner has no model service, so `--embedder hash`
swaps in a deterministic 768-d hashed bag of words, word pairs and character trigrams,
and `--rerank none` keeps the fused order instead of calling the cross-encoder. The
keyword and vector arms, weighted RRF, per-chunk collapse, the current-state gate and
fresh arm, and the candidate budgets all run as in production; only the two models are
stand-ins. CI fails when the verdict against `baseline.json` is FAIL (overall
recall@10, MRR@10 or nDCG@10 down by 0.01, or one category with at least 3 queries
down by 0.03 nDCG@10) and writes the table to the job summary. A change that improves
it should commit a new baseline with `--write-baseline eval/public/baseline.json`.

What it can measure: a change to fusion, the arms, chunking, the gate or the budgets
that moves ranking. Switching `--recall-vectors notes` costs 0.025 nDCG@10,
interleaving the arms instead of RRF 0.039, and disabling the fresh arm drops
current-state 0.13.

What it cannot measure:

- semantic quality. The hash embedder knows no synonyms, so paraphrase questions
  that share no words with their note score near zero and the vector arm is
  mostly a second keyword arm. Do not tune embedding-specific knobs on it.
- the reranker and the rerank blend. With `--rerank none` the blend is the
  identity; measure reranker changes on the private set.
- real wording. The notes are templated and cleaner than real ones, and identifier
  questions are nearly solved (nDCG@10 0.99), so a gain here is necessary, not
  sufficient: confirm it on the private set before trusting it.

Baseline (2026-09-28, hash embedder, no rerank, per-chunk vectors):

| split | n | recall@10 | MRR@10 | nDCG@10 | pool |
|---|---|---|---|---|---|
| all | 123 | 0.873 | 0.705 | 0.731 | 0.968 |
| dev | 82 | 0.874 | 0.726 | 0.751 | 0.963 |
| test | 41 | 0.870 | 0.664 | 0.693 | 0.976 |

Weakest: paraphrase (0.451) and current-state (0.510: the right note is in the
pool and top 10 but an older note of its topic usually ranks above it).

## Running

The corpus is a scratch clone; the index and replay cache are scratch files.
Embeddings must come from the same model production uses (apphost's
`memd-embed`, bound to its localhost), so tunnel it:

```bash
ssh -N -L 18079:127.0.0.1:8079 svcuser@10.10.1.10 &      # nomic-embed-text-v1, as production
git clone <your-notes-repo-ssh-url> /tmp/eval-corpus
git -C /tmp/eval-corpus checkout <snapshot-sha>          # the snapshot the labels describe
python -m memd.recall_eval --golden eval/golden-mine.jsonl --clone /tmp/eval-corpus \
    --db /tmp/eval.db --cache /tmp/eval-replay.sqlite --baseline eval/baseline-mine.json
```

The first run embeds the whole corpus (~15 min on apphost's CPU; production
recall's vector arm may fall back to keyword while it runs). After that every
embed/rerank result is replayed from the cache; `--offline` fails on any miss
instead of calling a backend, which makes a run exactly reproducible.
`--show q010` prints one query's gold, ranking and per-arm candidates.

The `pool` column is gold found anywhere in the candidate pool before the rerank
cut. A high pool with a low recall@10 is a ranking problem, not retrieval.

## Results (2026-09-27)

| ranking | nDCG@10 | MRR@10 | recall@10 | pool |
|---|---|---|---|---|
| production `2a70f4a` (interleave arms, rerank top 10) | 0.737 | 0.834 | 0.760 | 0.923 |
| FTS normalisation + distilled keyword arm + weighted RRF + rerank/fused blend | **0.777** | 0.858 | **0.822** | 0.943 |

Held-out test split: nDCG@10 +0.056 [+0.010, +0.110], recall@10 +0.081
[+0.021, +0.143]. Biggest gains: current-state, multi and paraphrase (+0.14 each
on test). Tried and rejected on dev: embedding the distilled query instead of
the raw prompt (-0.013), a larger keyword weight, and a bigger rerank head
without blending (the cross-encoder alone caps quality; fused order alone was
already as good), and a recency boost for "current/latest/now" queries (+0.09
to +0.13 on dev current-state, but on test current-state did not move and
agent-framed fell 0.06: incidental "now"/"still" in real prompts trigger it).

### 2026-09-28: current-state queries

"What is the current X" was the weakest category (nDCG@10 0.54). Diagnosis: a
topic has many dated notes with near-identical vocabulary, and the newest (the
answer) was often outside the candidate pool (pool rank 60-90 or absent), and
the reranker judged every note by its first 1000 body characters although 95%
of answers are longer. Fix, both measured on the 147 held-out (test + fresh)
queries against the 09-27 ranking: nDCG@10 +0.045 [+0.023, +0.069], no category
worse, current-state 0.574 -> 0.627:

- a strict present-state intent gate ("current", "latest", "right now", "at the
  moment", "these days", trailing "now"); when it fires, a third candidate list
  of the 30 newest notes holding at least 30% of the query's IDF weight joins
  the fusion. The gate never fires on the 45 controls' history questions.
- the reranker sees "title + opening body" (titles here are dense summaries);
  +32 ms for 15 documents.

Tried and rejected: a recency multiplier on fused scores, reserved rerank seats
for fresh notes, an always-on fresh list (-0.009, paraphrase -0.027), a
query-centred body window for the reranker (lost on paraphrase/multi), stripping
the intent phrase before distillation (no gain), and nomic `search_document:` /
`search_query:` prefixes with or without titles in the embedded text (nDCG within
noise, pool recall slightly lower; not worth a full re-embed). Still weakest:
current-state (0.62) where the answer is one fact inside a long umbrella note,
and paraphrase (0.77) second-relevant notes; per-chunk embeddings are the next
lever for both.

### 2026-09-28: per-chunk vectors (landed, not yet measured)

The vector arm now searches per-chunk vectors (`memd/chunk.py`: heading- then
paragraph-aligned chunks of ~1000 characters, 150-character overlap within a
section, title and heading path prefixed to the embedded text). A note scores
its best chunk's cosine plus 0.005 per further matching chunk (at most 2), and
the reranker sees a 200-character lead plus the best chunk when that chunk lies
past the opening. Whole-note vectors are still indexed, so `--recall-vectors
notes` (in production `MEMD_RECALL_VECTORS=notes`) gives the previous arm on the
same index for an A/B.

This has **not** been run against the golden set: the set and corpus are
private and were not available where the change was written, so there is no
measured gain yet. Before trusting it, run `python -m memd.recall_eval` as
above with `--baseline eval/baseline-mine.json` (the first run re-embeds every
chunk, several times the old per-note work), compare against
`--recall-vectors notes`, and check current-state and paraphrase on the
held-out splits. Knobs to try on dev: `CHUNK_HIT_BONUS` (0 = pure max),
`RERANK_CHUNK`, `RERANK_LEAD_CHARS` in `memd/recall.py`, and the chunk sizes in
`memd/chunk.py` (bump `_CHUNK_VERSION` in `memd/index.py` when they change).

When the corpus moves on, labels can go stale (new notes that deserve gold).
Re-label rather than re-lock blindly; `winners`/`losers` in the compare output
show which queries to look at.
