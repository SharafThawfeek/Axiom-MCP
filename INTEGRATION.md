# Merging into `sharaf-ahmd/axiom-ai`

Verified against `origin/main @ f009673` ("RBAC"). Re-check before merging if
that repo has moved — everything below is a statement about a specific commit,
not a permanent truth.

## One blocker that needs Sharaf, not code

**The shared `docker-compose.yml` runs `postgres:16-alpine`, which does not
have pgvector.** Every retrieval feature in this half depends on it: the
`oss_incidents.embedding` column is `vector(384)`, the search index is
`hnsw (embedding vector_cosine_ops)`, and the first migration opens with
`CREATE EXTENSION IF NOT EXISTS vector`. On plain Postgres that migration
fails immediately — nothing degrades gracefully, it just doesn't start.

The fix is one line in `infrastructure/docker-compose.yml`:

```yaml
# image: postgres:16-alpine
image: pgvector/pgvector:pg16
```

`pgvector/pgvector:pg16` **is** Postgres 16 with the extension preinstalled —
same server, same data directory layout, no migration of existing data, and
nothing about the auth/RBAC tables changes. Worth agreeing on this before
merge day, since it touches shared infrastructure rather than this feature's
own files.

## Already compatible — verified, not assumed

These were checked by reading the current files in both repos side by side:

| Thing | Status |
|---|---|
| `app/models/base.py` | Identical (`Base(DeclarativeBase)`) — imports work unchanged |
| `app/database.py` | Same `engine` / `SessionLocal` / `get_db` surface |
| Service layer style | Both use `class XService:` with `@staticmethod async def` |
| Models | Both SQLAlchemy 2.0 `Mapped[]` |
| Routers | Both `APIRouter(prefix=..., tags=[...])` + `include_router` |
| `app/core/logger.py` | Different logger *name* only; `from app.core.logger import logger` works either way |
| Table names | No collision — `oss_incidents` / `analyses` vs `users` / `organizations` / `members` / `api_keys` |
| Dependencies | No conflicting pins; this half only *adds* groq, openai, fastembed, pgvector |

**Error responses now match too.** This half previously returned FastAPI's
default `{"detail": ...}` while the shared backend returns `{"error": ...}`
via `AppException` + `app_exception_handler`. That would have forced a
frontend to handle two error shapes. `app/core/exceptions.py` here now mirrors
that repo's version and the services raise `AppException` subclasses, so both
halves speak the same shape. Verified live: `400 {"error": "Invalid analysis
id"}`, `404 {"error": "Analysis not found"}`.

## Files that overlap and need a decision

| File | What to do |
|---|---|
| `app/config.py` | **Merge.** Keep the shared version (it has the required `REDIS_URL`, `JWT_SECRET`, etc.) and append this half's settings: `GROQ_API_KEY`, `ANALYSIS_MODEL`, `VERIFIER_MODEL`, `AGENT_MAX_ITERATIONS`, `GROQ_TIMEOUT_SECONDS`, `GEMINI_API_KEY`, `EXTRACTION_MODEL`, `GEMINI_BASE_URL`, `GEMINI_TIMEOUT_SECONDS`, `RETRIEVAL_MAX_DISTANCE`, `CACHE_TTL_DAYS`, `GITHUB_TOKEN`, `EMBEDDING_MODEL`, `EMBEDDING_DIM`. Note the shared `Settings` has **no defaults** on several fields, so `.env` must be complete or the app won't boot. |
| `app/core/exceptions.py` | **Merge, additively.** Take the shared file and add `AnalysisFailed`, `InvalidAnalysisId`, `AnalysisNotFound`. Also adopt the `status_code` attribute added here — it defaults to 400, so every existing exception there behaves exactly as it does today, but `AnalysisNotFound` can return a correct 404. |
| `app/core/__init__.py` | **Take the shared version.** It re-exports `setup_logging` and `RequestIDMiddleware`, which this half doesn't own. |
| `app/api/health.py` | **Drop this half's.** The shared one is wired into their logging/middleware. |
| `app/main.py` | **Take the shared version**, then add `app.include_router(analysis_router)`. The exception handlers are already registered there. CORS is configured here but not there — raise it if the frontend needs it. |
| `app/api/__init__.py` | **Merge.** Add `analysis_router` to their exports. Note their naming is inconsistent (`health_Router` with a capital R, `users_router` lowercase); follow whatever they settle on. |
| `.github/workflows/ci.yml` | **Both repos have one.** Theirs runs their tests; this one adds the three-mode failure reaction. Merge as one workflow rather than two competing files. |

## Alembic will have two heads

Both repos have independent migration chains:

- shared: `fcbbc55ea8ce` … through their auth/RBAC revisions
- this half: `5cdf4996c443` → `5954adf2c9e6` → `381f16155fa3` → `a1c4e7b92d38` → `c8f2a15d4e60`

After copying files in, `alembic upgrade head` will refuse to run with two
heads. Resolve with:

```bash
alembic merge -m "merge debug agent into axiom-ai" <their_head> c8f2a15d4e60
```

This half's chain never touches their tables, so the merge revision is a
no-op join — but it has to exist. Re-point `down_revision` on
`5cdf4996c443` instead only if a single linear history is preferred.

## What to copy in

Everything under these paths is self-contained and collides with nothing:

```
backend/app/agent/          the harness — client, prompts, tools, loop, verifier
backend/app/parsers/        traceback parsing
backend/app/models/incident.py, analysis.py
backend/app/schemas/analysis.py, incident.py
backend/app/services/       analysis, retrieval, embedding, version
backend/app/api/analysis.py
backend/indexer/            offline crawler (not part of the served app)
backend/evals/              retrieval quality harness
backend/migrations/versions/  (see two-heads note above)
backend/tests/              97 tests
```

Add to `backend/requirements.txt`: `groq`, `openai`, `fastembed`, `pgvector`.
Pinned versions are in this repo's `requirements.txt`; the shared repo
currently pins nothing, so match whichever convention wins.
