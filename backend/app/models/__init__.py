"""Importing this package registers every model on Base.metadata.

Alembic's env.py relies on that side effect for autogenerate to see the
tables at all — these imports are load-bearing, not conveniences.
"""

from app.models.analysis import Analysis
from app.models.incident import OSSIncident

__all__ = ["Analysis", "OSSIncident"]
