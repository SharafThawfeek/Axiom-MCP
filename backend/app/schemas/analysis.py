from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.incident import MatchedIssue


class AnalyzeRequest(BaseModel):
    log: str = Field(
        min_length=1,
        max_length=500_000,
        description="Raw failing log or traceback, as pasted from CI."
    )
    # Optional hint only. The parser detects the real language from the log
    # itself, so a wrong or missing value here costs nothing.
    language: Literal["python", "javascript"] | None = None

    # Optional `pip freeze` / requirements.txt paste. Not required — but
    # without it the version-check tool has no installed version to check
    # against, since this service never sees the caller's filesystem. Capped
    # like `log` — this is an unauthenticated endpoint, so every field needs
    # a bound, not just the obviously-large one.
    dependencies: str | None = Field(default=None, max_length=50_000)

    # Real content of the file(s) the traceback implicates, if the caller has
    # repo access and can provide it (e.g. a CI job that already checked out
    # the code). Without this the agent only ever sees the single source line
    # a traceback happens to print — not enough to safely propose a patch
    # against. suggested_patch is only ever produced when this is present.
    file_context: str | None = Field(default=None, max_length=100_000)


class StackFrame(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    file: str
    line: int
    function: str
    code: str | None = None


class ParsedFailure(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # Detected from the log, not taken from the request — a CI job often runs
    # more than one toolchain, so what the caller claims is a hint at best.
    language: str

    exception_type: str
    exception_message: str
    frames: list[StackFrame]

    # Normalised form of the failure, stable across runs. This is what gets
    # embedded, not the raw message — paths, addresses and IDs shouldn't
    # dominate the vector.
    signature: str

    # Deepest frame that isn't site-packages or stdlib — usually the caller's
    # own code, and the best hint at which library is implicated.
    origin: StackFrame | None = None


class VersionVerdict(BaseModel):
    """What check_latest_version found for one package.

    Deliberately not persisted with the analysis: "latest on PyPI" is
    time-sensitive, so serving a stored copy on a cache hit would hand back
    a verdict that may since have gone stale. Same reasoning as
    suggested_patch — fresh runs report it, stored records don't.
    """

    package: str
    installed_version: str | None
    verdict: str


class Analysis(BaseModel):
    """The structured explanation the agent returns via finalize_analysis. Mirrors the tool schema in app/agent/tools.py."""

    summary: str
    root_cause: str
    explanation: str
    confidence: Literal["high", "medium", "low"]
    suspected_library: str | None = None
    next_steps: list[str]

    # A minimal unified diff against file_context, only ever produced when
    # file_context was provided and the agent is confident enough for a
    # precise, reviewable fix — never fabricated against a file it hasn't
    # actually seen. Null means "no safe patch to propose," not "no fix
    # exists" — the text explanation above always stands on its own.
    suggested_patch: str | None = None


class AnalysisResponse(BaseModel):
    id: str | None = None
    failure: ParsedFailure | None
    analysis: Analysis
    matched_issues: list[MatchedIssue] = []
    version_verdict: VersionVerdict | None = None

    # Which tools the agent actually called, and in what order — visible
    # for debugging and for judge Q&A ("show me it actually investigated").
    agent_trace: dict | None = None

    # True if this is a stored answer for a previously-seen failure_signature,
    # returned without re-running the agent. Never silently indistinguishable
    # from a fresh investigation.
    from_cache: bool = False
