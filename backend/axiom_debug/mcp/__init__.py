"""MCP surface for Axiom Debug.

Import-light on purpose: nothing here pulls FastAPI, Postgres drivers or LLM
clients at module scope, so `uvx axiom-debug-mcp` installs and runs with the
base dependency set alone. Heavier paths (the OSS corpus, the agent loop)
are imported inside the functions that need them and registered only when
configured. See server.py.
"""

from axiom_debug.mcp.server import build_server, main, mcp

__all__ = ["build_server", "main", "mcp"]
