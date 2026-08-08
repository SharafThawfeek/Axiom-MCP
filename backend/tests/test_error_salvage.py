"""Recovering a usable tool call from a request Groq rejected.

Both payloads below are real, captured from live Groq 400s. gpt-oss leaks its
internal harmony channel token into the tool name in two different shapes,
and Groq's server-side name validation rejects the whole request for it.
"""
import json

from app.agent.loop import _clean_tool_name, _salvage_tool_call_from_error


class FakeBadRequest:
    """Stands in for groq.BadRequestError, which only needs `.body` here."""

    def __init__(self, body):
        self.body = body


FINALIZE_ARGS = {
    "cited_incident_ids": [],
    "confidence": "high",
    "explanation": "Starting with pandas 2.0, DataFrame.append was removed.",
    "next_steps": ["Replace DataFrame.append with pandas.concat."],
    "root_cause": "Use of the removed DataFrame.append method.",
    "summary": "AttributeError: 'DataFrame' object has no attribute 'append'.",
    "suspected_library": "pandas",
}

SEARCH_ARGS = {"library": "pandas", "query": "append removed pandas"}


def _error(name, arguments, code="tool_use_failed"):
    return FakeBadRequest({
        "error": {
            "code": code,
            "type": "invalid_request_error",
            "message": f"attempted to call tool '{name}' which was not in request.tools",
            "failed_generation": json.dumps({"name": name, "arguments": arguments}),
        }
    })


def test_strips_a_channel_token_appended_to_a_real_tool_name():
    # Observed live: 'search_incidents<|channel|>commentary'
    assert _clean_tool_name("search_incidents<|channel|>commentary") == "search_incidents"
    assert _clean_tool_name("finalize_analysis") == "finalize_analysis"


def test_recovers_a_real_tool_whose_name_had_a_channel_token():
    result = _salvage_tool_call_from_error(
        _error("search_incidents<|channel|>commentary", SEARCH_ARGS)
    )

    assert result == ("search_incidents", SEARCH_ARGS)


def test_recovers_finalize_when_the_name_is_only_the_channel():
    # Observed live: the name was just 'commentary', but the arguments were
    # a complete finalize_analysis.
    name, arguments = _salvage_tool_call_from_error(_error("commentary", FINALIZE_ARGS))

    assert name == "finalize_analysis"
    assert arguments["confidence"] == "high"
    assert arguments["suspected_library"] == "pandas"


def test_handles_arguments_serialised_as_a_json_string():
    body = _error("commentary", FINALIZE_ARGS)
    body.body["error"]["failed_generation"] = json.dumps(
        {"name": "commentary", "arguments": json.dumps(FINALIZE_ARGS)}
    )

    assert _salvage_tool_call_from_error(body) is not None


def test_fills_in_optional_finalize_fields_the_model_left_out():
    partial = {k: v for k, v in FINALIZE_ARGS.items()
               if k not in ("suspected_library", "cited_incident_ids")}

    _, arguments = _salvage_tool_call_from_error(_error("commentary", partial))

    assert arguments["suspected_library"] is None
    assert arguments["cited_incident_ids"] == []


def test_ignores_errors_that_are_not_tool_use_failures():
    assert _salvage_tool_call_from_error(
        _error("commentary", FINALIZE_ARGS, code="rate_limit_exceeded")
    ) is None


def test_gives_up_on_an_unrecognisable_payload():
    # Unknown name, and the arguments aren't finalize-shaped either.
    assert _salvage_tool_call_from_error(_error("mystery", {"foo": "bar"})) is None


def test_survives_a_malformed_error_body():
    assert _salvage_tool_call_from_error(FakeBadRequest(None)) is None
    assert _salvage_tool_call_from_error(FakeBadRequest({})) is None
    assert _salvage_tool_call_from_error(
        FakeBadRequest({"error": {"code": "tool_use_failed", "failed_generation": "not json"}})
    ) is None
