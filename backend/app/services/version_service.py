"""Checks an implicated library's installed version against its registry's
latest release — the "you're on v1.5.3, this was removed in 2.0" step.

Per-ecosystem: PyPI for Python, the npm registry for JavaScript. The lookup
shape is identical, so only the URL and the response field differ.
"""

import json
import re

import httpx

from app.core.logger import logger
from app.schemas.analysis import VersionVerdict

REGISTRY_NAMES = {"python": "PyPI", "javascript": "npm"}

PYPI_URL = "https://pypi.org/pypi/{package}/json"
NPM_URL = "https://registry.npmjs.org/{package}/latest"

# "pandas==2.1.0" / "pandas>=2.1.0" / "pandas 2.1.0" from a pip-freeze or
# requirements.txt paste. Case-insensitive; PyPI names are too.
_PIP_PIN = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*(?:==|>=|~=|\s)\s*([0-9][0-9A-Za-z.\-]*)",
    re.MULTILINE,
)

# `npm ls`-style or package.json-style lines, e.g. `"express": "^4.18.2"`
# or `express@4.18.2`. The leading ^ / ~ / >= is stripped — it's a range
# operator, not part of the version.
_NPM_PIN = re.compile(
    r'"?(?P<name>@?[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)?)"?\s*[:@]\s*'
    r'"?[\^~>=<\s]*(?P<version>[0-9][0-9A-Za-z.\-]*)"?',
)


def _installed_from_npm(dependencies_text: str, package: str) -> str | None:
    """package.json is JSON, so parse it as JSON when it is one — a regex
    over nested JSON picks up matches from unrelated sections (scripts,
    resolutions) that happen to look like a dependency line."""
    stripped = dependencies_text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            for field in ("dependencies", "devDependencies", "peerDependencies"):
                pinned = (data.get(field) or {}).get(package)
                if isinstance(pinned, str):
                    match = re.search(r"[0-9][0-9A-Za-z.\-]*", pinned)
                    if match:
                        return match.group(0)
            return None

    for match in _NPM_PIN.finditer(dependencies_text):
        if match.group("name") == package:
            return match.group("version")
    return None


class VersionService:

    @staticmethod
    def installed_version(
        dependencies_text: str, package: str, language: str = "python"
    ) -> str | None:
        """Pull `package`'s pinned version out of whatever the caller pasted."""
        if language == "javascript":
            return _installed_from_npm(dependencies_text, package)

        for match in _PIP_PIN.finditer(dependencies_text):
            if match.group(1).lower() == package.lower():
                return match.group(2)
        return None

    @staticmethod
    async def latest_version(package: str, language: str = "python") -> str | None:
        if language == "javascript":
            url = NPM_URL.format(package=package)
            extract = lambda payload: payload.get("version")  # noqa: E731
        else:
            url = PYPI_URL.format(package=package)
            extract = lambda payload: payload.get("info", {}).get("version")  # noqa: E731

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(
                    "%s lookup failed for %s: %s",
                    REGISTRY_NAMES.get(language, "Registry"), package, exc,
                )
                return None

        return extract(response.json())

    @staticmethod
    async def verdict(
        package: str,
        installed_version: str | None,
        language: str = "python",
    ) -> VersionVerdict:
        registry = REGISTRY_NAMES.get(language, "the registry")

        if installed_version is None:
            return VersionVerdict(
                package=package,
                installed_version=None,
                verdict="Installed version could not be determined.",
            )

        latest = await VersionService.latest_version(package, language)
        if latest is None:
            return VersionVerdict(
                package=package,
                installed_version=installed_version,
                verdict=f"Could not reach {registry} to check {package}.",
            )

        if installed_version == latest:
            verdict = f"{package} {installed_version} is the latest release."
        else:
            verdict = f"{package} {installed_version} is behind the latest ({latest})."

        return VersionVerdict(
            package=package,
            installed_version=installed_version,
            verdict=verdict,
        )
