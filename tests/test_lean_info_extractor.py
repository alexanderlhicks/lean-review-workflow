"""Unit tests for lean_info_extractor.py core functions."""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lean_info_extractor import (
    get_lean_declarations,
    get_module_name,
    extract_sorry_warnings,
    extract_light_info,
    format_for_review,
    main,
)
from lean_utils import file_path_to_module_name


class TestGetLeanDeclarations:
    def test_basic_declarations(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("""
import Mathlib

def myDef (n : Nat) : Nat := n + 1

theorem myTheorem : True := trivial

lemma myLemma : 1 = 1 := rfl

structure MyStruct where
  x : Nat

noncomputable def myNonComp := Classical.choice ⟨0⟩
""")
        decls = get_lean_declarations(str(lean_file))
        assert "myDef" in decls
        assert "myTheorem" in decls
        assert "myLemma" in decls
        assert "MyStruct" in decls
        assert "myNonComp" in decls

    def test_empty_file(self, tmp_path):
        lean_file = tmp_path / "Empty.lean"
        lean_file.write_text("")
        assert get_lean_declarations(str(lean_file)) == []

    def test_nonexistent_file(self):
        assert get_lean_declarations("/nonexistent/file.lean") == []


class TestGetModuleName:
    def test_with_src_prefix(self, tmp_path):
        # Without lakefile, falls back to heuristic
        os.chdir(tmp_path)
        assert get_module_name("src/Foo/Bar.lean") == "Foo.Bar"

    def test_with_lakefile_toml(self, tmp_path):
        os.chdir(tmp_path)
        (tmp_path / "lakefile.toml").write_text('srcDir = "ArkLib"\n')
        assert get_module_name("ArkLib/Crypto/Hash.lean") == "Crypto.Hash"


class TestExtractSorryWarnings:
    def test_finds_sorry(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        locs = extract_sorry_warnings(str(lean_file))
        assert len(locs) == 1
        assert ":1" in locs[0]

    def test_ignores_comments(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("-- sorry\ndef foo := 1\n")
        locs = extract_sorry_warnings(str(lean_file))
        assert len(locs) == 0

    def test_ignores_nested_block_comments(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("/- outer\n  /- inner sorry -/\n  still outer\n-/\ndef foo := 1\n")
        locs = extract_sorry_warnings(str(lean_file))
        assert len(locs) == 0

    def test_finds_sorry_after_block_comment(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("/- comment -/\ntheorem foo : True := sorry\n")
        locs = extract_sorry_warnings(str(lean_file))
        assert len(locs) == 1
        assert ":2" in locs[0]

    def test_empty_file(self, tmp_path):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("")
        assert extract_sorry_warnings(str(lean_file)) == []


class TestFormatForReview:
    def test_no_issues(self):
        info = {"sorry_locations": [], "axiom_summary": {}, "files": {}, "errors": []}
        result = format_for_review(info)
        assert "No issues detected" in result

    def test_with_sorry(self):
        info = {
            "sorry_locations": ["Foo.lean:42"],
            "axiom_summary": {},
            "files": {},
            "errors": []
        }
        result = format_for_review(info)
        assert "Foo.lean:42" in result
        assert "sorry" in result.lower() or "Incomplete" in result

    def test_with_diagnostics(self):
        info = {
            "sorry_locations": [],
            "axiom_summary": {},
            "files": {},
            "diagnostics": ["Foo.lean:10: warning: unused variable"],
            "errors": []
        }
        result = format_for_review(info)
        assert "Foo.lean:10" in result

    def test_with_axioms(self):
        info = {
            "sorry_locations": [],
            "axiom_summary": {"Foo.myAxiom": ["myCustomAxiom"]},
            "files": {},
            "errors": []
        }
        result = format_for_review(info)
        assert "myCustomAxiom" in result


class TestExtractLightInfo:
    def test_finds_sorry_in_summary_files(self, tmp_path):
        lean_file = tmp_path / "Summary.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        result = extract_light_info([str(lean_file)])
        assert len(result["sorry_locations"]) == 1
        assert result["files_scanned"] == 1

    def test_no_sorry(self, tmp_path):
        lean_file = tmp_path / "Clean.lean"
        lean_file.write_text("def foo := 1\n")
        result = extract_light_info([str(lean_file)])
        assert result["sorry_locations"] == []
        assert result["files_scanned"] == 1

    def test_skips_non_lean(self, tmp_path):
        md_file = tmp_path / "README.md"
        md_file.write_text("sorry\n")
        result = extract_light_info([str(md_file)])
        assert result["files_scanned"] == 0

    def test_handles_nonexistent(self):
        result = extract_light_info(["/nonexistent/file.lean"])
        assert result["files_scanned"] == 0


class TestGitHubOutputFormatting:
    def test_multiline_outputs_use_heredoc_syntax(self, tmp_path, monkeypatch, capsys):
        lean_file = tmp_path / "Foo.lean"
        lean_file.write_text("theorem foo : True := sorry\n")
        github_output = tmp_path / "github_output.txt"

        monkeypatch.setenv("CHANGED_FILES", str(lean_file))
        monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
        monkeypatch.delenv("SUMMARY_FILES", raising=False)
        monkeypatch.setattr(sys, "argv", ["lean_info_extractor.py"])

        main()

        output_text = github_output.read_text()
        assert "lean_info_json<<" in output_text
        assert "lean_info_formatted<<" in output_text
        assert '"sorry_locations": [' in output_text
