"""The agent loop must survive Groq rejecting a mislabelled tool call.

test_error_salvage.py covers the parsing in isolation. This covers the wiring:
that run_agent actually catches the error, executes the recovered call, and
still produces an answer — the part unit-testing the parser can't prove.

The error payload is real, captured from a live Groq 400.
"""
import json

import httpx
import pytest
from groq import BadRequestError

from app.agent import loop as agent_loop
from app.parsers import parse

LOG = """Traceback (most recent call last):
  File "/app/utils.py", line 17, in process
    return frame.append(row)
AttributeError: 'DataFrame' object has no attribute 'append'
"""

FINALIZE_ARGS = {
    "summary": "DataFrame.append no longer exists.",
    "root_cause": "Removed in pandas 2.0.",
    "explanation": "The method was removed, so the attribute lookup fails.",
    "confidence": "high",
    "next_steps": ["Use pd.concat instead."],
    "suspected_library": "pandas",
    "cited_incident_ids": [],
}


def _tool_use_failed(name: str, arguments: dict) -> BadRequestError:
    body = {
        "error": {
            "code": "tool_use_failed",
            "type": "invalid_request_error",
            "message": f"attempted to call tool '{name}' which was not in request.tools",
            "failed_generation": json.dumps({"name": name, "arguments": arguments}),
        }
    }
    return BadRequestError(
        "Tool call validation failed",
        response=httpx.Response(400, request=httpx.Request("POST", "https://api.groq.com")),
        body=body,
    )


class _RejectingClient:
    """A Groq client that always rejects with the channel-token quirk."""

    def __init__(self, name, arguments):
        self._error = _tool_use_failed(name, arguments)
        self.calls = 0

        outer = self

        class _Completions:
            async def create(self, **kwargs):
                outer.calls += 1
                raise outer._error

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.mark.asyncio
async def test_recovers_an_answer_when_finalize_is_mislabelled(monkeypatch):
    client = _RejectingClient("commentary", FINALIZE_ARGS)
    monkeypatch.setattr(agent_loop, "get_client", lambda: client)

    result = await agent_loop.run_agent(
        db=None,  # no tool touches the DB on this path
        log=LOG,
        failure=parse(LOG),
    )

    assert result.analysis.summary == "DataFrame.append no longer exists."
    assert result.analysis.confidence == "high"
    assert result.agent_trace["tool_calls"][0]["recovered"] is True


@pytest.mark.asyncio
async def test_unrecoverable_rejection_becomes_a_clean_error_not_a_crash(monkeypatch):
    # Name isn't a real tool and the args aren't finalize-shaped.
    client = _RejectingClient("mystery", {"foo": "bar"})
    monkeypatch.setattr(agent_loop, "get_client", lambda: client)

    with pytest.raises(ValueError, match="rejected this request"):
        await agent_loop.run_agent(db=None, log=LOG, failure=parse(LOG))
