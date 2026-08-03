# manage-gitignore

Build a repository's `.gitignore` from [gitignore.io](https://www.toptal.com/developers/gitignore),
writing the template block **verbatim** and preserving the repo's own custom rules —
then review the diff and, with confirmation, commit and push it.

Ships as a Python package **and** as a [Claude Code](https://claude.com/claude-code)
skill: the CLI makes every mechanical decision, and `SKILL.md` tells an agent how
to drive it.

```bash
pipx install manage-gitignore   # or: pip install manage-gitignore
manage-gitignore install        # symlink the skill into ~/.claude/skills/
```

`install` links rather than copies, so upgrading the package upgrades the skill —
no second step, and no chance of the two drifting. `manage-gitignore uninstall`
removes that link again, and refuses to touch anything it did not create: a real
directory, or a link pointing somewhere else, is left alone unless you pass
`--force`. Both take `--dest` if your skills directory is not `~/.claude/skills`.
Removing the package itself is `pipx uninstall manage-gitignore`.

## Using the CLI directly

```bash
manage-gitignore templates --dir . --recommend      # scan the repo, propose a set
manage-gitignore templates --dir . --force \
    --facts-out facts.json node python vim          # fetch, merge, write, verify
manage-gitignore git --dir . status                 # the real diff, via git
manage-gitignore git --dir . push-plan              # what a push would do
manage-gitignore summary facts.json                 # the run summary
```

`templates --list` prints every template name; `--list --count` prints just how
many there are. `--detect` reports what an existing `.gitignore` is made of.

## Design principle

**Anything a program can decide, a program decides.** When an agent drives this,
it asks the user, judges the answers, writes the commit message, and relays
results — nothing else. Scanning, merging, verification, every number in the
summary, and every git mutation live in the code, where they are testable and
where a wrong answer is an exit code rather than a plausible sentence.

## Layout

| Path | Owns |
|---|---|
| `src/manage_gitignore/templates.py` | the `.gitignore` file: scan → recommend → fetch → merge → write → verify |
| `src/manage_gitignore/gitwork.py` | git: status/diff, commit, push planning, push, facts |
| `src/manage_gitignore/summary.py` | the end-of-run summary format |
| `src/manage_gitignore/shared.py` | one sanitiser, one no-follow reader, one JSON contract |
| `src/manage_gitignore/cli.py` | the console script: dispatch, plus `install`/`uninstall` |
| `src/manage_gitignore/skill/SKILL.md` | the agent-facing procedure |
| `src/manage_gitignore/skill/references/` | on-demand detail (force-push procedure, question splitting, worked example) |
| `tests/` | pytest suite |

The skill ships **inside** the package rather than beside it, and nothing remaps
it at build time. So `manage_gitignore/skill/SKILL.md` names the same file in
this checkout and in `site-packages` — a path in a traceback, or the target of
the installed symlink, resolves to the repository by relative position, with no
layout translation to work out first.

## Development

```bash
pip install -e '.[dev]'
make verify     # lint + format check + mypy + tests — run before shipping
make build      # sdist + wheel into dist/
```

The package has **no third-party runtime dependencies** — only the standard
library, plus `curl` and `git` on `PATH`.

### Testing notes

- **No test touches the network.** A stub `curl` goes on `PATH` (see
  `tests/conftest.py`), so the real fetch path — including the streaming byte cap
  and the response validation — runs offline against canned responses.
- **Git behaviour is tested against real repositories**, not mocks. This is code
  that commits and pushes; a mock that agrees with a wrong assumption is worse
  than no test.
- Most tests pin a defect found in review. Where that is so, the docstring says
  which one, so a regression fails with its reason attached.

## Safety properties

Each fails closed, and each has a test:

- a response that is not exactly the requested gitignore.io block is never written
- a symlinked, FIFO, or oversized `.gitignore` is refused, never followed
- the file is re-read and verified after writing (block intact, custom rules
  present, no ANSI or bidi characters) before success is reported
- a `.gitignore` created or edited *during* the fetch is detected, not clobbered
- `commit` refuses a file whose checksum no longer matches what was verified, and
  proves the committed blob is that content — not just that the path matched
- a commit touching anything besides `.gitignore` is reported, never pushed
- `push` executes only what its own freshly computed plan permits, by explicit
  refspec, and will not force outside a genuinely diverged branch
- a force-push is leased against the remote sha the **user approved**, not the
  tracking ref that `push`'s own `git fetch` just refreshed
- `ext::` remotes and interactive credential prompts are refused outright
- no repo- or API-derived text can forge a line or an escape sequence in the
  summary

## Provenance

Built with Claude Code, then reviewed across ten rounds by a nine-agent panel
(skill design, Python, testing, application security, shell, documentation,
software design, test design, UX). Around 390 findings were applied; a few were
declined because they would have broken the job — those declines are recorded in
the code comments where they apply.

## License

MIT — see [LICENSE](LICENSE).
