"""Installer for the `manage-gitignore` Claude Code skill.

This package ships one command pair -- `install` and `uninstall` -- and the
skill directory they link. It holds none of the logic: `skill/scripts/` does,
beside the SKILL.md that drives it, and those scripts import each other by
plain module name and never import this package. Uninstall the wheel and the
skill directory still works if you keep it.

- `cli`      install / uninstall
- `skill/`   SKILL.md, references/, and the scripts that do the work
"""

__version__ = "0.1.0"
