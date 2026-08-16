"""The shipped skill is read by three products, so its metadata is a contract.

Claude Code, Codex and GitHub Copilot all read the Agent Skills format
(<https://agentskills.io/specification>) and all three ignore fields they do not
know -- but the packaging and upload paths around them do not: a field outside
the spec is a hard error there, not a shrug. These tests hold `SKILL.md` to the
six fields the spec defines, and to the constraints it puts on each.

Parsed with a real YAML parser rather than by hand, deliberately. A hand-rolled
reader would agree with whatever this file happens to contain today and disagree
with the parsers that actually load the skill.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from manage_gitignore import cli

try:  # tomllib landed in 3.11; on 3.10 the checks needing it simply do not run
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    tomllib = None

SPEC_FIELDS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def frontmatter() -> dict[str, object]:
    text = (cli.skill_source() / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must open with YAML frontmatter"
    end = text.index("\n---\n", 3)
    return yaml.safe_load(text[4:end])


def skill_files() -> list[Path]:
    root = cli.skill_source()
    return [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in (".md", ".py")]


class TestFrontmatterFollowsTheSpec:
    def test_it_is_valid_yaml_with_the_required_fields(self):
        data = frontmatter()
        assert isinstance(data, dict)
        assert {"name", "description"} <= set(data)

    def test_no_field_outside_the_specification(self):
        """A field the spec does not define fails an upload outright.

        Claude Code accepts a dozen more (`model`, `context`, `argument-hint`
        and friends). They are exactly what makes a skill unportable, so the
        cost of adding one is a failing test rather than a bug report from
        somebody running Codex.
        """
        assert set(frontmatter()) <= SPEC_FIELDS

    def test_the_name_is_a_legal_skill_name(self):
        name = frontmatter()["name"]
        assert isinstance(name, str)
        assert len(name) <= 64
        assert NAME_PATTERN.match(name), "lowercase, digits and inner hyphens only"

    def test_the_name_matches_the_directory_it_is_installed_as(self):
        """Copilot and Codex list a skill by directory; a mismatch reads as two
        different things depending on where you look."""
        assert frontmatter()["name"] == cli.SKILL_NAME

    def test_the_description_fits_the_limit_and_says_when_to_use_it(self):
        description = frontmatter()["description"]
        assert isinstance(description, str)
        assert 0 < len(description) <= 1024
        # All three products match a skill against this string and nothing else.
        assert "Use when" in description

    def test_compatibility_fits_the_limit(self):
        compatibility = frontmatter().get("compatibility")
        assert isinstance(compatibility, str)
        assert len(compatibility) <= 500

    def test_allowed_tools_is_a_string_not_a_list(self):
        """The spec says space-separated string.

        Defect this pins: it was a YAML list, which Claude Code accepts and the
        spec does not describe -- the kind of difference that works everywhere
        it is tested and fails on the one product nobody tried.
        """
        assert isinstance(frontmatter()["allowed-tools"], str)

    def test_metadata_is_a_map_of_strings(self):
        metadata = frontmatter().get("metadata")
        assert isinstance(metadata, dict)
        assert all(isinstance(k, str) and isinstance(v, str) for k, v in metadata.items())

    def test_metadata_does_not_carry_a_second_copy_of_the_version(self):
        """The packaged version has one home, and it is pyproject.toml."""
        metadata = frontmatter().get("metadata", {})
        assert isinstance(metadata, dict)
        assert "version" not in metadata


class TestAllowedToolsMatchesWhatTheSkillPermits:
    def test_git_is_not_pre_approved(self):
        """SKILL.md forbids running git directly; scripts/gitwork.py is the only
        path to a mutation. Pre-approving `git` would hand the agent the very
        thing the procedure exists to keep out of its hands."""
        assert "Bash(git" not in frontmatter()["allowed-tools"]

    def test_bash_is_not_pre_approved_wholesale(self):
        """Defect this pins: `allowed-tools` listed bare `Bash`, which grants
        every command there is -- broader than the handful the procedure runs."""
        granted = str(frontmatter()["allowed-tools"]).split()
        assert "Bash" not in granted


class TestTheSkillSaysWhatItActuallyNeeds:
    def test_the_python_version_it_claims_is_the_one_the_package_supports(self):
        """Defect this pins: SKILL.md said "Python 3.11+" while the package
        supported 3.10 and CI tested it. On a 3.10 machine the skill instructed
        the agent to stop before doing anything."""
        if tomllib is None:
            pytest.skip("tomllib needs 3.11+; CI checks this on every later version")
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if not pyproject.is_file():
            pytest.skip("no checkout")
        requires = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
            "requires-python"
        ]
        floor = requires.removeprefix(">=").strip()
        body = (cli.skill_source() / "SKILL.md").read_text(encoding="utf-8")
        claimed = re.findall(r"Python (\d+\.\d+)\+", body)
        assert claimed, "SKILL.md should state the Python version it needs"
        assert set(claimed) == {floor}

    def test_compatibility_names_the_things_that_are_not_python(self):
        """`compatibility` is where a host looks before running a skill at all."""
        compatibility = str(frontmatter()["compatibility"])
        for needed in ("git", "curl", "gitignore.io"):
            assert needed in compatibility


class TestNothingIsWrittenForOneAgentOnly:
    def test_a_claude_only_tool_is_never_named_without_saying_so(self):
        """AskUserQuestion exists in Claude Code and nowhere else.

        Naming it unqualified turns a menu into a missing tool, and the skill's
        own headless clause would then fire on every Codex or Copilot run.
        """
        offenders = [
            path.name
            for path in skill_files()
            if "AskUserQuestion" in path.read_text(encoding="utf-8")
            and "Claude Code" not in path.read_text(encoding="utf-8")
        ]
        assert offenders == []

    def test_every_step_referred_to_is_a_step_that_exists(self):
        """Defect this pins: a proposed change renumbered SKILL.md's steps and
        left five cross-references in `references/` pointing at the old numbers.
        One of them was `push-safety.md`, which sent the force-push path -- the
        one place where being wrong is unrecoverable -- to what had become the
        commit step instead of the summary.

        Nothing caught it: the numbers stayed valid, they just stopped meaning
        what they had meant. The sibling test above pins the same failure for
        subcommand names, and this is the other half.
        """
        body = (cli.skill_source() / "SKILL.md").read_text(encoding="utf-8")
        defined = {int(n) for n in re.findall(r"^## Step (\d+) ", body, re.MULTILINE)}
        assert defined, "SKILL.md must define its steps as `## Step N — ...` headings"
        dangling = [
            f"{path.name}:{i} -> Step {n}"
            for path in skill_files()
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            for n in map(int, re.findall(r"\bStep (\d+)\b", line))
            if n not in defined
        ]
        assert dangling == []

    def test_no_file_calls_a_subcommand_that_no_longer_exists(self):
        """Defect this pins: `references/push-safety.md` told the agent to run
        `manage-gitignore git ... push`, which stopped being a command when the
        work moved into the scripts. The force-push path -- the one place where
        being wrong is unrecoverable -- exited 2 instead of pushing."""
        stale = re.compile(r"manage-gitignore\s+(git|templates|summary)\b")
        offenders = [
            f"{path.name}:{i}"
            for path in skill_files()
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if stale.search(line)
        ]
        assert offenders == []
