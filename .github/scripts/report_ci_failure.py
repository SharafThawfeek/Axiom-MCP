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
import urllib.request

# Verified live: a real "no traceback parsed, work from raw log" analysis
# — the harder case, more agent iterations — took ~110s end-to-end on
# Groq's free tier, mostly rate-limit retry waits (see loop.py's
# MAX_INTERACTIVE_RATE_LIMIT_RETRIES). 90s cut that off before it finished;
# this leaves real margin above the observed worst case, not just past it.
TIMEOUT_SECONDS = 180
MAX_FILE_CONTEXT_CHARS = 20_000

# A full CI job log fetched via the Actions API is raw and noisy — checkout,
# dependency install, every passing test — none of it diagnostic. Verified
# live: a real 53KB job log measured at ~24,300 tokens (a ~2.2 chars/token
# density, denser than prose because of timestamps and short lines), which
# blew straight through Groq's free-tier 8000 TPM ceiling on a single
# request and made /analyze itself 400. The backend's own MAX_LOG_CHARS
# (60,000 chars) assumes a clean, already-relevant traceback, not a raw job
# log at this density — nowhere near a safe cap for this content type.
# Keeping the tail (like the backend's own truncation) rather than the head:
# the actual failure and its traceback are what's near the end of a job log,
# after all the setup noise, not before it.
MAX_LOG_CHARS_FOR_ANALYSIS = 12_000

# GitHub caps issue/PR comment bodies at 65,536 characters — a real,
# documented limit that's broken other tools in production (Renovate,
# docker/scout-action, among others), not a theoretical concern.
# suggested_patch has no length bound anywhere upstream (the model has been
# observed adding full docstrings to a "minimal" fix), so a verbose one plus
# the explanation could exceed it — and unlike every other failure mode in
# this pipeline, an oversized body fails the *final* `gh pr comment` step
# outright, with no fallback after it, defeating the whole "this should
# never be why CI shows red" principle. Real headroom below the hard limit,
# not cutting it close.
MAX_COMMENT_CHARS = 60_000

# Mirrors backend/app/parsers/python.py and javascript.py's FRAME/vendor
# markers — this script is intentionally standalone (no dependency on the
# backend package), so the minimal bit of parsing it actually needs is
# duplicated, not shared. Axiom Debug analyzes both Python and JS/TS
# failures (see the backend's language registry), so both trace shapes are
# recognised here too — otherwise a JS/TS caller would silently never get
# file_context, and suggested_patch would never fire for them.
_PY_FRAME = re.compile(r'File "(?P<file>.+?)", line (?P<line>\d+)')
_PY_VENDOR_MARKERS = ("site-packages", "dist-packages", "/usr/lib/python", "\\lib\\python", "<frozen ")

# V8/Node: "at fn (file:line:col)" or "at file:line:col" — frames run
# innermost-first (opposite of Python), so the first non-vendor match is
# the one that matters, not the last.
_JS_FRAME = re.compile(r"^\s*at\s+(?:.+?\s+\()?(?P<file>[^()\s][^()]*?):\d+:\d+\)?\s*$", re.MULTILINE)
_JS_VENDOR_MARKERS = ("node_modules",)
_JS_VENDOR_PREFIXES = ("node:", "internal/")


def _is_js_vendor(path: str) -> bool:
    normalised = path.replace("\\", "/")
    if normalised.startswith(_JS_VENDOR_PREFIXES):
        return True
    return any(marker in normalised for marker in _JS_VENDOR_MARKERS)


def truncate_log_for_analysis(log_text: str) -> str:
    """Shared between this module's main() and apply_fix.py's — both send a
    freshly-fetched job log to /analyze and both need the same cap."""
    if len(log_text) <= MAX_LOG_CHARS_FOR_ANALYSIS:
        return log_text
    return "[log truncated — showing the final portion]\n" + log_text[-MAX_LOG_CHARS_FOR_ANALYSIS:]


def find_implicated_file(log_text: str) -> str | None:
    """The file path most likely to be the caller's own code, whichever
    language's trace shape is present — same "deepest frame that's actually
    the caller's" heuristic the backend's parsers use, reimplemented here
    since this script can't import that package.

    Tries Python's shape first (frames run outermost -> innermost, so the
    LAST non-vendor match wins); a Python-shaped log is unambiguous — a
    JS/TS log never contains a `File "...", line N` frame — so falling
    through to the JS shape only happens for an actual JS/TS trace.
    """
    py_candidates = [
        m.group("file") for m in _PY_FRAME.finditer(log_text)
        if not any(marker in m.group("file") for marker in _PY_VENDOR_MARKERS)
    ]
    if py_candidates:
        return py_candidates[-1]

    js_candidates = [
        m.group("file") for m in _JS_FRAME.finditer(log_text)
        if not _is_js_vendor(m.group("file"))
    ]
    return js_candidates[0] if js_candidates else None


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
        prefix = "\n\n<details><summary>📎 Suggested fix (review before applying)</summary>\n\n```diff\n"
        suffix = "\n```\n\n</details>"
        budget = MAX_COMMENT_CHARS - len(comment) - len(prefix) - len(suffix)
        if len(patch) > budget:
            note = "\n... (truncated — patch too large to include in full here)"
            patch = patch[:max(budget - len(note), 0)] + note
        comment += prefix + patch + suffix

    if len(comment) > MAX_COMMENT_CHARS:  # final safety net either way
        comment = comment[:MAX_COMMENT_CHARS - 20] + "\n\n*(truncated)*"

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
        log_text = truncate_log_for_analysis(f.read())

    implicated = find_implicated_file(log_text)
    file_context = resolve_file_context(implicated, os.getcwd()) if implicated else None

    try:
        result = analyze(api_url, log_text, file_context)
        analysis = result["analysis"]
    except Exception as exc:
        # Broad on purpose: network errors, a malformed (non-JSON) response
        # body, or an unexpected response shape missing "analysis" should
        # all degrade the same way — this reporting step is a value-add on
        # top of CI, never a reason for CI itself to show red.
        print(f"Axiom Debug call failed ({exc}); skipping comment.", file=sys.stderr)
        return 0

    # An unset repo variable arrives as an empty string, not a missing key —
    # `or` catches that case too, not just true absence (see apply_fix.py's
    # TEST_COMMAND for why this distinction matters).
    mode = (os.environ.get("AXIOM_MODE") or "manual").strip().lower()
    comment = format_comment(analysis, show_patch=(mode == "review"))
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(comment)

    print(f"Wrote comment to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
