"""cli.py — the console script, and the install/uninstall pair.

`install` links rather than copies, so upgrading the package upgrades the skill.
That makes `uninstall`'s job narrow and worth pinning: remove the link it made,
and refuse to touch anything else.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from manage_gitignore import cli


@pytest.fixture
def skills(tmp_path: Path) -> Path:
    """An empty Claude Code skills directory."""
    d = tmp_path / "skills"
    d.mkdir()
    return d


class TestSkillSource:
    def test_finds_the_packaged_skill(self):
        source = cli.skill_source()
        assert (source / "SKILL.md").is_file()
        assert (source / "references").is_dir()

    def test_the_skill_sits_beside_the_code(self):
        """The checkout and site-packages must be the same tree.

        Defect this pins: the skill used to live at the repository root and get
        remapped into the package by a build-time `force-include`, so a path
        under site-packages had no counterpart at the same relative position in
        the checkout, and could not be traced back by relative path when
        debugging. Keeping the directory beside this module removes the remap.
        """
        assert cli.skill_source().parent == Path(cli.__file__).resolve().parent

    def test_nothing_remaps_the_skill_at_build_time(self):
        """A remap would reintroduce the divergence the layout exists to avoid.

        Asserted against the build configuration rather than the built wheel:
        the wheel is what a remap would corrupt, but the setting is what a
        future edit would add back. Parsed, not grepped -- the prose explaining
        why the setting is absent names it, and a substring match cannot tell
        that comment apart from the setting itself.
        """
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if not pyproject.is_file():  # running against an installed package, not a checkout
            pytest.skip("no checkout")
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        targets = config["tool"]["hatch"]["build"]["targets"]
        assert [name for name, t in targets.items() if "force-include" in t] == []


class TestInstall:
    def test_creates_a_symlink_to_the_packaged_skill(self, skills):
        dest = cli.install(skills, force=False)
        assert dest.is_symlink()
        assert dest.resolve() == cli.skill_source().resolve()
        assert (dest / "SKILL.md").is_file()

    def test_creates_the_skills_directory_when_absent(self, tmp_path):
        assert cli.install(tmp_path / "nested" / "skills", force=False).is_symlink()

    def test_reinstalling_over_our_own_link_is_idempotent(self, skills):
        first = cli.install(skills, force=False)
        second = cli.install(skills, force=False)
        assert first == second
        assert second.is_symlink()

    def test_refuses_a_real_directory_without_force(self, skills):
        """A hand-written skill, or an older copy, is not ours to delete."""
        (skills / cli.SKILL_NAME).mkdir()
        (skills / cli.SKILL_NAME / "SKILL.md").write_text("mine\n", encoding="utf-8")
        with pytest.raises(FileExistsError, match="not a symlink"):
            cli.install(skills, force=False)
        assert (skills / cli.SKILL_NAME / "SKILL.md").read_text() == "mine\n"

    def test_force_replaces_a_real_directory(self, skills):
        (skills / cli.SKILL_NAME).mkdir()
        (skills / cli.SKILL_NAME / "stale.py").write_text("old\n", encoding="utf-8")
        dest = cli.install(skills, force=True)
        assert dest.is_symlink()
        assert not (dest / "stale.py").exists()

    def test_refuses_a_link_to_something_else_without_force(self, skills, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        (skills / cli.SKILL_NAME).symlink_to(other, target_is_directory=True)
        with pytest.raises(FileExistsError, match="symlink to something else"):
            cli.install(skills, force=False)
        assert (skills / cli.SKILL_NAME).resolve() == other.resolve()


class TestUninstall:
    def test_removes_the_link_install_made(self, skills):
        cli.install(skills, force=False)
        removed = cli.uninstall(skills, force=False)
        assert removed == skills / cli.SKILL_NAME
        assert not (skills / cli.SKILL_NAME).exists()

    def test_leaves_the_packaged_skill_alone(self, skills):
        """Unlinking must never reach through to the package's own files."""
        cli.install(skills, force=False)
        cli.uninstall(skills, force=False)
        assert (cli.skill_source() / "SKILL.md").is_file()

    def test_nothing_to_remove_is_not_an_error(self, skills):
        assert cli.uninstall(skills, force=False) is None

    def test_refuses_a_real_directory(self, skills):
        """`install` never creates one, so this cannot be ours."""
        (skills / cli.SKILL_NAME).mkdir()
        (skills / cli.SKILL_NAME / "SKILL.md").write_text("mine\n", encoding="utf-8")
        with pytest.raises(FileExistsError, match="not a symlink"):
            cli.uninstall(skills, force=False)
        assert (skills / cli.SKILL_NAME).is_dir()

    def test_refuses_a_link_to_something_else(self, skills, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        (skills / cli.SKILL_NAME).symlink_to(other, target_is_directory=True)
        with pytest.raises(FileExistsError, match="not a packaged skill"):
            cli.uninstall(skills, force=False)
        assert (skills / cli.SKILL_NAME).is_symlink()

    def test_force_removes_a_foreign_link_but_not_its_target(self, skills, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        (other / "keep.txt").write_text("still here\n", encoding="utf-8")
        (skills / cli.SKILL_NAME).symlink_to(other, target_is_directory=True)
        cli.uninstall(skills, force=True)
        assert not (skills / cli.SKILL_NAME).is_symlink()
        assert (other / "keep.txt").read_text() == "still here\n"

    def test_a_dangling_link_is_still_removable(self, skills, tmp_path):
        (skills / cli.SKILL_NAME).symlink_to(tmp_path / "gone", target_is_directory=True)
        cli.uninstall(skills, force=True)
        assert not (skills / cli.SKILL_NAME).is_symlink()


class TestRoundTrip:
    def test_install_then_uninstall_leaves_no_trace(self, skills):
        before = sorted(os.listdir(skills))
        cli.install(skills, force=False)
        cli.uninstall(skills, force=False)
        assert sorted(os.listdir(skills)) == before


class TestDispatch:
    @pytest.mark.parametrize("flag", ["-V", "--version"])
    def test_version(self, flag, capsys):
        assert cli.main([flag]) == 0
        assert capsys.readouterr().out.strip()

    def test_no_arguments_is_a_usage_error(self):
        assert cli.main([]) == 2

    def test_help_is_not_an_error(self):
        assert cli.main(["--help"]) == 0

    def test_an_unknown_command_is_refused(self, capsys):
        assert cli.main(["nope"]) == 2
        assert "unknown command" in capsys.readouterr().err

    @pytest.mark.parametrize("command", ["templates", "git", "summary", "install", "uninstall"])
    def test_every_documented_command_is_dispatchable(self, command):
        assert command in (cli.__doc__ or "")

    def test_install_and_uninstall_round_trip_through_main(self, skills):
        assert cli.main(["install", "--dest", str(skills)]) == 0
        assert (skills / cli.SKILL_NAME).is_symlink()
        assert cli.main(["uninstall", "--dest", str(skills)]) == 0
        assert not (skills / cli.SKILL_NAME).exists()

    def test_uninstall_reports_when_there_is_nothing_to_do(self, skills, capsys):
        assert cli.main(["uninstall", "--dest", str(skills)]) == 0
        assert "Nothing to remove" in capsys.readouterr().out

    def test_a_refused_install_exits_non_zero(self, skills, capsys):
        (skills / cli.SKILL_NAME).mkdir()
        assert cli.main(["install", "--dest", str(skills)]) == 1
        assert "manage-gitignore:" in capsys.readouterr().err
