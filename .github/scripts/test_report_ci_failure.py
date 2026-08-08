import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import report_ci_failure
from report_ci_failure import find_implicated_file, format_comment, resolve_file_context

ANALYSIS = {
    "summary": "DataFrame.append no longer exists.",
    "root_cause": "Removed in pandas 2.0.",
    "explanation": "The method was removed, so the attribute lookup fails.",
    "confidence": "high",
    "next_steps": ["Use pd.concat instead.", "Pin pandas<2.0 if you can't migrate yet."],
    "suggested_patch": "--- a/utils.py\n+++ b/utils.py\n@@ -1 +1 @@\n-frame.append(row)\n+pd.concat([frame, row])\n",
}

LOG = '''Traceback (most recent call last):
  File "/home/runner/work/repo/repo/utils.py", line 17, in process
    return frame.append(row)
  File "/opt/venv/lib/python3.11/site-packages/pandas/core/generic.py", line 5989, in __getattr__
    return object.__getattribute__(self, name)
AttributeError: 'DataFrame' object has no attribute 'append'
'''


def test_format_comment_includes_every_field():
    comment = format_comment(ANALYSIS, show_patch=False)

    assert ANALYSIS["summary"] in comment
    assert ANALYSIS["root_cause"] in comment
    assert ANALYSIS["explanation"] in comment
    assert ANALYSIS["confidence"] in comment
    for step in ANALYSIS["next_steps"]:
        assert step in comment


def test_patch_hidden_in_manual_mode():
    comment = format_comment(ANALYSIS, show_patch=False)
    assert "Suggested fix" not in comment
    assert ANALYSIS["suggested_patch"] not in comment


def test_patch_shown_in_review_mode():
    comment = format_comment(ANALYSIS, show_patch=True)
    assert "Suggested fix" in comment
    assert ANALYSIS["suggested_patch"] in comment


def test_no_patch_shows_nothing_extra_even_in_review_mode():
    analysis = {**ANALYSIS, "suggested_patch": None}
    comment = format_comment(analysis, show_patch=True)
    assert "Suggested fix" not in comment


def test_oversized_patch_is_truncated_to_stay_under_github_limit():
    # GitHub's real hard limit is 65,536 characters — this is comfortably
    # over even that, to prove the truncation kicks in rather than just
    # coincidentally staying under budget.
    huge_patch = "+line\n" * 20_000  # ~120,000 chars
    analysis = {**ANALYSIS, "suggested_patch": huge_patch}

    comment = format_comment(analysis, show_patch=True)

    assert len(comment) <= 65_536
    assert len(comment) <= report_ci_failure.MAX_COMMENT_CHARS
    assert "truncated" in comment


def test_normal_sized_patch_is_not_truncated():
    comment = format_comment(ANALYSIS, show_patch=True)
    assert "truncated" not in comment
    assert ANALYSIS["suggested_patch"] in comment


def test_find_implicated_file_skips_vendor_frames():
    # The last frame is site-packages (pandas internals) — the caller's own
    # utils.py, one frame up, is the one that actually matters.
    assert find_implicated_file(LOG) == "/home/runner/work/repo/repo/utils.py"


def test_find_implicated_file_returns_none_for_no_traceback():
    assert find_implicated_file("just some build output, no traceback here") is None


def test_resolve_file_context_reads_a_real_file(tmp_path):
    target = tmp_path / "utils.py"
    target.write_text("def process(frame):\n    return frame.append(row)\n")

    content = resolve_file_context(str(target), str(tmp_path))

    assert "def process" in content


def test_resolve_file_context_none_for_missing_file(tmp_path):
    assert resolve_file_context("/does/not/exist.py", str(tmp_path)) is None


def test_missing_api_url_skips_cleanly(tmp_path):
    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "report_ci_failure.py"),
         str(log_file), str(output_file)],
        env={},  # no AXIOM_API_URL
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "not configured" in result.stdout
    assert not output_file.exists()


def test_unreachable_api_skips_without_crashing(tmp_path):
    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "report_ci_failure.py"),
         str(log_file), str(output_file)],
        env={"AXIOM_API_URL": "http://127.0.0.1:1"},  # nothing listens here
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert not output_file.exists()


def test_malformed_response_skips_without_crashing(monkeypatch, tmp_path):
    # Missing "analysis" key — a response shape mismatch, not a network
    # failure. This reporting step is a value-add on top of CI; it should
    # never be the reason a workflow step shows red with a raw traceback.
    monkeypatch.setenv("AXIOM_API_URL", "http://fake")
    monkeypatch.setattr(report_ci_failure, "analyze", lambda *a, **kw: {"unexpected": "shape"})

    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    monkeypatch.setattr(sys, "argv", ["report_ci_failure.py", str(log_file), str(output_file)])
    assert report_ci_failure.main() == 0
    assert not output_file.exists()


def test_non_json_response_skips_without_crashing(monkeypatch, tmp_path):
    def _raise_decode_error(*a, **kw):
        import json
        raise json.JSONDecodeError("bad json", "not json", 0)

    monkeypatch.setenv("AXIOM_API_URL", "http://fake")
    monkeypatch.setattr(report_ci_failure, "analyze", _raise_decode_error)

    log_file = tmp_path / "failure.log"
    log_file.write_text("Traceback...")
    output_file = tmp_path / "comment.md"

    monkeypatch.setattr(sys, "argv", ["report_ci_failure.py", str(log_file), str(output_file)])
    assert report_ci_failure.main() == 0
    assert not output_file.exists()
