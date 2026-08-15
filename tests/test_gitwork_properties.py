"""What `push-plan` promises, stated once and checked against generated input.

Most of gitwork.py talks to git, and is rightly tested against real repositories
next door. A handful of functions are pure, and they carry the decisions the
agent driving this skill acts on: the sentence the user is shown before
approving a push, the flag that says a push is even possible, and the boundary
that stops a repository-supplied name reaching git as a flag.

Two of these are structural rather than generative -- they read gitwork.py's own
source to find every action it can emit, so an action added without a guidance
sentence fails here instead of showing somebody the word `diverged` where an
explanation should be.
"""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import gitwork

# Same reasoning as tests/test_merge_properties.py: pure functions, microseconds
# per example, and a deadline that would only ever measure the CI runner.
# deadline only: `derandomize` and `max_examples` come from the profile
# conftest.py loads, so the gate and the scheduled search can differ in
# budget without three files disagreeing about it.
PROPERTY = settings(deadline=None)

REMOTES = st.sampled_from(["origin", "upstream", "fork", "gh"])
URLS = st.sampled_from(
    [
        "git@github.com:someone/repo.git",
        "https://github.com/someone/repo.git",
        "https://gitlab.example.invalid/team/repo",
        "/srv/git/repo.git",
    ]
)
BRANCHES = st.sampled_from(["main", "master", "feature/a-b", "release/1.x", "wip"])


def actions_in_source() -> set[str]:
    """Every `action` value gitwork.py assigns, read from the file itself.

    A hand-kept list here would be one more thing to forget. This finds both
    spellings the module uses -- a literal in a dict and an assignment to
    `plan["action"]` -- so a new outcome cannot be added without this noticing.
    """
    source = Path(gitwork.__file__).read_text(encoding="utf-8")
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "action"
                    and isinstance(value, ast.Constant)
                ):
                    found.add(str(value.value))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "action"
                ):
                    found.add(str(node.value.value))
    assert found, "found no action values in gitwork.py -- has the shape changed?"
    return found


class TestEveryOutcomeHasSomethingToSay:
    """`describe` produces the sentence a user reads before approving a push.

    `ACTION_GUIDANCE.get(action, action)` falls back to the bare action string,
    so a missing entry is not an error -- it silently shows somebody the word
    `stop-behind-only` where an explanation should be.
    """

    def test_every_action_the_module_can_emit_has_a_guidance_sentence(self):
        missing = sorted(actions_in_source() - set(gitwork.ACTION_GUIDANCE))
        assert missing == []

    def test_no_action_falls_back_to_its_own_name(self):
        for action in sorted(actions_in_source()):
            plan = gitwork.describe({"action": action})
            assert plan["guidance"] != action, f"{action} has no sentence, only its own name"
            assert len(str(plan["guidance"])) > len(action)

    def test_push_is_permitted_for_exactly_the_actions_the_push_path_executes(self):
        """`PUSH_PERMITTED` must agree with the code that does the pushing.

        Comparing `describe`'s answer against `PUSH_PERMITTED` would prove
        nothing: `describe` computes it from that same set, so an action wrongly
        dropped from it changes both sides together. The independent source is
        `cmd_push`, which branches on the action and dies on anything it does
        not recognise. If the two disagree, SKILL.md's procedure skips a push
        the code would have carried out, or offers one it would refuse.

        Every `stop-` action is excluded because `cmd_push` refuses the whole
        prefix before those comparisons are reached.
        """
        executed = {
            node.comparators[0].value
            for node in ast.walk(ast.parse(Path(gitwork.__file__).read_text(encoding="utf-8")))
            if isinstance(node, ast.FunctionDef) and node.name == "cmd_push"
            for node in ast.walk(node)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "action"
            and isinstance(node.ops[0], ast.Eq)
            and isinstance(node.comparators[0], ast.Constant)
        }
        executed = {action for action in executed if not str(action).startswith("stop-")}
        assert executed == gitwork.PUSH_PERMITTED

    def test_the_flag_reports_what_the_permitted_set_says(self):
        for action in sorted(actions_in_source() | set(gitwork.ACTION_GUIDANCE)):
            plan = gitwork.describe({"action": action})
            assert plan["permits_push"] is (action in gitwork.PUSH_PERMITTED)

    def test_an_unknown_action_does_not_claim_a_push_is_possible(self):
        """Fails closed: a value from a newer version, or a corrupted plan,
        must not be read as permission."""
        assert gitwork.describe({"action": "something-new"})["permits_push"] is False

    @PROPERTY
    @given(
        action=st.sampled_from(sorted(gitwork.ACTION_GUIDANCE)),
        remote=REMOTES,
        url=URLS,
        branch=BRANCHES,
    )
    def test_describing_a_plan_never_raises_and_never_says_nothing(
        self, action, remote, url, branch
    ):
        """Every guidance string is run through `.format(dest=...)`, so a stray
        brace in one of them would raise on the plan that used it -- at the
        moment the user is waiting to be told what a push would do."""
        plan = gitwork.describe(
            {
                "action": action,
                "remote": remote,
                "remote_url": url,
                "merge_ref": f"refs/heads/{branch}",
            }
        )
        assert str(plan["guidance"]).strip()


class TestDestinationAlwaysNamesWhereCodeWouldGo:
    """The documented property: a remote's nickname says nothing about where
    code goes, so the URL is named whenever one is known."""

    @PROPERTY
    @given(remote=REMOTES, url=URLS, branch=BRANCHES)
    def test_an_upstream_plan_names_the_branch_and_the_url(self, remote, url, branch):
        dest = gitwork.destination(
            {"remote": remote, "remote_url": url, "merge_ref": f"refs/heads/{branch}"}
        )
        assert url in dest
        assert f"{remote}/{branch}" in dest
        # refs/heads/ is a git implementation detail, not something to show.
        assert "refs/heads/" not in dest

    @PROPERTY
    @given(remote=REMOTES, url=URLS)
    def test_a_settled_first_push_names_the_url(self, remote, url):
        dest = gitwork.destination({"remote": remote, "remote_urls": {remote: url}})
        assert remote in dest
        assert url in dest

    @PROPERTY
    @given(urls=st.dictionaries(REMOTES, URLS, min_size=2, max_size=4))
    def test_an_unsettled_first_push_names_every_candidate_with_its_url(self, urls):
        """`remote` is null when several remotes exist and none is `origin`.
        The user is about to be asked which -- so every candidate has to arrive
        with its URL, not just its nickname.

        Each pair is asserted as a unit. Checking that every name appears and
        every URL appears would also pass if the two lists were paired up
        wrongly -- sorted independently, say -- which is the one mistake here
        that would actively mislead: the right names beside the wrong URLs.
        """
        dest = gitwork.destination({"remote": None, "remote_urls": urls})
        for name, url in urls.items():
            assert f"{name} ({url})" in dest

    @PROPERTY
    @given(
        plan=st.fixed_dictionaries(
            {},
            optional={
                "remote": st.one_of(st.none(), REMOTES),
                "remote_url": URLS,
                "remote_urls": st.dictionaries(REMOTES, URLS, max_size=3),
                "merge_ref": st.builds(lambda b: f"refs/heads/{b}", BRANCHES),
            },
        )
    )
    def test_it_never_returns_nothing(self, plan):
        """Whatever the plan carries or omits, there is always something to
        show. An empty destination would render as "it would go to "."""
        assert gitwork.destination(plan).strip()


class TestTheOptionBoundaryHolds:
    """`safe_ref` and `safe_token` exist because a remote or branch name comes
    out of repository config, which a checked-out repository controls."""

    @PROPERTY
    @given(
        value=st.text(
            alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=24
        )
    )
    def test_a_value_is_refused_if_and_only_if_it_looks_like_an_option(self, value):
        looks_like_an_option = value.startswith("-")
        if looks_like_an_option:
            with pytest.raises(SystemExit):
                gitwork.safe_ref(value)
        else:
            assert gitwork.safe_ref(value) == value

    @PROPERTY
    @given(
        value=st.text(
            alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=24
        ),
        what=st.sampled_from(["remote", "branch", "ref"]),
    )
    def test_the_same_boundary_guards_remotes_and_branches(self, value, what):
        if value.startswith("-"):
            with pytest.raises(SystemExit):
                gitwork.safe_token(value, what)
        else:
            assert gitwork.safe_token(value, what) == value


class TestRecordedPushKeepsTheWholeBranchName:
    """Not generative: one decision, documented in the code, worth pinning."""

    def test_a_branch_with_slashes_survives_intact(self, tmp_path):
        """`refs/heads/feature/foo` is the branch `feature/foo`, not `foo`.

        The code says `removeprefix` and not `rsplit` and explains why; this is
        the test that makes the explanation binding.
        """
        facts = tmp_path / "facts.json"
        facts.write_text(json.dumps({"tool": gitwork.FACTS_TOOL}), encoding="utf-8")
        args = argparse.Namespace(facts=str(facts))

        gitwork.record_push(
            args,
            {"merge_ref": "refs/heads/feature/foo", "remote": "origin"},
            "abc1234",
        )

        push = json.loads(facts.read_text(encoding="utf-8"))["commit"]["push"]
        assert push == {"sha": "abc1234", "remote": "origin", "branch": "feature/foo"}


class TestGapsFoundByMutationAudit:
    """Diagnostics the suite produced without ever reading, found by
    `python3 tests/mutate.py --subject gitwork`.

    Every survivor in that run was a string literal, and every one of them is
    shown to a person at a decision point: where a push would land, and what
    kind of value was refused. Asserting that a message merely exists leaves its
    wording free to become anything, including nothing.
    """

    def test_an_upstream_destination_reads_as_a_place(self):
        """Survived: the `" ("` and `")"` around the URL, separately.

        Dropping them leaves the URL in the string, so a substring check still
        passes -- and the user is shown `origin/maingit@github.com:x/y.git`.
        """
        dest = gitwork.destination(
            {
                "remote": "origin",
                "remote_url": "git@github.com:someone/repo.git",
                "merge_ref": "refs/heads/main",
            }
        )
        assert dest == "origin/main (git@github.com:someone/repo.git)"

    def test_a_settled_first_push_reads_as_a_place(self):
        """Survived: the same two, on the branch below it."""
        dest = gitwork.destination(
            {"remote": "fork", "remote_urls": {"fork": "https://example.invalid/x.git"}}
        )
        assert dest == "fork (https://example.invalid/x.git)"

    def test_an_upstream_without_a_known_url_is_still_a_sentence(self):
        """The other side of that conditional: no URL to name, and the result
        must still read as a destination rather than trailing an empty pair."""
        dest = gitwork.destination({"remote": "origin", "merge_ref": "refs/heads/x"})
        assert dest == "origin/x"

    def test_the_unsettled_list_reads_as_a_choice(self):
        """Survived: the `", "` between candidates and the `"one of "` prefix.

        Without the separator the pairs run together; without the prefix the
        sentence stops saying that a choice is coming. Both survive a check that
        only looks for each name beside its own URL.
        """
        dest = gitwork.destination(
            {
                "remote": None,
                "remote_urls": {"origin": "https://a.invalid/r", "fork": "https://b.invalid/r"},
            }
        )
        assert dest == (
            "one of fork (https://b.invalid/r), origin (https://a.invalid/r)"
            " — not settled yet, a follow-up question will confirm"
        )

    def test_a_refused_ref_says_what_kind_of_value_it_was(self, capsys):
        """Survived: the `"ref"` label emptied.

        `refuse_option_like` builds its message from that word, so an emptied
        one leaves "refusing  that looks like an option" and the caller is not
        told what was wrong.
        """
        with pytest.raises(SystemExit):
            gitwork.safe_ref("--output=/etc/passwd")
        assert "refusing ref that looks like an option" in capsys.readouterr().err
