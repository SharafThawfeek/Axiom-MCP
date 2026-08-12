"""Importing this package registers every model on Base.metadata.

Alembic's env.py relies on that side effect for autogenerate to see the
tables at all — these imports are load-bearing, not conveniences.
"""

from axiom_debug.models.analysis import Analysis
from axiom_debug.models.incident import OSSIncident

__all__ = ["Analysis", "OSSIncident"]
