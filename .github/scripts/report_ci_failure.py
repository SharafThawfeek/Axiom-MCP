"""Runs as a CI step, only on failure. Sends the failed job's log — and, if
findable, the real content of the file the traceback implicates — to Axiom
Debug's /analyze endpoint, then writes a markdown comment for the next step
to post on the PR.

This one script backs all three delivery modes (AXIOM_MODE):
  manual    — post the diagnosis only. Nothing else touches git. (default)
  review    — same, plus any suggested_patch shown as a reviewable diff.
  automatic — not this script; see apply_fix.py, which calls this module's
              functions directly rather than duplicating them.

Deliberately stdlib-only (no httpx/requests) — this runs in its own CI job,
not inside the backend's installed environment, so it has no dependencies to
install.

    python report_ci_failure.py <log_file> <output_file>
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 90
MAX_FILE_CONTEXT_CHARS = 20_000

# Mirrors backend/app/parsers/traceback.py's FRAME/VENDOR_MARKERS — this
# script is intentionally standalone (no dependency on the backend package),
# so the minimal bit of parsing it actually needs is duplicated, not shared.
_FRAME = re.compile(r'File "(?P<file>.+?)", line (?P<line>\d+)')
_VENDOR_MARKERS = ("site-packages", "dist-packages", "/usr/lib/python", "\\lib\\python", "<frozen ")


def find_implicated_file(log_text: str) -> str | None:
    """The last non-vendor file path mentioned in a traceback — same "deepest
    frame that's actually the caller's own code" heuristic the backend parser
    uses, reimplemented here since this script can't import that package."""
    candidates = [
        m.group("file") for m in _FRAME.finditer(log_text)
        if not any(marker in m.group("file") for marker in _VENDOR_MARKERS)
    ]
    return candidates[-1] if candidates else None


def resolve_file_context(path: str, repo_root: str) -> str | None:
    """Best-effort: CI traceback paths vary a lot by runner/language, so this
    tries the path as given, then as relative to the checkout root. Returns
    None rather than raising — no file_context just means no patch gets
    proposed, not a failed run."""
    candidates = [path, os.path.join(repo_root, path.lstrip("/\\"))]
    for candidate in candidates:
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8", errors="replace") as f:
                    return f.read()[:MAX_FILE_CONTEXT_CHARS]
            except OSError:
                return None
    return None


def format_comment(analysis: dict, show_patch: bool) -> str:
    next_steps = "\n".join(f"- {step}" for step in analysis["next_steps"])
    comment = (
        "### 🔍 Axiom Debug\n\n"
        f"**{analysis['summary']}**\n\n"
        f"**Root cause:** {analysis['root_cause']}\n\n"
        f"{analysis['explanation']}\n\n"
        f"**Next steps:**\n{next_steps}\n\n"
        f"*Confidence: {analysis['confidence']}*"
    )

    patch = analysis.get("suggested_patch")
    if show_patch and patch:
        comment += (
            "\n\n<details><summary>📎 Suggested fix (review before applying)</summary>\n\n"
            f"```diff\n{patch}\n```\n\n</details>"
        )

    return comment


def analyze(api_url: str, log_text: str, file_context: str | None) -> dict:
    payload = {"log": log_text}
    if file_context:
        payload["file_context"] = file_context

    request = urllib.request.Request(
        api_url.rstrip("/") + "/analyze",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.load(response)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: report_ci_failure.py <log_file> <output_file>", file=sys.stderr)
        return 1

    log_path, output_path = sys.argv[1], sys.argv[2]

    api_url = os.environ.get("AXIOM_API_URL", "").strip()
    if not api_url:
        print(
            "AXIOM_API_URL is not configured — skipping analysis. "
            "Set it as a repo variable to enable this step."
        )
        return 0

    with open(log_path, encoding="utf-8", errors="replace") as f:
        log_text = f.read()

    implicated = find_implicated_file(log_text)
    file_context = resolve_file_context(implicated, os.getcwd()) if implicated else None

    try:
        result = analyze(api_url, log_text, file_context)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"Axiom Debug was unreachable ({exc}); skipping comment.", file=sys.stderr)
        return 0

    # An unset repo variable arrives as an empty string, not a missing key —
    # `or` catches that case too, not just true absence (see apply_fix.py's
    # TEST_COMMAND for why this distinction matters).
    mode = (os.environ.get("AXIOM_MODE") or "manual").strip().lower()
    comment = format_comment(result["analysis"], show_patch=(mode == "review"))
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(comment)

    print(f"Wrote comment to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
