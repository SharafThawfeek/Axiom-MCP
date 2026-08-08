"""Harness resilience: the interactive agent loop must survive the same
class of provider failures the indexer already handles, without losing an
already-successful analysis.

Covers three real gaps found in a top-to-bottom audit of app/agent/loop.py:
1. RateLimitError was previously caught but never retried — one 429 failed
   the whole live request outright.
2. APIConnectionError/APITimeoutError aren't APIStatusError subclasses (no
   HTTP status — the request never completed), so they previously bypassed
   all exception handling and would have produced an unhandled 500.
3. A failure in the *secondary* citation-verification call (a provider
   hiccup unrelated to whether the analysis itself succeeded) previously
   crashed the whole response instead of degrading gracefully.
Also covers the channel-token tool-name quirk arriving in a successful (200)
response rather than triggering the already-handled 400 rejection path.
"""
import json

import httpx
import pytest
from groq import APIConnectionError, RateLimitError

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


def _rate_limit_error():
    return RateLimitError(
        "rate limited",
        response=httpx.Response(
            429, headers={"retry-after": "0"},
            request=httpx.Request("POST", "https://api.groq.com"),
        ),
        body=None,
    )


def _connection_error():
    return APIConnectionError(request=httpx.Request("POST", "https://api.groq.com"))


class _FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _FakeFunction(name, arguments)


class _FakeMessage:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls

    def model_dump(self, exclude_none=True):
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ],
        }


class _FakeChoice:
    def __init__(self, message):
        self.message = message


class _FakeResponse:
    def __init__(self, tool_calls):
        self.choices = [_FakeChoice(_FakeMessage(tool_calls))]


def _finalize_response(name="finalize_analysis", call_id="call_1"):
    return _FakeResponse([_FakeToolCall(call_id, name, json.dumps(FINALIZE_ARGS))])


class _ScriptedClient:
    """A Groq client stub whose create() plays a scripted sequence of
    responses/exceptions, repeating the last entry if called more times
    than the script is long."""

    def __init__(self, script: list):
        self._script = script
        self.calls = 0

        outer = self

        class _Completions:
            async def create(self, **kwargs):
                index = min(outer.calls, len(outer._script) - 1)
                behavior = outer._script[index]
                outer.calls += 1
                if isinstance(behavior, BaseException):
                    raise behavior
                return behavior

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


async def _no_sleep(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_rate_limit_retries_then_succeeds(monkeypatch):
    exc = _rate_limit_error()
    client = _ScriptedClient([exc, exc, _finalize_response()])
    monkeypatch.setattr(agent_loop, "get_client", lambda: client)
    monkeypatch.setattr(agent_loop.asyncio, "sleep", _no_sleep)

    result = await agent_loop.run_agent(db=None, log=LOG, failure=parse(LOG))

    assert client.calls == 3
    assert result.analysis.summary == FINALIZE_ARGS["summary"]


@pytest.mark.asyncio
async def test_rate_limit_exhausted_raises_clean_error(monkeypatch):
    client = _ScriptedClient([_rate_limit_error()])
    monkeypatch.setattr(agent_loop, "get_client", lambda: client)
    monkeypatch.setattr(agent_loop.asyncio, "sleep", _no_sleep)

    with pytest.raises(ValueError, match="temporarily overloaded"):
        await agent_loop.run_agent(db=None, log=LOG, failure=parse(LOG))

    assert client.calls == agent_loop.MAX_INTERACTIVE_RATE_LIMIT_RETRIES + 1


@pytest.mark.asyncio
async def test_connection_error_becomes_clean_error_not_a_crash(monkeypatch):
    client = _ScriptedClient([_connection_error()])
    monkeypatch.setattr(agent_loop, "get_client", lambda: client)

    with pytest.raises(ValueError, match="unavailable"):
        await agent_loop.run_agent(db=None, log=LOG, failure=parse(LOG))


@pytest.mark.asyncio
async def test_verifier_failure_does_not_crash_a_successful_analysis(monkeypatch):
    client = _ScriptedClient([_finalize_response()])
    monkeypatch.setattr(agent_loop, "get_client", lambda: client)

    async def _raise(*a, **kw):
        raise RuntimeError("verifier boom")

    monkeypatch.setattr(agent_loop, "verify_semantic_consistency", _raise)

    result = await agent_loop.run_agent(db=None, log=LOG, failure=parse(LOG))

    assert result.analysis.summary == FINALIZE_ARGS["summary"]
    assert result.analysis.confidence == "high"  # not downgraded — verification didn't run, wasn't failed


@pytest.mark.asyncio
async def test_channel_token_in_a_successful_response_still_dispatches(monkeypatch):
    # A 200 response whose tool name carries the leaked channel token, not a
    # rejected request — proves the happy-path cleaning, not the salvage path.
    client = _ScriptedClient([_finalize_response(name="finalize_analysis<|channel|>commentary")])
    monkeypatch.setattr(agent_loop, "get_client", lambda: client)

    result = await agent_loop.run_agent(db=None, log=LOG, failure=parse(LOG))

    assert result.analysis.summary == FINALIZE_ARGS["summary"]
