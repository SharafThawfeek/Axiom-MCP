"""Bearer-token authentication for the hosted HTTP surface.

Scope of what this is
---------------------
This is a resource server that verifies opaque bearer tokens against a
locally configured key set. It is not an OAuth 2.1 authorization server, and
it deliberately does not implement Dynamic Client Registration — which the
2026-07-28 spec deprecated anyway in favour of Client ID Metadata Documents.

That is a considered limit, not an oversight. The realistic deployment for
this product is a team self-hosting one instance and issuing keys to its own
developers; standing up an authorization server for that is infrastructure
nobody asked for. A public multi-tenant deployment would need the full
OAuth 2.1 flow, and `RemoteAuthProvider` is the seam where it would go.

What the tokens carry
---------------------
A key maps to exactly one `project_id` and a scope set. The project comes
from the key, never from the request — that is the whole point. A caller
holding team A's key cannot read team B's failure memory regardless of what
arguments the model chooses, because the tenant is resolved before any tool
body runs. See memory/project.py.

Storage
-------
Keys are compared by SHA-256 digest and never held in plaintext. Comparison
uses `hmac.compare_digest` so a wrong key takes the same time as a right
one — token verification is an unauthenticated endpoint by definition, so
it is exactly where a timing oracle would be probed.
"""

import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass

from fastmcp.server.auth import AccessToken, TokenVerifier

logger = logging.getLogger("axiom-debug")

READ_SCOPE = "memory:read"
WRITE_SCOPE = "memory:write"


@dataclass(frozen=True)
class ApiKeyRecord:
    """One issued key: which project it unlocks and what it may do."""

    digest: str
    project_id: str
    scopes: tuple[str, ...]
    label: str = ""


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def load_keys_from_env(var: str = "AXIOM_API_KEYS") -> list[ApiKeyRecord]:
    """Parse the configured key set.

    Expects JSON, because a delimiter-separated format falls apart the first
    time a project id or label contains the delimiter:

        [
          {"key": "<plaintext>", "project_id": "acme/app", "scopes": ["memory:read"]},
          {"key_sha256": "<digest>", "project_id": "acme/api"}
        ]

    `key_sha256` is preferred for real deployments — it means the deployment
    environment never holds a usable credential, only its digest. `key` is
    accepted for local testing and is hashed immediately on load.

    Scopes default to read-only. Granting write has to be deliberate: a
    compromised read key exposes history, a compromised write key lets an
    attacker plant a fabricated "resolution" that every future recall serves
    back as this team's verified fix.
    """
    raw = os.environ.get(var, "").strip()
    if not raw:
        return []

    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("%s is not valid JSON; no API keys loaded", var)
        return []

    if not isinstance(entries, list):
        logger.error("%s must be a JSON array; no API keys loaded", var)
        return []

    records: list[ApiKeyRecord] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        project_id = entry.get("project_id")
        if not project_id:
            logger.error("Skipping an API key entry with no project_id")
            continue

        digest = entry.get("key_sha256")
        if not digest:
            plaintext = entry.get("key")
            if not plaintext:
                logger.error("Skipping API key for %s: no key or key_sha256", project_id)
                continue
            digest = _digest(plaintext)

        scopes = entry.get("scopes") or [READ_SCOPE]
        records.append(
            ApiKeyRecord(
                digest=digest.lower(),
                project_id=str(project_id),
                scopes=tuple(scopes),
                label=str(entry.get("label", "")),
            )
        )

    return records


class ApiKeyVerifier(TokenVerifier):
    """Resolves a bearer token to a project and its scopes."""

    def __init__(self, keys: list[ApiKeyRecord], required_scopes: list[str] | None = None):
        super().__init__(required_scopes=required_scopes or [READ_SCOPE])
        self._keys = keys
        if not keys:
            # Loud, because the alternative is a server that rejects
            # everything and looks like a network problem.
            logger.warning(
                "Auth is enabled but no API keys are configured — every "
                "request will be rejected. Set AXIOM_API_KEYS."
            )

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = _digest(token)

        for record in self._keys:
            # compare_digest over every record rather than a dict lookup:
            # a short-circuiting comparison leaks which prefix was correct.
            if hmac.compare_digest(candidate, record.digest):
                return AccessToken(
                    token=token,
                    client_id=record.label or record.project_id,
                    scopes=list(record.scopes),
                    subject=record.project_id,
                    # `project_id` is read back out of here by the server to
                    # scope every query. It is authoritative precisely
                    # because it came from the verified key, not the request.
                    claims={"project_id": record.project_id},
                )

        logger.info("Rejected a bearer token that matched no configured key")
        return None


def project_id_from_token(access_token: AccessToken | None) -> str | None:
    """Extract the tenant from a verified token, if there is one."""
    if access_token is None:
        return None
    claims = access_token.claims or {}
    project = claims.get("project_id") or access_token.subject
    return str(project) if project else None
