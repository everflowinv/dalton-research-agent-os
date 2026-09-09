"""No test may hide inside a __main__ guard.

A method appended after `if __name__ == "__main__": unittest.main()` at the
guard's own indentation becomes part of the guard's body: it exists only when
the file is run directly, and `unittest discover` never sees it. The suite
count does not move and the test looks like it passed.

That happened once here, to a test asserting a contended scheduler lease is
reported as busy rather than crashing the planner child. A class defined at
column 0 after the guard is fine -- it is still imported -- so this checks the
thing that is actually broken rather than the thing that merely looks untidy.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent


class SuiteIntegrityTests(unittest.TestCase):
    def hidden_definitions(self) -> list[str]:
        found: list[str] = []
        for path in sorted(TESTS.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if not (isinstance(node, ast.If)
                        and ast.unparse(node.test).startswith("__name__")):
                    continue
                for inner in node.body:
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef,
                                          ast.ClassDef)):
                        found.append(f"{path.name}:{inner.lineno} {inner.name}")
        return found

    def test_nothing_is_defined_inside_a_main_guard(self):
        self.assertEqual(
            self.hidden_definitions(), [],
            "these are only defined when the file is run directly, so "
            "unittest discover never runs them",
        )

    def test_the_check_would_catch_the_shape_that_slipped_through(self):
        module = ast.parse(
            'import unittest\n'
            'class T(unittest.TestCase):\n'
            '    def test_a(self): pass\n'
            'if __name__ == "__main__":\n'
            '    unittest.main()\n'
            '    def test_b(self): pass\n'
        )
        guard = next(n for n in module.body
                     if isinstance(n, ast.If)
                     and ast.unparse(n.test).startswith("__name__"))
        self.assertTrue(any(isinstance(n, ast.FunctionDef) for n in guard.body))


if __name__ == "__main__":
    unittest.main()
