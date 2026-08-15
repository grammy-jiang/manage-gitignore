"""Structural checks on this repository's own Python, including the tests.

Both defects below share a property that makes them worth a test rather than a
review habit: **the suite goes on passing**. The tests still run, the count still
goes up, and nothing about the output says a class declaration was swallowed or
a test is shadowing another. A green run is exactly what you get.

Found by review of PR #50, where an edit consumed the `class TestClassify:` line
and left its docstring as a bare expression -- reparenting six tests into the
class above it. Every one of them still ran, under the wrong name, and `make
verify` was green.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SOURCES = sorted((REPO / "tests").glob("*.py")) + sorted(
    (REPO / "src").rglob("*.py"),
)


def parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def blocks(tree: ast.Module):
    """Every node that owns a statement list, with its name."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            yield node, body


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_string_literal_sits_where_a_docstring_cannot(path: Path):
    """A string expression that is not the first statement of its block.

    That is the signature of a lost `class` or `def` line: the docstring stays
    behind as a no-op expression and everything under it silently joins the
    block above. Python is happy, pytest is happy, and six tests have quietly
    changed class.
    """
    tree = parsed(path)
    orphans = [
        f"{path.name}:{stmt.lineno}"
        for _, body in blocks(tree)
        for index, stmt in enumerate(body)
        if index > 0
        and isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    ]
    assert orphans == [], "string literal where no docstring belongs -- a lost class or def?"


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_definition_shadows_another_in_the_same_block(path: Path):
    """Two functions of one name in one class: the second wins, the first is
    gone, and nothing reports it.

    The usual way to get here is a copy-paste or a merge, and the cost is a test
    that no longer exists while still being listed in the file -- the worst
    shape a missing test can take, because reading the file says it is covered.
    """
    tree = parsed(path)
    clashes = []
    for node, body in blocks(tree):
        names = [
            stmt.name for stmt in body if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef)
        ]
        owner = getattr(node, "name", "<module>")
        clashes += [
            f"{path.name}:{owner}.{name} defined {count} times"
            for name, count in collections.Counter(names).items()
            if count > 1
        ]
    assert clashes == []


class TestThePropertyGateIsDeterministic:
    """A commit's test result must be a property of the commit.

    Hypothesis randomises by default, which makes a green run mean "150 random
    examples passed on this machine at this moment". Two runs of an unchanged
    tree then answer different questions, and a counterexample found once may
    never be seen again -- the same defect this repository keeps finding in its
    own checks, one level up.

    Measured before the profile existed: three runs of one property drew three
    different input sets.
    """

    def test_the_default_profile_derandomises(self):
        from hypothesis import settings

        assert settings.default.derandomize is True

    def test_the_same_property_draws_the_same_inputs_twice(self):
        """The claim itself, not the setting that is supposed to cause it."""
        import hashlib

        from hypothesis import given
        from hypothesis import strategies as st

        def drawn() -> str:
            seen: list[str] = []

            @given(st.text(max_size=8))
            def collect(value):
                seen.append(value)

            collect()
            return hashlib.sha256("\x00".join(seen).encode()).hexdigest()

        assert drawn() == drawn()

    def test_the_exploring_profile_is_still_random_and_bigger(self):
        """The other half of the trade. If the scheduled search were derandomised
        too, raising its budget would buy a longer run of the same examples and
        the project would have given up finding anything new."""
        from hypothesis import settings

        explore = settings.get_profile("explore")
        assert explore.derandomize is False
        assert explore.max_examples > settings.default.max_examples


def test_the_check_itself_is_looking_at_something():
    """A guard on the two above: an empty file list makes both vacuous, and a
    glob that stops matching is exactly how that would happen quietly."""
    assert len(SOURCES) > 10
    assert any(p.name == "test_gitignore.py" for p in SOURCES)
    assert any(p.name == "gitwork.py" for p in SOURCES)
