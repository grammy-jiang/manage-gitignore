"""Build a repository's .gitignore from gitignore.io, keeping its custom rules.

The package behind the `manage-gitignore` Claude Code skill. Each module owns one
decision the skill must not make by eye:

- `templates`  the .gitignore file: scan, recommend, fetch, merge, write, verify
- `gitwork`    git: status/diff, commit, push planning, push, facts
- `summary`    the end-of-run summary format
- `shared`     one sanitiser, one no-follow reader, one JSON contract
"""

__version__ = "0.1.0"
