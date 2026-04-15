"""Shared utilities for Lean 4 project analysis.

Provides common functions used across discover_files.py,
lean_info_extractor.py, and review.py to avoid logic duplication.
"""

import os
import re
from typing import Dict, List, Optional, Tuple


def detect_src_dir() -> Optional[str]:
    """Detects the Lean source directory by parsing lakefile.toml or lakefile.lean."""
    for lakefile, pattern in [
        ('lakefile.toml', r'srcDir\s*=\s*"([^"]+)"'),
        ('lakefile.lean', r'srcDir\s*:=\s*"([^"]+)"'),
    ]:
        if os.path.exists(lakefile):
            try:
                with open(lakefile, 'r') as f:
                    content = f.read()
                m = re.search(pattern, content)
                if m:
                    return m.group(1)
            except Exception:
                pass
    return None


def file_path_to_module_name(file_path: str, src_dir: Optional[str] = None) -> str:
    """Converts a file path to a Lean module name.

    Resolution order:
    1. Explicitly provided src_dir parameter
    2. LEAN_SRC_DIR environment variable
    3. Lakefile parsing (toml, then lean)
    4. Heuristic prefixes: src/, lib/, Mathlib/
    """
    if src_dir is None:
        src_dir = os.environ.get('LEAN_SRC_DIR')
    if src_dir is None:
        src_dir = detect_src_dir()

    if src_dir and file_path.startswith(f"{src_dir}/"):
        file_path = file_path[len(src_dir) + 1:]
    else:
        for prefix in ['src/', 'lib/', 'Mathlib/']:
            if file_path.startswith(prefix):
                file_path = file_path[len(prefix):]
                break

    return file_path.replace('/', '.').replace('.lean', '')


def is_in_comment(line: str, nesting_depth: int) -> Tuple[bool, int]:
    """Determines if a line's code content is entirely within comments.

    Handles Lean 4's nested /- ... -/ block comments and -- line comments.

    Args:
        line: The source line to check.
        nesting_depth: Current block comment nesting depth (0 = not in block comment).

    Returns:
        (line_has_no_code_outside_comments, new_nesting_depth)
    """
    stripped = line.strip()

    # Quick path: not in block comment and single-line comment
    if nesting_depth == 0 and stripped.startswith('--'):
        return True, 0

    has_code = False
    i = 0
    while i < len(stripped):
        # Check for two-character comment delimiters
        if i + 1 < len(stripped):
            pair = stripped[i:i + 2]
            if pair == '/-':
                nesting_depth += 1
                i += 2
                continue
            if pair == '-/' and nesting_depth > 0:
                nesting_depth -= 1
                i += 2
                continue
            if pair == '--' and nesting_depth == 0:
                break  # rest of line is a single-line comment

        if nesting_depth == 0 and not stripped[i].isspace():
            has_code = True
        i += 1

    return not has_code, nesting_depth


class FileCache:
    """In-memory cache for file contents.

    Avoids redundant disk reads when the same file is accessed
    by multiple stages of the review pipeline. Thread-safe for
    concurrent reads (Python dict get/set are atomic under the GIL,
    and duplicate reads of the same uncached file are harmless).
    """

    def __init__(self):
        self._cache: Dict[str, Optional[str]] = {}

    def read(self, file_path: str) -> Optional[str]:
        """Read file contents, returning cached content if available.
        Returns None if the file does not exist or cannot be read."""
        if file_path not in self._cache:
            if not os.path.exists(file_path):
                self._cache[file_path] = None
            else:
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        self._cache[file_path] = f.read()
                except Exception:
                    self._cache[file_path] = None
        return self._cache[file_path]

    def readlines(self, file_path: str) -> Optional[List[str]]:
        """Read file as a list of lines (with line endings), using the cache."""
        content = self.read(file_path)
        if content is None:
            return None
        return content.splitlines(keepends=True)
