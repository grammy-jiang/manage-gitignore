"""The merge's invariants, stated once and checked against generated input.

Every other test in this suite is example-based: a fixture is written by hand,
the code is run on it, and the output is compared against another hand-written
string. That finds the mistakes somebody thought of. The README already states
what must be true of *every* input --

    the template block written verbatim, and the repo's own custom rules intact

-- and that is a property, not an example. Hypothesis generates the inputs
nobody would sit down and type: a rule repeated, two rules differing only by a
trailing space, a custom rule byte-identical to a line inside the fetched block,
a negation, a comment that looks like a marker.

Only the pure functions are exercised here, called in-process. They need no
repository, no network and no subprocess, so an example costs microseconds and a
few hundred of them cost less than one of the subprocess tests next door.

When one of these fails, read the shrunk counterexample Hypothesis prints rather
than the first failure it found: it reduces the input to the smallest one that
still breaks, which is usually the whole diagnosis.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

import templates
from conftest import api_block

# One place to tune the search, rather than thirteen.
#
# 150 rather than the default 100, and not more, because it was measured rather
# than guessed. This file alone: 3.3s at 100, 4.2s at 150, 7.8s at 300. The
# suite as a whole goes from 31s to 35s. Beyond that the search buys less than
# it costs -- these thirteen properties were run at 4000 examples each while
# being written, 52,000 cases, and found nothing that 150 does not reach.
#
# deadline=None because the per-example time limit measures a shared CI runner
# rather than the code -- a 200ms budget on a function that takes microseconds
# only ever fires when the machine hiccups, and a test that fails for that
# reason teaches nobody anything.
PROPERTY = settings(deadline=None, max_examples=150)

# The templates conftest.py has canned bodies for. Two of them overlap on
# `.env`, which is the interesting case for de-duplication.
TEMPLATE_NAMES = ("git", "node", "python", "vim", "dotenv")

# Printable ASCII minus space. Two reasons, and the first is not a convenience:
# `str.splitlines()` breaks on far more than "\n" -- U+000B, U+001C, U+0085 and
# U+2028 among them -- so a custom rule containing one of those would become two
# lines the moment the file was read back, and no such rule can exist in a
# .gitignore that was parsed from disk. This range excludes every one of them by
# construction. The second reason is that it carries no leading or trailing
# whitespace to reason about and has a low rejection rate, which keeps
# Hypothesis clear of its health checks. Non-ASCII rules come from the sampled
# list below instead.
_DENSE = st.characters(min_codepoint=33, max_codepoint=126)

_bare_pattern = st.one_of(
    # Realistic rules, including ones the canned bodies also contain, so the
    # "already covered by the block" path is hit often rather than by luck.
    st.sampled_from(
        [
            "*.log",
            "build/",
            "!keep.log",
            "node_modules/",
            "*.py[cod]",
            ".env",
            "*.orig",
            "dist",
            "japanese/日本語/",
            "with space.txt",
        ]
    ),
    st.text(alphabet=_DENSE, min_size=1, max_size=20).filter(lambda s: not s.startswith("#")),
)

# Deliberately including surrounding whitespace. `dedup_custom` compares by
# stripped value but keeps the line as written, so "*.log" and "*.log " are one
# rule to the de-duplicator and two different lines in the file. That asymmetry
# is where an accounting bug would hide, and no hand-written fixture contains it
# because nobody types a trailing space on purpose.
pattern_line = st.one_of(
    _bare_pattern,
    st.builds(lambda rule, pad: rule + pad, _bare_pattern, st.sampled_from([" ", "  ", "\t"])),
    st.builds(lambda rule, pad: pad + rule, _bare_pattern, st.sampled_from([" ", "\t"])),
)

# What is deliberately NOT generated: a custom line spelling the block's own
# `# Created by <API>/...` marker. split_regions matches those byte for byte and
# would read such a line as the start of a template block -- which templates.py
# documents as the safe direction of that trade. A property that generated one
# would be asserting the opposite of the design.

# The lines that are not rules. dedup_custom must leave these alone, which is
# what makes "kept" and "removed" disagree about length in interesting ways.
noise_line = st.one_of(
    st.just(""),
    st.just("   "),
    st.builds(lambda body: f"# {body}", st.text(alphabet=_DENSE, max_size=15)),
)

custom_lines = st.lists(st.one_of(pattern_line, noise_line), max_size=12)
template_names = st.lists(st.sampled_from(TEMPLATE_NAMES), min_size=1, max_size=4, unique=True)
ANY_NAME = (*TEMPLATE_NAMES, "rust", "go")


def patterns_of(lines: list[str]) -> list[str]:
    """The rule lines, stripped -- the form dedup_custom compares by."""
    return [line.strip() for line in lines if templates.is_pattern_line(line)]


class TestClassifyPartitionsTheSet:
    """`classify`'s docstring claims the categories "always partition the set".

    A claim in a docstring is worth exactly as much as the test behind it.
    """

    @PROPERTY
    @given(
        wanted=st.lists(st.sampled_from(ANY_NAME), unique=True),
        recommended=st.dictionaries(
            st.sampled_from(ANY_NAME),
            st.sampled_from(["package.json", "Cargo.toml", "go.mod"]),
        ),
        previous=st.lists(st.sampled_from(ANY_NAME), unique=True),
    )
    def test_every_wanted_template_lands_in_exactly_one_group(self, wanted, recommended, previous):
        groups = templates.classify(wanted, recommended, previous)
        placed = (
            groups["always_on"]
            + [r["name"] for r in groups["recommended"]]
            + groups["carried_over"]
            + groups["added"]
        )
        assert sorted(placed) == sorted(wanted)

    @PROPERTY
    @given(
        wanted=st.lists(st.sampled_from(ANY_NAME), unique=True),
        previous=st.lists(st.sampled_from(ANY_NAME), unique=True),
    )
    def test_removed_is_exactly_what_the_previous_set_lost(self, wanted, previous):
        groups = templates.classify(wanted, {}, previous)
        assert groups["removed"] == [t for t in previous if t not in wanted]


class TestSplitRegionsRecoversWhatWasAssembled:
    """The round trip the whole design rests on: a file is a block plus custom
    rules, and reading it back must return the two halves it was built from."""

    @PROPERTY
    @given(names=template_names, custom=custom_lines)
    def test_the_block_and_the_custom_rules_come_back(self, names, custom):
        api = api_block(names)
        text = api if not custom else api.rstrip("\n") + "\n\n" + "\n".join(custom) + "\n"

        got_names, block, got_custom = templates.split_regions(text)

        assert got_names == names
        assert block[0].startswith(templates.CREATED)
        assert block[-1].startswith(templates.ENDOF)
        # Blank lines are structural -- assembling inserts one -- so the claim is
        # about the rules and comments, in order, not about the whitespace.
        assert [x for x in got_custom if x.strip()] == [x for x in custom if x.strip()]

    @PROPERTY
    @given(custom=st.lists(st.one_of(pattern_line, noise_line), min_size=1, max_size=12))
    def test_a_file_with_no_block_is_custom_from_top_to_bottom(self, custom):
        names, block, got_custom = templates.split_regions("\n".join(custom) + "\n")
        assert (names, block) == ([], [])
        assert got_custom == custom


class TestDedupCustomLosesNothing:
    """The rule that matters most: de-duplication may drop a rule only because
    something else already covers it, and must say which."""

    @PROPERTY
    @given(names=template_names, custom=custom_lines)
    def test_every_rule_is_either_kept_or_reported(self, names, custom):
        api = api_block(names)
        kept, removed = templates.dedup_custom(custom, api)

        accounted = patterns_of(kept) + [line.strip() for line, _ in removed]
        assert sorted(accounted) == sorted(patterns_of(custom))

    @PROPERTY
    @given(names=template_names, custom=custom_lines)
    def test_nothing_survives_twice_and_nothing_the_block_covers_survives(self, names, custom):
        api = api_block(names)
        kept, _ = templates.dedup_custom(custom, api)

        surviving = patterns_of(kept)
        assert len(set(surviving)) == len(surviving)
        assert set(surviving).isdisjoint(templates.api_pattern_sections(api))

    @PROPERTY
    @given(names=template_names, custom=custom_lines)
    def test_running_it_again_changes_nothing(self, names, custom):
        """Idempotence. A second pass finding more to remove would mean the
        first pass left the file in a state it considers wrong."""
        api = api_block(names)
        kept, _ = templates.dedup_custom(custom, api)

        again, removed_again = templates.dedup_custom(kept, api)
        assert again == kept
        assert removed_again == []

    @PROPERTY
    @given(names=template_names, custom=custom_lines)
    def test_what_survives_keeps_the_order_it_arrived_in(self, names, custom):
        api = api_block(names)
        kept, _ = templates.dedup_custom(custom, api)

        surviving = patterns_of(kept)
        first_seen: list[str] = []
        for rule in patterns_of(custom):
            if rule in surviving and rule not in first_seen:
                first_seen.append(rule)
        assert surviving == first_seen


class TestReapplyCustomCarriesTheEdit:
    """`reapply_custom` puts a user's uncommitted change back on top of this
    run's result. Its two obligations are that nothing they added is lost, and
    nothing they deleted comes back."""

    @PROPERTY
    @given(kept=custom_lines, base=custom_lines)
    def test_no_edit_means_no_change(self, kept, base):
        """The common case, and the one that must be exactly free: the user
        touched nothing, so the run's own result stands unaltered."""
        result, added, removed = templates.reapply_custom(kept, base, list(base))
        assert result == kept
        assert (added, removed) == ([], [])

    @PROPERTY
    @given(kept=custom_lines, base=custom_lines, theirs=custom_lines)
    def test_every_line_the_user_added_is_in_the_result(self, kept, base, theirs):
        result, added, _ = templates.reapply_custom(kept, base, theirs)
        for line in added:
            assert line in result

    @PROPERTY
    @given(kept=custom_lines, base=custom_lines, theirs=custom_lines)
    def test_the_reported_edit_comes_from_the_two_versions_it_compared(self, kept, base, theirs):
        """`added` and `removed` are what the user did, so every line in them
        has to have come from their version or the committed one. A line in
        neither would be this function inventing a rule."""
        _, added, removed = templates.reapply_custom(kept, base, theirs)
        assert set(added) <= set(theirs)
        assert set(removed) <= set(base)

    @PROPERTY
    @given(kept=custom_lines, base=custom_lines, theirs=custom_lines)
    def test_a_deletion_never_increases_how_often_a_line_appears(self, kept, base, theirs):
        """Stated as a count, not as absence: `kept` may legitimately hold a
        line twice, and honouring one deletion removes one copy."""
        result, _, removed = templates.reapply_custom(kept, base, theirs)
        for line in set(removed):
            assert result.count(line) <= kept.count(line) + theirs.count(line)


class TestTheWholeRebuild:
    """End to end across the pure half: take a file, rebuild it against a fresh
    block, and check the README's claim about the result."""

    @PROPERTY
    @given(old_names=template_names, new_names=template_names, custom=custom_lines)
    def test_a_rebuild_keeps_every_custom_rule_the_new_block_does_not_cover(
        self, old_names, new_names, custom
    ):
        old_api = api_block(old_names)
        existing = old_api.rstrip("\n") + "\n\n" + "\n".join(custom) + "\n" if custom else old_api

        _, old_custom = templates.split_existing(existing)
        new_api = api_block(new_names)
        kept, removed = templates.dedup_custom(old_custom, new_api)

        rebuilt = new_api.rstrip("\n") + "\n\n" + "\n".join(kept) + "\n" if kept else new_api
        rebuilt_names, _, rebuilt_custom = templates.split_regions(rebuilt)

        assert rebuilt_names == new_names
        # The block is verbatim: every line of the fetched response survives.
        assert new_api.rstrip("\n") in rebuilt

        covered = set(templates.api_pattern_sections(new_api))
        should_survive = {p for p in patterns_of(custom) if p not in covered}
        assert should_survive <= set(patterns_of(rebuilt_custom))

        # And every rule that did not survive was reported, with a reason.
        for _line, why in removed:
            assert why
