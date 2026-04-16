"""Unit tests for lean_utils.py shared utilities."""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lean_utils import file_path_to_module_name, is_in_comment, FileCache, detect_src_dir


# --- file_path_to_module_name ---

class TestFilePathToModuleName:
    def test_src_prefix(self):
        assert file_path_to_module_name("src/My/Module.lean") == "My.Module"

    def test_mathlib_prefix(self):
        assert file_path_to_module_name("Mathlib/Algebra/Ring.lean") == "Algebra.Ring"

    def test_lib_prefix(self):
        assert file_path_to_module_name("lib/Foo/Bar.lean") == "Foo.Bar"

    def test_no_prefix(self):
        assert file_path_to_module_name("Foo/Bar/Baz.lean") == "Foo.Bar.Baz"

    def test_single_file(self):
        assert file_path_to_module_name("Main.lean") == "Main"

    def test_explicit_src_dir(self):
        assert file_path_to_module_name("ArkLib/Crypto/Hash.lean", src_dir="ArkLib") == "Crypto.Hash"

    def test_explicit_src_dir_no_match(self):
        # src_dir doesn't match the path prefix — falls through to heuristics
        assert file_path_to_module_name("src/Foo/Bar.lean", src_dir="ArkLib") == "Foo.Bar"


# --- is_in_comment ---

class TestIsInComment:
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

    # --- Nested block comment tests (the core fix) ---

    def test_nested_block_comment_open(self):
        """Opening a nested comment should increase depth to 2."""
        is_comment, depth = is_in_comment("  /- nested open", 1)
        assert is_comment is True
        assert depth == 2

    def test_nested_block_comment_inner_close(self):
        """Closing the inner comment should decrease depth from 2 to 1."""
        is_comment, depth = is_in_comment("  inner close -/", 2)
        assert is_comment is True
        assert depth == 1

    def test_nested_block_comment_still_open(self):
        """After inner close, we should still be in the outer comment."""
        is_comment, depth = is_in_comment("  still in outer", 1)
        assert is_comment is True
        assert depth == 1

    def test_nested_block_comment_outer_close(self):
        """Closing the outer comment should return to depth 0."""
        is_comment, depth = is_in_comment("  outer close -/", 1)
        assert is_comment is True
        assert depth == 0

    def test_full_nested_comment_sequence(self):
        """Integration test: process a full nested comment block."""
        lines = [
            "/- outer start",
            "  /- inner start",
            "    sorry",          # inside nested comment — should be ignored
            "  inner end -/",
            "  still in outer",
            "outer end -/",
            "def foo := sorry",   # this should NOT be in a comment
        ]
        depth = 0
        results = []
        for line in lines:
            is_comment, depth = is_in_comment(line, depth)
            results.append(is_comment)

        assert results == [True, True, True, True, True, True, False]
        assert depth == 0

    def test_code_after_block_comment_close(self):
        """A line with code after -/ should not be considered fully commented."""
        is_comment, depth = is_in_comment("-/ def foo := 1", 1)
        assert is_comment is False
        assert depth == 0

    def test_inline_comment_after_code(self):
        """Code followed by -- comment: line has code, so not fully in comment."""
        is_comment, depth = is_in_comment("def foo := 1 -- comment", 0)
        assert is_comment is False
        assert depth == 0


# --- FileCache ---

class TestFileCache:
    def test_read_existing_file(self, tmp_path):
        f = tmp_path / "test.lean"
        f.write_text("def foo := 1\n")
        cache = FileCache()
        content = cache.read(str(f))
        assert content == "def foo := 1\n"
        # Second read should return cached value
        assert cache.read(str(f)) is content

    def test_read_nonexistent_file(self):
        cache = FileCache()
        assert cache.read("/nonexistent/path.lean") is None

    def test_readlines(self, tmp_path):
        f = tmp_path / "test.lean"
        f.write_text("line1\nline2\n")
        cache = FileCache()
        lines = cache.readlines(str(f))
        assert lines == ["line1\n", "line2\n"]

    def test_readlines_nonexistent(self):
        cache = FileCache()
        assert cache.readlines("/nonexistent/path.lean") is None


# --- detect_src_dir ---

class TestDetectSrcDir:
    def test_lakefile_toml(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "lakefile.toml").write_text('srcDir = "MyLib"\n')
        assert detect_src_dir() == "MyLib"

    def test_lakefile_lean(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "lakefile.lean").write_text('srcDir := "ArkLib"\n')
        assert detect_src_dir() == "ArkLib"

    def test_no_lakefile(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert detect_src_dir() is None

    def test_toml_takes_precedence(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "lakefile.toml").write_text('srcDir = "FromToml"\n')
        (tmp_path / "lakefile.lean").write_text('srcDir := "FromLean"\n')
        assert detect_src_dir() == "FromToml"

    def test_lakefile_without_src_dir(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "lakefile.toml").write_text('name = "myProject"\n')
        assert detect_src_dir() is None
