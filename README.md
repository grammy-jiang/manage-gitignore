# manage-gitignore

[![PyPI](https://img.shields.io/pypi/v/manage-gitignore.svg)](https://pypi.org/project/manage-gitignore/)
[![Python](https://img.shields.io/pypi/pyversions/manage-gitignore.svg)](https://pypi.org/project/manage-gitignore/)
[![Downloads](https://img.shields.io/pypi/dm/manage-gitignore.svg)](https://pypistats.org/packages/manage-gitignore)
[![Runtime dependencies](https://img.shields.io/badge/runtime%20deps-none-brightgreen.svg)](#contributing)
[![License](https://img.shields.io/pypi/l/manage-gitignore.svg)](LICENSE)

[![CI](https://github.com/grammy-jiang/manage-gitignore/actions/workflows/ci.yml/badge.svg)](https://github.com/grammy-jiang/manage-gitignore/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-%E2%89%A590%25%20per%20file-brightgreen.svg)](tests/check_coverage.py)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-checked-2A6DB2.svg)](https://mypy-lang.org/)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen.svg?logo=pre-commit)](https://pre-commit.com/)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-spec%20compliant-6E56CF.svg)](https://agentskills.io/specification)

Build a repository's `.gitignore` from [gitignore.io](https://www.toptal.com/developers/gitignore),
writing the template block **verbatim** and preserving the repo's own custom rules —
then review the diff and, with confirmation, commit and push it.

This repository ships **one thing: an [Agent Skill](https://agentskills.io)**,
delivered as a Python package. It runs under
[Claude Code](https://claude.com/claude-code),
[Codex](https://developers.openai.com/codex/skills) and
[GitHub Copilot](https://docs.github.com/en/copilot/concepts/agents/about-agent-skills).
The package is the delivery mechanism — it carries the skill directory and
provides the `install`/`uninstall` pair that puts it where each of those looks.
It is not a general-purpose CLI.

## Install

```bash
pipx install manage-gitignore   # or: pip install manage-gitignore
manage-gitignore install        # link it wherever your agents will find it
```

`install` works out which agents are on this machine — a launcher on `PATH`, or
the product's own configuration directory — and links the skill into each one's
skills directory:

| Agent | Directory | Reload with |
| --- | --- | --- |
| Claude Code | `~/.claude/skills/` | restarting it |
| Codex | `~/.agents/skills/` | restarting it |
| GitHub Copilot CLI | `~/.agents/skills/` | `/skills reload` |

Codex and Copilot both read `~/.agents/skills`, so a machine with both gets one
link rather than the same skill twice. `--dry-run` shows the plan without
touching anything; `--agent claude` (repeatable), `--all` and `--dest DIR`
overrule the detection when it guesses wrong or finds nothing.

`install` links rather than copies, so upgrading the package upgrades the skill —
no second step, and no chance of the two drifting. `manage-gitignore uninstall`
removes those links again. It sweeps every directory it could have written to,
not only the ones whose agent is still installed, and refuses to touch anything
it did not create: a real directory, or a link pointing somewhere else, is left
alone unless you pass `--force`. Removing the package itself is
`pipx uninstall manage-gitignore`.

`install` and `uninstall` are the only commands. Everything else happens through
the skill.

## Use

Ask your agent, in whatever words you would use anyway:

> Give this repo a proper `.gitignore`.
>
> Add Rust and JetBrains ignores.
>
> What does my `.gitignore` actually cover?

All three match a skill against its `description`, so asking for the job is
usually enough — none of them needs the skill named.

### Calling it by name

| Agent | In a session | How the mention works |
| --- | --- | --- |
| Claude Code | `/manage-gitignore` | pick it from the `/` menu, or type the name |
| Codex | `$manage-gitignore` | type `$` for the list, or `/skills` for the picker |
| GitHub Copilot CLI | `/manage-gitignore` | named inside the sentence: `Use the /manage-gitignore skill to …` |

From a shell, with no interactive session:

```bash
claude   -p '/manage-gitignore give this repo a proper .gitignore'
codex    exec '$manage-gitignore add Rust and JetBrains ignores'
copilot  -p 'Use the /manage-gitignore skill to show what my .gitignore covers'
```

To confirm the agent can see it: `/skills` in Codex or Copilot, `copilot skill
list` from a shell, and the `/` menu in Claude Code. A skill installed mid-session
needs `/skills reload` in Copilot, and a restart in Claude Code or Codex.

### Two things that differ by agent

**Codex sandboxes the network.** The skill fetches from gitignore.io with
`curl`, so a run under a sandbox that denies network — `codex exec`'s default —
reports the refusal and stops rather than inventing a `.gitignore`. Give it
`--sandbox workspace-write` with network enabled, or an approval mode that
allows the fetch.

**Only Claude Code has a menu.** There, confirmations arrive as a multiple-choice
prompt; under Codex and Copilot the same questions are asked in prose and
answered in text. Nothing is skipped either way — the skill never commits or
pushes without an explicit answer.

The skill takes it from there: it scans the repository, proposes a template set
with the file that justifies each one, asks you to confirm, writes the file
preserving your own custom rules, shows you the real diff, and — only with your
say-so — commits and pushes it.

The scripts under `skill/scripts/` are how the skill does that work. They are
its internals, driven by `SKILL.md`; **calling them yourself is not a supported
interface** and their arguments may change without notice.

## Safety properties

This tool writes to your repository and, with your say-so, pushes. Each of these
fails closed, and each has a test:

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

Most of that list came from review rather than from the first draft: this was
built with Claude Code, then examined across ten rounds by a nine-agent panel
(skill design, Python, testing, application security, shell, documentation,
software design, test design, UX), and around 390 findings were applied.

## Contributing

Runs on Python 3.10 through 3.14, with **no third-party runtime dependencies** —
standard library only, plus `curl` and `git` on `PATH`.

See [CLAUDE.md](CLAUDE.md): repository layout, the rules that hold its shape,
build and test commands, and the release process.

## License

MIT — see [LICENSE](LICENSE).
