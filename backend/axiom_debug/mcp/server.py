"""The Axiom Debug MCP server.

Targets MCP spec 2026-07-28 via FastMCP 4. The protocol core is stateless
there, which suits this surface exactly: every tool below is a pure function
of its arguments plus the caller's project scope, so any request can land on
any instance behind a plain load balancer.

Design notes worth knowing before editing:

* No LLM call on any default tool path. `recall_failure_memory` answers from
  an index hit in the common case; embedding is a fallback, and the agent
  loop is opt-in behind AXIOM_ENABLE_AGENT.
* Corpus tools are registered only when a corpus is configured. A tool that
  exists but always errors is worse than a tool that isn't advertised — the
  host model wastes a call discovering it, every turn.
* Tools carry an explicit `version`. The output models in schemas.py are a
  public contract; clients need a way to see it move.
"""

import asyncio
import logging
import os
import sys

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from axiom_debug.mcp import schemas
from axiom_debug.mcp.settings import MCPSettings
from axiom_debug.memory import MemoryStore, derive_local_project_id

logger = logging.getLogger("axiom-debug")

INSTRUCTIONS = """\
Axiom Debug gives you this project's own history of build and test failures.

When a build, test run or script fails, the useful question is usually not
"what does this exception mean" — you can already read the traceback — but
"has this codebase hit this before, and what fixed it". That is what
recall_failure_memory answers, from a private per-project index.

Suggested order:
  1. parse_failure on the raw log to get a stable signature.
  2. recall_failure_memory with that signature. An 'exact' match is the same
     failure this project already solved; trust it and say where it came from.
  3. Only if memory is empty, fall back to your own analysis or the public
     corpus tools if they are available.
  4. Once the failure is genuinely fixed, call record_failure_resolution so
     the next person gets the answer immediately.

Step 4 is what makes this useful over time. A recall miss today becomes a
hit tomorrow only if resolutions get written back.
"""


mcp = FastMCP(
    name="axiom-debug",
    version="0.1.0",
    instructions=INSTRUCTIONS,
    # Tool errors reach the model as text it will try to act on. Internal
    # detail there is both an information leak and a prompt-injection
    # surface, so exceptions are masked into generic messages and the real
    # traceback goes to the server log instead.
    mask_error_details=True,
    # Reject arguments that don't match the declared schema rather than
    # coercing them. A silently coerced argument produces a confidently
    # wrong answer, which is the worst failure mode for a grounding tool.
    strict_input_validation=True,
)

_settings: MCPSettings | None = None
_store: MemoryStore | None = None
_store_lock = asyncio.Lock()
_local_project_id: str | None = None


def get_settings() -> MCPSettings:
    """Read configuration on first use, not at import.

    Import-time capture would make the environment a client's process
    inherits unchangeable for the lifetime of the module, and would mean
    merely importing this file touches the filesystem to create the memory
    directory.
    """
    global _settings
    if _settings is None:
        _settings = MCPSettings.from_env()
    return _settings


def reset_state() -> None:
    """Drop cached settings, store handle and project id.

    Test hook. Nothing in normal operation should need this — the process
    reads its environment once and keeps one database handle.
    """
    global _settings, _store, _local_project_id
    _settings = None
    _store = None
    _local_project_id = None


async def _get_store() -> MemoryStore:
    """Lazily open the memory database.

    Lazy rather than at import: `uvx axiom-debug-mcp --help` and the client's
    initial tools/list should not touch the filesystem. Double-checked under
    a lock because a client may issue concurrent tool calls on first contact.
    """
    global _store
    if _store is None:
        async with _store_lock:
            if _store is None:
                store = MemoryStore(get_settings().memory_url)
                await store.initialise()
                _store = store
    return _store


def _project_id() -> str:
    """Resolve the tenant for this call.

    Two modes, and the distinction is a security boundary rather than a
    convenience:

    * Hosted (AXIOM_REQUIRE_AUTH) — the project comes from a claim on the
      verified bearer token and nowhere else. If there is no token, this
      raises rather than falling back. A fallback here would hand an
      unauthenticated caller whatever project the server process happens to
      sit in, which is precisely the leak the whole scheme exists to prevent.
    * Local stdio — derived from the checkout the server was launched in.

    AXIOM_PROJECT_ID can pin the local value, which a monorepo splitting one
    checkout into several logical projects needs. That is not the hole
    memory/project.py warns about: an environment variable is set by whoever
    launched the process, whereas a tool argument is chosen by the model from
    context an attacker may have influenced. What matters is who controls the
    value, not whether it is configurable.
    """
    if get_settings().require_auth:
        from fastmcp.server.dependencies import get_access_token

        from axiom_debug.mcp.auth import project_id_from_token

        project = project_id_from_token(get_access_token())
        if not project:
            # Defence in depth: FastMCP should have rejected this request
            # before any tool ran. Failing closed costs nothing if that
            # holds, and prevents a cross-tenant read if it ever doesn't.
            raise ToolError("Unauthenticated request: no project scope.")
        return project

    global _local_project_id
    if _local_project_id is None:
        _local_project_id = os.environ.get("AXIOM_PROJECT_ID") or derive_local_project_id()
    return _local_project_id


# --- tools -----------------------------------------------------------------


@mcp.tool(
    version=1,
    tags={"read"},
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
def parse_failure(log: str) -> schemas.ParsedFailure:
    """Extract the failure from a raw build or test log.

    Strips CI noise (ANSI colour, timestamps), finds the traceback, and
    reduces it to a normalised signature that is stable across runs — the
    same bug on two machines produces the same string.

    Pure and local: no network, no database, no model. Call it freely.
    Handles Python and JavaScript; when a log contains both, the failure
    that appears last wins, since that is the one that propagated.

    Returns an error only if there is no parseable trace at all.
    """
    from axiom_debug.parsers import implicated_library, parse

    failure = parse(log)
    if failure is None:
        raise ToolError(
            "No traceback could be parsed from this log. It may be a "
            "non-crash failure (assertion output, linter error, timeout), "
            "in which case work from the raw text directly."
        )

    origin = None
    if failure.origin:
        origin = schemas.Frame(
            file=failure.origin.file,
            line=failure.origin.line,
            function=failure.origin.function,
        )

    return schemas.ParsedFailure(
        language=failure.language,
        exception_type=failure.exception_type,
        exception_message=failure.exception_message,
        signature=failure.signature,
        origin=origin,
        frame_count=len(failure.frames),
        implicated_library=implicated_library(failure),
    )


@mcp.tool(
    version=1,
    tags={"read"},
    annotations={"readOnlyHint": True, "openWorldHint": False},
)
async def recall_failure_memory(
    signature: str | None = None,
    log: str | None = None,
    limit: int = 5,
) -> schemas.RecallResult:
    """Ask whether this project has hit this failure before, and what fixed it.

    This is the tool worth reaching for first on any build or test failure.
    It searches a private index scoped to this repository — not the public
    internet — so a hit is this team's own verified fix, with the commit or
    PR that shipped it when that was recorded.

    Pass `signature` from parse_failure when you have it. Passing `log`
    instead parses it first, which is a convenience, not a different search.

    Read `match` carefully: 'exact' is the identical normalised signature and
    means the same bug. 'similar' is a nearby neighbour and is a lead, not a
    conclusion — say which one you got rather than presenting them alike.

    An empty result is a real answer: this project has not recorded this
    failure. It is not a reason to guess.
    """
    if not signature and not log:
        raise ToolError("Provide either a signature or a log.")

    if not signature:
        from axiom_debug.parsers import parse

        failure = parse(log or "")
        if failure is None:
            raise ToolError(
                "No traceback could be parsed from that log, so there is no "
                "signature to match on. Pass a signature explicitly if you have one."
            )
        signature = failure.signature

    store = await _get_store()
    project = _project_id()

    hits = await store.recall(
        project_id=project, signature=signature, limit=max(1, min(limit, 20))
    )
    total = await store.count(project)

    memories = [
        schemas.RecalledMemory(
            signature=hit.memory.signature,
            resolution=hit.memory.resolution,
            resolved_by=hit.memory.resolved_by,
            description=hit.memory.description,
            exception_type=hit.memory.exception_type,
            match="exact" if hit.exact else "similar",
            similarity=round(hit.similarity, 4),
            occurrences=hit.memory.occurrences,
            source=hit.memory.source,
            last_seen=hit.memory.last_seen.isoformat(),
        )
        for hit in hits
    ]

    note = None
    if not memories:
        note = (
            f"No match. This project has {total} recorded failure(s), none of "
            "them this one. Diagnose it normally, then call "
            "record_failure_resolution once it is actually fixed."
            if total
            else (
                "Failure memory is empty for this project. It fills up as "
                "resolutions get recorded — call record_failure_resolution "
                "after you fix something."
            )
        )

    return schemas.RecallResult(
        searched_signature=signature,
        memories=memories,
        total_known_failures=total,
        note=note,
    )


@mcp.tool(
    version=1,
    tags={"write"},
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
async def record_failure_resolution(
    signature: str,
    resolution: str,
    resolved_by: str | None = None,
    description: str | None = None,
    exception_type: str | None = None,
    language: str = "python",
    source: str = "agent",
) -> schemas.RecordResult:
    """Record how a failure was actually fixed, for next time.

    Call this only once the fix is real — applied and verified, not proposed.
    Memory is valuable precisely because it holds confirmed resolutions; a
    guess written here becomes a confident wrong answer for whoever hits this
    next, which is worse than an empty index.

    `signature` must come from parse_failure so it matches on recall.
    `resolution` should be specific enough to act on: the change that fixed
    it, not "updated the code". Pass `resolved_by` (commit SHA or PR URL)
    whenever you know it — provenance is what makes a recall trustworthy.

    Recording the same signature again updates the resolution and increments
    the occurrence count rather than creating a duplicate.
    """
    if not signature.strip():
        raise ToolError("signature must not be empty.")
    if not resolution.strip():
        raise ToolError(
            "resolution must not be empty — an unexplained memory entry is noise."
        )
    if source not in {"human", "agent", "ci"}:
        raise ToolError("source must be one of: human, agent, ci.")

    store = await _get_store()
    row = await store.record(
        project_id=_project_id(),
        signature=signature.strip(),
        resolution=resolution.strip(),
        language=language,
        exception_type=exception_type,
        description=description,
        resolved_by=resolved_by,
        source=source,
    )

    return schemas.RecordResult(
        recorded=True,
        signature=row.signature,
        occurrences=row.occurrences,
        note=(
            "Updated the existing entry for this signature."
            if row.occurrences > 1
            else "New entry recorded."
        ),
    )


@mcp.tool(
    version=1,
    tags={"read"},
    annotations={"readOnlyHint": True, "openWorldHint": True},
)
async def check_package_version(
    package: str,
    dependencies: str | None = None,
    language: str = "python",
) -> schemas.VersionCheck:
    """Compare an installed package version against the registry's latest.

    Worth calling when a failure has a real version signal — a method that
    reads as deprecated, an import that moved, behaviour that changed between
    releases. Not worth calling on every failure.

    `dependencies` is the text of a requirements.txt / pip freeze /
    package.json; the installed version is read out of it. Without it only
    the latest release can be reported. Queries PyPI or the npm registry.
    """
    from axiom_debug.services.version_service import VersionService

    installed = None
    if dependencies:
        installed = VersionService.installed_version(dependencies, package, language)

    verdict = await VersionService.verdict(package, installed, language)
    return schemas.VersionCheck(
        package=verdict.package,
        installed_version=verdict.installed_version,
        verdict=verdict.verdict,
    )


def _register_corpus_tools() -> None:
    """Register the public-corpus tools, if a corpus is configured.

    Conditional on purpose. These need Postgres with pgvector and a populated
    index; without one they could only ever return an error. Registering them
    anyway would put two permanently-broken tools in every tools/list
    response — context the host model pays for on every single turn.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(get_settings().corpus_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @mcp.tool(
        version=1,
        tags={"read", "corpus"},
        annotations={"readOnlyHint": True, "openWorldHint": False},
    )
    async def search_public_incidents(
        query: str, library: str | None = None, language: str = "python"
    ) -> schemas.CorpusSearchResult:
        """Search solved open-source issues matching a failure description.

        A fallback for when recall_failure_memory comes back empty — this is
        the public corpus, not your project's history, so a hit here is
        evidence rather than a confirmed fix for this codebase.

        Pass the exception type and message, not the whole traceback.
        Genuinely unrelated results are filtered out rather than shown, so an
        empty list means nothing matched, not that ranking was poor.
        """
        from axiom_debug.services.retrieval_service import RetrievalService

        async with session_factory() as session:
            results = await RetrievalService.search(
                session, query, library=library, language=language
            )

        if not results:
            return schemas.CorpusSearchResult(
                note="No sufficiently similar public incident. Try a shorter, more general query."
            )

        return schemas.CorpusSearchResult(
            incidents=[
                schemas.MatchedIncident(
                    incident_id=r.incident_id,
                    library=r.library,
                    title=r.title,
                    problem_summary=r.problem_summary,
                    resolution_summary=r.resolution_summary,
                    similarity=r.similarity,
                    issue_url=r.citation.issue_url,
                    fixing_commit_url=r.citation.fixing_commit_url,
                )
                for r in results
            ]
        )


def build_server() -> FastMCP:
    """Assemble the server for the current environment."""
    settings = get_settings()

    if settings.require_auth:
        from axiom_debug.mcp.auth import READ_SCOPE, ApiKeyVerifier, load_keys_from_env

        keys = load_keys_from_env()
        mcp.auth = ApiKeyVerifier(keys, required_scopes=[READ_SCOPE])
        logger.info("Bearer-token auth enabled (%d key(s) configured)", len(keys))
    else:
        # stdio on a developer's own machine: the OS process boundary is the
        # security boundary, and there is no network listener to protect.
        logger.info("Auth disabled — local mode")

    if settings.corpus_url:
        _register_corpus_tools()
        logger.info("Public corpus tools registered")
    else:
        logger.info("No AXIOM_CORPUS_URL set — running with failure memory only")

    return mcp


def build_http_app(path: str = "/mcp"):
    """Streamable HTTP ASGI app, for mounting into the hosted deployment.

    `stateless_http=True` matches the 2026-07-28 protocol core, which removed
    session state entirely. Requests carry their own protocol version and
    capabilities in `_meta`, so any request can land on any instance behind a
    plain round-robin load balancer with no shared session store — which is
    what makes horizontal scaling and cold-start-friendly hosting possible.

    Origin validation is applied here rather than left to the deployment.
    The spec makes it a MUST: without it a page in a user's browser can POST
    to a localhost or intranet MCP server and drive its tools, since a
    same-site cookie is not required for a bearer-less local deployment. This
    is the DNS-rebinding class of attack, and the check is the whole defence.
    """
    from axiom_debug.mcp.origin import OriginValidationMiddleware, allowed_origins

    server = build_server()
    return server.http_app(
        path=path,
        transport="http",
        stateless_http=True,
        middleware=[OriginValidationMiddleware.asgi(allowed_origins())],
    )


def main() -> None:
    """Console entrypoint: `axiom-debug-mcp`, stdio transport.

    Logging is forced to stderr. On stdio the protocol owns stdout, and a
    single stray byte written there corrupts JSON-RPC framing — the client
    then either sees a malformed message or blocks forever waiting for one.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | axiom-debug | %(message)s",
    )

    server = build_server()
    server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
