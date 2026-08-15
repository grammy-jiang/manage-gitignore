"""Tests for the mutation harness itself.

`mutate.py` is the tool that decides whether the rest of the suite is worth
anything, which makes a wrong answer here worse than a wrong answer in the
product: it does not break a run, it certifies one. Both defects pinned below
were of exactly that shape -- a report of success for work that was not done.

Only the cheap parts are exercised. Actually running the harness means running
the suite once per mutation, and a test that costs ten minutes is a test nobody
runs.
"""

from __future__ import annotations

import ast
import subprocess

import mutate


def offered(source: str) -> list[str]:
    """Every mutation `--all-functions` would try, as text."""
    return [m.description for m in mutate.candidates(source, None)]


class TestWhatAFinishedRunReports:
    """Pinned by review of the --all-functions work (PR #49).

    Both reviewers found the same hole independently: the run exited 0 whenever
    nothing survived, including when mutations had been skipped rather than
    tried, so an audit that scored 600 of 743 and gave up on the rest reported
    a clean sweep.
    """

    def test_a_clean_sweep_is_success(self):
        assert mutate.verdict([], []) == 0

    def test_survivors_are_a_failure(self):
        survivor = mutate.Mutation(index=0, line=1, description="True -> False")
        assert mutate.verdict([survivor], []) == 1

    def test_an_unscored_mutant_is_not_a_clean_sweep(self):
        """The defect: no survivors, but nothing ran either."""
        skipped = mutate.Mutation(index=0, line=1, description="True -> False")
        assert mutate.verdict([], [(skipped, "still running after 120s")]) == mutate.EXIT_UNSOUND
        assert mutate.EXIT_UNSOUND != 0

    def test_an_unscored_mutant_outranks_survivors(self):
        """Reported as unsound rather than as an ordinary failure: the survivor
        list cannot be read as complete when part of the run never happened."""
        one = mutate.Mutation(index=0, line=1, description="True -> False")
        two = mutate.Mutation(index=1, line=2, description="1 -> 2")
        assert mutate.verdict([one], [(two, "pytest exited 3")]) == mutate.EXIT_UNSOUND


class TestWhatIsOfferedForMutation:
    ENTRYPOINT = (
        'def main():\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n'
    )

    def test_the_entrypoint_guard_is_never_flipped(self):
        """Flipping `==` to `!=` there runs main() on *import*, so pytest exits
        during collection and the mutant can never be scored -- a guaranteed
        non-result at the price of a full test run.

        It aborted the first --all-functions audit of gitwork.py at mutation
        743 of 744, discarding seven workers' results.
        """
        assert "Eq -> NotEq" not in offered(self.ENTRYPOINT)

    def test_the_main_literal_is_still_offered(self):
        """The other half of that decision, and the reason it is not simply
        "skip the whole statement": emptying the string stops
        `python3 gitwork.py --status` doing anything, which the suite drives as
        a subprocess and does notice."""
        assert "'__main__' -> ''" in offered(self.ENTRYPOINT)

    def test_a_comparison_against_name_elsewhere_is_still_offered(self):
        """The exclusion is the entrypoint guard, not every mention of
        `__name__`. Matching too widely would silently stop auditing real code."""
        source = 'def f(x):\n    return x.__name__ == "expected"\n'
        assert "Eq -> NotEq" in offered(source)

    def test_docstrings_are_never_emptied(self):
        source = 'def f():\n    """A docstring."""\n    return "a value"\n'
        assert offered(source) == ["'a value' -> ''"]


class TestATimedOutMutantIsNotAKill:
    def test_run_tests_reports_a_timeout_as_no_result(self, monkeypatch, tmp_path):
        """A mutant can turn a loop bound around -- `fetch_bytes` reads its
        response in a `while` -- and without a limit one stalls a worker for the
        rest of the audit, silently, since nothing prints until a batch ends.

        None, not a CompletedProcess: a timeout is the absence of a result, and
        anything that looked like an exit code would be scored as one.
        """

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="pytest", timeout=1)

        monkeypatch.setattr(mutate.subprocess, "run", timeout)
        subject = mutate.SUBJECTS["shared"]
        assert mutate.run_tests(tmp_path, subject, timeout=1) is None


class TestTheHarnessAgreesWithItself:
    def test_every_offered_mutation_can_be_applied(self):
        """`candidates` and `mutated_source` walk the tree twice, independently.
        If they ever disagreed about which mutation is which, the report would
        name one change and the run would have made another.
        """
        source = (mutate.REPO / mutate.SUBJECTS["shared"].path).read_text(encoding="utf-8")
        found = mutate.candidates(source, None)
        assert found, "the audit would be vacuous with nothing to mutate"
        for mutation in found:
            mutated = mutate.mutated_source(source, mutation.index, None)
            assert mutated != ast.unparse(ast.parse(source)), (
                f"mutation {mutation.index} ({mutation.description}) changed nothing"
            )
