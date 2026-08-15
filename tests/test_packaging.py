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

import os
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
    # --no-isolation, because `build` otherwise creates a throwaway environment
    # and fetches the backend named in [build-system] from PyPI. No test here
    # touches the network, and that rule is worth more than build isolation:
    # hatchling is in the dev extra so this resolves to the installed one.
    subprocess.run(
        [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(out), str(REPO)],
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
        # Every member shares one top-level directory, `manage_gitignore-<version>`.
        # Derived from all of them rather than from names[0]: archive order is
        # not specified, so the first entry need not be the root.
        roots = {Path(name).parts[0] for name in names}
        assert len(roots) == 1, f"expected one top-level directory, got {sorted(roots)}"
        root = roots.pop()
        missing = [f for f in SKILL_FILES if f"{root}/src/{f}" not in names]
        assert missing == []


class TestTheBackendIsPinnedEverywhereItIsResolved:
    """`python -m build` isolates by default, so the backend the release
    actually runs comes from `[build-system] requires` and not from anything
    installed into the job.

    Found in review: pinning hatchling in the dev extra looked like it pinned
    the release build, and did not. A hatchling release between two builds of
    one tag could then run unreviewed backend code and produce different
    artifacts -- exactly what SOURCE_DATE_EPOCH is there to rule out.

    Two pins is two homes for one fact, so this is the test that stops them
    drifting apart.
    """

    @staticmethod
    def _pyproject() -> dict:
        tomllib = pytest.importorskip("tomllib", reason="tomllib is 3.11+")
        return tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

    def test_the_build_backend_is_pinned_exactly(self):
        requires = self._pyproject()["build-system"]["requires"]
        assert [r for r in requires if "==" not in r] == [], (
            "an unpinned build-system requirement is resolved from PyPI at build time"
        )

    def test_both_pins_name_the_same_hatchling(self):
        data = self._pyproject()
        build = [r for r in data["build-system"]["requires"] if r.startswith("hatchling")]
        dev = [
            r for r in data["project"]["optional-dependencies"]["dev"] if r.startswith("hatchling")
        ]
        assert build == dev, "the isolated build and the offline build would use different backends"


@pytest.mark.slow
class TestTheBuildIsReproducible:
    """Two builds of one tree must produce the same bytes.

    Without `SOURCE_DATE_EPOCH` every member of the archive is stamped with the
    moment the builder happened to unpack it, so the artifact for a tag differs
    on every rebuild and "this file is that tag" is a claim nobody can check --
    including the person publishing it.

    The release workflow takes the value from the tagged commit, which is a
    property of the tag rather than of when the job ran, so re-running it on the
    same tag rebuilds the same bytes. This asserts the property that setting is
    for, rather than asserting the setting.
    """

    @staticmethod
    def _build(out: Path, epoch: str) -> bytes:
        subprocess.run(
            [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(out), str(REPO)],
            capture_output=True,
            check=True,
            env={**os.environ, "SOURCE_DATE_EPOCH": epoch},
        )
        made = list(out.glob("*.whl"))
        assert len(made) == 1, f"expected one wheel, got {made}"
        return made[0].read_bytes()

    def test_the_same_epoch_gives_the_same_wheel(self, tmp_path):
        if not (REPO / "pyproject.toml").is_file():  # installed package, not a checkout
            pytest.skip("no checkout")
        epoch = "1735689600"  # 2025-01-01T00:00:00Z, any fixed instant will do
        first = self._build(tmp_path / "a", epoch)
        second = self._build(tmp_path / "b", epoch)
        assert first == second, "two builds of one tree differ byte for byte"

    def test_a_different_epoch_gives_a_different_wheel(self, tmp_path):
        """The guard on the test above: if the wheel ignored the variable
        entirely the first test would pass for the wrong reason, and the release
        would still be publishing timestamps nobody can reproduce."""
        if not (REPO / "pyproject.toml").is_file():
            pytest.skip("no checkout")
        assert self._build(tmp_path / "c", "1735689600") != self._build(
            tmp_path / "d", "1767225600"
        )


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
        # --no-index: the package has no runtime dependencies, so nothing should
        # be fetched, and this makes that a guarantee rather than an expectation.
        subprocess.run(
            [
                str(bin_dir / "pip"),
                "install",
                "--no-index",
                "--no-cache-dir",
                "--quiet",
                str(wheel),
            ],
            check=True,
        )

        console = bin_dir / "manage-gitignore"
        subprocess.run([str(console), "--version"], check=True)

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
        #
        # The venv's interpreter, not `sys.executable`. The outer one is the dev
        # environment, where pytest, ruff, mypy and PyYAML are all importable --
        # so a script that grew an undeclared third-party import would run there
        # and fail on a user's machine. The venv holds this package and nothing
        # else, which is the condition actually being claimed.
        python = bin_dir / ("python.exe" if sys.platform == "win32" else "python")
        script = home / ".claude" / "skills" / "manage-gitignore" / "scripts" / "templates.py"
        done = subprocess.run(
            [str(python), str(script), "--help"],
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir)},
        )
        assert done.returncode == 0, done.stderr

        subprocess.run([str(console), "uninstall"], check=True, env=env)
        assert not (home / ".claude" / "skills" / "manage-gitignore").exists()
