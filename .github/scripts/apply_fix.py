"""Runs as a CI step, only on failure, when AXIOM_MODE=automatic. Applies
Axiom Debug's suggested_patch to a fresh branch, runs the test suite, and —
only if that's clean — pushes the branch and opens a PR. Never commits
directly to the branch that triggered it: "automatic" here means zero
manual effort up to a merge click, not an unsupervised write to a
protected branch. If the patch doesn't apply, or fails tests, or none was
proposed at all, this falls back to the same review-mode comment
report_ci_failure.py would have posted — the developer still gets the
diagnosis and (if one exists) the unapplied patch to look at by hand.

Reuses report_ci_failure's log-parsing and API-calling functions rather
than duplicating them — this script IS the "automatic" mode; that module
covers "manual" and "review".

    python apply_fix.py <log_file> <output_file>
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from report_ci_failure import (
    analyze,
    find_implicated_file,
    format_comment,
    resolve_file_context,
    truncate_log_for_analysis,
)

# GitHub Actions sets an unset `vars.X` as an empty string, not an absent
# key — dict.get()'s default only applies to a truly missing key, so an
# unconfigured AXIOM_TEST_COMMAND would silently become "", and
# `subprocess.run("", shell=True)` succeeds as a no-op. That would make
# Automatic mode treat "ran nothing" as "tests passed" and push an
# unverified patch — `or` catches the falsy-empty case too, not just absence.
TEST_COMMAND = os.environ.get("AXIOM_TEST_COMMAND") or "cd backend && pytest"


def _run(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True)


def _reset_to(starting_ref: str) -> None:
    """Restores the working tree to exactly where it started, regardless of
    how far the attempt got. A plain `checkout -- .` only discards unstaged
    changes — if push_fix_branch got as far as creating a new branch and
    committing to it before failing (push rejected, gh pr create failed),
    that leaves the runner sitting on the new branch with a local, unpushed
    commit, not back on the ref that triggered this run."""
    _git("checkout", starting_ref)
    _git("reset", "--hard", starting_ref)
    _git("clean", "-fd")


def _parse_openai_style_patch(raw: str) -> list[tuple[str, str, str]] | None:
    """gpt-oss defaults to an OpenAI-style "*** Begin Patch" convention
    despite being explicitly asked for a unified diff, even with a literal
    format example in the prompt (observed live, twice — the same "prompting
    alone isn't enough" lesson as the citation-omission fix). Rather than
    keep fighting the prompt, this recognises that shape and converts it.

    Returns a list of (file_path, old_block, new_block), one per hunk, or
    None if `raw` doesn't look like this format at all (in which case the
    caller tries it as a real unified diff instead).
    """
    if "*** Begin Patch" not in raw and "*** Update File:" not in raw:
        return None

    hunks: list[tuple[str, str, str]] = []
    current_path: str | None = None
    old_lines: list[str] = []
    new_lines: list[str] = []

    def flush():
        if current_path and (old_lines or new_lines):
            hunks.append((current_path, "\n".join(old_lines), "\n".join(new_lines)))

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("*** Update File:"):
            flush()
            current_path = stripped.split(":", 1)[1].strip()
            old_lines, new_lines = [], []
        elif stripped.startswith(("*** ", "@@")):
            continue  # Begin/End Patch markers, hunk separators
        elif line.startswith("-") and not line.startswith("---"):
            old_lines.append(line[1:])
        elif line.startswith("+") and not line.startswith("+++"):
            new_lines.append(line[1:])
        elif line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])

    flush()
    return hunks or None


def _resolve_path(path: str) -> Path | None:
    for candidate in (Path(path), Path(path.lstrip("/\\"))):
        if candidate.is_file():
            return candidate
    return None


def _apply_text_substitution_hunks(hunks: list[tuple[str, str, str]]) -> tuple[bool, str]:
    """Applies each (file, old, new) hunk via exact text substitution rather
    than fabricated unified-diff line numbers — this format doesn't carry
    real line numbers, and guessing them is how a patch silently lands in
    the wrong place. Requires an EXACT, UNAMBIGUOUS match for every hunk,
    validated before anything is written — a false negative (a correct fix
    that fails to apply over whitespace drift) is safe; a false positive
    (applied to the wrong spot) is not, so ambiguity always fails closed."""
    resolved_hunks = []
    for file_path, old_block, new_block in hunks:
        target = _resolve_path(file_path)
        if target is None:
            return False, f"could not find {file_path} in the checkout"

        content = target.read_text(encoding="utf-8")
        count = content.count(old_block)
        if count != 1:
            return False, (
                f"{file_path}: expected exactly one match for the patch's "
                f"old content, found {count}"
            )
        resolved_hunks.append((target, content, old_block, new_block))

    for target, content, old_block, new_block in resolved_hunks:
        target.write_text(content.replace(old_block, new_block), encoding="utf-8")

    return True, ""


def try_apply_patch(patch: str) -> tuple[bool, str]:
    """Returns (applied, reason). Tries the OpenAI-style convention first
    (see _parse_openai_style_patch); if the patch doesn't match that shape,
    falls back to treating it as a real unified diff via git apply. Never
    leaves a partial change in the working tree on failure either way."""
    openai_style_hunks = _parse_openai_style_patch(patch)
    if openai_style_hunks is not None:
        return _apply_text_substitution_hunks(openai_style_hunks)

    patch_file = Path("axiom-suggested.patch")
    patch_file.write_text(patch, encoding="utf-8")

    check = _git("apply", "--check", str(patch_file))
    if check.returncode != 0:
        patch_file.unlink(missing_ok=True)
        return False, f"patch did not apply cleanly:\n{check.stderr}"

    apply = _git("apply", str(patch_file))
    patch_file.unlink(missing_ok=True)
    if apply.returncode != 0:
        return False, f"git apply failed:\n{apply.stderr}"

    return True, ""


def run_tests() -> tuple[bool, str]:
    result = _run(TEST_COMMAND)
    return result.returncode == 0, (result.stdout + result.stderr)[-4000:]


def push_fix_branch(branch: str, commit_message: str) -> tuple[bool, str]:
    for cmd in (
        ["checkout", "-b", branch],
        ["add", "-A"],
        ["commit", "-m", commit_message],
    ):
        result = _git(*cmd)
        if result.returncode != 0:
            return False, f"git {' '.join(cmd)} failed:\n{result.stderr}"

    push = _git("push", "origin", branch)
    if push.returncode != 0:
        return False, f"git push failed:\n{push.stderr}"

    return True, ""


def open_pr(branch: str, base: str, title: str, body: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["gh", "pr", "create", "--head", branch, "--base", base,
         "--title", title, "--body-file", "-"],
        input=body, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, f"gh pr create failed:\n{result.stderr}"
    return True, result.stdout.strip()


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: apply_fix.py <log_file> <output_file>", file=sys.stderr)
        return 1

    log_path, output_path = sys.argv[1], sys.argv[2]

    api_url = os.environ.get("AXIOM_API_URL", "").strip()
    if not api_url:
        print("AXIOM_API_URL is not configured — skipping analysis.")
        return 0

    with open(log_path, encoding="utf-8", errors="replace") as f:
        log_text = truncate_log_for_analysis(f.read())

    implicated = find_implicated_file(log_text)
    file_context = resolve_file_context(implicated, os.getcwd()) if implicated else None

    try:
        result = analyze(api_url, log_text, file_context)
        analysis = result["analysis"]
    except Exception as exc:
        # Broad on purpose: network errors, a malformed response body, or an
        # unexpected shape missing "analysis" all degrade the same way —
        # automatic mode's whole point is not to touch git on anything but
        # a confirmed-good patch, and that starts with a confirmed-good
        # response in the first place.
        print(f"Axiom Debug call failed ({exc}); skipping.", file=sys.stderr)
        return 0
    patch = analysis.get("suggested_patch")
    run_id = os.environ.get("GITHUB_RUN_ID", "local")
    base_branch = os.environ.get("GITHUB_BASE_REF") or "main"
    starting_ref = _git("rev-parse", "HEAD").stdout.strip()

    fallback_reason = None
    if not patch:
        fallback_reason = "no suggested_patch was proposed for this failure"
    else:
        applied, reason = try_apply_patch(patch)
        if not applied:
            fallback_reason = reason
        else:
            tests_ok, test_output = run_tests()
            if not tests_ok:
                _reset_to(starting_ref)
                fallback_reason = f"applied patch failed the test suite:\n{test_output}"
            else:
                branch = f"axiom-fix/{run_id}"
                pushed, reason = push_fix_branch(
                    branch,
                    f"Axiom Debug: {analysis['summary']}\n\n{analysis['root_cause']}",
                )
                if not pushed:
                    _reset_to(starting_ref)
                    fallback_reason = reason
                else:
                    ok, info = open_pr(
                        branch, base_branch,
                        f"Axiom Debug: {analysis['summary']}",
                        format_comment(analysis, show_patch=True),
                    )
                    if not ok:
                        fallback_reason = info
                    else:
                        print(f"Opened PR: {info}")
                        with open(output_path, "w", encoding="utf-8") as f:
                            f.write(
                                f"### 🔍 Axiom Debug\n\n"
                                f"Applied a fix and opened a PR: {info}\n\n"
                                f"**{analysis['summary']}** — tests passed on the new branch."
                            )
                        return 0

    # Any fallback path lands here: same review-mode comment a human would
    # get from report_ci_failure.py, plus why automatic mode didn't finish.
    comment = format_comment(analysis, show_patch=True)
    if fallback_reason:
        comment += (
            f"\n\n*Automatic mode didn't complete ({fallback_reason.splitlines()[0]}) "
            "— review above and apply manually if it looks right.*"
        )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(comment)

    print(f"Wrote fallback comment to {output_path}: {fallback_reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
