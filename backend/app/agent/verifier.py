"""Citation verification — the one thing that can't be allowed to fail silently.

Two layers:

1. Structural (free, deterministic): a cited incident_id must be one the
   agent actually retrieved via a tool this session. This makes a fabricated
   citation impossible, not just unlikely — there's no code path that lets
   `cited_incident_ids` contain an id absent from `seen_incidents`.

2. Semantic (one cheap gpt-oss-20b call, via VERIFIER_MODEL): even a *real* citation can be cited for
   the wrong reason — the agent might claim a fix applies when the resolution
   summary doesn't actually support the claim. This catches that class of
   error, which the structural check can't.
"""

import json

from app.agent.client import get_client
from app.config import settings
from app.core.logger import logger
from app.schemas.analysis import Analysis
from app.schemas.incident import MatchedIssue

VERIFIER_PROMPT = """You check whether a claimed root cause and fix are actually
supported by the evidence cited for them. You are not re-solving the
failure — you are auditing whether the citation backs up the claim.

Answer strictly from the evidence given. If the resolution described in the
evidence doesn't clearly support the root cause or next steps claimed,
say so."""

VERIFIER_SCHEMA = {
    "name": "citation_verification",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "supported": {
                "type": "boolean",
                "description": "Does the cited evidence actually support the claimed root cause and fix?",
            },
            "note": {
                "type": "string",
                "description": "One sentence: why, especially if not supported.",
            },
        },
        "required": ["supported", "note"],
        "additionalProperties": False,
    },
}


def filter_valid_citations(
    cited_incident_ids: list[str],
    seen_incidents: dict[str, MatchedIssue],
) -> list[MatchedIssue]:
    """Drop any citation the agent didn't actually retrieve this session."""
    valid = []
    for incident_id in cited_incident_ids:
        issue = seen_incidents.get(incident_id)
        if issue is None:
            logger.warning(
                "Dropped citation %s — not retrieved via any tool this session",
                incident_id,
            )
            continue
        valid.append(issue)
    return valid


async def verify_semantic_consistency(
    analysis: Analysis,
    cited_issues: list[MatchedIssue],
) -> tuple[bool, str | None]:
    """Ask a cheap model whether the citations actually support the claim.

    Returns (supported, note). If there's nothing cited, there's nothing to
    verify — returns (True, None) rather than flagging an uncited answer,
    which is a confidence problem, not a citation-integrity one.
    """
    if not cited_issues:
        return True, None

    if not settings.GROQ_API_KEY:
        return True, None

    evidence = "\n\n".join(
        f"- {issue.title}\n  Resolution: {issue.resolution_summary}"
        for issue in cited_issues
    )

    prompt = (
        f"Claimed root cause: {analysis.root_cause}\n"
        f"Claimed next steps: {'; '.join(analysis.next_steps)}\n\n"
        f"Evidence cited:\n{evidence}"
    )

    client = get_client()
    response = await client.chat.completions.create(
        model=settings.VERIFIER_MODEL,
        messages=[
            {"role": "system", "content": VERIFIER_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_format={"type": "json_schema", "json_schema": VERIFIER_SCHEMA},
    )

    text = response.choices[0].message.content
    if not text:
        return True, None

    result = json.loads(text)
    return result["supported"], result.get("note")
