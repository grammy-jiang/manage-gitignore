"""Fixtures: throwaway git repos, a stub `curl`, and canned API responses.

No test touches the network. A stub `curl` goes on PATH, so the real fetch code
— including the streaming byte cap and the response validation — runs offline
against controllable responses.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "src" / "manage_gitignore" / "skill" / "scripts"

# The tests drive the scripts as subprocesses, by path, exactly as SKILL.md
# does. Mapping the old script names keeps every call site readable across the
# renames.
MODULE = {
    "gitignore.py": SCRIPTS / "templates.py",
    "gitwork.py": SCRIPTS / "gitwork.py",
    "render_summary.py": SCRIPTS / "summary.py",
    "summary": SCRIPTS / "summary.py",
}
API = "https://www.toptal.com/developers/gitignore/api"

TEMPLATE_NAMES = [
    "django",
    "dotenv",
    "git",
    "go",
    "java",
    "jetbrains+all",
    "linux",
    "macos",
    "maven",
    "node",
    "python",
    "rails",
    "rust",
    "unity",
    "vim",
    "visualstudiocode",
    "windows",
]

# Canned section bodies. `git` deliberately carries a "# Created by git for
# backups" comment, exactly as the real template does: it must never be mistaken
# for the block's own "# Created by <url>" marker.
_BODIES = {
    "git": ["# Created by git for backups. To disable backups in Git:", "*.orig", "*.rej"],
    "node": ["node_modules/", ".env", "npm-debug.log*"],
    "python": ["__pycache__/", "*.py[cod]", ".venv/"],
    "vim": ["*.swp", "!*.svg", "[._]*.un~"],
    "dotenv": [".env"],
}


def api_block(
    names: list[str],
    *,
    trailing: str = "",
    omit_end: bool = False,
    header_names: list[str] | None = None,
) -> str:
    """A realistic gitignore.io response for `names`."""
    joined = ",".join(header_names if header_names is not None else names)
    out = [f"# Created by {API}/{joined}", ""]
    for name in names:
        out.append(f"### {name.capitalize()} ###")
        out.extend(_BODIES.get(name, [f"# {name} body", f"{name}-artifacts/"]))
        out.append("")
    if not omit_end:
        out.append(f"# End of {API}/{joined}")
    if trailing:
        out.append(trailing)
    return "\n".join(out) + "\n"


_CURL_STUB = '''#!/usr/bin/env python3
"""Stand-in for curl: serves canned bodies from $FAKE_API_DIR keyed by URL tail."""
import os, sys, time

url = sys.argv[-1]
mode = os.environ.get("FAKE_CURL_MODE", "ok")
if mode == "fail":
    sys.stderr.write("curl: (22) The requested URL returned error: 500\\n")
    sys.exit(22)
if mode == "hang":
    time.sleep(float(os.environ.get("FAKE_CURL_HANG", "300")))
if mode == "flood":                       # unbounded body -> the cap must fire
    while True:
        sys.stdout.buffer.write(b"x" * 65536)
        sys.stdout.buffer.flush()

with open(os.path.join(os.environ["FAKE_API_DIR"], "argv.log"), "a") as log:
    log.write("\\x00".join(sys.argv) + "\\n")

tail = url.split("/api/", 1)[1]

# Simulate a racing writer, but only on the template fetch -- the earlier
# catalogue fetch happens before the existence gate, so touching there would
# test the ordinary gate instead of the race window after it.
touch = os.environ.get("FAKE_CURL_TOUCH")
if touch and not tail.startswith("list"):
    with open(touch, "w") as fh:
        fh.write("someone else got here first\\n")
path = os.path.join(os.environ["FAKE_API_DIR"], "list" if tail.startswith("list") else "block")
if not os.path.exists(path):
    sys.stderr.write("curl: (22) no canned response for %s\\n" % url)
    sys.exit(22)
with open(path, "rb") as fh:
    sys.stdout.buffer.write(fh.read())
'''


class FakeApi:
    """Controls what the stub curl returns."""

    def __init__(self, directory: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.dir = directory
        self._monkeypatch = monkeypatch

    def set_list(self, names: list[str]) -> None:
        (self.dir / "list").write_text("\n".join(names) + "\n", encoding="utf-8")

    def set_block(self, text: str) -> None:
        (self.dir / "block").write_text(text, encoding="utf-8")

    def race_creates(self, path) -> None:
        """Have the stub create `path` mid-fetch, simulating another writer."""
        self._monkeypatch.setenv("FAKE_CURL_TOUCH", str(path))

    def invocations(self) -> list[list[str]]:
        """Every argv the stub curl was called with, for flag assertions."""
        log = self.dir / "argv.log"
        if not log.exists():
            return []
        return [line.split("\x00") for line in log.read_text().splitlines() if line]

    def set_mode(self, mode: str, *, hang_seconds: float | None = None) -> None:
        """ok | fail | hang | flood. Routed through monkeypatch so it is undone."""
        self._monkeypatch.setenv("FAKE_CURL_MODE", mode)
        if hang_seconds is not None:
            self._monkeypatch.setenv("FAKE_CURL_HANG", str(hang_seconds))


@pytest.fixture
def api(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    api_dir = tmp_path / "_api"
    api_dir.mkdir()
    fake = FakeApi(api_dir, monkeypatch)
    fake.set_list(TEMPLATE_NAMES)
    fake.set_block(api_block(["git", "node"]))

    bin_dir = tmp_path / "_bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(_CURL_STUB, encoding="utf-8")
    curl.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_API_DIR", str(api_dir))
    monkeypatch.setenv("FAKE_CURL_MODE", "ok")
    return fake


# ── git helpers ─────────────────────────────────────────────────────────────
def git(repo: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc


def init_repo(path: pathlib.Path, *, seed: bool = True) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main", ".")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "Test")
    git(path, "config", "commit.gpgsign", "false")
    if seed:
        (path / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(path, "add", "-A")
        git(path, "commit", "-qm", "seed")
    return path


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A git repo with one commit."""
    return init_repo(tmp_path / "repo")


@pytest.fixture
def empty_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A git repo with an unborn HEAD (no commits at all)."""
    return init_repo(tmp_path / "empty", seed=False)


@pytest.fixture
def plain_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """A directory that is not a git repo."""
    path = tmp_path / "plain"
    path.mkdir()
    return path


def init_bare(path: pathlib.Path) -> pathlib.Path:
    """A bare repo whose HEAD already points at `main`.

    Without `-b main` the bare defaults to `master`, and cloning it warns
    "remote HEAD refers to nonexistent ref" and leaves no checkout.
    """
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(path)], check=True)
    return path


def remote_head(bare: pathlib.Path, branch: str = "main") -> str:
    """The sha a bare repo's branch points at (its HEAD is not a checkout)."""
    return subprocess.run(
        ["git", "-C", str(bare), "rev-parse", branch], capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def make_bare(tmp_path: pathlib.Path):
    """Factory for extra bare repos inside a test."""

    def _make(name: str) -> pathlib.Path:
        return init_bare(tmp_path / f"{name}.git")

    return _make


@pytest.fixture
def remote_pair(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """(work, bare) with `main` pushed and tracking configured."""
    bare = init_bare(tmp_path / "bare.git")
    work = init_repo(tmp_path / "work")
    git(work, "remote", "add", "origin", str(bare))
    git(work, "push", "-q", "-u", "origin", "main")
    return work, bare


@pytest.fixture
def clone_of():
    """Factory: a second clone of a bare repo, for creating divergence."""

    def _clone(bare: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
        subprocess.run(["git", "clone", "-q", str(bare), str(dest)], check=True)
        git(dest, "config", "user.email", "t2@example.invalid")
        git(dest, "config", "user.name", "Test2")
        git(dest, "config", "commit.gpgsign", "false")
        return dest

    return _clone


@pytest.fixture
def run_script():
    """Factory: run a bundled script as a subprocess and return the result."""

    def _run(script: str, *args: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        # PYTHONPATH is removed, not extended. The scripts are bundled with the
        # skill and run by path, so the only thing that may put their siblings
        # within reach is sys.path[0] -- the directory the script itself is in.
        # Handing them an import path here would hide a dependency on something
        # installed, which is precisely what must not exist.
        env.pop("PYTHONPATH", None)
        return subprocess.run(
            [sys.executable, str(MODULE[script]), *args],
            capture_output=True,
            text=True,
            env=env,
        )

    return _run
