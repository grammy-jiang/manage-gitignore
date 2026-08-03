"""The `manage-gitignore` console script.

A thin dispatcher: each subcommand hands straight to the module that owns that
decision, so the CLI adds no logic of its own and nothing here can drift from
what the tests exercise.

    manage-gitignore templates --dir REPO --recommend
    manage-gitignore git       --dir REPO status
    manage-gitignore summary   FACTS.json
    manage-gitignore install-skill
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from manage_gitignore import __version__

SKILL_FILES = ("SKILL.md",)
SKILL_DIRS = ("references",)


def skill_source() -> Path:
    """Where the packaged skill files live.

    Installed as package data next to this module; in a source checkout they sit
    at the repository root, so both layouts work without a build step.
    """
    packaged = Path(__file__).resolve().parent / "skill"
    if (packaged / "SKILL.md").is_file():
        return packaged
    repo = Path(__file__).resolve().parents[2] / "skill"
    if (repo / "SKILL.md").is_file():
        return repo
    raise FileNotFoundError("packaged skill files not found")


def install_skill(dest_root: Path, *, force: bool) -> Path:
    """Copy SKILL.md + references into a Claude Code skills directory."""
    source = skill_source()
    dest = dest_root / "manage-gitignore"
    if dest.exists() and not force:
        raise FileExistsError(f"{dest} already exists -- re-run with --force to replace it")
    dest.mkdir(parents=True, exist_ok=True)
    for name in SKILL_FILES:
        shutil.copy2(source / name, dest / name)
    for name in SKILL_DIRS:
        target = dest / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source / name, target)
    return dest


def cmd_install_skill(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="manage-gitignore install-skill",
        description="Install the skill files into a Claude Code skills directory.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--dest",
        default=str(Path.home() / ".claude" / "skills"),
        help="skills directory (default: ~/.claude/skills)",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing install")
    args = parser.parse_args(argv)
    try:
        dest = install_skill(Path(args.dest), force=args.force)
    except (FileExistsError, FileNotFoundError, OSError) as exc:
        print(f"manage-gitignore: {exc}", file=sys.stderr)
        return 1
    print(f"Installed the manage-gitignore skill to {dest}")
    print("Its SKILL.md drives the tools above; restart Claude Code to pick it up.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-V", "--version"):
        print(__version__)
        return 0
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2

    command, rest = argv[0], argv[1:]
    # Each module owns its own argument parsing; sys.argv is rewritten so their
    # usage strings and error messages stay accurate under the console script.
    if command == "templates":
        from manage_gitignore import templates

        sys.argv = ["manage-gitignore templates", *rest]
        templates.main()
        return 0
    if command == "git":
        from manage_gitignore import gitwork

        sys.argv = ["manage-gitignore git", *rest]
        return gitwork.main()
    if command == "summary":
        from manage_gitignore import summary

        sys.argv = ["manage-gitignore summary", *rest]
        summary.main()
        return 0
    if command == "install-skill":
        return cmd_install_skill(rest)

    print(f"manage-gitignore: unknown command {command!r}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
