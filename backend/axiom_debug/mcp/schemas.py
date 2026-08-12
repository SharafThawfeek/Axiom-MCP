"""Structured output contracts for the MCP tools.

Every tool returns a typed model rather than prose. Two reasons: the client
gets a real `outputSchema` it can validate against, and the response stops
being something the host model has to parse out of English — which is both
a token cost and a place for it to go wrong.

These are part of the public contract. Changing a field is a breaking change
for every installed client, which is why tools carry an explicit `version`
in server.py.
"""

from pydantic import BaseModel, Field


class Frame(BaseModel):
    file: str
    line: int
    function: str


class ParsedFailure(BaseModel):
    """What the deterministic parser extracted from a log."""

    language: str
    exception_type: str
    exception_message: str

    signature: str = Field(
        description=(
            "Normalised, stable identity for this failure. Addresses, quoted "
            "strings and numbers are replaced with placeholders so the same "
            "bug produces the same signature across runs and machines. This "
            "is the key recall_failure_memory matches on."
        )
    )

    origin: Frame | None = Field(
        default=None,
        description="Deepest frame that isn't vendored — usually the caller's own code.",
    )
    frame_count: int = 0
    implicated_library: str | None = None


class RecalledMemory(BaseModel):
    """One past failure this project already resolved."""

    signature: str
    resolution: str = Field(description="What actually fixed it last time.")
    resolved_by: str | None = Field(
        default=None, description="Commit SHA, PR URL or ticket, when recorded."
    )
    description: str | None = None
    exception_type: str | None = None

    match: str = Field(
        description=(
            "'exact' means the identical normalised signature — the same bug. "
            "'similar' means a near neighbour by embedding, which may or may "
            "not be the same root cause and should be read as a lead."
        )
    )
    similarity: float
    occurrences: int = Field(description="How many times this project has hit it.")
    source: str = Field(description="human | agent | ci — who supplied the resolution.")
    last_seen: str


class RecallResult(BaseModel):
    searched_signature: str | None = None
    memories: list[RecalledMemory] = []
    total_known_failures: int = Field(
        default=0, description="Distinct failures recorded for this project."
    )
    note: str | None = Field(
        default=None,
        description="Set when there are no matches, explaining what that means.",
    )


class RecordResult(BaseModel):
    recorded: bool
    signature: str
    occurrences: int
    note: str | None = None


class VersionCheck(BaseModel):
    package: str
    installed_version: str | None
    verdict: str


class MatchedIncident(BaseModel):
    """A solved issue from the public OSS corpus."""

    incident_id: str
    library: str
    title: str
    problem_summary: str
    resolution_summary: str
    similarity: float
    issue_url: str
    fixing_commit_url: str | None = None


class CorpusSearchResult(BaseModel):
    incidents: list[MatchedIncident] = []
    note: str | None = None
