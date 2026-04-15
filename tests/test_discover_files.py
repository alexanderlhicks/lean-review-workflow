"""Unit tests for discover_files.py core functions."""

import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discover_files import (
    get_lean_module_name,
    get_dependent_lean_files,
    get_dependency_lean_files,
    get_transitive_dependencies,
    convert_module_to_file_path,
)
from lean_utils import file_path_to_module_name


class TestGetLeanModuleName:
    def test_src_prefix(self):
        assert get_lean_module_name("src/My/Module.lean") == "My.Module"

    def test_mathlib_prefix(self):
        assert get_lean_module_name("Mathlib/Algebra/Ring.lean") == "Algebra.Ring"

    def test_lib_prefix(self):
        assert get_lean_module_name("lib/Foo/Bar.lean") == "Foo.Bar"

    def test_no_prefix(self):
        assert get_lean_module_name("Foo/Bar/Baz.lean") == "Foo.Bar.Baz"

    def test_single_file(self):
        assert get_lean_module_name("Main.lean") == "Main"


class TestGetDependentLeanFiles:
    def test_basic_dependency(self):
        graph = [
            {"name": "A", "imports": ["B"]},
            {"name": "B", "imports": []},
            {"name": "C", "imports": ["A"]},
        ]
        # If B changed, A depends on B
        result = get_dependent_lean_files({"B"}, graph)
        assert "A" in result
        assert "C" not in result  # C depends on A, not B directly

    def test_no_dependents(self):
        graph = [
            {"name": "A", "imports": []},
            {"name": "B", "imports": []},
        ]
        result = get_dependent_lean_files({"A"}, graph)
        assert result == []


class TestGetDependencyLeanFiles:
    def test_basic(self):
        graph = [
            {"name": "A", "imports": ["B", "C"]},
            {"name": "B", "imports": []},
            {"name": "C", "imports": []},
        ]
        result = get_dependency_lean_files({"A"}, graph)
        assert "B" in result
        assert "C" in result

    def test_excludes_changed(self):
        graph = [
            {"name": "A", "imports": ["B"]},
            {"name": "B", "imports": []},
        ]
        # Both A and B changed — B should not be in dependencies
        result = get_dependency_lean_files({"A", "B"}, graph)
        assert "B" not in result


class TestConvertModuleToFilePath:
    def test_basic(self):
        index = ["src/Foo/Bar.lean", "src/Baz.lean"]
        assert convert_module_to_file_path("Foo.Bar", index) == "src/Foo/Bar.lean"

    def test_fallback(self):
        index = []
        # When not found, returns heuristic path
        result = convert_module_to_file_path("Foo.Bar", index)
        assert result.endswith("Foo" + os.sep + "Bar.lean")


class TestTransitiveDependencies:
    GRAPH = [
        {"name": "A", "imports": ["B", "C"]},
        {"name": "B", "imports": ["D"]},
        {"name": "C", "imports": ["D", "E"]},
        {"name": "D", "imports": ["F"]},
        {"name": "E", "imports": []},
        {"name": "F", "imports": []},
    ]

    def test_depth_1_matches_direct(self):
        """At depth 1, should match get_dependency_lean_files behavior."""
        result = get_transitive_dependencies({"A"}, self.GRAPH, max_depth=1)
        assert set(result.keys()) == {"B", "C"}
        assert all(d == 1 for d in result.values())

    def test_depth_2_finds_imports_of_imports(self):
        """At depth 2, should also find D and E (imports of B and C)."""
        result = get_transitive_dependencies({"A"}, self.GRAPH, max_depth=2)
        assert "B" in result and result["B"] == 1
        assert "C" in result and result["C"] == 1
        assert "D" in result and result["D"] == 2
        assert "E" in result and result["E"] == 2
        assert "F" not in result  # depth 3

    def test_depth_3_finds_deeper(self):
        result = get_transitive_dependencies({"A"}, self.GRAPH, max_depth=3)
        assert "F" in result and result["F"] == 3

    def test_excludes_changed_modules(self):
        """Changed modules should not appear in the result."""
        result = get_transitive_dependencies({"A", "B"}, self.GRAPH, max_depth=2)
        assert "A" not in result
        assert "B" not in result
        assert "C" in result  # direct import of A
        assert "D" in result  # import of C (depth 2 from A, or depth 1 from B — but B is changed)

    def test_cycle_handling(self):
        """Cycles should not cause infinite loops."""
        cyclic_graph = [
            {"name": "A", "imports": ["B"]},
            {"name": "B", "imports": ["C"]},
            {"name": "C", "imports": ["A"]},  # cycle back to A
        ]
        result = get_transitive_dependencies({"A"}, cyclic_graph, max_depth=5)
        assert "B" in result and result["B"] == 1
        assert "C" in result and result["C"] == 2
        # A is in changed_modules, so it's excluded

    def test_empty_graph(self):
        result = get_transitive_dependencies({"A"}, [], max_depth=2)
        assert result == {}

    def test_depth_tags_are_correct(self):
        """Each module should be tagged with its minimum depth."""
        result = get_transitive_dependencies({"A"}, self.GRAPH, max_depth=3)
        # D is reachable at depth 2 (A->B->D or A->C->D)
        assert result["D"] == 2
        # F is reachable at depth 3 (A->B->D->F or A->C->D->F)
        assert result["F"] == 3
