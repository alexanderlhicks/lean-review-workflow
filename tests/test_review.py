"""Unit tests for review.py core functions."""

import pytest
import sys
import os
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from review import (
    split_diff_into_files,
    _extract_added_lines,
    _fetch_url_content,
    _is_in_string,
    _normalize_external_url,
    run_mechanical_prechecks,
    _get_diff_lines,
    _load_prompt,
    _fit_replacements_to_budget,
    _format_repo_files,
    _validate_url,
    _check_ip_safe,
    _resolve_and_validate,
)
from lean_utils import is_in_comment
from llm_provider import (
    ContentPart, TokenUsage, _is_retryable_generic, _is_rate_limit_generic,
    create_provider, extract_pdf_text,
)


# --- split_diff_into_files ---

class TestSplitDiffIntoFiles:
    def test_basic_split(self):
        diff = """diff --git a/Foo.lean b/Foo.lean
--- a/Foo.lean
+++ b/Foo.lean
@@ -1,3 +1,4 @@
 import Bar
+import Baz
 def foo := 1
diff --git a/Bar.lean b/Bar.lean
--- a/Bar.lean
+++ b/Bar.lean
@@ -1,2 +1,2 @@
-def bar := 1
+def bar := 2
"""
        result = split_diff_into_files(diff)
        assert "Foo.lean" in result
        assert "Bar.lean" in result
        assert len(result) == 2

    def test_empty_diff(self):
        assert split_diff_into_files("") == {}

    def test_rename(self):
        diff = """diff --git a/Old.lean b/New.lean
similarity index 90%
rename from Old.lean
rename to New.lean
--- a/Old.lean
+++ b/New.lean
@@ -1 +1 @@
-old content
+new content
"""
        result = split_diff_into_files(diff)
        assert "New.lean" in result
        assert "Old.lean" not in result

    def test_non_lean_files_included(self):
        diff = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-old
+new
"""
        result = split_diff_into_files(diff)
        assert "README.md" in result


# --- _extract_added_lines ---

class TestExtractAddedLines:
    def test_basic(self):
        diff = """+++ b/Foo.lean
@@ -1,3 +1,4 @@
 import Bar
+import Baz
+import Qux
 def foo := 1
"""
        added = _extract_added_lines(diff)
        assert "import Baz" in added
        assert "import Qux" in added
        assert "import Bar" not in added

    def test_ignores_diff_header(self):
        diff = """+++ b/Foo.lean
@@ -1 +1 @@
+new line
"""
        added = _extract_added_lines(diff)
        assert added == ["new line"]
        # +++ line should not appear
        assert "++ b/Foo.lean" not in added


# --- is_in_comment (now from lean_utils) ---

class TestIsInComment:
    """Tests for lean_utils.is_in_comment with nested block comment support."""

    def test_single_line_comment(self):
        is_comment, depth = is_in_comment("  -- this is a comment", 0)
        assert is_comment is True
        assert depth == 0

    def test_not_comment(self):
        is_comment, depth = is_in_comment("def foo := 1", 0)
        assert is_comment is False
        assert depth == 0

    def test_block_comment_start(self):
        is_comment, depth = is_in_comment("/- start of block", 0)
        assert is_comment is True
        assert depth == 1

    def test_inside_block_comment(self):
        is_comment, depth = is_in_comment("  still in block", 1)
        assert is_comment is True
        assert depth == 1

    def test_block_comment_end(self):
        is_comment, depth = is_in_comment("  end of block -/", 1)
        assert is_comment is True
        assert depth == 0

    def test_single_line_block_comment(self):
        is_comment, depth = is_in_comment("/- single line -/", 0)
        assert is_comment is True
        assert depth == 0

    def test_nested_comment_preserves_outer(self):
        """Closing inner /- -/ should NOT close the outer block."""
        # depth=2 means we're inside /- /- ... here
        is_comment, depth = is_in_comment("  inner close -/", 2)
        assert is_comment is True
        assert depth == 1  # still inside the outer comment


# --- _is_in_string ---

class TestIsInString:
    def test_keyword_in_string(self):
        assert _is_in_string("sorry", 'let msg := "sorry about that"') is True

    def test_keyword_outside_string(self):
        assert _is_in_string("sorry", "  sorry") is False

    def test_keyword_both(self):
        # "sorry" appears both in a string and outside — should return False
        assert _is_in_string("sorry", 'let x := "sorry"; sorry') is False

    def test_no_strings(self):
        assert _is_in_string("axiom", "axiom myAxiom : True") is False


# --- run_mechanical_prechecks ---

class TestMechanicalPrechecks:
    def test_no_findings(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("def foo := 1\n")
        diff = "+def foo := 1\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        assert "No escape hatches" in result

    def test_sorry_in_diff(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        diff = "+theorem foo : True := sorry\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        assert "sorry" in result
        assert "introduced" in result.lower()

    def test_sorry_in_comment_ignored(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("-- sorry this is a comment\ndef foo := 1\n")
        diff = "+-- sorry this is a comment\n+def foo := 1\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        # sorry in comment should not be flagged as introduced
        assert "**`sorry`** introduced" not in result

    def test_non_lean_file_skipped(self, tmp_path):
        md_file = tmp_path / "README.md"
        md_file.write_text("sorry\n")
        result = run_mechanical_prechecks({str(md_file): "+sorry\n"})
        assert "No escape hatches" in result

    def test_large_file_warning(self, tmp_path):
        lean_file = tmp_path / "Big.lean"
        lean_file.write_text("def x := 1\n" * 2000)
        diff = "+def x := 1\n"
        result = run_mechanical_prechecks({str(lean_file): diff})
        assert "Large file" in result


# --- _get_diff_lines ---

class TestGetDiffLines:
    def test_basic_lines(self):
        diff = """@@ -1,3 +1,4 @@
 context line 1
+added line
 context line 2
 context line 3
"""
        lines = _get_diff_lines(diff)
        assert 1 in lines   # context
        assert 2 in lines   # added
        assert 3 in lines   # context
        assert 4 in lines   # context

    def test_empty_diff(self):
        assert _get_diff_lines("") == set()

    def test_deleted_lines_not_included(self):
        diff = """@@ -1,3 +1,2 @@
 context
-deleted line
 remaining
"""
        lines = _get_diff_lines(diff)
        assert 1 in lines
        assert 2 in lines
        # Only 2 lines in new file, no line 3


# --- _load_prompt ---

class TestLoadPrompt:
    def test_basic_replacement(self, tmp_path):
        """Test that _load_prompt correctly substitutes placeholders."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        prompt_file = prompts_dir / "test_prompt.md"
        prompt_file.write_text("Hello {{NAME}}, your role is {{ROLE}}.")

        import review
        original_path = review.ACTION_PATH
        try:
            review.ACTION_PATH = str(tmp_path)
            result = _load_prompt("test_prompt.md", {"NAME": "Alice", "ROLE": "reviewer"})
            assert result == "Hello Alice, your role is reviewer."
        finally:
            review.ACTION_PATH = original_path


# --- URL Validation ---

class TestValidateUrl:
    def test_valid_https(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
        )
        is_safe, _ = _validate_url("https://arxiv.org/pdf/2301.12345.pdf")
        assert is_safe is True

    def test_valid_http(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
        )
        is_safe, _ = _validate_url("http://example.com/file.pdf")
        assert is_safe is True

    def test_blocked_localhost(self):
        is_safe, reason = _validate_url("http://localhost:8080/secret")
        assert is_safe is False
        assert "localhost" in reason.lower()

    def test_blocked_private_ip(self):
        is_safe, reason = _validate_url("http://192.168.1.1/admin")
        assert is_safe is False
        assert "private" in reason.lower() or "reserved" in reason.lower()

    def test_blocked_loopback(self):
        is_safe, reason = _validate_url("http://127.0.0.1/metadata")
        assert is_safe is False

    def test_blocked_non_http_scheme(self):
        is_safe, reason = _validate_url("file:///etc/passwd")
        assert is_safe is False
        assert "scheme" in reason.lower()

    def test_blocked_metadata_endpoint(self):
        is_safe, reason = _validate_url("http://metadata.google.internal/v1/instance")
        assert is_safe is False

    def test_no_hostname(self):
        is_safe, reason = _validate_url("http://")
        assert is_safe is False

    def test_blocked_private_ip_via_dns_resolution(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("169.254.169.254", 0))]
        )
        is_safe, reason, _ = _resolve_and_validate("https://example.com/spec.pdf")
        assert is_safe is False
        assert "private" in reason.lower() or "cloud metadata" in reason.lower()


class TestCheckIpSafe:
    def test_public_ip(self):
        is_safe, _ = _check_ip_safe("93.184.216.34")
        assert is_safe is True

    def test_private_ip(self):
        is_safe, reason = _check_ip_safe("192.168.1.1")
        assert is_safe is False
        assert "private" in reason.lower()

    def test_aws_metadata_ip(self):
        is_safe, reason = _check_ip_safe("169.254.169.254")
        assert is_safe is False

    def test_azure_metadata_ip(self):
        is_safe, reason = _check_ip_safe("168.63.129.16")
        assert is_safe is False
        assert "cloud metadata" in reason.lower()

    def test_alibaba_metadata_ip(self):
        is_safe, reason = _check_ip_safe("100.100.100.200")
        assert is_safe is False
        assert "cloud metadata" in reason.lower()

    def test_loopback(self):
        is_safe, reason = _check_ip_safe("127.0.0.1")
        assert is_safe is False


class TestResolveAndValidate:
    def test_safe_url(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
        )
        is_safe, reason, ips = _resolve_and_validate("https://example.com/file.pdf")
        assert is_safe is True
        assert "93.184.216.34" in ips

    def test_dns_resolves_to_private(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("10.0.0.1", 0))]
        )
        is_safe, reason, _ = _resolve_and_validate("https://example.com/file.pdf")
        assert is_safe is False

    def test_ip_url_skips_dns(self):
        is_safe, reason, ips = _resolve_and_validate("https://93.184.216.34/file.pdf")
        assert is_safe is True
        assert "93.184.216.34" in ips


class TestExternalFetch:
    def test_normalize_github_blob_url(self):
        result = _normalize_external_url("https://github.com/org/repo/blob/main/Foo.lean")
        assert result == "https://raw.githubusercontent.com/org/repo/main/Foo.lean"

    def test_redirect_revalidated_before_following(self, monkeypatch):
        monkeypatch.setattr(
            "review.socket.getaddrinfo",
            lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
            if host == "example.com"
            else [(None, None, None, None, ("169.254.169.254", 0))]
        )

        class FakeSession:
            def get(self, url, timeout, headers, allow_redirects):
                if url == "https://example.com/start":
                    return SimpleNamespace(
                        status_code=302,
                        headers={"Location": "http://metadata.google.internal/secret"},
                        raise_for_status=lambda: None,
                    )
                raise AssertionError(f"Unexpected fetch: {url}")

        monkeypatch.setattr("review.requests.Session", lambda: FakeSession())

        with pytest.raises(ValueError, match="Blocked unsafe URL"):
            _fetch_url_content("https://example.com/start")


# --- Retry Logic ---

class TestRetryLogic:
    def test_rate_limit_is_retryable(self):
        error = Exception("429 Resource has been exhausted")
        assert _is_retryable_generic(error) is True
        assert _is_rate_limit_generic(error) is True

    def test_server_error_is_retryable(self):
        error = Exception("500 Internal Server Error")
        assert _is_retryable_generic(error) is True
        assert _is_rate_limit_generic(error) is False

    def test_auth_error_not_retryable(self):
        error = Exception("403 Permission denied")
        assert _is_retryable_generic(error) is False

    def test_invalid_request_not_retryable(self):
        error = Exception("400 Invalid request")
        assert _is_retryable_generic(error) is False


# --- REPO_CONTEXT rendering and exclusion ---

class TestFormatRepoFiles:
    def test_empty_dict_returns_placeholder(self):
        out = _format_repo_files({})
        assert "No repository context files" in out

    def test_renders_all_files_without_exclude(self):
        files = {"A.lean": "aaa", "B.lean": "bbb"}
        out = _format_repo_files(files)
        assert "content from A.lean" in out
        assert "content from B.lean" in out
        assert "aaa" in out and "bbb" in out

    def test_excludes_named_files(self):
        files = {"A.lean": "aaa", "B.lean": "bbb", "C.lean": "ccc"}
        out = _format_repo_files(files, exclude={"A.lean", "C.lean"})
        assert "B.lean" in out
        assert "A.lean" not in out
        assert "C.lean" not in out
        assert "bbb" in out
        assert "aaa" not in out

    def test_exclude_everything_returns_placeholder(self):
        files = {"A.lean": "aaa"}
        out = _format_repo_files(files, exclude={"A.lean"})
        # Sentinel so the model knows context is intentionally empty, not missing.
        assert "No repository context files" in out or "after excluding" in out

    def test_changed_files_siblings_excluded(self):
        """The core Change-1 invariant: when reviewing one changed file, the
        other changed files are not duplicated into REPO_CONTEXT (they are
        reviewed on their own per-file pass)."""
        files = {f"Compose/F{i}.lean": f"body-{i}" for i in range(5)}
        changed = set(files.keys())
        target = "Compose/F2.lean"
        out = _format_repo_files(files, exclude=changed)
        for path in files:
            assert path not in out
        # Size of rendered context collapses to the placeholder when all
        # discovered files are also changed.
        assert len(out) < 200


# --- Prompt-size budget ---

class TestFitPromptToBudget:
    TEMPLATE = (
        "HEADER\n"
        "File: {{FILE_PATH}}\n"
        "Diff:\n{{FILE_DIFF}}\n"
        "Content:\n{{FULL_CONTENT}}\n"
        "Repo:\n{{REPO_CONTEXT}}\n"
        "FOOTER\n"
    )

    def _base(self, repo_chars=0, content_chars=100):
        return {
            "FILE_PATH": "Foo.lean",
            "FILE_DIFF": "a" * 50,
            "FULL_CONTENT": "f" * content_chars,
            "REPO_CONTEXT": "r" * repo_chars,
        }

    def test_returns_unchanged_when_under_budget(self):
        reps = self._base(repo_chars=100)
        out = _fit_replacements_to_budget(self.TEMPLATE, reps, max_chars=10_000)
        assert out == reps  # identical dict

    def test_truncates_repo_context_when_over_budget(self):
        reps = self._base(repo_chars=5_000)
        out = _fit_replacements_to_budget(self.TEMPLATE, reps, max_chars=3_000)
        # REPO_CONTEXT is trimmed; other fields untouched.
        assert len(out["REPO_CONTEXT"]) < 5_000
        assert "truncated to fit context window" in out["REPO_CONTEXT"]
        assert out["FILE_DIFF"] == reps["FILE_DIFF"]
        assert out["FULL_CONTENT"] == reps["FULL_CONTENT"]
        # Rendered result must fit.
        rendered = self.TEMPLATE
        for k, v in out.items():
            rendered = rendered.replace("{{" + k + "}}", v)
        assert len(rendered) <= 3_000

    def test_drops_repo_context_entirely_when_still_over(self):
        # FULL_CONTENT alone exceeds the budget — REPO_CONTEXT can't save it,
        # but we still mark REPO_CONTEXT omitted and warn.
        reps = self._base(repo_chars=2_000, content_chars=5_000)
        out = _fit_replacements_to_budget(self.TEMPLATE, reps, max_chars=1_000)
        assert "omitted" in out["REPO_CONTEXT"]
        # FULL_CONTENT is preserved; we don't trim the file under review.
        assert out["FULL_CONTENT"] == reps["FULL_CONTENT"]

    def test_handles_missing_trimmable_key(self):
        reps = {
            "FILE_PATH": "Foo.lean",
            "FILE_DIFF": "a" * 50,
            "FULL_CONTENT": "f" * 10_000,
            # REPO_CONTEXT deliberately missing — mirrors cross-file path which
            # uses DEPENDENCY_CONTEXT instead.
        }
        out = _fit_replacements_to_budget(self.TEMPLATE, reps, max_chars=1_000)
        # Nothing to trim; returns dict with FULL_CONTENT intact.
        assert out["FULL_CONTENT"] == reps["FULL_CONTENT"]
        assert "REPO_CONTEXT" not in out or out["REPO_CONTEXT"] == ""

    def test_trims_dependency_context(self):
        template = "D:\n{{DEPENDENCY_CONTEXT}}\nC:\n{{FULL_CONTENT}}\n"
        reps = {
            "DEPENDENCY_CONTEXT": "d" * 5_000,
            "FULL_CONTENT": "f" * 100,
        }
        out = _fit_replacements_to_budget(template, reps, max_chars=2_000)
        assert "truncated" in out["DEPENDENCY_CONTEXT"] or "omitted" in out["DEPENDENCY_CONTEXT"]
        assert out["FULL_CONTENT"] == reps["FULL_CONTENT"]

    def test_warning_logged_when_trimming(self, caplog):
        reps = self._base(repo_chars=5_000)
        with caplog.at_level("WARNING"):
            _fit_replacements_to_budget(
                self.TEMPLATE, reps, max_chars=3_000, context_label="Foo.lean"
            )
        assert any("Foo.lean" in rec.message and "REPO_CONTEXT" in rec.message
                   for rec in caplog.records)


# --- Pydantic Schema Tests ---

class TestPydanticSchemas:
    def test_file_review_schema(self):
        from review import FileReview, Finding
        review = FileReview(
            analysis="The code defines a ring homomorphism. Key risk: missing commutativity hypothesis.",
            verdict="Approved",
            checklist_results=[],
            critical_misformalizations=[],
            lean_issues=[Finding(description="test", location="Foo.lean:1")],
            nitpicks=[]
        )
        assert review.verdict == "Approved"
        assert "ring homomorphism" in review.analysis
        assert len(review.lean_issues) == 1

    def test_file_review_analysis_optional(self):
        from review import FileReview
        review = FileReview(verdict="Approved")
        assert review.analysis == ""

    def test_spec_checklist_schema(self):
        from review import SpecChecklist, ChecklistItem, ReferenceMappingEntry
        checklist = SpecChecklist(
            reference_mapping=[
                ReferenceMappingEntry(
                    paper_result="Theorem 3.1",
                    mathematical_content="For all n >= 1, the bound holds with error <= 1/n",
                    status="Present"
                )
            ],
            items=[
                ChecklistItem(
                    concept="Completeness",
                    verification_steps=["Check hypotheses"],
                    severity="Critical"
                )
            ]
        )
        assert len(checklist.reference_mapping) == 1
        assert checklist.items[0].severity == "Critical"

    def test_cross_file_analysis_schema(self):
        from review import CrossFileAnalysis, Finding
        analysis = CrossFileAnalysis(
            composition_issues=[Finding(description="type mismatch", location="A.lean -> B.lean")],
            escape_hatch_impact=[],
            external_dependency_issues=[],
            missing_cross_file_verification=[]
        )
        assert len(analysis.composition_issues) == 1

    def test_triage_result_schema(self):
        from review import TriageResult, ReviewCluster
        triage = TriageResult(clusters=[
            ReviewCluster(
                name="Sumcheck chain",
                files=["A.lean", "B.lean"],
                review_question="Do types match?",
                priority="critical",
                review_strategy="Check that error bounds compose across the sumcheck chain.",
                key_hypotheses=["Output type of Steps.lean matches input of CoreInteraction.lean"]
            )
        ])
        assert triage.clusters[0].priority == "critical"
        assert "error bounds" in triage.clusters[0].review_strategy
        assert len(triage.clusters[0].key_hypotheses) == 1

    def test_triage_strategy_optional(self):
        from review import ReviewCluster
        cluster = ReviewCluster(name="test", files=["A.lean"], review_question="", priority="low")
        assert cluster.review_strategy == ""
        assert cluster.key_hypotheses == []

    def test_cross_file_analysis_has_analysis(self):
        from review import CrossFileAnalysis
        analysis = CrossFileAnalysis(
            analysis="Traced chain: A.lean -> B.lean -> C.lean. Type flow is consistent.",
        )
        assert "Traced chain" in analysis.analysis


# --- Structured Synthesis Input ---

class TestStructuredSynthesisInput:
    def test_structured_data_serialization(self):
        """Verify structured review data is correctly serialized for synthesis."""
        import json
        from review import FileReview, Finding, ChecklistResult

        reviews = {
            "Foo.lean": FileReview(
                verdict="Changes Requested",
                checklist_results=[
                    ChecklistResult(item="Completeness", status="violated", explanation="Missing hypothesis"),
                    ChecklistResult(item="Soundness", status="satisfied", explanation="OK"),
                ],
                critical_misformalizations=[Finding(description="Wrong bound")],
                lean_issues=[Finding(description="Issue 1"), Finding(description="Issue 2")],
                nitpicks=[]
            ),
            "Bar.lean": FileReview(
                verdict="Approved",
                checklist_results=[],
                critical_misformalizations=[],
                lean_issues=[],
                nitpicks=[Finding(description="Naming")]
            ),
        }

        # Build structured data the same way synthesize_overall_summary does
        structured = {}
        for fp, review in reviews.items():
            structured[fp] = {
                "verdict": review.verdict,
                "critical_count": len(review.critical_misformalizations),
                "issue_count": len(review.lean_issues),
                "nitpick_count": len(review.nitpicks),
                "violated_checklist": [cr.item for cr in review.checklist_results if cr.status == "violated"],
                "unclear_checklist": [cr.item for cr in review.checklist_results if cr.status == "unclear"],
            }

        assert structured["Foo.lean"]["verdict"] == "Changes Requested"
        assert structured["Foo.lean"]["critical_count"] == 1
        assert structured["Foo.lean"]["issue_count"] == 2
        assert structured["Foo.lean"]["violated_checklist"] == ["Completeness"]
        assert structured["Bar.lean"]["verdict"] == "Approved"
        assert structured["Bar.lean"]["nitpick_count"] == 1

        # Verify it serializes to valid JSON
        json_str = json.dumps(structured, indent=2)
        parsed = json.loads(json_str)
        assert parsed["Foo.lean"]["critical_count"] == 1


class TestMainFlow:
    def test_main_exits_early_when_no_lean_files_changed(self, monkeypatch, capsys):
        import review

        monkeypatch.setattr(review, "get_pr_diff", lambda pr_number: ("diff --git a/README.md b/README.md\n", []))

        def fail_create_provider(*args, **kwargs):
            raise AssertionError("Provider setup should not run for non-Lean PRs")

        monkeypatch.setattr(review, "create_provider", fail_create_provider)
        monkeypatch.setattr(sys, "argv", ["review.py", "--pr-number", "123"])

        review.main()
        output = capsys.readouterr().out
        assert "No Lean files were changed in this PR." in output
