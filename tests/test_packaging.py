"""What comes out of the build, rather than what the build was told to do.

`tests/test_cli.py` asserts that `pyproject.toml` carries no `force-include`,
because that setting is what a future edit would add back. It reads the
configuration. Nothing read the artifact -- and the defect that rule exists to
prevent was a wheel whose contents were fine by every metadata check and wrong
by position: the skill files lived at a path that existed in the wheel and
nowhere in the checkout, so a traceback could not be traced back.

`twine check` validates metadata. These tests open the wheel.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Every file the installer's symlink has to reach. A wheel missing one of these
# installs cleanly, links cleanly, and fails on somebody's machine at the first
# step of a run.
SKILL_FILES = (
    "manage_gitignore/skill/SKILL.md",
    "manage_gitignore/skill/scripts/templates.py",
    "manage_gitignore/skill/scripts/gitwork.py",
    "manage_gitignore/skill/scripts/summary.py",
    "manage_gitignore/skill/scripts/shared.py",
    "manage_gitignore/skill/references/asking-the-user.md",
    "manage_gitignore/skill/references/push-safety.md",
    "manage_gitignore/skill/references/example-output.md",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    """One `python -m build` for this module: sdist and wheel, from this checkout."""
    if not (REPO / "pyproject.toml").is_file():  # installed package, not a checkout
        pytest.skip("no checkout")
    out = tmp_path_factory.mktemp("dist")
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(out), str(REPO)],
        capture_output=True,
        check=True,
    )
    return out


@pytest.fixture(scope="module")
def wheel(built: Path) -> Path:
    made = list(built.glob("*.whl"))
    assert len(made) == 1, f"expected one wheel, got {made}"
    return made[0]


@pytest.fixture(scope="module")
def sdist(built: Path) -> Path:
    made = list(built.glob("*.tar.gz"))
    assert len(made) == 1, f"expected one sdist, got {made}"
    return made[0]


@pytest.mark.slow
class TestTheWheelCarriesTheSkill:
    def test_every_skill_file_is_in_it(self, wheel):
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
        assert [f for f in SKILL_FILES if f not in names] == []

    def test_the_wheel_holds_them_where_the_checkout_does(self, wheel):
        """The layout rule, checked against the artifact rather than the config.

        Defect this pins: a build-time `force-include` remapped the skill into
        the package, so a path under site-packages had no counterpart at the
        same relative position in the checkout. Any remap that came back would
        leave the two lists below disagreeing.
        """
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
        packaged = sorted(n for n in names if n.startswith("manage_gitignore/skill/"))
        on_disk = sorted(
            f"manage_gitignore/{path.relative_to(REPO / 'src' / 'manage_gitignore').as_posix()}"
            for path in (REPO / "src" / "manage_gitignore" / "skill").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
        assert packaged == on_disk

    def test_no_compiled_or_cache_files_ride_along(self, wheel):
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
        assert [n for n in names if n.endswith(".pyc") or "__pycache__" in n] == []


@pytest.mark.slow
class TestTheSdistCarriesTheSkillToo:
    """The release publishes both, so both are checked.

    An sdist missing the skill still installs -- and produces a package whose
    `install` raises `packaged skill files not found`. `[tool.hatch.build.targets.sdist]`
    lists directories by hand, which is exactly the kind of list that goes stale
    when a new one appears.
    """

    def test_the_skill_files_are_in_it(self, sdist):
        with tarfile.open(sdist) as archive:
            names = archive.getnames()
        root = Path(names[0]).parts[0]  # manage_gitignore-<version>
        missing = [f for f in SKILL_FILES if f"{root}/src/{f}" not in names]
        assert missing == []


@pytest.mark.slow
class TestTheWheelInstallsAndRuns:
    """Install it the way a user would, and make it do something.

    A wheel that imports is not the claim this package makes. The claim is that
    `install` links a directory whose scripts run from wherever they land, with
    nothing else installed -- so the test links into a throwaway home and then
    runs a script through the link.
    """

    def test_it_installs_links_and_the_scripts_run_from_there(self, wheel, tmp_path):
        env_dir = tmp_path / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(env_dir)], check=True)
        bin_dir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
        subprocess.run(
            [str(bin_dir / "pip"), "install", "--no-cache-dir", "--quiet", str(wheel)],
            check=True,
        )

        console = bin_dir / "manage-gitignore"
        assert subprocess.run([str(console), "--version"], check=True).returncode == 0

        home = tmp_path / "home"
        home.mkdir()
        env = {"HOME": str(home), "PATH": str(bin_dir)}
        subprocess.run([str(console), "install", "--all"], check=True, env=env)

        for skills in (home / ".claude" / "skills", home / ".agents" / "skills"):
            link = skills / "manage-gitignore"
            assert link.is_symlink()
            assert (link / "SKILL.md").is_file()

        # The point of the symlink: the scripts resolve each other from wherever
        # they were linked to, with the package not on PYTHONPATH at all.
        script = home / ".claude" / "skills" / "manage-gitignore" / "scripts" / "templates.py"
        done = subprocess.run(
            [sys.executable, str(script), "--help"],
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir)},
        )
        assert done.returncode == 0, done.stderr

        subprocess.run([str(console), "uninstall"], check=True, env=env)
        assert not (home / ".claude" / "skills" / "manage-gitignore").exists()
