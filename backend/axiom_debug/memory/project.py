"""Deriving the tenant boundary.

`project_id` decides whose failure memory a caller can read and write. It is
never accepted as a tool argument, and that is a security property rather
than a style preference: a tool parameter is chosen by the model, and the
model's context can be influenced by whatever it just read — a stack trace,
a dependency's README, a CI log. Letting that reach the tenant key is a
textbook confused-deputy hole, where a caller with legitimate access to one
project induces the server into reading another.

So it comes from somewhere the caller cannot address:

  - stdio (local)  — derived from the checkout the server was launched in.
  - HTTP (hosted)  — a claim on the verified access token, resolved before
                     any tool body runs. See axiom_debug/mcp/auth.py.
"""

import hashlib
import re
import subprocess
from pathlib import Path

# Trailing ".git", any "user@" or scheme prefix, and the scp-style colon all
# vary between the URLs git hands out for the same repository. Two developers
# who cloned the same project over SSH and HTTPS must land on one id.
_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_USERINFO = re.compile(r"^[^/@]+@")


def normalise_remote(url: str) -> str:
    """Reduce a git remote URL to a stable identity string.

    git@github.com:Foo/Bar.git and https://github.com/Foo/Bar.git both
    become github.com/foo/bar.
    """
    text = url.strip()
    text = _SCHEME.sub("", text)
    text = _USERINFO.sub("", text)
    # scp-style "host:path" — only the first colon, and only when it isn't a
    # port number, which a normalised https URL can still carry.
    host, sep, path = text.partition(":")
    if sep and not path[:1].isdigit():
        text = f"{host}/{path}"
    if text.endswith(".git"):
        text = text[:-4]
    return text.strip("/").lower()


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # git missing, or not executable. Not fatal — the caller falls back.
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def derive_local_project_id(start: Path | None = None) -> str:
    """Identify the checkout this server was launched in.

    Prefers the origin remote, so every clone of the same repository shares
    one memory. Falls back to the toplevel directory path for a repo with no
    remote, and finally to the working directory for a plain directory that
    isn't a git checkout at all — a local-only id is still a correct tenant
    boundary, it just doesn't travel between machines.
    """
    cwd = (start or Path.cwd()).resolve()

    remote = _git(["config", "--get", "remote.origin.url"], cwd)
    if remote:
        identity = normalise_remote(remote)
    else:
        toplevel = _git(["rev-parse", "--show-toplevel"], cwd)
        identity = (toplevel or str(cwd)).lower()

    # Hashed rather than stored raw: a remote URL can embed a token
    # (https://x-access-token:ghp_...@github.com/...), and this value is
    # logged, traced, and used as a database key.
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
