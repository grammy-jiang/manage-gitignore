#!/usr/bin/env python3
"""Mutation audit: break the code on purpose and see whether the tests notice.

Coverage answers "was this line executed". It cannot answer "would anything fail
if this line were wrong", and at 99% coverage that second question is the only
one left. This changes one operator or literal at a time, runs the tests, and
reports every mutation that survived -- each one a line the suite executes
without checking.

Run by hand, never in CI:

    python3 tests/mutate.py                    # the pure merge core of templates.py
    python3 tests/mutate.py --subject gitwork  # what a push would do, and the option boundary
    python3 tests/mutate.py --all-functions    # every function in the file
    python3 tests/mutate.py --list             # what would be tried, without running

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
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = Path("src/manage_gitignore/skill/scripts")


@dataclass(frozen=True)
class Subject:
    """A file to mutate, the part of it worth mutating, and what to run.

    `pure` names the functions that need no repository, no network and no
    subprocess. Restricting to those is what makes this practical: their tests
    run in seconds, so a few hundred mutants cost a minute rather than an
    afternoon. `--all-functions` lifts the restriction for anyone with time.
    """

    path: Path
    pure: tuple[str, ...]
    tests: tuple[str, ...]


SUBJECTS = {
    "templates": Subject(
        path=SCRIPTS / "templates.py",
        # The merge, and the checks around it.
        pure=(
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
        ),
        tests=("tests/test_gitignore.py", "tests/test_merge_properties.py"),
    ),
    "gitwork": Subject(
        path=SCRIPTS / "gitwork.py",
        # What a push would do, said to the user, and the boundary that stops a
        # repository-supplied name reaching git as a flag. The rest of this file
        # runs git, and a mutant there costs minutes rather than seconds.
        pure=(
            "destination",
            "describe",
            "safe_ref",
            "safe_token",
            "commit_scope",
            "scope_violation",
            "record_push",
        ),
        tests=("tests/test_gitwork.py", "tests/test_gitwork_properties.py"),
    ),
    "summary": Subject(
        path=SCRIPTS / "summary.py",
        # The renderer is pure throughout apart from `main`: it turns a facts
        # document into text. Everything it produces is read by a person, which
        # is what the first two audits found matters most.
        pure=(
            "names",
            "color_diffstat",
            "value_column",
            "emit_section",
            "render",
        ),
        tests=("tests/test_render_summary.py", "tests/test_summary_properties.py"),
    ),
    "shared": Subject(
        path=SCRIPTS / "shared.py",
        # `clean` is the whole defence behind the README's claim that no
        # repo- or API-derived text can forge a line in the summary, and
        # `has_suspicious_chars` is what reports the bytes it must not strip.
        pure=("clean", "has_suspicious_chars", "refuse_option_like"),
        # Everything, unusually. shared.py is imported by all three other
        # scripts, so its coverage is spread across the whole suite: with only
        # test_shared.py selected, breaking `refuse_option_like` so that it
        # rejects *every* value survived, because the tests that would notice
        # live in test_gitignore.py and test_gitwork.py. Five mutations against
        # the full suite is still under a minute.
        tests=("tests/",),
    ),
}

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
        self._entrypoint_tests: set[int] = set()

    def visit_Module(self, node: ast.Module) -> ast.AST:
        self._note_docstrings(node)
        self._note_entrypoint_guard(node)
        return self.generic_visit(node)

    def _note_entrypoint_guard(self, node: ast.Module) -> None:
        """Remember `if __name__ == "__main__":` so its operator is never flipped.

        Not an exclusion for tidiness: `==` to `!=` there makes the module run
        `main()` when it is *imported*, so `import gitwork` parses argparse's
        view of pytest's own argv and exits. pytest cannot collect a module that
        exits during import, so it stops with INTERNALERROR and exit code 3 --
        neither pass nor failure, and nothing about the mutant is learned.

        The `"__main__"` literal itself stays mutable. Emptying it stops
        `python3 gitwork.py --status` doing anything, which the suite drives as a
        subprocess and does notice.
        """
        for child in ast.walk(node):
            if not isinstance(child, ast.If) or not isinstance(child.test, ast.Compare):
                continue
            left = child.test.left
            if isinstance(left, ast.Name) and left.id == "__name__":
                self._entrypoint_tests.add(id(child.test))

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
        entrypoint = id(node) in self._entrypoint_tests
        self.generic_visit(node)
        if entrypoint:
            return node
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

# This tool's own exit codes: 0 every mutation died, 1 some survived, and this
# one for a run whose score does not mean what it says -- the tests failed
# before anything was mutated, or some mutants could not be run at all.
EXIT_UNSOUND = 2

# What a mutant is allowed to do to the clock before the run gives up on it, as
# a multiple of how long the unmutated suite takes. A mutation can turn a loop
# bound around -- `fetch_bytes` reads its response in a `while` -- and without a
# limit one such mutant stalls a worker for the rest of the audit, silently,
# because nothing prints until a batch finishes.
TIMEOUT_FACTOR = 6
TIMEOUT_FLOOR = 120.0


def run_tests(
    workspace: Path, subject: Subject, timeout: float | None = None
) -> subprocess.CompletedProcess[bytes] | None:
    """Run the subject's tests. None means the mutant ran out of time."""
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-n", "0", *subject.tests],
            cwd=workspace,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None


def run_batch(
    args: tuple[Path, Subject, str, list[int], set[str] | None, float],
) -> list[tuple[int, bool | None, str]]:
    """Run a batch of mutations in one workspace, one after another.

    The batches are round-robin slices of the mutation list, so a worker's
    indexes are not contiguous -- what matters is only that each workspace
    belongs to exactly one worker. A workspace holds one mutated file at a time,
    so two tasks sharing one would overwrite each other's mutation and report on
    whichever won the race.

    Each result is (index, survived, why). `survived` is None when the run could
    not be scored at all -- see `main` for why that is reported rather than
    raised.
    """
    workspace, subject, source, indexes, functions, timeout = args
    results: list[tuple[int, bool | None, str]] = []
    for index in indexes:
        target = workspace / subject.path
        target.write_text(mutated_source(source, index, functions), encoding="utf-8")
        done = run_tests(workspace, subject, timeout)
        if done is None:
            results.append((index, None, f"still running after {timeout:.0f}s"))
        elif done.returncode in (PYTEST_PASSED, PYTEST_TESTS_FAILED):
            results.append((index, done.returncode == PYTEST_PASSED, ""))
        else:
            tail = done.stdout.decode(errors="replace").strip().splitlines()
            results.append(
                (index, None, f"pytest exited {done.returncode}: {tail[-1][:120] if tail else ''}")
            )
    return results


def verdict(survivors: list[Mutation], unscored: list[tuple[Mutation, str]]) -> int:
    """The exit status for a finished run.

    An unscored mutant outranks a clean sweep. Reporting success because nothing
    happened to survive, when some mutations were never actually tried, is the
    same failure this tool exists to catch one level up -- a green result
    standing in for a check that did not run. It is the status a failing
    baseline gets, because both mean the same thing: the score does not mean
    what it says.
    """
    if unscored:
        return EXIT_UNSOUND
    return 1 if survivors else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--subject",
        choices=sorted(SUBJECTS),
        default="templates",
        help="which script to mutate (default: templates)",
    )
    parser.add_argument("--all-functions", action="store_true", help="not just the pure core")
    parser.add_argument("--list", action="store_true", help="print the mutations, run nothing")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()

    subject = SUBJECTS[args.subject]
    functions = None if args.all_functions else set(subject.pure)
    source = (REPO / subject.path).read_text(encoding="utf-8")
    found = candidates(source, functions)

    if args.list:
        for mutation in found:
            print(f"  {mutation.index:4}  line {mutation.line:5}  {mutation.description}")
        print(f"{len(found)} mutations")
        return 0

    print(f"{subject.path.name}: {len(found)} mutations, {args.workers} workers")
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
        started = time.monotonic()
        baseline = run_tests(workspaces[0], subject)
        assert baseline is not None  # no timeout is passed for the baseline
        if baseline.returncode != PYTEST_PASSED:
            print(
                f"the tests do not pass unmutated (pytest exited {baseline.returncode}), "
                f"so nothing here would mean anything:\n"
                f"{baseline.stdout.decode(errors='replace')[-2000:]}",
                file=sys.stderr,
            )
            return EXIT_UNSOUND
        # Derived from this machine rather than fixed: the same audit runs on a
        # laptop and on twenty cores, and a constant would either strangle the
        # slow one or let the fast one hang for minutes.
        timeout = max(TIMEOUT_FLOOR, TIMEOUT_FACTOR * (time.monotonic() - started))
        print(f"  baseline: the suite passes unmutated; giving each mutant {timeout:.0f}s")

        survivors: list[Mutation] = []
        unscored: list[tuple[Mutation, str]] = []
        batches: list[list[int]] = [[] for _ in range(args.workers)]
        for position, mutation in enumerate(found):
            batches[position % args.workers].append(mutation.index)
        work = [
            (workspaces[worker], subject, source, batch, functions, timeout)
            for worker, batch in enumerate(batches)
            if batch
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for finished, batch in enumerate(pool.map(run_batch, work), 1):
                survivors.extend(found[i] for i, survived, _ in batch if survived)
                unscored.extend((found[i], why) for i, survived, why in batch if survived is None)
                print(
                    f"  worker {finished}/{len(work)} done, {len(survivors)} survived so far"
                    + (f", {len(unscored)} unscored" if unscored else "")
                )

    # An unscoreable mutant is not a killed one. Counting it as a kill is the
    # same lie as counting a missing dependency as a kill, so it leaves the
    # denominator and is reported on its own.
    scored = len(found) - len(unscored)
    print(f"\n{scored - len(survivors)}/{scored} killed")
    if survivors:
        print("\nSurvived -- the suite runs these lines without checking them:")
        for mutation in sorted(survivors, key=lambda m: m.line):
            print(f"  {subject.path.name}:{mutation.line:<5} {mutation.description}")
    if unscored:
        print(f"\nUnscored -- {len(unscored)} of {len(found)} could not be run at all:")
        for mutation, why in sorted(unscored, key=lambda pair: pair[0].line):
            print(f"  {subject.path.name}:{mutation.line:<5} {mutation.description}  ({why})")
    return verdict(survivors, unscored)


if __name__ == "__main__":
    raise SystemExit(main())
