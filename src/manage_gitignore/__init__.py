"""Installer for the `manage-gitignore` Claude Code skill.

This package ships one command pair -- `install` and `uninstall` -- and the
skill directory they link. It holds none of the logic: `skill/scripts/` does,
beside the SKILL.md that drives it, and those scripts import each other by
plain module name and never import this package. Uninstall the wheel and the
skill directory still works if you keep it.

- `cli`      install / uninstall
- `skill/`   SKILL.md, references/, and the scripts that do the work
"""

from importlib.metadata import PackageNotFoundError, version

try:
    # Read from the installed distribution rather than repeated here. This line
    # used to be `__version__ = "0.1.0"`, a second authoritative home for a fact
    # pyproject.toml already owned -- and it drifted on the very first bump, so
    # 0.2.0 shipped to PyPI announcing itself as 0.1.0.
    __version__ = version("manage-gitignore")
except PackageNotFoundError:  # pragma: no cover - a checkout with nothing installed
    # Running from source with PYTHONPATH=src and no install, as the Makefile
    # does. Saying so beats reporting a number that came from nowhere.
    __version__ = "0+unknown"
