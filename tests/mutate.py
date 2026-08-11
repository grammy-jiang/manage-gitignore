#!/usr/bin/env python3
"""Mutation audit: break the code on purpose and see whether the tests notice.

Coverage answers "was this line executed". It cannot answer "would anything fail
if this line were wrong", and at 99% coverage that second question is the only
one left. This changes one operator or literal at a time, runs the tests, and
reports every mutation that survived -- each one a line the suite executes
without checking.

Run by hand, never in CI:

    python3 tests/mutate.py                 # the pure merge core of templates.py
    python3 tests/mutate.py --all-functions # every function in the file
    python3 tests/mutate.py --list          # what would be tried, without running

Why this and not mutmut: mutmut derives a module name from a file's path and
expects `manage_gitignore.skill.scripts.templates`, while the suite imports
`templates` -- the scripts import each other by plain module name, which is rule
2 in CLAUDE.md and the reason an installed skill can be a bare symlink. Bending
the layout to suit a tool that runs once a year is the wrong way round. This is
the part of mutmut that was actually needed, in a form that fits.

Each worker gets its own copy of the repository, so the working tree is never
mutated and an interrupted run leaves nothing behind.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGET = Path("src/manage_gitignore/skill/scripts/templates.py")

# The pure half of templates.py: the merge, and the checks around it. These need
# no repository, no network and no subprocess, so the tests that cover them run
# in well under a second -- which is what makes a few hundred mutants practical.
PURE_CORE = (
    "is_pattern_line",
    "count_patterns",
    "strip_bom",
    "api_pattern_sections",
    "risky_patterns",
    "classify",
    "count_esc",
    "verify_bytes",
    "split_regions",
    "split_existing",
    "reapply_custom",
    "dedup_custom",
    "normalize_templates",
)

# The tests that exercise that half, and only those. Adding the rest of the
# suite would multiply the runtime by fifty to say nothing new about this code.
TESTS = ("tests/test_gitignore.py", "tests/test_merge_properties.py")

# Deliberately a small, explainable set. Each entry turns one node into another
# that is still valid Python and means something different. Operators that
# usually produce equivalent mutants (reordering commutative operands, renaming
# locals) are left out: they cost a test run each and teach nothing.
COMPARISONS = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}
ARITHMETIC = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}
BOOLEANS = {ast.And: ast.Or, ast.Or: ast.And}


@dataclass(frozen=True)
class Mutation:
    index: int
    line: int
    description: str


class Mutator(ast.NodeTransformer):
    """Applies the mutation whose running number matches `wanted`.

    One pass per mutation rather than all at once: two mutations in one file can
    cancel each other out, and a survivor has to name a single change.
    """

    def __init__(self, wanted: int | None, functions: set[str] | None) -> None:
        self.wanted = wanted
        self.functions = functions
        self.found: list[Mutation] = []
        self._stack: list[str] = []
        self._docstrings: set[int] = set()

    def visit_Module(self, node: ast.Module) -> ast.AST:
        self._note_docstrings(node)
        return self.generic_visit(node)

    def _note_docstrings(self, node: ast.AST) -> None:
        """Remember docstring nodes so they are never mutated.

        Emptying a docstring changes nothing a test could see, so every one
        would be an equivalent mutant -- a guaranteed survivor that costs a full
        test run and means nothing. templates.py is heavily documented, so this
        is the difference between a report worth reading and one that is mostly
        noise.
        """
        for child in ast.walk(node):
            if isinstance(
                child, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                body = getattr(child, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    self._docstrings.add(id(body[0].value))

    def _in_scope(self) -> bool:
        return self.functions is None or bool(self.functions & set(self._stack))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self._stack.append(node.name)
        try:
            return self.generic_visit(node)
        finally:
            self._stack.pop()

    def _offer(self, node: ast.AST, description: str) -> bool:
        """Record this candidate, and say whether it is the one to apply."""
        if not self._in_scope():
            return False
        index = len(self.found)
        self.found.append(Mutation(index, getattr(node, "lineno", 0), description))
        return index == self.wanted

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        for position, op in enumerate(node.ops):
            replacement = COMPARISONS.get(type(op))
            if replacement is None:
                continue
            if self._offer(node, f"{type(op).__name__} -> {replacement.__name__}"):
                node.ops[position] = replacement()
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        replacement = ARITHMETIC.get(type(node.op))
        if replacement and self._offer(node, f"{type(node.op).__name__} -> {replacement.__name__}"):
            node.op = replacement()
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        replacement = BOOLEANS.get(type(node.op))
        if replacement and self._offer(node, f"{type(node.op).__name__} -> {replacement.__name__}"):
            node.op = replacement()
        return node

    def visit_UnaryOp(self, node: ast.UnaryOp) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.op, ast.Not) and self._offer(node, "drop `not`"):
            return node.operand
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if id(node) in self._docstrings:
            return node
        if isinstance(node.value, bool):
            if self._offer(node, f"{node.value} -> {not node.value}"):
                return ast.Constant(value=not node.value)
        elif isinstance(node.value, int) and self._offer(node, f"{node.value} -> {node.value + 1}"):
            return ast.Constant(value=node.value + 1)
        elif isinstance(node.value, str) and node.value:
            shown = node.value if len(node.value) <= 30 else node.value[:27] + "..."
            if self._offer(node, f"{shown!r} -> ''"):
                return ast.Constant(value="")
        return node


def candidates(source: str, functions: set[str] | None) -> list[Mutation]:
    mutator = Mutator(wanted=None, functions=functions)
    mutator.visit(ast.parse(source))
    return mutator.found


def mutated_source(source: str, index: int, functions: set[str] | None) -> str:
    tree = ast.parse(source)
    mutator = Mutator(wanted=index, functions=functions)
    tree = mutator.visit(tree)
    return ast.unparse(ast.fix_missing_locations(tree))


# pytest's exit codes. Only one of them means "a test failed", and only that one
# means a mutant was killed. Treating every non-zero status as a kill turns a
# missing dependency, a collection error or an interrupted run into a perfect
# score -- the tool reporting success for having done nothing.
PYTEST_PASSED = 0
PYTEST_TESTS_FAILED = 1


def run_tests(workspace: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-x", "-q", "-n", "0", *TESTS],
        cwd=workspace,
        capture_output=True,
    )


def run_batch(args: tuple[Path, str, list[int], set[str] | None]) -> list[tuple[int, bool]]:
    """Run a batch of mutations in one workspace, one after another.

    The batches are round-robin slices of the mutation list, so a worker's
    indexes are not contiguous -- what matters is only that each workspace
    belongs to exactly one worker. A workspace holds one mutated file at a time,
    so two tasks sharing one would overwrite each other's mutation and report on
    whichever won the race.
    """
    workspace, source, indexes, functions = args
    results = []
    for index in indexes:
        (workspace / TARGET).write_text(mutated_source(source, index, functions), encoding="utf-8")
        done = run_tests(workspace)
        if done.returncode not in (PYTEST_PASSED, PYTEST_TESTS_FAILED):
            raise RuntimeError(
                f"pytest exited {done.returncode} on mutation {index}, which is neither "
                f"pass nor failure -- the run cannot be scored. Last output:\n"
                f"{done.stdout.decode(errors='replace')[-2000:]}"
            )
        results.append((index, done.returncode == PYTEST_PASSED))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--all-functions", action="store_true", help="not just the pure core")
    parser.add_argument("--list", action="store_true", help="print the mutations, run nothing")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    functions = None if args.all_functions else set(PURE_CORE)
    source = (REPO / TARGET).read_text(encoding="utf-8")
    found = candidates(source, functions)

    if args.list:
        for mutation in found:
            print(f"  {mutation.index:4}  line {mutation.line:5}  {mutation.description}")
        print(f"{len(found)} mutations")
        return 0

    print(f"{len(found)} mutations, {args.workers} workers")
    with tempfile.TemporaryDirectory() as scratch:
        workspaces = []
        for worker in range(args.workers):
            workspace = Path(scratch) / f"w{worker}"
            # The tree only, without .git: nothing here commits anything, and
            # copying the history would dominate the setup time.
            shutil.copytree(REPO, workspace, ignore=shutil.ignore_patterns(".git", "mutants"))
            workspaces.append(workspace)

        # Before mutating anything: do the tests pass as they are? Without this,
        # a missing dependency or a broken fixture makes every mutant look
        # killed, and the report says the suite is perfect precisely when it is
        # not running. The baseline runs in a workspace no mutation has touched.
        baseline = run_tests(workspaces[0])
        if baseline.returncode != PYTEST_PASSED:
            print(
                f"the tests do not pass unmutated (pytest exited {baseline.returncode}), "
                f"so nothing here would mean anything:\n"
                f"{baseline.stdout.decode(errors='replace')[-2000:]}",
                file=sys.stderr,
            )
            return 2
        print("  baseline: the suite passes unmutated")

        survivors: list[Mutation] = []
        batches: list[list[int]] = [[] for _ in range(args.workers)]
        for position, mutation in enumerate(found):
            batches[position % args.workers].append(mutation.index)
        work = [
            (workspaces[worker], source, batch, functions)
            for worker, batch in enumerate(batches)
            if batch
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for finished, batch in enumerate(pool.map(run_batch, work), 1):
                survivors.extend(found[index] for index, survived in batch if survived)
                print(f"  worker {finished}/{len(work)} done, {len(survivors)} survived so far")

    print(f"\n{len(found) - len(survivors)}/{len(found)} killed")
    if survivors:
        print("\nSurvived -- the suite runs these lines without checking them:")
        for mutation in sorted(survivors, key=lambda m: m.line):
            print(f"  {TARGET.name}:{mutation.line:<5} {mutation.description}")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
