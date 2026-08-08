"""The agent's tool surface, in OpenAI/Groq function-calling shape.

Groq has no equivalent of Anthropic's Tool Runner — nothing auto-executes a
decorated function when the model calls it. So this is split in two:
`TOOL_SCHEMAS` (static, sent with every request) and `ToolExecutor` (bound
to one request's `db` session and shared state, dispatches by name).

`seen_incidents` and `finalized` are the same shared-state pattern as
before: every incident actually retrieved gets recorded (the source of
truth for citation verification), and `finalize_analysis` fills a one-slot
box that ends the loop.
"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.incident import OSSIncident
from app.schemas.analysis import VersionVerdict
from app.schemas.incident import Citation, MatchedIssue
from app.services.retrieval_service import RetrievalService
from app.services.version_service import VersionService

# search_incidents returns up to 5 candidates, and every one of them re-enters
# context on every remaining loop turn (the full message history gets resent
# each call — see agent/loop.py). Truncating summaries here keeps that cost
# from growing with the index; get_issue_details still returns full text for
# whichever one candidate the agent actually decides to look closer at.
SEARCH_SUMMARY_CHARS = 400


def _truncate(text: str, limit: int = SEARCH_SUMMARY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_incidents",
            "description": (
                "Search the index of solved open-source issues for a failure matching "
                "this description. Call this first for any code-level exception. Pass "
                "the exception type and message, not the full traceback — e.g. "
                "\"AttributeError: DataFrame object has no attribute 'append'\". If "
                "results come back with low similarity or none at all, try again with a "
                "shorter, more general query before giving up on this failure being in "
                "the index. Each result carries a `similarity` from 0 to 1: above ~0.9 "
                "is the same failure, ~0.75 is the same kind of error, below ~0.6 is "
                "probably unrelated — judge matches by that, and note that genuinely "
                "unrelated results are filtered out entirely rather than shown to you. "
                "Summaries here are shortened — call get_issue_details on the one "
                "candidate worth confirming before citing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The failure to search for."},
                    "library": {
                        "type": ["string", "null"],
                        "description": "Restrict results to this package, if known. Omit to search everything indexed.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_issue_details",
            "description": (
                "Fetch the full stored detail for one previously matched issue, by its "
                "incident_id. Use this when a search result's summary isn't enough to "
                "confirm it's actually the same failure, before citing it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "incident_id": {"type": "string"},
                },
                "required": ["incident_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_latest_version",
            "description": (
                "Check the latest released version of a Python package on PyPI, and "
                "compare it to the caller's installed version if provided. Call this "
                "only when the failure has a real version signal — a method that "
                "sounds deprecated, an import that moved. Not on every failure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "package": {"type": "string"},
                },
                "required": ["package"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_analysis",
            "description": (
                "Submit your completed analysis. Call this exactly once, when you're "
                "done investigating — this is how your answer is returned, not plain text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "One sentence: what failed."},
                    "root_cause": {"type": "string", "description": "The specific mechanism that caused the failure."},
                    "explanation": {"type": "string", "description": "Plain-language explanation for someone who didn't write this code."},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "next_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Concrete, checkable actions — not \"investigate further\".",
                    },
                    "suspected_library": {
                        "type": ["string", "null"],
                        "description": "Third-party package implicated, if any.",
                    },
                    "cited_incident_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "incident_ids you actually retrieved via search_incidents or "
                            "get_issue_details this session, and that your root_cause and "
                            "next_steps are actually based on. Empty array only if nothing "
                            "you retrieved genuinely informed this answer. Never include an "
                            "id you haven't seen."
                        ),
                    },
                    "suggested_patch": {
                        "type": ["string", "null"],
                        "description": (
                            "A minimal fix for the failure, ONLY if <file_context> was provided "
                            "and you're confident in a precise, narrow fix grounded in that real "
                            "file content. MUST be a real unified diff (--- a/, +++ b/, "
                            "@@ -n,m +n,m @@ with real line numbers) — exactly what `git diff` "
                            "produces, nothing else; it goes straight to `git apply`, so any "
                            "other convention just fails. Null if no file_context was given, or "
                            "the fix isn't small and certain enough to apply automatically. "
                            "Never invent file contents you weren't shown."
                        ),
                    },
                },
                # Strict mode requires every property in `required` — optionality
                # is expressed via nullable types instead. This is deliberate: it's
                # what stops the model from silently omitting cited_incident_ids
                # even when it clearly used a retrieved incident (observed live).
                "required": [
                    "summary", "root_cause", "explanation", "confidence",
                    "next_steps", "suspected_library", "cited_incident_ids",
                    "suggested_patch",
                ],
                "additionalProperties": False,
            },
            "strict": True,
        },
    },
]


class ToolExecutor:

    def __init__(
        self,
        db: AsyncSession,
        dependencies_text: str | None,
        language: str,
        seen_incidents: dict[str, MatchedIssue],
        finalized: dict[str, Any],
    ):
        self.db = db
        self.dependencies_text = dependencies_text
        # Picks the registry (PyPI vs npm) and how to read the caller's
        # pasted dependency block. Detected from the log, not requested.
        self.language = language
        self.seen_incidents = seen_incidents
        self.finalized = finalized

        # Populated only by get_issue_details, not search — a deliberate
        # lookup is a much stronger "the agent actually engaged with this"
        # signal than merely appearing in a candidate list. Used as a
        # fallback in loop.py if the model leaves cited_incident_ids empty
        # despite having clearly used a retrieved incident.
        self.confirmed_incident_ids: list[str] = []

        # Every check_latest_version result, in call order. The agent gets
        # these back as tool output and reasons over them, but they also
        # need to reach the API response as structured data — otherwise a
        # frontend has nothing to render but prose. A list because the agent
        # can legitimately check more than one package.
        self.version_verdicts: list[VersionVerdict] = []

    async def execute(self, name: str, arguments: dict) -> str:
        handler = {
            "search_incidents": self._search_incidents,
            "get_issue_details": self._get_issue_details,
            "check_latest_version": self._check_latest_version,
            "finalize_analysis": self._finalize_analysis,
        }.get(name)

        if handler is None:
            return json.dumps({"error": f"Unknown tool: {name}"})

        try:
            return await handler(**arguments)
        except TypeError as exc:
            # A malformed tool call (wrong/missing args) — feed it back to the
            # model as a tool error rather than crashing the whole request.
            return json.dumps({"error": f"Invalid arguments for {name}: {exc}"})

    async def _search_incidents(self, query: str, library: str | None = None) -> str:
        # Language comes from the detected failure, not from the model — it
        # isn't asked for as a tool argument because there's nothing for the
        # agent to judge here, and a wrong guess would silently hide the
        # right answer.
        results = await RetrievalService.search(
            self.db, query, library=library, language=self.language
        )

        for r in results:
            self.seen_incidents[r.incident_id] = r

        if not results:
            return json.dumps(
                {"results": [], "note": "No matches. Try a broader or different query."}
            )

        return json.dumps(
            {
                "results": [
                    {
                        "incident_id": r.incident_id,
                        "library": r.library,
                        "title": r.title,
                        # Shortened — seen_incidents above keeps the full text,
                        # so citations still carry the untruncated resolution.
                        # get_issue_details returns the full text if needed.
                        "problem_summary": _truncate(r.problem_summary),
                        "resolution_summary": _truncate(r.resolution_summary),
                        "similarity": r.similarity,
                        "issue_url": r.citation.issue_url,
                    }
                    for r in results
                ]
            }
        )

    async def _get_issue_details(self, incident_id: str) -> str:
        result = await self.db.execute(
            select(OSSIncident).where(OSSIncident.id == incident_id)
        )
        incident = result.scalar_one_or_none()
        if incident is None:
            return json.dumps({"error": "No incident with that id."})

        if str(incident.id) not in self.confirmed_incident_ids:
            self.confirmed_incident_ids.append(str(incident.id))

        # Not from a ranked search, so there's no fusion score to report —
        # 1.0 signals "the agent looked this up directly," not a rank.
        self.seen_incidents[str(incident.id)] = MatchedIssue(
            incident_id=str(incident.id),
            library=incident.library,
            title=incident.issue_title,
            problem_summary=incident.problem_summary,
            resolution_summary=incident.resolution_summary,
            similarity=1.0,
            rank_score=1.0,
            citation=Citation(
                issue_url=incident.issue_url,
                issue_title=incident.issue_title,
                fixing_commit_url=incident.fixing_commit_url,
            ),
        )

        return json.dumps(
            {
                "library": incident.library,
                "issue_title": incident.issue_title,
                "problem_summary": incident.problem_summary,
                "resolution_summary": incident.resolution_summary,
                "issue_url": incident.issue_url,
                "fixing_commit_url": incident.fixing_commit_url,
            }
        )

    async def _check_latest_version(self, package: str) -> str:
        installed = None
        if self.dependencies_text:
            installed = VersionService.installed_version(
                self.dependencies_text, package, self.language
            )

        verdict = await VersionService.verdict(package, installed, self.language)
        self.version_verdicts.append(verdict)
        return json.dumps(verdict.model_dump())

    async def _finalize_analysis(
        self,
        summary: str,
        root_cause: str,
        explanation: str,
        confidence: str,
        next_steps: list[str],
        suspected_library: str | None = None,
        cited_incident_ids: list[str] | None = None,
        suggested_patch: str | None = None,
    ) -> str:
        self.finalized["data"] = {
            "summary": summary,
            "root_cause": root_cause,
            "explanation": explanation,
            "confidence": confidence,
            "next_steps": next_steps,
            "suspected_library": suspected_library,
            "cited_incident_ids": cited_incident_ids or [],
            "suggested_patch": suggested_patch,
        }
        return "Analysis recorded."
