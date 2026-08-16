"""The one safety property the README states and nothing checked.

    no repo- or API-derived text can forge a line or an escape sequence
    in the summary

That is a claim about *every* value, and the values are not ours: file names
come from the repository, template names and section headings from the API, the
commit subject from a person, and error text straight from a remote server. Any
of them can contain a newline, an ESC, a bidi override, or a codepoint that
carries hidden ASCII.

`shared.clean` is the whole defence and `summary.render` is what depends on it,
so the properties are split the same way: what the sanitiser guarantees, and
what the renderer guarantees given it.

Both are pure. No repository, no network, no subprocess.
"""

from __future__ import annotations

import copy
import re
from typing import ClassVar

from hypothesis import given, settings
from hypothesis import strategies as st
from test_render_summary import FULL_FACTS

import shared
import summary

# deadline only: `derandomize` and `max_examples` come from the profile
# conftest.py loads, so the gate and the scheduled search can differ in
# budget without three files disagreeing about it.
PROPERTY = settings(deadline=None)

# Everything the sanitiser exists to remove, sampled directly so the generator
# cannot fail to produce one. Each entry is a real technique rather than a
# curiosity: a newline forges a row, ESC repaints the terminal, RLO reverses
# what the reader sees, the tag block smuggles ASCII inside an innocent string.
#
# Written out here rather than derived from `shared.CONTROL_CHARS`, and that is
# the whole point: a property that asks the module's own pattern whether the
# module's own pattern caught everything cannot fail. Narrowing the pattern
# would narrow the check with it. This list is the independent statement of what
# must never survive.
#
# Written as the complete ranges rather than one example per technique. A
# representative list would miss a range narrowed at its boundary -- allowing
# U+2066 while still rejecting U+2069 -- and those characters reorder text just
# as well as the ones that were sampled.
FORBIDDEN_RANGES = (
    (0x0000, 0x001F),  # C0, which includes newline, carriage return and ESC
    (0x007F, 0x009F),  # DEL and the C1 block, where U+009B is a one-character CSI
    (0x061C, 0x061C),  # Arabic letter mark
    (0x200B, 0x200F),  # zero-width spaces and joiners, and the bidi marks
    (0x2028, 0x2029),  # line and paragraph separators: str.splitlines() breaks on these
    (0x202A, 0x202E),  # bidi embeddings and overrides
    (0x2060, 0x2064),  # word joiner and the invisible operators
    (0x2066, 0x2069),  # bidi isolates
    (0xFE00, 0xFE0F),  # variation selectors
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
    (0xE0000, 0xE007F),  # the tag block, which can carry hidden ASCII
    (0xE0100, 0xE01EF),  # variation selectors, supplement
)
MUST_NOT_SURVIVE = tuple(
    chr(code) for start, end in FORBIDDEN_RANGES for code in range(start, end + 1)
)
HOSTILE_CHAR = st.sampled_from(MUST_NOT_SURVIVE)

# The same characters as they would actually arrive: an escape sequence is an
# ESC and the letters after it, and only the ESC is a control character.
HOSTILE = st.one_of(HOSTILE_CHAR, st.sampled_from(["\x1b[31m", "\x9b31m"]))

# Text a person or a repository would plausibly produce, with no leading or
# trailing whitespace of its own so that `clean`'s strip is not doing the work.
ORDINARY = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=30
)

# An ordinary value with something hostile buried in it, which is the shape a
# real attempt would take -- not a field made entirely of control characters.
POISONED = st.builds(
    lambda before, bad, after: f"{before}{bad}{after}", ORDINARY, HOSTILE, ORDINARY
)


class TestTheSanitiserGuarantee:
    """What `clean` promises about any single value."""

    @PROPERTY
    @given(value=st.one_of(POISONED, ORDINARY, st.text(max_size=40)))
    def test_nothing_it_returns_can_forge_anything(self, value):
        """Checked twice, against two different statements of the rule.

        `CONTROL_CHARS` is the module's own pattern, so asking it whether the
        module caught everything proves only that the code agrees with itself --
        narrowing the pattern narrows the check with it. Defect this pins:
        removing the bidi range from `_INVISIBLE` left this test passing.
        `MUST_NOT_SURVIVE` is the independent half.
        """
        cleaned = shared.clean(value)
        assert not shared.CONTROL_CHARS.search(cleaned)
        assert not set(cleaned) & set(MUST_NOT_SURVIVE)

    def test_every_forbidden_codepoint_is_neutralised(self):
        """Not sampled -- all of them, one at a time.

        Hypothesis draws from `MUST_NOT_SURVIVE`, so a range narrowed at its
        boundary would be found only if that particular codepoint happened to be
        drawn. There are around seven hundred of them and `clean` is a regex
        substitution, so checking every one costs less than a second.
        """
        survived = [
            f"U+{ord(char):04X}"
            for char in MUST_NOT_SURVIVE
            if char in shared.clean(f"before{char}after")
        ]
        assert survived == []

    @PROPERTY
    @given(value=st.one_of(POISONED, st.text(max_size=40)))
    def test_it_never_makes_a_value_longer(self, value):
        """Column alignment is computed from these lengths. A sanitiser that
        expanded a value -- escaping rather than replacing, say -- would push
        every column after it out of line, which is its own kind of forgery."""
        assert len(shared.clean(value)) <= len(value)

    @PROPERTY
    @given(value=st.one_of(POISONED, ORDINARY, st.text(max_size=40)))
    def test_running_it_twice_changes_nothing(self, value):
        once = shared.clean(value)
        assert shared.clean(once) == once

    @PROPERTY
    @given(value=ORDINARY)
    def test_an_ordinary_value_survives_intact(self, value):
        """The property that stops the others being satisfied by deleting
        everything. A sanitiser returning "" would pass every check above."""
        assert shared.clean(value) == value

    @PROPERTY
    @given(before=ORDINARY, bad=HOSTILE_CHAR, after=ORDINARY)
    def test_a_neutralised_character_leaves_a_gap_rather_than_closing_one(self, before, bad, after):
        """Replaced with a space, not deleted.

        `a\\nb` has to read as two words rather than silently becoming `ab`,
        which is a different name -- and the whole reason the code says
        `sub(" ", ...)` rather than `sub("", ...)`.

        Defect this pins: the first version asserted only that the result
        started with `before` and ended with `after`, which deletion satisfies
        too. Exact equality is the claim. One hostile character, not an escape
        sequence, because only its ESC is a control character and the letters
        after it legitimately survive.
        """
        assert shared.clean(f"{before}{bad}{after}") == f"{before} {after}"


class TestTheSummaryCannotBeForged:
    """What `render` promises given that sanitiser, on the document as a whole.

    Every field is filled from the same fixture the example-based tests use, so
    a failure here is about the hostile value rather than about a shape the
    renderer has never seen.
    """

    @staticmethod
    def _rendered(facts):
        return summary.render(facts, summary.Pal(False))

    @staticmethod
    def _poison(field, value):
        """FULL_FACTS with one repo-derived field replaced."""
        facts = {
            key: dict(section) for key, section in FULL_FACTS.items() if isinstance(section, dict)
        }
        facts.update({k: v for k, v in FULL_FACTS.items() if not isinstance(v, dict)})
        section, key = field
        facts[section] = {**facts[section], key: value}
        return facts

    # Every value the renderer takes from outside this tool, written as the path
    # it arrives on. `foo[]` is a list, and a path continuing past it addresses a
    # dict inside that list.
    #
    # Enumerated rather than sampled, because the guarantee is about *every*
    # value: a path missing from this list is a field nothing checks. The first
    # version poisoned three fields and left the rest to the golden fixtures,
    # which use benign strings -- so dropping `clean` from `commit.push.remote`
    # or from a recommendation's `reason` changed no test at all.
    EXTERNAL_PATHS: tuple[str, ...] = (
        "title",
        # Renders only when it differs from `commit.choice`, which every poison
        # value here does -- so it reaches the output on its own, with no gate.
        "requested_action",
        "notes[]",
        "scan.detected[]",
        "scan.prev_templates_count",
        "scan.custom_lines",
        "templates.total",
        "templates.always_on[]",
        "templates.recommended[].name",
        "templates.recommended[].reason",
        "templates.carried_over[]",
        "templates.added[]",
        "templates.removed[]",
        "merge.esc_bytes",
        "merge.custom_kept",
        "merge.custom_removed[].line",
        "merge.custom_removed[].covered_by",
        "review.negations[]",
        "review.broad[]",
        "write.path",
        "write.reason",
        "commit.choice",
        "commit.subject",
        "commit.hash",
        "commit.scope",
        "commit.untouched",
        "commit.push.remote",
        "commit.push.branch",
        "commit.push.sha",
        "net.prev_count",
        "net.new_count",
        "net.diffstat",
    )

    # Values that decide only whether a field is reached at all: an overwrite
    # reason is not rendered for a newly created file, and `esc_bytes` appears
    # only when the block is verbatim. Set on every case so each path above is
    # live -- and `test_every_field_in_that_list_actually_reaches_the_output` is
    # what proves they are, rather than this comment.
    GATES: tuple[tuple[str, str, object], ...] = (
        ("write", "mode", "overwrite"),
        ("merge", "verbatim", True),
        ("scan", "gitignore", "existing"),
    )

    @classmethod
    def _with(cls, path: str, value: object):
        """FULL_FACTS with the value at `path` replaced.

        Deep-copied, so poisoning a nested field can never leak into the next
        example through a shared dict.
        """
        facts = copy.deepcopy(dict(FULL_FACTS))
        for section, key, gate in cls.GATES:
            facts[section] = {**(facts.get(section) or {}), key: gate}

        node = facts
        segments = path.split(".")
        for index, segment in enumerate(segments):
            listed = segment.endswith("[]")
            key = segment[:-2] if listed else segment
            if index == len(segments) - 1:
                node[key] = [value] if listed else value
            elif listed:
                # One entry is enough: the property is about a single value, and
                # a second would only test the same code path twice.
                child: dict = {}
                node[key] = [child]
                node = child
            else:
                child = dict(node.get(key) or {})
                node[key] = child
                node = child
        return facts

    def test_no_external_field_can_forge_anything_with_any_forbidden_codepoint(self):
        """The whole grid: every path against every codepoint, not a sample.

        A property drawing one (path, codepoint) pair per example cannot cover
        thirty-one paths times four hundred-odd codepoints in 150 examples, so a
        field that lost its `clean` would be caught only if that exact pair
        happened to be drawn. Measured, not assumed: with the sampled version
        alone, removing `clean` from `write.path`, from `merge.esc_bytes` and
        from a recommendation's `reason` left the suite green.

        The grid is about fourteen thousand renders and runs in under a second,
        which is cheaper than reasoning about whether sampling was lucky.
        """
        failures = []
        for path in self.EXTERNAL_PATHS:
            baseline = len(self._rendered(self._with(path, "ordinary")).splitlines())
            for char in MUST_NOT_SURVIVE:
                rendered = self._rendered(self._with(path, f"before{char}after"))
                forged = len(rendered.splitlines()) != baseline or any(
                    shared.SUSPICIOUS_CHARS.search(line) for line in rendered.splitlines()
                )
                if forged:
                    failures.append(f"{path} U+{ord(char):04X}")
        assert not failures, f"{len(failures)} forged: {failures[:10]}"

    @PROPERTY
    @given(field=st.sampled_from(EXTERNAL_PATHS), value=POISONED)
    def test_no_externally_derived_field_can_forge_a_line_in_ordinary_text(self, field, value):
        """What the grid above does not vary: the text around the hostile
        character. A real value is a file name or a commit subject with
        something buried in it, not `before<char>after`, and a renderer that
        handled the fixed shape but not an arbitrary one would pass the grid."""
        baseline = self._rendered(self._with(field, "ordinary"))
        poisoned = self._rendered(self._with(field, value))
        assert len(poisoned.splitlines()) == len(baseline.splitlines())

    def test_every_field_in_that_list_actually_reaches_the_output(self):
        """The two properties above are worth nothing for a field whose value
        never arrives. A wrong key or a wrong shape here would not fail them --
        it would quietly stop testing that field, which is the failure mode this
        whole file exists to avoid."""
        missing = [
            path
            for path in self.EXTERNAL_PATHS
            if "MARKERVALUE" not in self._rendered(self._with(path, "MARKERVALUE"))
        ]
        assert missing == []

    @PROPERTY
    @given(subject=POISONED, detected=POISONED, title=POISONED)
    def test_no_escape_sequence_reaches_the_output(self, subject, detected, title):
        """The README's claim, stated as one assertion over three fields at
        once: whatever arrives, the rendered summary carries nothing that could
        repaint a terminal or reorder what the reader sees."""
        facts = self._poison(("commit", "subject"), subject)
        facts = {**facts, "scan": {**facts["scan"], "detected": [detected]}, "title": title}
        rendered = self._rendered(facts)

        assert "\x1b" not in rendered
        # Newlines are structural here, so the file-content pattern is the right
        # one: it allows the line endings the document is made of and nothing
        # else. `render` builds lines, so anything it kept is inside one.
        for line in rendered.splitlines():
            assert not shared.SUSPICIOUS_CHARS.search(line)

    @PROPERTY
    @given(value=POISONED)
    def test_the_value_column_does_not_move(self, value):
        """A forgery does not have to add a line.

        The summary is a column layout, and every label row within a section
        puts its value at the same offset. A value that could shift that offset
        would let one field's text begin where another field's label ends --
        which reads as a row the tool never wrote.

        Compared against the same document with a benign subject rather than
        asserted absolutely: the column depends on the longest label, which is
        the fixture's business, not this property's.
        """
        benign = self._rendered(self._poison(("commit", "subject"), "ordinary subject"))
        poisoned = self._rendered(self._poison(("commit", "subject"), value))

        def value_offsets(text: str) -> list[int]:
            # A label row is two spaces, a label, a run of spaces, then a value.
            return [
                match.end()
                for line in text.splitlines()
                if (match := re.match(r"^ {2}\S[^ ]*(?: \S[^ ]*)* +", line))
            ]

        assert value_offsets(poisoned) == value_offsets(benign)


class TestAFailedPushCannotBeForgedEither:
    """The same grid, on the other document shape.

    `commit.push.status` and `.reason` exist only in a run whose push did *not*
    land -- a successful push has a sha, and the sha branch renders instead. So
    they cannot be reached from FULL_FACTS, which is a successful run, and a
    path added to the list above would fail
    `test_every_field_in_that_list_actually_reaches_the_output` rather than
    check anything. Two shapes, two fixtures, one guarantee.

    `reason` matters most: it is the only one of the two that is a sentence, and
    it is composed from the plan -- including a remote name the repository
    supplies and a `--remote` value the caller does.
    """

    BASE: ClassVar[dict] = {
        "requested_action": "commit + push",
        "commit": {
            "choice": "commit only",
            "hash": "726bc13",
            "push": {"status": "not attempted", "remote": "origin", "branch": "main"},
        },
    }
    # `reason` is displayed. `status` is only ever compared against a literal,
    # so it must reach the output *nowhere* -- both are checked below, because
    # "it is only a discriminator" is a claim about today's renderer.
    DISPLAYED = ("reason",)
    PATHS = ("status", "reason")

    @classmethod
    def _with(cls, key: str, value: object):
        base = copy.deepcopy(cls.BASE)
        base["commit"]["push"][key] = value
        return base

    @staticmethod
    def _rendered(facts):
        return summary.render(facts, summary.Pal(False))

    def test_every_displayed_field_here_actually_reaches_the_output(self):
        marker = "MARKER" + "VALUE"
        missing = [
            key for key in self.DISPLAYED if marker not in self._rendered(self._with(key, marker))
        ]
        assert missing == []

    def test_the_status_is_a_discriminator_and_never_reaches_the_output(self):
        marker = "MARKER" + "VALUE"
        assert marker not in self._rendered(self._with("status", marker))

    def test_no_field_of_a_failed_push_can_forge_anything(self):
        failures = []
        for key in self.PATHS:
            baseline = len(self._rendered(self._with(key, "ordinary")).splitlines())
            for char in MUST_NOT_SURVIVE:
                rendered = self._rendered(self._with(key, f"before{char}after"))
                if len(rendered.splitlines()) != baseline or any(
                    shared.SUSPICIOUS_CHARS.search(line) for line in rendered.splitlines()
                ):
                    failures.append(f"commit.push.{key} U+{ord(char):04X}")
        assert not failures, f"{len(failures)} forged: {failures[:10]}"

    @PROPERTY
    @given(field=st.sampled_from(PATHS), value=POISONED)
    def test_no_field_of_a_failed_push_can_forge_a_line_in_ordinary_text(self, field, value):
        baseline = self._rendered(self._with(field, "ordinary"))
        assert len(self._rendered(self._with(field, value)).splitlines()) == len(
            baseline.splitlines()
        )
