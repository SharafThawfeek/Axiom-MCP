# Axiom Debug

**Your codebase's failure memory, in your IDE.** An MCP server that answers the
question your coding agent can't: *has this project hit this failure before,
and what actually fixed it?*

```
✗ AttributeError: 'DataFrame' object has no attribute 'append'

  recall_failure_memory → exact match, seen 3× in this repo
  "DataFrame.append was removed in pandas 2.0 — use pd.concat."
  fixed in github.com/acme/app/pull/412
```

Targets MCP spec [`2026-07-28`](https://modelcontextprotocol.io/specification/2026-07-28)
(stateless core) on [FastMCP 4](https://gofastmcp.com).

---

## Why this exists

A coding agent can already read a traceback. It can search the web, read your
source, and reason about the exception. What it cannot do is know that *your
team* hit this exact failure six weeks ago, and that the fix was a one-line pin
bump nobody wrote down.

That gap is the product. Everything else here serves it.

**What this is not:** a documentation index. [Context7](https://context7.com)
already does that well, across 9,000+ libraries. Documentation tells you the
correct API; it never tells you why your build broke or what your colleague did
about it. Different question, different data.

## Install

Requires [uv](https://docs.astral.sh/uv/). No database, no Docker, no API key.

```bash
uvx axiom-debug-mcp
```

That's the whole setup. Memory lives in a local SQLite file under
`~/.axiom-debug/`, scoped by git remote so every clone of a repo shares one
memory.

<details>
<summary><b>Google Antigravity</b> — <code>~/.gemini/config/mcp_config.json</code></summary>

```json
{
  "mcpServers": {
    "axiom-debug": { "command": "uvx", "args": ["axiom-debug-mcp"] }
  }
}
```
</details>

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add axiom-debug -- uvx axiom-debug-mcp
```
</details>

<details>
<summary><b>Cursor / VS Code</b> — <code>.cursor/mcp.json</code> or <code>.vscode/mcp.json</code></summary>

```json
{
  "mcpServers": {
    "axiom-debug": { "command": "uvx", "args": ["axiom-debug-mcp"] }
  }
}
```
</details>

## Tools

| Tool | What it does | Cost |
|---|---|---|
| `parse_failure` | Log → normalised, stable failure signature | pure function |
| `recall_failure_memory` | **Has this project hit this before?** | ~6 ms |
| `record_failure_resolution` | Write back a confirmed fix | one insert |
| `check_package_version` | Installed vs. registry latest | one HTTP call |
| `search_public_incidents` | Solved OSS issues — registered only if a corpus is configured | hybrid retrieval |

**No LLM call on any default path.** The agent you already have does the
reasoning; this server supplies grounded facts it cannot otherwise obtain.

## How recall works

Two tiers, and the ordering is the main performance decision in the project:

1. **Exact signature match.** The parser normalises addresses, quoted strings
   and numbers out of the exception message, so the same bug yields a
   byte-identical signature across machines and runs. Most repeat CI failures
   land here — an index lookup, no model, no embedding.
2. **Vector fallback**, only on a miss. Embeds the query and scores it against
   the project's stored signatures.

Tier 1 answering means the ~130 MB embedding model is never loaded at all. A
test asserts that rather than assuming it: if an exact hit ever starts
embedding, `test_exact_recall_does_not_embed` fails.

Vectors are packed `float32` BLOBs scored with numpy, not pgvector. A project's
failure memory is hundreds to low thousands of rows, where an HNSW index is
pure overhead — and the payoff is that identical code runs on SQLite and
Postgres, which is what makes the zero-dependency local install possible.

## Measured

From a clean-environment install driven over real stdio JSON-RPC, not estimates:

| | |
|---|---|
| Cold start (first ever run) | 5.5 s |
| Warm start | 1.35 s |
| `parse_failure` | 69 ms first call, sub-ms after |
| `recall_failure_memory` (exact) | **6.0 ms** median |
| Base install | no FastAPI, Postgres driver, pgvector, or LLM client |

For contrast, the optional LLM-backed `analyze_failure` path is ~15 s and two
model calls. That is why it is opt-in.

## Security

Failure memory is private data — logs, stack traces, internal fixes — so the
tenant boundary is the central design constraint.

- **`project_id` is never a tool argument.** It comes from a verified token
  claim (hosted) or the local git remote (stdio). A tool parameter is chosen by
  the model, and the model's context can be influenced by anything it just read
  — a dependency's README, a CI log. Routing the tenant key through it would be
  a textbook confused-deputy hole.
- **Fails closed.** With auth enabled and no valid token, tenant resolution
  raises rather than falling back to the server's local project.
- **API keys held as SHA-256 digests**, compared with `hmac.compare_digest`.
  Write scope must be granted explicitly; keys default to read-only.
- **Origin validation** on the HTTP transport, which the spec makes normative.
  Without it, any open web page can drive a localhost MCP server's tools.
- **`mask_error_details`**, so internal exceptions never reach the model as
  text it will act on.

### What leaves your machine

Nothing, by default, except one thing: `check_package_version` queries PyPI or
the npm registry for a package's latest release. That is the server's only
outbound request in the local configuration.

There is no telemetry and no phone-home. Failure memory is a SQLite file under
`~/.axiom-debug/` and is never transmitted anywhere — the hosted Postgres mode
exists so a *team* can share memory deliberately, not as a default.

## Deliberately not built

- **MCP Apps** (interactive UI). Real, and shipping in six hosts — but a chart
  of your failure history is decoration. The value here is a fact handed to an
  agent mid-task.
- **Tasks extension** (long-running work). Every tool answers in milliseconds;
  durable task handles would be protocol surface with nothing behind it.
- **Full OAuth 2.1 / CIMD.** The realistic deployment is a team self-hosting one
  instance and issuing its own keys. `RemoteAuthProvider` is the seam if that
  changes.

## Development

```bash
uv sync --extra server --extra indexer
uv run pytest                          # 131 tests
uv run ruff check backend .github
```

CI enforces three things beyond the suite: lint (previously configured but
never run), that `uv.lock` hasn't drifted from `pyproject.toml`, and that the
published wheel installs **without** any server-only dependency leaking into
the base set — the invariant that keeps `uvx` viable.

## Hosted deployment

The MCP endpoint mounts into the FastAPI app at `/mcp` as Streamable HTTP,
stateless per the 2026-07-28 core, so it scales horizontally behind a plain
round-robin load balancer with no shared session store.

```bash
AXIOM_REQUIRE_AUTH=true
AXIOM_API_KEYS='[{"key_sha256":"<digest>","project_id":"acme/app","scopes":["memory:read","memory:write"]}]'
AXIOM_MEMORY_URL=postgresql+asyncpg://...
AXIOM_ALLOWED_ORIGINS=https://your-app.example.com
```

## Repository

```
backend/axiom_debug/
├── mcp/          the MCP server — tools, schemas, auth, origin validation
├── memory/       failure memory: portable store, vectors, project identity
├── parsers/      language registry (Python, JavaScript) → stable signatures
├── services/     retrieval, embedding, version lookup
├── agent/        optional LLM investigation loop (opt-in)
└── api/          the original HTTP surface
```

Design decisions and the options rejected: [docs/DECISIONS.md](docs/DECISIONS.md).

The previous incarnation of this project — an LLM agent service that diagnosed
CI failures against a crawled corpus of public GitHub issues — is archived at
[docs/README-v0-agent-service.md](docs/README-v0-agent-service.md). Its
retrieval stack, parsers and CI integration are still here and still tested;
it is no longer the headline.

## Licence

MIT
