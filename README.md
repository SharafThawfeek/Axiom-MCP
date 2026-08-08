# Axiom Debug

AI that debugs your failures using the world's fixes.

Paste a failing CI log. Axiom Debug parses the traceback, investigates it
with an LLM agent that decides for itself what to check, and returns an
explanation grounded in how other developers actually solved the same
failure — with citations back to the original issue and the commit that
fixed it, and a version verdict if the failure looks version-related.

Built as a standalone service. Designed to integrate later into
[axiom-ai](https://github.com/sharaf-ahmd/axiom-ai), a collaborative project —
see [Architecture](#architecture) for how the two fit together.

## Why an agent, not a fixed pipeline

The obvious design is a fixed sequence: parse → embed → retrieve → check
version → answer. That's not what this is. A fixed pipeline runs the same
five steps on every failure, whether or not they're relevant — it forces a
version check on failures that have nothing to do with versions, and it
always produces an answer, even when the evidence doesn't support one.

Axiom Debug's agent decides its own investigation. It has four tools —
`search_incidents`, `get_issue_details`, `check_latest_version`, and
`finalize_analysis` — and works out which to call, in what order, and when
it has enough evidence. A `ModuleNotFoundError` never triggers an issue
search, because that's a dependency problem, not a code bug. A weak first
search gets reformulated and retried before the agent gives up. A timeout
gets correctly identified as environmental instead of forced into a false
match.

`finalize_analysis` can optionally include `suggested_patch` — a real,
minimal fix — but only when the caller supplies `file_context` (the actual
current content of the implicated file). Never fabricated against a file
the agent hasn't seen: dropped defensively even if the model returns one
anyway when no `file_context` was given, regardless of what the prompt
asked for. That code-side guard is the actual safety boundary, not the
prompt.

**Citations can't be fabricated.** The agent can only cite an incident it
actually retrieved via a tool call this session — there is no code path
that lets it cite an id it never looked up, and a code-side fallback covers
the case where the model retrieves evidence but forgets to list it. A
second, cheap model call then checks that the citation's content actually
supports the claim being made, not just that the citation exists — verified
live: it correctly downgraded confidence when the agent's suggested fixes
went beyond what the cited issue's resolution actually said.

## Model provider: Groq, not a hosted frontier model

The agent runs on [Groq](https://groq.com) — `openai/gpt-oss-120b` for the
investigation loop, the smaller `openai/gpt-oss-20b` for the cheap citation
verifier. Free, self-serve API keys, fast inference, no billing setup
required to run this project.

The indexer's bulk extraction pass (thousands of small classify+summarize
calls, one per crawled issue) runs on Gemini instead, with Groq as an
automatic fallback — two independent free-tier quotas cover each other's
weak spot. Confirmed live: Groq's free tier caps at 8000 TPM on the small
model, which a real crawl blows through in seconds at any real concurrency.
Gemini's per-minute throughput held up far better on the same workload — but
it has its own hard ceiling, confirmed live via a real 429:
`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, exactly 500 requests
per day per model. Retrying against that is pointless (every retry burns
another of the same exhausted daily budget), so `indexer/extract.py`
detects that specific error and skips straight to the Groq fallback with
zero wasted retries, verified against the real error body. Gemini is
optional — `GEMINI_API_KEY` unset just means extraction runs on Groq alone.

That choice has a real consequence worth being upfront about: Groq's
OpenAI-compatible tool calling is less reliable than a frontier model at
following instructions embedded only in prose. During development, the
agent would retrieve a real incident, clearly use it, and then leave the
citation field empty in its final answer — despite the system prompt
explicitly saying to include it. The fix wasn't a stronger prompt (tried
that first; didn't help); it was making the schema itself enforce the
behavior (`strict: true` + the field moved into `required`, so the key
can no longer be silently omitted) plus a code-side fallback that cites
whatever the agent explicitly looked up via `get_issue_details` if the
model's own citation list comes back empty. Both are in `app/agent/tools.py`
and `app/agent/loop.py`, and both were verified against real API responses,
not assumed.

## CI integration: three modes, one setting

`.github/workflows/ci.yml` runs the test suite on every push/PR, and on a
real failure, reacts automatically instead of waiting for someone to
manually paste the log in. One repo variable, `AXIOM_MODE`, picks how far
it goes:

- **manual** (default) — post the diagnosis as a PR comment. Nothing else
  touches git.
- **review** — same, plus any `suggested_patch` shown as a reviewable diff
  right in the comment.
- **automatic** — apply the patch on a fresh branch, run the test suite,
  and only if that's clean, push the branch and open a PR. Never commits to
  the branch that triggered it — "automatic" means zero manual effort up to
  a merge click, not an unsupervised write to a protected branch. Any
  failure along the way (patch doesn't apply, tests fail) falls back to the
  same review-mode comment instead of silently doing nothing.

Requires `AXIOM_API_URL` (a repo variable) pointing at a reachable Axiom
Debug deployment — GitHub's runners can't reach a developer's `localhost`,
so this step skips cleanly, without failing the build, if it isn't set.

**A real discovery while building this, worth documenting alongside the
citation-omission story above — same lesson, different symptom.** Despite
an explicit unified-diff format example in the system prompt, gpt-oss-120b
defaulted to a different, OpenAI-specific `*** Begin Patch` patch
convention twice in a row in live testing. Prompting alone didn't fix it,
same as citations. The fix was structural again: `apply_fix.py` recognises
that shape and converts it via verified, unambiguous text substitution
(exact match required across every hunk, checked before anything is
written — any ambiguity fails closed rather than guessing) instead of
fighting the model's prior. Tested against both real captured patches from
the live runs that found the problem, not synthetic recreations.

## Architecture

```
Failing CI log
      │
      ▼
Traceback parser  ──── strips CI noise, extracts exception + frames,
      │                normalises into a stable signature
      ▼
Groq agent  ◄────────► search_incidents / get_issue_details /
 (manual tool loop)     check_latest_version / finalize_analysis
      │                        │
      ▼                        ▼
Grounded answer         Incident index (pgvector + full-text,
                         hybrid search with RRF fusion)
                                ▲
                                │
                         Offline indexer
                         (GitHub GraphQL → LLM extraction → embed)
```

Retrieval is hybrid, not pure vector search: a dense pass (pgvector cosine
similarity) and a sparse pass (Postgres full-text search) are fused with
Reciprocal Rank Fusion. Pure embeddings blur exactly the signals that matter
most for error text — exception types, library names — so the keyword pass
recovers what the vector pass smooths over.

## Project layout

```
backend/
├── app/
│   ├── agent/        the harness — client, prompts, tools, loop, verifier
│   ├── parsers/       traceback parsing (noise → structured failure)
│   ├── models/        SQLAlchemy — OSSIncident, Analysis
│   ├── schemas/        Pydantic request/response contracts
│   ├── services/       embedding, hybrid retrieval, version lookup, orchestration
│   └── api/             FastAPI routes
├── indexer/            offline batch job — crawls GitHub, extracts, embeds, loads.
│                        Not part of the served app; run once before launch.
├── evals/               retrieval quality harness — recall@k, MRR against a
│                        golden set, so retrieval changes are measured, not guessed.
├── migrations/          Alembic
└── tests/
infrastructure/
└── docker-compose.yml  Postgres 16 + pgvector
.github/
├── workflows/ci.yml     test suite + the three-mode failure reaction
└── scripts/             report_ci_failure.py (manual/review), apply_fix.py
                          (automatic) — stdlib-only, no install step needed
                          for their own standalone CI job
```

Every layer mirrors the conventions of the shared `axiom-ai` backend (thin
routers, static-method service classes, `ValueError` → `HTTPException`,
SQLAlchemy 2.0 `Mapped[]` models) so integrating this into that repo later is
close to a straight copy. The only seam is three files this project owns
that the shared repo owns differently — `config.py`, `database.py`,
`models/base.py` — everything else imports from those by name and doesn't
know which version it's talking to.

## Setup

Requires Python 3.11+, Docker Desktop, and a
[Groq API key](https://console.groq.com/keys) — free, self-serve.

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\Activate.ps1          # Windows PowerShell

# 2. Install dependencies
pip install -r backend/requirements.txt

# 3. Start Postgres + pgvector
cd infrastructure
docker compose up -d
cd ..

# 4. Configure environment
cp backend/.env.example backend/.env
# then edit backend/.env and set GROQ_API_KEY

# 5. Apply migrations
cd backend
alembic upgrade head

# 6. Run the tests
pytest

# 7. Start the server
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000` — you should see
`{"name":"Axiom Debug","status":"running"}`, and `http://localhost:8000/health`
should report the database as connected.

### Try it

```bash
curl -X POST http://localhost:8000/analyze \
  -H "Content-Type: application/json" \
  -d '{"log": "Traceback (most recent call last):\n  File \"main.py\", line 1, in <module>\nValueError: bad input"}'
```

Without an indexed database, the agent will find no matching incidents and
say so — that's the honest answer, not a bug. To get real, grounded matches,
populate the index first:

```bash
# needs GITHUB_TOKEN in backend/.env (repo read scope) in addition to GROQ_API_KEY
python -m indexer --library pandas   # index one library, to start
python -m indexer                    # index everything in indexer/libraries.py
```

Then check retrieval quality against the golden set:

```bash
python -m evals.run
```

## What's actually been verified — live, not assumed

Everything below was run for real, not just written and assumed correct:

- **Schema** — applied to a live Postgres+pgvector container; inspected the
  actual DDL (`vector(384)`, HNSW index, generated `tsvector`, GIN index,
  unique constraint); ran a full downgrade→upgrade round-trip. Live schema
  re-checked against both migrations and the ORM models — no drift.
- **Embeddings** — real `bge-small` model, downloaded and run locally, zero
  API key, 384-dim vectors confirmed.
- **Hybrid retrieval** — inserted a real row, embedded it, searched, got it
  back with the correct citation.
- **The full HTTP path, fresh and cached** — booted the server, hit `/`,
  `/health`, `POST /analyze` with a real pandas traceback: correct root
  cause, correct next steps, coherent tool-calling trace
  (`search_incidents` ×3 with reformulated queries → `check_latest_version`
  → `finalize_analysis`), persisted correctly. Re-sent the identical
  request: cache hit, `from_cache: true`, 15s → 0.05s. `GET /analyze/{id}`,
  a malformed body (422), an invalid id (400), and a nonexistent id (404)
  all checked directly against the running server.
- **The agent loop, live, end-to-end, on real failures** — not a mock. Real
  Groq calls, real tool-calling trace, a correct diagnosis of a seeded
  pandas failure, and a genuinely useful fix. Caught and fixed several real
  bugs this way over the project's life — a schema mismatch in
  `get_issue_details`, the citation-omission issue described above, and
  (most recently, in a full top-to-bottom harness audit) three resilience
  gaps that unit tests alone hadn't caught: `RateLimitError` on the
  interactive path had no retry at all; `APIConnectionError`/
  `APITimeoutError` aren't `APIStatusError` subclasses and were bypassing
  exception handling entirely (unhandled 500); and a transient failure in
  the *secondary* citation-verification call could discard an
  already-successful analysis. All three fixed and covered by tests that
  reproduce the exact failure with a scripted client, not just reasoned about.
- **Citation integrity** — tests prove a fabricated citation id cannot
  survive `filter_valid_citations`; live testing proved the semantic
  verifier catches real over-claiming, not just fabrication.
- **The whole point of the product, end-to-end against a real index** —
  for a long stretch this was the honest gap: every live `/analyze` test
  returned `matched_issues: []` because the index was empty, so grounded
  citation had never actually been demonstrated, only unit-tested in
  pieces. Now proven with real crawled pandas incidents: a realistic test
  failure (`AssertionError` on a dtype after `rename`) produced
  `search_incidents` → `get_issue_details` (a deliberate confirmation
  lookup) → `check_latest_version` → `finalize_analysis`, citing the real
  [pandas#65315](https://github.com/pandas-dev/pandas/issues/65315) and
  the real PR that fixed it, at honestly-calibrated `medium` confidence.
  The negative case matters as much: an unrelated SMTP `TimeoutError`
  returned **zero** citations and never called `search_incidents` at all —
  the agent correctly judged it environmental rather than forcing a match.
  Retrieval was separately confirmed to return the correct incident at
  rank 1 (similarity 0.85–0.89) for real queries, and nothing whatsoever
  for nonsense input.
- **Data sanitization at every DB boundary** — a real crawl hit
  `CharacterNotInRepertoireError` (Postgres rejects NUL bytes in text
  columns outright) on real GitHub issue text, losing an entire committed
  batch. Fixed in the indexer's `load()`, and the same class of risk was
  found and fixed in the live `/analyze` path too (`log_excerpt` is raw
  user-pasted text) before it could ever happen there.
- **Eval harness, against real crawled data** — `evals/cases.jsonl` now
  holds real cases derived from genuinely indexed pandas issues (the
  synthetic placeholders are gone). MRR 1.0, recall@1/3/5 all 1.0 across
  9 cases. **That number is not as impressive as it looks, and shouldn't be
  read as "retrieval is solved":** with only ~10 incidents indexed, there
  are almost no near-duplicates to confuse a query, and the queries are
  paraphrases of the indexed summaries. It proves the harness and the
  retrieval path genuinely work against real data end-to-end — not that
  quality holds at 10,000 incidents where similar issues actually compete.
  Re-run this once the index is substantially larger; a drop from 1.0 there
  would be the real signal.
- **Patch generation and application, live** — a real `/analyze` call with
  real `file_context` produced a real patch on the first correctly-formatted
  attempt; the OpenAI-style-convention problem described above was found
  this way, not hypothesised, and the fix was verified against those exact
  captured patches applying cleanly to a real git repo, including the
  ambiguous-match and missing-file failure modes failing closed correctly.

**90 tests pass** (60 backend, 30 covering the CI scripts — including real
`git apply`/branch/commit operations against a real temporary repo, not
just mocked subprocess calls).

The offline indexer is fully wired and running (`GITHUB_TOKEN` and
`GROQ_API_KEY` configured; `GEMINI_API_KEY` optional). The index is still
populating as of this writing — both providers' free-tier rate limits are
real and were hit hard during development (see above), so a full crawl
takes real wall-clock time. That's an external quota constraint, not a
code gap: the indexer now survives it correctly (per-issue provider
fallback, per-library failure isolation, no data loss on a bad row) rather
than crashing, which is what it did before today's fixes.
