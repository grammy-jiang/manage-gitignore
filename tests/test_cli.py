"""cli.py — the console script, and the install/uninstall pair.

`install` links rather than copies, so upgrading the package upgrades the skill.
That makes `uninstall`'s job narrow and worth pinning: remove the link it made,
and refuse to touch anything else.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

try:  # tomllib landed in 3.11; on 3.10 this one check simply does not run
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    tomllib = None

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

    def test_a_missing_skill_names_the_path_it_looked_at(self, tmp_path, monkeypatch):
        """`install` from a broken install should say where it expected to find
        the skill, not just that it did not."""
        stand_in = tmp_path / "pkg" / "cli.py"
        stand_in.parent.mkdir(parents=True)
        stand_in.touch()
        monkeypatch.setattr(cli, "__file__", str(stand_in))
        with pytest.raises(FileNotFoundError, match=str(tmp_path / "pkg" / "skill")):
            cli.skill_source()

    def test_nothing_remaps_the_skill_at_build_time(self):
        """A remap would reintroduce the divergence the layout exists to avoid.

        Asserted against the build configuration rather than the built wheel:
        the wheel is what a remap would corrupt, but the setting is what a
        future edit would add back. Parsed, not grepped -- the prose explaining
        why the setting is absent names it, and a substring match cannot tell
        that comment apart from the setting itself.
        """
        if tomllib is None:
            pytest.skip("tomllib needs 3.11+; CI checks this on every later version")
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if not pyproject.is_file():  # running against an installed package, not a checkout
            pytest.skip("no checkout")
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        targets = config["tool"]["hatch"]["build"]["targets"]
        assert [name for name, t in targets.items() if "force-include" in t] == []


class TestLinkInspection:
    """`is_our_link` decides whether `uninstall` may delete something. Every
    answer it can give is worth pinning: a wrong `True` deletes a stranger's
    skill, a wrong `False` makes uninstall useless."""

    def test_link_target_of_a_real_directory_is_none(self, skills):
        (skills / "plain").mkdir()
        assert cli.link_target(skills / "plain") is None

    def test_link_target_of_a_missing_path_is_none(self, skills):
        assert cli.link_target(skills / "nothing-here") is None

    def test_link_target_reports_a_relative_link_verbatim(self, skills):
        (skills / "link").symlink_to("../elsewhere/skill", target_is_directory=True)
        assert cli.link_target(skills / "link") == Path("../elsewhere/skill")

    def test_a_real_directory_is_not_our_link(self, skills):
        (skills / "plain").mkdir()
        assert cli.is_our_link(skills / "plain") is False

    def test_a_dangling_link_is_not_our_link(self, skills, tmp_path):
        (skills / "gone").symlink_to(tmp_path / "vanished", target_is_directory=True)
        assert cli.is_our_link(skills / "gone") is False

    def test_a_directory_named_skill_without_a_skill_md_is_not_ours(self, skills, tmp_path):
        """The name alone must not be enough -- `install` links a directory that
        actually holds a SKILL.md, so anything else is somebody else's."""
        impostor = tmp_path / "skill"
        impostor.mkdir()
        (skills / cli.SKILL_NAME).symlink_to(impostor, target_is_directory=True)
        assert cli.is_our_link(skills / cli.SKILL_NAME) is False

    def test_a_relative_link_to_the_packaged_skill_is_ours(self, skills):
        """Resolved against the link's own directory, not the process cwd."""
        rel = os.path.relpath(cli.skill_source(), skills)
        (skills / cli.SKILL_NAME).symlink_to(rel, target_is_directory=True)
        assert cli.is_our_link(skills / cli.SKILL_NAME) is True


class TestTheScriptsStandAlone:
    """The package installs the skill. It does not run it, and it is not needed
    to run it. Both halves of that are structural, so both are checked by
    reading the imports rather than by trusting the prose."""

    def scripts(self) -> list[Path]:
        found = sorted((cli.skill_source() / "scripts").glob("*.py"))
        assert found, "no scripts found beside SKILL.md"
        return found

    def imported_roots(self, path: Path) -> set[str]:
        """Every top-level module name `path` imports, at any indent."""
        roots: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                roots |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
        return roots

    def test_no_script_imports_the_package(self):
        """Otherwise the skill directory would not work with the wheel gone."""
        offenders = {p.name: self.imported_roots(p) & {"manage_gitignore"} for p in self.scripts()}
        assert {name: found for name, found in offenders.items() if found} == {}

    def test_the_installer_does_not_import_the_scripts(self):
        """The reverse direction: installing must not run any of the work."""
        script_names = {p.stem for p in self.scripts()}
        assert self.imported_roots(Path(cli.__file__)) & script_names == set()

    def test_the_scripts_only_import_each_other_and_the_standard_library(self):
        """No third-party runtime dependency can creep in unnoticed: the skill
        is installed by symlink, so nothing would install one for it."""
        allowed = {p.stem for p in self.scripts()} | set(sys.stdlib_module_names)
        for path in self.scripts():
            assert self.imported_roots(path) <= allowed, path.name


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

    @pytest.mark.parametrize("command", ["install", "uninstall"])
    def test_every_documented_command_is_dispatchable(self, command, skills):
        """Dispatched, not merely mentioned.

        This used to assert the command appeared in the module docstring, which
        `git` passed on the strength of the word "gitignore". Running it is the
        only check that means anything.
        """
        assert cli.main([command, "--dest", str(skills)]) == 0

    @pytest.mark.parametrize(
        ("command", "script"),
        [("templates", "templates.py"), ("git", "gitwork.py"), ("summary", "summary.py")],
    )
    def test_a_command_that_moved_into_the_skill_says_where_it_went(self, command, script, capsys):
        """These were subcommands until the work moved into the skill.

        A bare "unknown command" would be true and useless: the work still
        exists, and the person typing this wants to know where.
        """
        assert cli.main([command]) == 2
        err = capsys.readouterr().err
        assert script in err
        assert "not a command of this installer" in err

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

    def test_a_refused_uninstall_exits_non_zero(self, skills, capsys):
        """A real directory is not ours; refusing must be an exit code, not a
        message on stdout that a caller could mistake for success."""
        (skills / cli.SKILL_NAME).mkdir()
        assert cli.main(["uninstall", "--dest", str(skills)]) == 1
        assert "not a symlink" in capsys.readouterr().err
        assert (skills / cli.SKILL_NAME).is_dir()

    def test_force_uninstall_removes_a_foreign_link_through_main(self, skills, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        (skills / cli.SKILL_NAME).symlink_to(other, target_is_directory=True)
        assert cli.main(["uninstall", "--dest", str(skills), "--force"]) == 0
        assert not (skills / cli.SKILL_NAME).is_symlink()
        assert other.is_dir()

    def test_install_reports_where_it_linked(self, skills, capsys):
        assert cli.main(["install", "--dest", str(skills)]) == 0
        out = capsys.readouterr().out
        assert str(skills / cli.SKILL_NAME) in out
        assert str(cli.skill_source()) in out

    def test_an_unwritable_skills_directory_is_an_error_not_a_traceback(self, tmp_path, capsys):
        """OSError is caught alongside FileExistsError; a permission problem
        should read like a refusal, not a crash."""
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o500)
        try:
            assert cli.main(["install", "--dest", str(locked / "skills")]) == 1
            assert "manage-gitignore:" in capsys.readouterr().err
        finally:
            locked.chmod(0o700)
