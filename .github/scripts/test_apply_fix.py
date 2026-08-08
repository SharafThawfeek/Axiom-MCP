"""Tests run real git operations against a real temp repo — a patch-apply
script is exactly the kind of thing where "the logic looks right" isn't
good enough; git apply either actually works against a real diff or it
doesn't."""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import apply_fix

ANALYSIS_WITH_PATCH = {
    "summary": "DataFrame.append no longer exists.",
    "root_cause": "Removed in pandas 2.0.",
    "explanation": "The method was removed.",
    "confidence": "high",
    "next_steps": ["Use pd.concat instead."],
    "suggested_patch": None,  # filled in per-test with a real diff
}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


@pytest.fixture
def real_repo(tmp_path, monkeypatch):
    """A real git repo with one committed file, with cwd set to it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    target = repo / "utils.py"
    target.write_text("def process(frame):\n    return frame.append(row)\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")

    monkeypatch.chdir(repo)
    return repo


def _real_diff_for(repo, path: Path, new_content: str) -> str:
    """Generates an actual git-formatted patch by making the edit for real,
    diffing it, then reverting — so the tests apply a patch git itself
    produced, not a hand-typed guess at the format."""
    original = path.read_text()
    path.write_text(new_content)
    diff = _git(repo, "diff", "--", path.name).stdout
    path.write_text(original)
    _git(repo, "checkout", "--", path.name)
    return diff


# Captured live from two real gpt-oss-120b /analyze calls — despite an
# explicit unified-diff format example in the prompt, it defaulted to this
# OpenAI-style convention both times. Real evidence, not a synthetic guess
# at what the format might look like.
REAL_OPENAI_STYLE_PATCH_1 = (
    "*** Begin Patch\n"
    "*** Update File: /app/etl/transform.py\n"
    "@@\n"
    "-def run(frame, new_row):\n"
    "-    return frame.append(new_row, ignore_index=True)\n"
    "+def run(frame, new_row):\n"
    "+    new_row_df = pd.DataFrame([new_row])\n"
    "+    return pd.concat([frame, new_row_df], ignore_index=True)\n"
    "*** End Patch\n"
    "*** End Patch"
)

REAL_OPENAI_STYLE_PATCH_2 = (
    "*** Begin Patch\n"
    "*** Update File: /app/services/report_builder.py\n"
    "@@\n"
    "-def build_summary(frame, extra_row):\n"
    "-    total = frame.append(extra_row, ignore_index=True)\n"
    "-    return total\n"
    "+def build_summary(frame, extra_row):\n"
    "+    total = pd.concat([frame, extra_row], ignore_index=True)\n"
    "+    return total\n"
    "*** End Patch\n"
    "*** End Patch"
)


def test_parses_real_captured_openai_style_patch():
    hunks = apply_fix._parse_openai_style_patch(REAL_OPENAI_STYLE_PATCH_1)

    assert hunks == [(
        "/app/etl/transform.py",
        "def run(frame, new_row):\n    return frame.append(new_row, ignore_index=True)",
        (
            "def run(frame, new_row):\n    new_row_df = pd.DataFrame([new_row])\n    "
            "return pd.concat([frame, new_row_df], ignore_index=True)"
        ),
    )]


def test_real_unified_diff_is_not_mistaken_for_openai_style():
    real_diff = "--- a/utils.py\n+++ b/utils.py\n@@ -1 +1 @@\n-old\n+new\n"
    assert apply_fix._parse_openai_style_patch(real_diff) is None


def test_applies_real_captured_openai_style_patch_end_to_end(real_repo):
    target = real_repo / "app" / "etl" / "transform.py"
    target.parent.mkdir(parents=True)
    target.write_text("def run(frame, new_row):\n    return frame.append(new_row, ignore_index=True)\n")

    applied, _reason = apply_fix.try_apply_patch(REAL_OPENAI_STYLE_PATCH_1)

    assert applied is True
    assert "pd.concat" in target.read_text()
    assert "frame.append" not in target.read_text()


def test_second_real_captured_patch_also_applies(real_repo):
    target = real_repo / "app" / "services" / "report_builder.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def build_summary(frame, extra_row):\n"
        "    total = frame.append(extra_row, ignore_index=True)\n"
        "    return total\n"
    )

    applied, _reason = apply_fix.try_apply_patch(REAL_OPENAI_STYLE_PATCH_2)

    assert applied is True
    assert "pd.concat" in target.read_text()


def test_openai_style_patch_fails_closed_on_ambiguous_match(real_repo):
    # Two identical functions — the old_block matches both, so this must
    # refuse rather than guess which one was meant.
    target = real_repo / "app" / "etl" / "transform.py"
    target.parent.mkdir(parents=True)
    target.write_text(
        "def run(frame, new_row):\n    return frame.append(new_row, ignore_index=True)\n\n\n"
        "def run(frame, new_row):\n    return frame.append(new_row, ignore_index=True)\n"
    )

    applied, reason = apply_fix.try_apply_patch(REAL_OPENAI_STYLE_PATCH_1)

    assert applied is False
    assert "found 2" in reason
    assert "frame.append" in target.read_text()  # untouched


def test_openai_style_patch_fails_closed_when_file_missing(real_repo):
    applied, reason = apply_fix.try_apply_patch(REAL_OPENAI_STYLE_PATCH_1)

    assert applied is False
    assert "could not find" in reason


def test_try_apply_patch_succeeds_with_a_real_valid_patch(real_repo):
    target = real_repo / "utils.py"
    patch = _real_diff_for(real_repo, target, "def process(frame):\n    return pd.concat([frame, row])\n")

    applied, _reason = apply_fix.try_apply_patch(patch)

    assert applied is True
    assert "pd.concat" in target.read_text()


def test_try_apply_patch_fails_gracefully_on_garbage(real_repo):
    applied, reason = apply_fix.try_apply_patch("this is not a diff at all")

    assert applied is False
    assert reason


def test_try_apply_patch_leaves_no_partial_state_on_failure(real_repo):
    apply_fix.try_apply_patch("this is not a diff at all")

    status = _git(real_repo, "status", "--porcelain").stdout
    assert status.strip() == ""  # nothing touched, patch file cleaned up


def test_empty_string_test_command_env_falls_back_to_default(monkeypatch):
    # GitHub Actions sets an unset repo variable as "", not an absent key —
    # this is the actual shape apply_fix.py sees in CI, not just a unit-test
    # convenience. Reload the module with the env var explicitly present-but-
    # empty to prove the real startup path, not just the TEST_COMMAND
    # constant in isolation.
    monkeypatch.setenv("AXIOM_TEST_COMMAND", "")
    import importlib
    importlib.reload(apply_fix)
    try:
        assert apply_fix.TEST_COMMAND == "cd backend && pytest"
    finally:
        importlib.reload(apply_fix)  # restore normal state for later tests


def test_run_tests_reports_pass(monkeypatch):
    monkeypatch.setattr(apply_fix, "TEST_COMMAND", f'"{sys.executable}" -c "exit(0)"')
    ok, _ = apply_fix.run_tests()
    assert ok is True


def test_run_tests_reports_failure(monkeypatch):
    monkeypatch.setattr(apply_fix, "TEST_COMMAND", f'"{sys.executable}" -c "exit(1)"')
    ok, _ = apply_fix.run_tests()
    assert ok is False


def test_main_skips_cleanly_on_malformed_response(real_repo, monkeypatch, tmp_path):
    # Missing "analysis" key — automatic mode's whole point is never
    # touching git without a confirmed-good response to act on.
    monkeypatch.setenv("AXIOM_API_URL", "http://fake")
    monkeypatch.setattr(apply_fix, "analyze", lambda *a, **kw: {"unexpected": "shape"})

    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    monkeypatch.setattr(sys, "argv", ["apply_fix.py", str(log_file), str(output_file)])
    assert apply_fix.main() == 0
    assert not output_file.exists()

    status = _git(real_repo, "status", "--porcelain").stdout
    assert status.strip() == ""


def test_main_falls_back_when_no_patch_proposed(real_repo, monkeypatch, tmp_path):
    monkeypatch.setenv("AXIOM_API_URL", "http://fake")
    monkeypatch.setattr(apply_fix, "analyze", lambda *a, **kw: {"analysis": ANALYSIS_WITH_PATCH})

    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    monkeypatch.setattr(sys, "argv", ["apply_fix.py", str(log_file), str(output_file)])
    assert apply_fix.main() == 0

    comment = output_file.read_text(encoding="utf-8")
    assert "no suggested_patch" in comment
    assert "didn't complete" in comment


def test_main_restores_original_branch_when_tests_fail_after_apply(real_repo, monkeypatch, tmp_path):
    starting_sha = _git(real_repo, "rev-parse", "HEAD").stdout.strip()
    target = real_repo / "utils.py"
    patch = _real_diff_for(real_repo, target, "def process(frame):\n    return pd.concat([frame, row])\n")
    good_analysis = {**ANALYSIS_WITH_PATCH, "suggested_patch": patch}

    monkeypatch.setenv("AXIOM_API_URL", "http://fake")
    monkeypatch.setattr(apply_fix, "analyze", lambda *a, **kw: {"analysis": good_analysis})
    monkeypatch.setattr(apply_fix, "TEST_COMMAND", f'"{sys.executable}" -c "exit(1)"')  # tests fail

    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    monkeypatch.setattr(sys, "argv", ["apply_fix.py", str(log_file), str(output_file)])
    assert apply_fix.main() == 0

    assert _git(real_repo, "rev-parse", "HEAD").stdout.strip() == starting_sha
    assert _git(real_repo, "status", "--porcelain").stdout.strip() == ""
    assert "frame.append" in target.read_text()  # patch was reverted, not left applied


def test_main_restores_original_branch_when_push_fails(real_repo, monkeypatch, tmp_path):
    # No remote configured on this repo, so push_fix_branch's `git push
    # origin` fails — proves cleanup happens even after a branch was
    # created and committed to, not just on a same-branch failure.
    #
    # _reset_to checks out the starting *commit*, not a branch name — real
    # GitHub Actions checkouts for pull_request events are detached HEAD to
    # begin with (refs/pull/<PR>/merge, not a local branch), so restoring
    # the exact commit is the correct goal, not restoring a branch name
    # that may never have existed in the first place.
    starting_sha = _git(real_repo, "rev-parse", "HEAD").stdout.strip()
    target = real_repo / "utils.py"
    patch = _real_diff_for(real_repo, target, "def process(frame):\n    return pd.concat([frame, row])\n")
    good_analysis = {**ANALYSIS_WITH_PATCH, "suggested_patch": patch}

    monkeypatch.setenv("AXIOM_API_URL", "http://fake")
    monkeypatch.setattr(apply_fix, "analyze", lambda *a, **kw: {"analysis": good_analysis})
    monkeypatch.setattr(apply_fix, "TEST_COMMAND", f'"{sys.executable}" -c "exit(0)"')  # tests pass

    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    monkeypatch.setattr(sys, "argv", ["apply_fix.py", str(log_file), str(output_file)])
    assert apply_fix.main() == 0

    assert _git(real_repo, "rev-parse", "HEAD").stdout.strip() == starting_sha
    assert _git(real_repo, "status", "--porcelain").stdout.strip() == ""

    comment = output_file.read_text(encoding="utf-8")
    assert "didn't complete" in comment


def test_main_writes_a_comment_pointing_to_the_new_pr_on_success(real_repo, monkeypatch, tmp_path):
    target = real_repo / "utils.py"
    patch = _real_diff_for(real_repo, target, "def process(frame):\n    return pd.concat([frame, row])\n")
    good_analysis = {**ANALYSIS_WITH_PATCH, "suggested_patch": patch}

    monkeypatch.setenv("AXIOM_API_URL", "http://fake")
    monkeypatch.setattr(apply_fix, "analyze", lambda *a, **kw: {"analysis": good_analysis})
    monkeypatch.setattr(apply_fix, "TEST_COMMAND", f'"{sys.executable}" -c "exit(0)"')
    monkeypatch.setattr(apply_fix, "push_fix_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(apply_fix, "open_pr", lambda *a, **kw: (True, "https://github.com/x/y/pull/1"))

    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    monkeypatch.setattr(sys, "argv", ["apply_fix.py", str(log_file), str(output_file)])
    assert apply_fix.main() == 0

    comment = output_file.read_text(encoding="utf-8")
    assert "https://github.com/x/y/pull/1" in comment


def test_main_falls_back_when_patch_does_not_apply(real_repo, monkeypatch, tmp_path):
    bad_patch_analysis = {**ANALYSIS_WITH_PATCH, "suggested_patch": "not a real diff"}
    monkeypatch.setenv("AXIOM_API_URL", "http://fake")
    monkeypatch.setattr(apply_fix, "analyze", lambda *a, **kw: {"analysis": bad_patch_analysis})

    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    monkeypatch.setattr(sys, "argv", ["apply_fix.py", str(log_file), str(output_file)])
    assert apply_fix.main() == 0

    comment = output_file.read_text(encoding="utf-8")
    assert "did not apply cleanly" in comment or "didn't complete" in comment

    # And the repo is left clean — a failed automatic attempt must not leave
    # a half-applied patch sitting in the working tree.
    status = _git(real_repo, "status", "--porcelain").stdout
    assert status.strip() == ""
