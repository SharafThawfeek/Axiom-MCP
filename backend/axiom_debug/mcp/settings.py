"""Configuration for the MCP server.

Separate from axiom_debug.config on purpose. That module is the hosted
FastAPI app's settings and declares Groq, Gemini, Postgres and LangSmith
fields the local stdio server has no use for — importing it would drag
provider configuration into a process that never calls a provider.

Everything here has a working default. `uvx axiom-debug-mcp` with no
environment at all must start and be useful; configuration only ever adds
capability, it is never required to get off the ground.
"""

import os
from dataclasses import dataclass
from pathlib import Path

# Local memory lives outside the repository. Putting it in the working tree
# would mean a database file that shows up in git status, gets committed by
# accident, and leaks one project's failure history into a public repo.
DEFAULT_MEMORY_DIR = Path.home() / ".axiom-debug"
DEFAULT_MEMORY_FILE = "memory.db"


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class MCPSettings:
    # Where failure memory is stored. A SQLAlchemy URL, so pointing this at
    # Postgres is a configuration change rather than a code path:
    #   postgresql+asyncpg://user:pass@host/db
    memory_url: str

    # Postgres URL for the OSS incident corpus. Unset is the normal local
    # case — the corpus tools simply aren't registered, rather than being
    # registered and failing at call time. See server.py.
    corpus_url: str | None

    # Opt-in. Registers analyze_failure, which runs the full Groq agent loop:
    # two LLM calls and ~15s per invocation, against a surface where every
    # other tool answers in milliseconds. Off by default so the expensive
    # path is never the one a client reaches for by accident.
    enable_agent: bool

    # Hosted HTTP mode only. Ignored by stdio, where the tenant comes from
    # the local checkout instead.
    require_auth: bool

    @staticmethod
    def from_env() -> "MCPSettings":
        memory_url = os.environ.get("AXIOM_MEMORY_URL")
        if not memory_url:
            DEFAULT_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
            memory_url = f"sqlite+aiosqlite:///{DEFAULT_MEMORY_DIR / DEFAULT_MEMORY_FILE}"

        return MCPSettings(
            memory_url=memory_url,
            corpus_url=os.environ.get("AXIOM_CORPUS_URL") or None,
            enable_agent=_flag("AXIOM_ENABLE_AGENT"),
            require_auth=_flag("AXIOM_REQUIRE_AUTH"),
        )
