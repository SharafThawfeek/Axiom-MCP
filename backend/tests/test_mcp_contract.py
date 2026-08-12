"""The MCP tool contract, exercised through a real client.

These go through fastmcp's in-memory transport rather than calling the
Python functions directly, so they cover what a client actually sees:
schema generation, argument validation, structured output, and error
shaping. A test that called the functions directly would pass while the
served contract was broken.

The inventory test is deliberately strict. Silent schema drift between
versions is a known MCP failure mode — a client pins a tool name or an
output field, the server quietly renames it, and nothing fails until
someone's agent stops working. Changing the assertion below should be a
conscious act with a version bump attached.
"""

import numpy as np
import pytest
from axiom_debug.mcp import server as mcp_server
from axiom_debug.services.embedding_service import EmbeddingService
from fastmcp import Client

PANDAS_TRACEBACK = """\
2026-08-12T09:14:02.1234567Z Run pytest
Traceback (most recent call last):
  File "/home/runner/work/app/app/etl/transform.py", line 44, in normalise
    frame = frame.append(row)
  File "/usr/lib/python3.11/site-packages/pandas/core/generic.py", line 6299, in __getattr__
    return object.__getattribute__(self, name)
AttributeError: 'DataFrame' object has no attribute 'append'
"""

EXPECTED_TOOLS = {
    "parse_failure",
    "recall_failure_memory",
    "record_failure_resolution",
    "check_package_version",
}

READ_ONLY_TOOLS = {"parse_failure", "recall_failure_memory", "check_package_version"}


@pytest.fixture
def fake_embeddings(monkeypatch):
    async def _embed(text: str) -> list[float]:
        rng = np.random.default_rng(abs(hash(text)) % (2**32))
        vec = rng.standard_normal(384).astype(np.float32)
        return (vec / np.linalg.norm(vec)).tolist()

    monkeypatch.setattr(EmbeddingService, "aembed_one", staticmethod(_embed))


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """A private memory database and a pinned tenant, per test."""
    monkeypatch.setenv("AXIOM_MEMORY_URL", f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    monkeypatch.setenv("AXIOM_PROJECT_ID", "test-project")
    monkeypatch.delenv("AXIOM_CORPUS_URL", raising=False)
    mcp_server.reset_state()
    yield
    mcp_server.reset_state()


@pytest.fixture
async def client(isolated_env, fake_embeddings):
    async with Client(mcp_server.build_server()) as c:
        yield c


# --- contract --------------------------------------------------------------


async def test_tool_inventory_is_exactly_as_declared(client):
    names = {t.name for t in await client.list_tools()}
    assert names == EXPECTED_TOOLS


async def test_every_tool_has_a_description_and_structured_output(client):
    for tool in await client.list_tools():
        assert tool.description and len(tool.description) > 40, tool.name
        assert tool.output_schema, f"{tool.name} returns unstructured text"
        assert tool.input_schema.get("type") == "object", tool.name


async def test_read_and_write_tools_are_annotated_correctly(client):
    """Hosts surface these hints for consent prompts, so they must be right."""
    for tool in await client.list_tools():
        assert tool.annotations is not None, tool.name
        read_only = tool.annotations.read_only_hint
        if tool.name in READ_ONLY_TOOLS:
            assert read_only is True, f"{tool.name} should be read-only"
        else:
            assert read_only is False, f"{tool.name} must not claim read-only"


async def test_only_the_write_tool_mutates(client):
    write = [
        t.name
        for t in await client.list_tools()
        if t.annotations and t.annotations.read_only_hint is False
    ]
    assert write == ["record_failure_resolution"]


async def test_corpus_tools_are_absent_when_no_corpus_configured(client):
    """An always-failing tool costs context on every turn. Don't advertise it."""
    names = {t.name for t in await client.list_tools()}
    assert "search_public_incidents" not in names


# --- behaviour -------------------------------------------------------------


async def test_parse_failure_extracts_a_normalised_signature(client):
    result = await client.call_tool("parse_failure", {"log": PANDAS_TRACEBACK})
    data = result.data

    assert data.exception_type == "AttributeError"
    assert data.language == "python"
    # The quoted literal is normalised out, which is what makes the signature
    # stable across runs — assert that rather than the raw message.
    assert "<str>" in data.signature
    assert "AttributeError" in data.signature
    assert data.origin.function == "normalise"
    assert data.origin.file.endswith("transform.py")
    assert data.implicated_library == "pandas"


async def test_parse_failure_reports_a_usable_error_on_a_non_traceback(client):
    """mask_error_details must not swallow deliberate guidance."""
    with pytest.raises(Exception) as exc:
        await client.call_tool("parse_failure", {"log": "ok\nall good\n"})

    assert "no traceback" in str(exc.value).lower()


async def test_recall_on_empty_memory_explains_itself(client):
    result = await client.call_tool(
        "recall_failure_memory", {"signature": "ValueError: nope @ main"}
    )

    assert result.data.memories == []
    assert result.data.total_known_failures == 0
    assert "empty" in (result.data.note or "").lower()


async def test_record_then_recall_returns_the_real_fix(client):
    parsed = (await client.call_tool("parse_failure", {"log": PANDAS_TRACEBACK})).data

    await client.call_tool(
        "record_failure_resolution",
        {
            "signature": parsed.signature,
            "resolution": "DataFrame.append was removed in pandas 2.0; use pd.concat.",
            "resolved_by": "https://github.com/acme/app/pull/412",
            "source": "human",
        },
    )

    recalled = (
        await client.call_tool(
            "recall_failure_memory", {"signature": parsed.signature}
        )
    ).data

    assert len(recalled.memories) == 1
    hit = recalled.memories[0]
    assert hit.match == "exact"
    assert hit.similarity == 1.0
    assert "pd.concat" in hit.resolution
    assert hit.resolved_by.endswith("/412")
    assert hit.source == "human"
    assert recalled.total_known_failures == 1


async def test_recall_accepts_a_raw_log_instead_of_a_signature(client):
    parsed = (await client.call_tool("parse_failure", {"log": PANDAS_TRACEBACK})).data
    await client.call_tool(
        "record_failure_resolution",
        {"signature": parsed.signature, "resolution": "use pd.concat"},
    )

    recalled = (
        await client.call_tool("recall_failure_memory", {"log": PANDAS_TRACEBACK})
    ).data

    assert len(recalled.memories) == 1
    assert recalled.memories[0].match == "exact"


async def test_recall_requires_at_least_one_input(client):
    with pytest.raises(Exception) as exc:
        await client.call_tool("recall_failure_memory", {})
    assert "signature or a log" in str(exc.value).lower()


async def test_record_rejects_an_empty_resolution(client):
    """A memory row with no resolution is noise that crowds out real ones."""
    with pytest.raises(Exception) as exc:
        await client.call_tool(
            "record_failure_resolution",
            {"signature": "ValueError: x @ main", "resolution": "   "},
        )
    assert "resolution must not be empty" in str(exc.value).lower()


async def test_record_rejects_an_unknown_source(client):
    with pytest.raises(Exception) as exc:
        await client.call_tool(
            "record_failure_resolution",
            {
                "signature": "ValueError: x @ main",
                "resolution": "fixed it",
                "source": "hearsay",
            },
        )
    assert "source must be one of" in str(exc.value).lower()


async def test_recording_the_same_signature_twice_updates_in_place(client):
    sig = "ValueError: bad input @ main"
    for resolution in ("first guess", "the actual fix"):
        result = await client.call_tool(
            "record_failure_resolution", {"signature": sig, "resolution": resolution}
        )

    assert result.data.occurrences == 2

    recalled = (
        await client.call_tool("recall_failure_memory", {"signature": sig})
    ).data
    assert recalled.total_known_failures == 1
    assert recalled.memories[0].resolution == "the actual fix"
    assert recalled.memories[0].occurrences == 2
