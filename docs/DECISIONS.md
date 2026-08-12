# Design decisions

Why the architecture is what it is, including the options rejected. Each entry
records the decision and the reasoning available at the time — not a claim that
the reasoning was right.

---

## 1. The product is failure memory, not OSS issue search

**Decision.** The headline capability is recall of *this project's own*
resolved failures. The crawled corpus of public GitHub issues is demoted to an
optional secondary source.

**Why the original framing was weak.** Four independent problems:

1. Index size is an unwinnable race. Context7 has 9,000+ libraries and a funded
   team; this had ~10 incidents behind free-tier crawl quotas of 500
   requests/day.
2. GitHub ships an official MCP server that searches issues, free and
   maintained.
3. A 2026 coding agent already has the log, the source tree, and web search.
   The marginal value of "here is a similar public issue" has fallen sharply.
4. The strongest differentiator — code-enforced citation integrity — only holds
   while *this* server runs the agent loop. Exposing primitives to a host agent
   dissolves it.

**Why memory is defensible.** It is private, it is not web-searchable, it
compounds with use rather than requiring months of upfront indexing, and it is
useful from the first recorded entry. The `analyses` model docstring had already
identified this as the intent; it had simply never been built.

**Cost.** The public corpus work is no longer the centrepiece. It still exists,
still passes its tests, and still runs when configured.

---

## 2. Memory is a new table, not the `analyses` table

**Decision.** `failure_memories` is separate from `analyses`.

**Why.** `analyses` records what the agent *guessed* — a generated root cause
and suggested next steps, written before anyone confirmed them. Memory records
what actually *fixed* the failure. Merging them would let every wrong diagnosis
harden into permanent "team knowledge", and a confidently wrong recall is worse
than an empty one.

Memory also binds to its own declarative base. The shared `Base` metadata holds
pgvector, ARRAY and JSONB columns that cannot be created on SQLite at all, so a
separate base is what allows `create_all()` against a local file.

---

## 3. SQLite locally, Postgres hosted — with no dialect branching

**Decision.** Embeddings are stored as packed `float32` BLOBs and scored with
numpy. No pgvector for memory.

**Why.** The original plan called for a retrieval-strategy interface with two
implementations. That turned out to be unnecessary complexity: a project's
failure memory is hundreds to low thousands of rows, and brute-force cosine over
a few thousand 384-dim vectors is sub-millisecond. At that scale an HNSW index
costs more than it saves.

Removing the vector-extension dependency is what makes `uvx axiom-debug-mcp`
work with no Docker, no Postgres and no migrations. That is the difference
between a tool people install and one they read about.

pgvector is retained for the OSS corpus, which is genuinely large and
Postgres-only.

**Revisit if** a single project ever exceeds ~20k distinct signatures
(`MAX_SCORED_ROWS`). Scoring loads only `(id, vector)` pairs, so the ceiling is
memory bandwidth, not row size.

---

## 4. `project_id` never comes from a tool argument

**Decision.** The tenant key is resolved from a verified token claim (hosted) or
the local git remote (stdio), before any tool body runs.

**Why.** A tool parameter is chosen by the model, and the model's context can be
influenced by anything it has read — a stack trace, a dependency's README, a CI
log. Letting that reach the tenant key is a confused-deputy vulnerability: a
caller with legitimate access to one project induces the server into reading
another.

`AXIOM_PROJECT_ID` can pin the value, which a monorepo needs. That is not the
same hole: an environment variable is set by whoever launched the process. What
matters is who controls the value, not whether it is configurable.

Auth mode **fails closed** — no valid token raises rather than falling back to
the server's own checkout.

---

## 5. FastMCP 4.0.0b2 (beta) over 3.4.7 (stable)

**Decision.** Build against the beta.

**Why.** 3.4.7 targets the superseded `2025-11-25` revision. The `2026-07-28`
spec — released 2026-07-28, the largest revision since launch — moved the
protocol core to stateless, which suits this tool surface exactly: every tool is
a pure function of its arguments plus the resolved tenant, so any request can
land on any instance with no shared session store.

**The risk is real.** It is a beta, and 4.0 removed server-initiated sampling
and roots along with the 3.x compatibility shims. Mitigations: the version is
pinned exactly, `uv.lock` fixes the whole transitive set, and the decorator API
is unchanged from 3.x. FastMCP 4 also serves both protocol eras, so older
clients still connect.

**Corrected along the way.** Secondary sources reported that the Python SDK
renamed `FastMCP` to `MCPServer`. Introspecting the installed package showed
that is false — the class is still `FastMCP`. Verify against the artifact, not
the write-up.

---

## 6. Corpus tools are registered conditionally

**Decision.** `search_public_incidents` only exists when `AXIOM_CORPUS_URL` is
set.

**Why.** Without a corpus it could only ever return an error. Registering it
anyway would place a permanently broken tool in every `tools/list` response —
context the host model pays for on every single turn, plus a wasted call the
first time it tries.

---

## 7. Two-tier recall, exact match first

**Decision.** Exact signature lookup, then embedding search only on a miss.

**Why.** The parser normalises addresses, quoted strings and integers out of the
exception message, so repeat failures are byte-identical. Always embedding would
cost a ~130 MB model load plus per-call inference to produce an answer tier 1
already had. Measured: 6 ms exact recall versus ~15 s for the LLM path.

This is asserted, not assumed — `test_exact_recall_does_not_embed` replaces the
embedding function with one that raises.

---

## 8. Not built: MCP Apps, Tasks, full OAuth 2.1

**MCP Apps.** Shipping in six hosts and genuinely interesting, but a rendered
chart of failure history is decoration. The value is a fact returned to an agent
mid-task, which is text.

**Tasks extension.** Designed for long-running work. Every tool here answers in
milliseconds; durable handles would be protocol surface with nothing behind it.

**Full OAuth 2.1 / CIMD.** The realistic deployment is a team self-hosting one
instance and issuing keys to its own developers. Standing up an authorization
server for that is infrastructure nobody asked for. `RemoteAuthProvider` is the
documented seam if the deployment model changes.

Declining these is itself a decision worth recording: an MCP server that uses
every extension badly reads worse than one that uses three primitives well.
