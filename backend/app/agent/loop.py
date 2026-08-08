"""Drives the manual tool-calling loop for one analysis.

Groq's chat completions API is OpenAI-shaped: no Tool Runner, so the loop is
explicit — call the model, execute whatever tools it asked for, append the
results, repeat until it calls `finalize_analysis` (see tools.py for why
that's a tool rather than a free-text final message) or the iteration cap
is hit.
"""

import asyncio
import json
import re
import uuid

from groq import APIError, APIStatusError, BadRequestError, RateLimitError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.client import get_client
from app.agent.prompts import SYSTEM_PROMPT
from app.agent.tools import TOOL_SCHEMAS, ToolExecutor
from app.agent.verifier import filter_valid_citations, verify_semantic_consistency
from app.config import settings
from app.core.logger import logger
from app.schemas.analysis import Analysis, AnalysisResponse, ParsedFailure
from app.schemas.incident import MatchedIssue

MAX_LOG_CHARS = 60_000
VALID_CONFIDENCE = {"high", "medium", "low"}

# A live request has a user waiting synchronously, unlike the indexer's bulk
# extraction — retries here stay short rather than honouring a long
# retry-after, so one 429 adds seconds, not minutes, before surfacing a
# clean error.
MAX_INTERACTIVE_RATE_LIMIT_RETRIES = 2
INTERACTIVE_RETRY_CAP_SECONDS = 15.0

REQUIRED_FINALIZE_FIELDS = {
    "summary", "root_cause", "explanation", "confidence", "next_steps",
}

KNOWN_TOOLS = {t["function"]["name"] for t in TOOL_SCHEMAS}

# gpt-oss uses "harmony" channels internally (analysis / commentary / final)
# and the channel token sometimes leaks into the tool name it emits — either
# appended to the real name ("search_incidents<|channel|>commentary") or
# replacing it entirely ("commentary").
_CHANNEL_TOKEN = re.compile(r"<\|.*")


def _clean_tool_name(name: str) -> str:
    return _CHANNEL_TOKEN.sub("", name or "").strip()


def _salvage_tool_call_from_error(exc: BadRequestError) -> tuple[str, dict] | None:
    """Recover a usable tool call from a request Groq rejected.

    Groq validates tool names server-side, so a leaked channel token fails
    the whole request — but the generated arguments are intact in the error
    body and are usually perfectly good. Discarding real work over a
    mislabelled envelope is worse than recovering it.

    Two recovery routes, in order:
      1. Strip the channel token; if what's left names a real tool, use it.
      2. Otherwise, if the arguments have finalize_analysis's shape, treat it
         as that — this covers the case where the name is *only* the channel.

    Returns (tool_name, arguments), or None if it isn't recognisable, in
    which case the caller should surface the original error.
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None

    error = body.get("error") or {}
    if error.get("code") != "tool_use_failed":
        return None

    raw = error.get("failed_generation")
    if not raw:
        return None

    try:
        generated = json.loads(raw)
        arguments = generated.get("arguments")
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
    except (json.JSONDecodeError, AttributeError):
        return None

    if not isinstance(arguments, dict):
        return None

    name = _clean_tool_name(generated.get("name", ""))
    if name in KNOWN_TOOLS:
        return name, arguments

    if REQUIRED_FINALIZE_FIELDS.issubset(arguments):
        arguments.setdefault("suspected_library", None)
        arguments.setdefault("cited_incident_ids", [])
        arguments.setdefault("suggested_patch", None)
        return "finalize_analysis", arguments

    return None


async def _create_completion(client, messages: list[dict]):
    """One completion call, retrying a bounded number of times on RateLimitError.

    RateLimitError is a subclass of APIStatusError, so without this it would
    be caught by the generic handler below and fail the whole request on the
    first 429 — wasteful for a transient burst a short wait would clear.
    """
    for attempt in range(MAX_INTERACTIVE_RATE_LIMIT_RETRIES + 1):
        try:
            return await client.chat.completions.create(
                model=settings.ANALYSIS_MODEL,
                messages=messages,
                tools=TOOL_SCHEMAS,
            )
        except RateLimitError as exc:
            if attempt == MAX_INTERACTIVE_RATE_LIMIT_RETRIES:
                raise

            wait = INTERACTIVE_RETRY_CAP_SECONDS
            try:
                wait = min(
                    float(exc.response.headers.get("retry-after", wait)),
                    INTERACTIVE_RETRY_CAP_SECONDS,
                )
            except (AttributeError, TypeError, ValueError):
                pass

            logger.info(
                "Analysis call rate-limited, retrying in %.1fs (attempt %d/%d)",
                wait, attempt + 1, MAX_INTERACTIVE_RATE_LIMIT_RETRIES,
            )
            await asyncio.sleep(wait)


MAX_FILE_CONTEXT_CHARS = 20_000


def _build_initial_message(
    log: str,
    failure: ParsedFailure | None,
    library_hint: str | None,
    dependencies_text: str | None,
    file_context: str | None,
) -> str:
    trimmed = log
    if len(log) > MAX_LOG_CHARS:
        trimmed = "[log truncated — showing the final portion]\n" + log[-MAX_LOG_CHARS:]

    parts = [f"<log>\n{trimmed}\n</log>"]

    if failure:
        detail = {
            "exception_type": failure.exception_type,
            "exception_message": failure.exception_message,
            "signature": failure.signature,
        }
        if failure.origin:
            detail["origin"] = (
                f"{failure.origin.file}:{failure.origin.line} in {failure.origin.function}"
            )
        if library_hint:
            detail["deepest_vendored_package"] = library_hint
        parts.append("<parsed_failure>\n" + json.dumps(detail, indent=2) + "\n</parsed_failure>")
    else:
        parts.append(
            "<parsed_failure>\nNo Python traceback could be parsed from this log. "
            "Work from the raw output, and say so if it's insufficient.\n</parsed_failure>"
        )

    if dependencies_text:
        parts.append(f"<dependencies>\n{dependencies_text.strip()[:5000]}\n</dependencies>")

    if file_context:
        parts.append(
            "<file_context>\n"
            f"{file_context.strip()[:MAX_FILE_CONTEXT_CHARS]}\n"
            "</file_context>\n"
            "This is the real, current content of the file(s) implicated by the "
            "traceback. If you're confident in a precise, minimal fix, you may "
            "propose it as a unified diff in suggested_patch — but only against "
            "what's actually shown above, never a file you haven't seen."
        )

    parts.append(
        "Investigate this failure using your tools, then call finalize_analysis "
        "with your complete answer."
    )

    return "\n\n".join(parts)


async def run_agent(
    db: AsyncSession,
    log: str,
    failure: ParsedFailure | None,
    library_hint: str | None = None,
    dependencies_text: str | None = None,
    file_context: str | None = None,
) -> AnalysisResponse:
    client = get_client()  # raises ValueError before any tool/DB work if unconfigured

    seen_incidents: dict[str, MatchedIssue] = {}
    finalized: dict = {}
    executor = ToolExecutor(db, dependencies_text, seen_incidents, finalized)

    user_message = _build_initial_message(log, failure, library_hint, dependencies_text, file_context)
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    trace: list[dict] = []

    for _ in range(settings.AGENT_MAX_ITERATIONS):
        try:
            response = await _create_completion(client, messages)
        except BadRequestError as exc:
            salvaged = _salvage_tool_call_from_error(exc)
            if salvaged is None:
                logger.error("Groq rejected the request: %s", exc)
                raise ValueError(
                    "The analysis provider rejected this request. Try again."
                ) from exc

            name, arguments = salvaged
            logger.warning(
                "Recovered a mislabelled '%s' tool call (gpt-oss channel-token "
                "quirk) from a rejected request", name,
            )

            # Rebuild the exchange the rejected response would have produced,
            # so the loop carries on from a consistent history.
            call_id = f"recovered_{uuid.uuid4().hex[:8]}"
            messages.append({
                "role": "assistant",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }],
            })
            result = await executor.execute(name, arguments)
            messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
            trace.append({"tool": name, "input": json.dumps(arguments), "recovered": True})

            if "data" in finalized:
                break
            continue
        except RateLimitError as exc:
            logger.error("Groq rate-limited the analysis request after retries: %s", exc)
            raise ValueError(
                "The analysis provider is temporarily overloaded. Try again shortly."
            ) from exc
        except APIStatusError as exc:
            logger.error("Groq request failed (%s): %s", exc.status_code, exc)
            raise ValueError(
                "The analysis provider is unavailable. Try again shortly."
            ) from exc
        except APIError as exc:
            # Catches APIConnectionError/APITimeoutError — these aren't
            # APIStatusError subclasses (no HTTP status: the request never
            # completed), so without this they'd propagate uncaught into an
            # unhandled 500 instead of the same clean error every other
            # provider failure produces.
            logger.error("Groq request failed to complete: %s", exc)
            raise ValueError(
                "The analysis provider is unavailable. Try again shortly."
            ) from exc

        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            # Model stopped without calling finalize_analysis at all.
            break

        for tool_call in message.tool_calls:
            # Defensive: Groq's server-side tool-name validation is expected
            # to reject a leaked channel token outright (the BadRequestError
            # path above), but that's inferred from limited live observation,
            # not a documented guarantee — cleaning here too costs nothing
            # and avoids silently burning an iteration on "Unknown tool" if
            # a mangled name ever does slip through in a 200 response.
            name = _clean_tool_name(tool_call.function.name)

            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                result = json.dumps({"error": "Malformed tool-call arguments JSON."})
            else:
                result = await executor.execute(name, arguments)

            trace.append({"tool": name, "input": tool_call.function.arguments})
            messages.append(
                {"role": "tool", "tool_call_id": tool_call.id, "content": result}
            )

        if "data" in finalized:
            break

    if "data" not in finalized:
        raise ValueError(
            "The agent stopped without finalizing an analysis "
            f"(hit the {settings.AGENT_MAX_ITERATIONS}-iteration limit, or a "
            "malformed response). Try again."
        )

    data = finalized["data"]
    confidence = str(data["confidence"]).strip().lower()
    if confidence not in VALID_CONFIDENCE:
        logger.warning("Agent returned invalid confidence %r; defaulting to low", confidence)
        confidence = "low"

    suggested_patch = data.get("suggested_patch")
    if suggested_patch and not file_context:
        # No file_context means there was nothing real to diff against — a
        # patch here can only be fabricated. Drop it rather than let a
        # hallucinated diff reach Automatic mode's git apply, regardless of
        # what the prompt asked for; this is the actual safety boundary; the
        # prompt is just a request.
        logger.warning("Agent produced a suggested_patch with no file_context; dropping it")
        suggested_patch = None

    analysis = Analysis(
        summary=data["summary"],
        root_cause=data["root_cause"],
        explanation=data["explanation"],
        confidence=confidence,
        suspected_library=data.get("suspected_library"),
        next_steps=data["next_steps"],
        suggested_patch=suggested_patch,
    )

    cited_ids = data["cited_incident_ids"]
    if not cited_ids and executor.confirmed_incident_ids:
        # The model used a retrieved incident but left citations empty —
        # observed live on gpt-oss. Fall back to whatever it deliberately
        # looked up via get_issue_details rather than silently citing nothing.
        logger.info(
            "Model left cited_incident_ids empty; falling back to %d confirmed lookup(s)",
            len(executor.confirmed_incident_ids),
        )
        cited_ids = executor.confirmed_incident_ids

    cited_issues = filter_valid_citations(cited_ids, seen_incidents)

    try:
        supported, note = await verify_semantic_consistency(analysis, cited_issues)
    except Exception as exc:
        # This is a secondary safety check on an already-successful analysis —
        # a provider hiccup here (rate limit, timeout, anything) shouldn't
        # discard a good result and 500 the whole request. Degrade to
        # "couldn't verify" rather than "couldn't analyse".
        logger.warning("Citation verification failed to run, proceeding unverified: %s", exc)
        supported, note = True, None

    if not supported:
        logger.warning("Verifier flagged citation as unsupported: %s", note)
        analysis.confidence = "low"
        analysis.explanation += (
            f"\n\n(Automated verification flagged this citation as possibly not "
            f"fully supporting the claim: {note})"
        )

    return AnalysisResponse(
        failure=failure,
        analysis=analysis,
        matched_issues=cited_issues,
        agent_trace={"tool_calls": trace},
    )
