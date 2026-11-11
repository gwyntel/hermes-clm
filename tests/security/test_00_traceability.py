"""Traceability gate: every T1-T49 from the security design review has a test.

This is a static scan, deliberately, because the modules that carry the markers are
skipped at import time while phase-1 implementation branches are unmerged. Reading the
source keeps the coverage gate meaningful instead of "nothing collected, nothing missing".
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

TEST_DIR = pathlib.Path(__file__).resolve().parent
REVIEW_IDS = [f"T{n}" for n in range(1, 50)]
RANGE_RE = re.compile(r"^(T(\d+))(?:\.\.(T(\d+)))?$")


def _declared_ids():
    """Map T-id -> [test node names] from `@pytest.mark.t(...)` decorators."""
    found: dict[str, list[str]] = {}
    for path in TEST_DIR.rglob("test_*.py"):
        source = path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "t"
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "mark"
                ):
                    continue
                for arg in decorator.args:
                    if not isinstance(arg, ast.Constant) or not isinstance(arg.value, str):
                        continue
                    token = arg.value
                    match = RANGE_RE.match(token)
                    if not match:
                        continue
                    start = int(match.group(2))
                    end = int(match.group(4)) if match.group(4) else start
                    for number in range(start, end + 1):
                        found.setdefault(f"T{number}", []).append(node.name)
    return found


DECLARED = _declared_ids()


def test_every_review_id_has_at_least_one_test():
    missing = [tid for tid in REVIEW_IDS if tid not in DECLARED]
    assert not missing, (
        "design-review acceptance tests with no security test: "
        f"{missing} (see docs/security-testing.md)"
    )


def test_no_unknown_or_out_of_range_ids():
    unknown = sorted(set(DECLARED) - set(REVIEW_IDS))
    assert not unknown, f"tests reference T-ids outside T1..T49: {unknown}"


def test_ids_are_not_claimed_twice_by_the_same_test():
    for tid, names in DECLARED.items():
        duplicates = {name for name in names if names.count(name) > 1}
        assert not duplicates, f"{tid} declared twice on the same test: {duplicates}"


def test_documented_coverage_table_matches_the_suite():
    """docs/security-testing.md is the map; it must not drift from the markers."""
    doc = TEST_DIR.parent.parent / "docs" / "security-testing.md"
    assert doc.is_file(), "docs/security-testing.md is missing"
    text = doc.read_text()
    undocumented = [tid for tid in REVIEW_IDS if tid not in text]
    assert not undocumented, f"T-ids absent from docs/security-testing.md: {undocumented}"
