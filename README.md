# manage-gitignore

Build a repository's `.gitignore` from [gitignore.io](https://www.toptal.com/developers/gitignore),
writing the template block **verbatim** and preserving the repo's own custom rules —
then review the diff and, with confirmation, commit and push it.

This repository ships **one thing: a [Claude Code](https://claude.com/claude-code)
skill**, delivered as a Python package. The package is the delivery mechanism —
it carries the skill directory and provides the `install`/`uninstall` pair that
puts it where Claude Code looks. It is not a general-purpose CLI.

## Install

```bash
pipx install manage-gitignore   # or: pip install manage-gitignore
manage-gitignore install        # symlink the skill into ~/.claude/skills/
```

Restart Claude Code, and the skill is available. `install` links rather than
copies, so upgrading the package upgrades the skill — no second step, and no
chance of the two drifting. `manage-gitignore uninstall` removes that link
again, and refuses to touch anything it did not create: a real directory, or a
link pointing somewhere else, is left alone unless you pass `--force`. Both take
`--dest` if your skills directory is not `~/.claude/skills`. Removing the
package itself is `pipx uninstall manage-gitignore`.

`install` and `uninstall` are the only commands. Everything else happens through
the skill.

## Use

Ask Claude Code, in whatever words you would use anyway:

> Give this repo a proper `.gitignore`.
>
> Add Rust and JetBrains ignores.
>
> What does my `.gitignore` actually cover?

The skill takes it from there: it scans the repository, proposes a template set
with the file that justifies each one, asks you to confirm, writes the file
preserving your own custom rules, shows you the real diff, and — only with your
say-so — commits and pushes it.

The scripts under `skill/scripts/` are how the skill does that work. They are
its internals, driven by `SKILL.md`; **calling them yourself is not a supported
interface** and their arguments may change without notice.

## Design principle

**Anything a program can decide, a program decides.** When an agent drives this,
it asks the user, judges the answers, writes the commit message, and relays
results — nothing else. Scanning, merging, verification, every number in the
summary, and every git mutation live in the code, where they are testable and
where a wrong answer is an exit code rather than a plausible sentence.

## Layout

Everything the skill does lives in the skill directory. The package around it is
one file.

| Path | Owns |
|---|---|
| `src/manage_gitignore/skill/SKILL.md` | the agent-facing procedure |
| `src/manage_gitignore/skill/scripts/templates.py` | the `.gitignore` file: scan → recommend → fetch → merge → write → verify |
| `src/manage_gitignore/skill/scripts/gitwork.py` | git: status/diff, commit, push planning, push, facts |
| `src/manage_gitignore/skill/scripts/summary.py` | the end-of-run summary format |
| `src/manage_gitignore/skill/scripts/shared.py` | one sanitiser, one no-follow reader, one JSON contract |
| `src/manage_gitignore/skill/references/` | on-demand detail (force-push procedure, question splitting, worked example) |
| `src/manage_gitignore/cli.py` | the installer: `install` / `uninstall`. Nothing else |
| `tests/` | pytest suite |

Two properties this layout is chosen for, both of them tested:

**The scripts are self-contained.** They import each other by plain module name,
resolved from their own directory the way `python3 <dir>/foo.py` resolves
anything, and they never import `manage_gitignore`. The skill directory is
therefore complete on its own — which is what lets `install` publish it as a
bare symlink, with the installed skill depending on nothing the symlink does not
already reach. The test suite runs the scripts as subprocesses with `PYTHONPATH`
*removed*, so a green run is evidence of this rather than an assertion about it.

**The checkout and the installed tree are the same paths.** Nothing is remapped
at build time, so `manage_gitignore/skill/scripts/gitwork.py` names the same file
here and in `site-packages`: a path in a traceback, or the target of the
installed symlink, traces back to this repository by relative position, with no
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
