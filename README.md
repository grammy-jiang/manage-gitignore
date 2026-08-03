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

The package has **no third-party runtime dependencies** — standard library only,
plus `curl` and `git` on `PATH`.

See [CLAUDE.md](CLAUDE.md): repository layout, the rules that hold its shape,
build and test commands, and the release process.

## License

MIT — see [LICENSE](LICENSE).
