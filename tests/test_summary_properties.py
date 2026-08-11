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

import re

from hypothesis import given, settings
from hypothesis import strategies as st
from test_render_summary import FULL_FACTS

import shared
import summary

PROPERTY = settings(deadline=None, max_examples=150)

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
MUST_NOT_SURVIVE = (
    "\n",  # forges a row
    "\r",
    "\x1b",  # repaints the terminal
    "\x9b",  # the same, as a single C1 codepoint
    "\x00",
    "\x7f",
    chr(0x202E),  # right-to-left override
    chr(0x200B),  # zero-width space
    chr(0x2028),  # str.splitlines() treats this as a line break
    chr(0xFEFF),
    chr(0xE0041),  # tag block: hidden "A"
    chr(0xFE0F),  # variation selector
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
        for char in MUST_NOT_SURVIVE:
            assert char not in cleaned

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

    @PROPERTY
    @given(value=POISONED)
    def test_a_hostile_commit_subject_cannot_add_a_line(self, value):
        """The subject is typed by a person and echoed back for approval. A
        newline in it would let them write a row the tool never emitted."""
        baseline = self._rendered(self._poison(("commit", "subject"), "ordinary subject"))
        poisoned = self._rendered(self._poison(("commit", "subject"), value))
        assert len(poisoned.splitlines()) == len(baseline.splitlines())

    @PROPERTY
    @given(value=POISONED)
    def test_a_hostile_detected_name_cannot_add_a_line(self, value):
        """These come from scanning the repository, so the file names in them
        are chosen by whoever wrote the repository."""
        baseline = self._rendered(self._poison(("scan", "detected"), ["ordinary.json"]))
        poisoned = self._rendered(self._poison(("scan", "detected"), [value]))
        assert len(poisoned.splitlines()) == len(baseline.splitlines())

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
