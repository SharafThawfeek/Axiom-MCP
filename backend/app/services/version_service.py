"""Checks whether an implicated library's installed version matches what's
in requirements.txt / a lock file, and asks PyPI when a given version was
released — the "you're on v2.3.1, this bug was introduced there" step.
"""

import re

import httpx

from app.core.logger import logger
from app.schemas.analysis import VersionVerdict

PYPI_URL = "https://pypi.org/pypi/{package}/json"

# Matches "pandas==2.1.0" / "pandas>=2.1.0" / "pandas 2.1.0" from a
# pip-freeze or requirements.txt paste. Case-insensitive; package names are
# case-insensitive on PyPI.
_PIN_PATTERN = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*(?:==|>=|~=|\s)\s*([0-9][0-9A-Za-z.\-]*)",
    re.MULTILINE,
)


class VersionService:

    @staticmethod
    def installed_version(dependencies_text: str, package: str) -> str | None:
        """Pull `package`'s pinned version out of a pasted requirements/freeze block."""
        for match in _PIN_PATTERN.finditer(dependencies_text):
            if match.group(1).lower() == package.lower():
                return match.group(2)
        return None

    @staticmethod
    async def latest_version(package: str) -> str | None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(PYPI_URL.format(package=package))
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("PyPI lookup failed for %s: %s", package, exc)
                return None

        return response.json().get("info", {}).get("version")

    @staticmethod
    async def verdict(
        package: str,
        installed_version: str | None,
    ) -> VersionVerdict:
        if installed_version is None:
            return VersionVerdict(
                package=package,
                installed_version=None,
                verdict="Installed version could not be determined.",
            )

        latest = await VersionService.latest_version(package)
        if latest is None:
            return VersionVerdict(
                package=package,
                installed_version=installed_version,
                verdict=f"Could not reach PyPI to check {package}.",
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
