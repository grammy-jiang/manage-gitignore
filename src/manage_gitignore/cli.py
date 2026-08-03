"""The `manage-gitignore` console script.

A thin dispatcher: each subcommand hands straight to the module that owns that
decision, so the CLI adds no logic of its own and nothing here can drift from
what the tests exercise.

    manage-gitignore templates --dir REPO --recommend
    manage-gitignore git       --dir REPO status
    manage-gitignore summary   FACTS.json

    manage-gitignore install     symlink the skill into ~/.claude/skills
    manage-gitignore uninstall   remove that symlink again
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from manage_gitignore import __version__

SKILL_NAME = "manage-gitignore"
DEFAULT_SKILLS_DIR = Path.home() / ".claude" / "skills"


def skill_source() -> Path:
    """The packaged skill directory: SKILL.md plus references/.

    One path, not a search: the skill sits beside this module in the checkout
    and in site-packages alike, because nothing remaps it at build time. A file
    found under either root is therefore at the same path relative to the
    package, which is what makes a path in a traceback traceable to the repo.
    """
    source = Path(__file__).resolve().parent / "skill"
    if not (source / "SKILL.md").is_file():
        raise FileNotFoundError(f"packaged skill files not found at {source}")
    return source


def link_target(link: Path) -> Path | None:
    """What `link` points at, or None if it is not a symlink at all."""
    if not link.is_symlink():
        return None
    return Path(os.readlink(link))


def is_our_link(link: Path) -> bool:
    """True only for a symlink this package's `install` could have created.

    `uninstall` removes nothing else: a real directory, or a link to something
    else, belongs to somebody and is not ours to delete.
    """
    target = link_target(link)
    if target is None:
        return False
    resolved = target.resolve() if target.is_absolute() else (link.parent / target).resolve()
    return resolved.name == "skill" and (resolved / "SKILL.md").is_file()


def install(dest_root: Path, *, force: bool) -> Path:
    """Symlink the packaged skill into a Claude Code skills directory.

    A link rather than a copy, so upgrading the package upgrades the skill with
    no second step and no chance of the two drifting.
    """
    source = skill_source()
    dest = dest_root / SKILL_NAME

    if dest.is_symlink():
        if is_our_link(dest) or force:
            dest.unlink()  # installing over our own link is idempotent
        else:
            raise FileExistsError(
                f"{dest} is a symlink to something else -- re-run with --force to replace it"
            )
    elif dest.exists():
        # Never silently delete a real directory: it may be a hand-written skill,
        # or an older copy holding files this package did not put there.
        if not force:
            raise FileExistsError(
                f"{dest} already exists and is not a symlink -- inspect it, then either "
                "remove it yourself or re-run with --force"
            )
        shutil.rmtree(dest)

    dest_root.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(source, target_is_directory=True)
    return dest


def uninstall(dest_root: Path, *, force: bool) -> Path | None:
    """Remove what `install` created. Returns the path removed, or None.

    Refuses anything else: a real directory was never made by `install`, and a
    link pointing elsewhere is not this package's to remove.
    """
    dest = dest_root / SKILL_NAME
    if not dest.is_symlink() and not dest.exists():
        return None
    if not dest.is_symlink():
        raise FileExistsError(
            f"{dest} is a directory, not a symlink -- `install` never creates one, so this "
            "is not ours to remove. Delete it yourself if you no longer want it."
        )
    if not is_our_link(dest) and not force:
        raise FileExistsError(
            f"{dest} points at {link_target(dest)}, which is not a packaged skill -- "
            "re-run with --force if you are sure"
        )
    dest.unlink()  # the link only; whatever it pointed at is untouched
    return dest


def _dest_parser(prog: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=description, allow_abbrev=False)
    parser.add_argument(
        "--dest",
        default=str(DEFAULT_SKILLS_DIR),
        help=f"skills directory (default: {DEFAULT_SKILLS_DIR})",
    )
    parser.add_argument("--force", action="store_true", help="act even on something not ours")
    return parser


def cmd_install(argv: list[str]) -> int:
    args = _dest_parser(
        "manage-gitignore install", "Symlink the skill into a Claude Code skills directory."
    ).parse_args(argv)
    try:
        dest = install(Path(args.dest), force=args.force)
    except (FileExistsError, FileNotFoundError, OSError) as exc:
        print(f"manage-gitignore: {exc}", file=sys.stderr)
        return 1
    print(f"Linked {dest} -> {skill_source()}")
    print("Upgrading the package now upgrades the skill. Restart Claude Code to pick it up.")
    return 0


def cmd_uninstall(argv: list[str]) -> int:
    args = _dest_parser(
        "manage-gitignore uninstall", "Remove the symlink that `install` created."
    ).parse_args(argv)
    try:
        removed = uninstall(Path(args.dest), force=args.force)
    except (FileExistsError, OSError) as exc:
        print(f"manage-gitignore: {exc}", file=sys.stderr)
        return 1
    if removed is None:
        print(f"Nothing to remove: no {SKILL_NAME} in {args.dest}")
        return 0
    print(f"Removed {removed}. The package itself is untouched -- `pipx uninstall` removes that.")
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
    if command == "install":
        return cmd_install(rest)
    if command == "uninstall":
        return cmd_uninstall(rest)

    print(f"manage-gitignore: unknown command {command!r}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
