# Working on manage-gitignore

This repository ships **one thing: a Claude Code skill**, delivered as a Python
package. `README.md` is written for people installing it. This file is for
whoever is editing it.

Run `make verify` before you push, and `make coverage` if you touched a script.
CI runs the tests on 3.10 through 3.14, lints and type-checks on 3.10, gates
every file at 95% coverage, and runs every pre-commit hook. A tag will not build
if any of that fails.

The workflows are audited by `zizmor`, as a hook, so it runs locally and in CI
alike. It reads them for the mistakes YAML cannot have an opinion about, and it
found four real ones: `actions/checkout` leaving the job's `GITHUB_TOKEN` in
`.git/config` seven times over, a pip cache restored into the job that builds
what gets published to PyPI, a release workflow with no concurrency group, and
Dependabot with no cooldown before proposing a brand-new release. It runs at
`--persona=auditor`, which also insists every job has a name and every
`permissions:` grant a comment saying why.

Two things were considered and declined, so nobody has to work out why twice:

- **`pip-audit`.** There are no runtime dependencies, so nothing it could find
  would ever reach a user's machine. It would cover the `dev` extra, where
  Dependabot already opens a pull request for every release — and a CVE in a
  linter blocking an unrelated documentation change buys nothing.
- **`agents/openai.yaml`.** Codex reads it for a display name and icon. It would
  be a second home for the skill's name and description, which `SKILL.md`
  already owns, in exchange for cosmetics on one of the three products and no
  way to check the result from here.

`pre-commit install` once, so the hooks run where they are cheap to fix.
`make verify` covers none of them — gitleaks, markdownlint, yamllint and the
base hygiene hooks all live only in `.pre-commit-config.yaml`.

The hooks job scans for secrets twice on purpose. The gitleaks hook runs
`gitleaks git --staged`, which is the right question locally and an empty one in
CI, where nothing is staged; a separate step runs `gitleaks dir` over the
checked-out tree, at the version the config pins.

## Layout

| Path | Owns |
| --- | --- |
| `src/manage_gitignore/skill/SKILL.md` | the agent-facing procedure |
| `src/manage_gitignore/skill/scripts/templates.py` | the `.gitignore` file: scan → recommend → fetch → merge → write → verify |
| `src/manage_gitignore/skill/scripts/gitwork.py` | git: status/diff, commit, push planning, push, facts |
| `src/manage_gitignore/skill/scripts/summary.py` | the end-of-run summary format |
| `src/manage_gitignore/skill/scripts/shared.py` | one sanitiser, one no-follow reader, one JSON contract |
| `src/manage_gitignore/skill/references/` | on-demand detail (force-push procedure, question splitting, worked example) |
| `src/manage_gitignore/cli.py` | the installer: `install` / `uninstall`. Nothing else |
| `src/manage_gitignore/agents.py` | which agents exist, how to spot one, where each reads skills |
| `tests/` | pytest suite |

## Five rules that hold the shape

Each has a test, so breaking one fails the build rather than rotting quietly.
The tests say *what*; this section says *why*, which is the part worth knowing
before you decide to argue with one.

**1. The package is an installer. It never owns the work.** `cli.py` links the
skill directory into `~/.claude/skills` and removes that link again. That is the
one job which cannot be done from inside the skill directory, because it is what
puts the directory where Claude Code looks. Everything else belongs to the
skill. Do not add a subcommand that does work; add a script, or an argument to
one.

**2. The scripts are self-contained.** They import each other by plain module
name, resolved from their own directory the way `python3 <dir>/foo.py` resolves
anything, and none of them imports `manage_gitignore`. This is what lets
`install` publish the skill as a bare symlink — the installed skill depends on
nothing the symlink does not already reach. The suite runs them as subprocesses
with `PYTHONPATH` *removed*, so a green run is evidence rather than assertion.

**3. No third-party runtime dependency.** Standard library only, plus `curl` and
`git` on `PATH`. The skill is installed by symlink, so nothing would ever install
a dependency on its behalf — an import of anything else is a runtime failure on
someone else's machine, not a packaging inconvenience. A test walks the scripts'
ASTs and rejects any import outside stdlib and each other. The `dev` extra is a
separate matter: PyYAML is there so the frontmatter test parses `SKILL.md` the
way the three products do, and it never ships.

**4. The skill is written for three products, not for one.** `SKILL.md` carries
only the six frontmatter fields the [Agent Skills spec](https://agentskills.io)
defines — Claude Code accepts a dozen more, and every one of them is a way to
work on one product and break on another. In the body, name capabilities rather
than tools: "your file-write tool", not "the Write tool". `AskUserQuestion` is
Claude Code's and may be named only where the text says so. `agents.py` owns the
matching half — where each product looks, and how to tell it is installed — so
adding a fourth is a row in one table. `tests/test_skill_metadata.py` enforces
all of this, including that no file calls a subcommand that no longer exists,
which is how `references/push-safety.md` came to tell agents to run
`manage-gitignore git ... push` for a whole release.

**5. The checkout and the installed tree are the same paths.** Nothing is
remapped at build time — no `force-include`. `manage_gitignore/skill/scripts/gitwork.py`
names the same file here and in `site-packages`, so a path in a traceback, or the
target of the installed symlink, traces back to this repository by relative
position with no layout translation to work out. This was a real defect once;
`test_nothing_remaps_the_skill_at_build_time` exists to stop it coming back.

## Design principle

**Anything a program can decide, a program decides.** The agent driving this
skill asks the user, judges the answers, writes the commit message, and relays
results — nothing else. Scanning, merging, verification, every number in the
summary, and every git mutation live in the scripts, where they are testable and
where a wrong answer is an exit code rather than a plausible sentence.

So when a step needs a new fact: return it from a script as a field in the JSON
that step already reads. Do not add a paragraph to `SKILL.md` telling the agent
to work it out. `SKILL.md` went from 538 to 275 lines by applying this once.

## Commands

```bash
pip install -e '.[dev]'
make verify     # lint + format check + mypy + tests — before every push
make coverage   # the same suite under coverage, then the per-file floor
make format     # ruff format + ruff check --fix
make build      # sdist + wheel into dist/
make install    # symlink the skill from this checkout into ~/.claude/skills/
```

The suite runs in parallel (`-n auto --dist loadfile`, from pytest-xdist): it
spends most of its time in subprocesses and throwaway git repos, so it scales
well across cores. `loadfile` keeps one module's tests on one worker, which
keeps a failure's output contiguous. To debug serially, `pytest -p no:xdist`.

`make install` points the link at the working tree, so edits take effect on the
next Claude Code restart with no rebuild. To test what users get instead, build
a wheel and `pipx install --force dist/*.whl`.

mypy needs both directories declared as package bases (`mypy_path` +
`explicit_package_bases` in `pyproject.toml`). With only one it reaches each
script under two module names at once — `manage_gitignore.skill.scripts.shared`
*and* `shared` — and refuses to check anything at all.

## Testing

- **No test touches the network.** A stub `curl` goes on `PATH` (see
  `tests/conftest.py`), so the real fetch path — including the streaming byte cap
  and the response validation — runs offline against canned responses.
- **Git behaviour is tested against real repositories**, not mocks. This is code
  that commits and pushes; a mock that agrees with a wrong assumption is worse
  than no test.
- Most tests pin a defect found in review. Where that is so, the docstring says
  which one, so a regression fails with its reason attached. Keep this up: when
  you fix a bug, the test that pins it should say what it is.

### Coverage

**Every file must stay at or above 95%**, enforced by `tests/check_coverage.py`
in both `make coverage` and CI. The floor is per file on purpose: a project
total hides a hole, because one thoroughly covered 800-line script carries a
barely-touched 200-line one well past 95% overall. The floor was 90% until every
file had been at 97.9% or better for long enough that 90 was not a floor at all,
only a number nothing could fail.

Coverage says a line ran. It does not say the tests would notice if that line
were wrong, and at 99% that is the only question left. `tests/mutate.py` answers
it: it changes one operator or literal at a time in `templates.py`, runs the two
test files that cover it, and reports what survived. By hand, never in CI —
`python3 tests/mutate.py`, about a minute on eight cores.

The first run scored **78/90**. The twelve survivors were: which `### Name ###`
section a dropped rule is attributed to (four), the fallback when a rule sits
before any section, the exact extent of the block when custom rules touch the
end marker, two diagnostics nothing read, and the header's template list.
`TestGapsFoundByMutationAudit` in `tests/test_gitignore.py` pins each, with the
surviving mutation named in the docstring. It now scores **89/90**.

The one survivor is `autojunk=False` in `reapply_custom`. Several inputs built
to trigger difflib's popularity heuristic produce an identical diff either way,
so it is equivalent as far as anything reachable goes. The argument stays: it is
a deliberate "do not guess" on the function that decides which of somebody's
rules come back.

`gitwork.py` has not had this treatment. Its tests drive real repositories, so a
run there costs minutes per mutant rather than seconds.

Measuring coverage needs `MG_COVER_SUBPROCESS=1` (which `make coverage` sets). Most of
the suite drives the scripts as subprocesses, which a plain coverage run cannot
see — without it the report reads about 66% when the truth is 99%. Do not chase
that phantom third with new tests; run `make coverage` and look at the real
number first.

What is deliberately not covered: two defensive branches in `gitwork.py` — a git
plumbing command failing outright, and `commit` failing *and* its cleanup
`git reset` failing too. Both would need a mock of git itself, which would test
the mock.

### Python versions

3.10 through 3.14, and the test matrix runs all five. Two constraints follow
from 3.10 that are easy to trip:

- `typing.NotRequired` is 3.11+. Use a total base class plus a `total=False`
  subclass instead (see `DetectRule` in `templates.py`). `typing_extensions`
  is not an option — a symlink-installed skill has nothing to install it.
- `tomllib` is 3.11+. The one test that needs it skips on 3.10.

mypy runs at `python_version = "3.10"` so it catches both classes of mistake
before a user on 3.10 does.

## Safety properties

These are the reason the tool is allowed near someone's repository. Each fails
closed and each has a test; treat a change that weakens one as a change to the
product, not a refactor.

- a response that is not exactly the requested gitignore.io block is never written
- a symlinked, FIFO, or oversized `.gitignore` is refused, never followed
- the file is re-read and verified after writing (block intact, custom rules
  present, no ANSI or bidi characters) before success is reported
- a `.gitignore` created or edited *during* the fetch is detected, not clobbered
- a run commits only what that run wrote. When `.gitignore` already carries an
  uncommitted change, the rebuild is based on the **committed** file, and the
  user's edit is re-applied on top in the work tree and put back staged or
  unstaged exactly as it was found — `git status` reads the same before and
  after. The work tree therefore holds more than the commit does, which is why
  `status` and `commit` both take `--facts`
- re-applying is done at the level of the custom rules, because this run owns the
  template block and the user owns everything outside it. A whole-file three-way
  merge conflicts on a deletion; a rule-level one does not. An edit *inside* the
  block cannot be carried across and is reported rather than silently dropped
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

## Releasing

Tag-driven and gated. Bump `version` in `pyproject.toml`, tag `v<that version>`,
push the tag. `release.yml` then runs, in order:

1. `check-tag` — the tag must match the packaged version. Seconds, so a typo
   fails before five test matrices have run.
2. `gates` — `ci.yml` called as a reusable workflow: tests on 3.10 through 3.14,
   lint, type-check, and the per-file coverage floor. Not a copy of those steps;
   a copy drifts, and the copy is the one a release would be trusting.
3. `build` — sdist and wheel, `twine check`, uploaded as an artifact.
4. `github-release` — attaches them to a GitHub Release.
5. `pypi` — Trusted Publishing (OIDC), no token stored here. Last on purpose:
   the artifacts are already downloadable by then, and this is the one step that
   cannot be undone.

Nothing is built unless every gate passes.

The trusted publisher is configured at
<https://pypi.org/manage/account/publishing/> — project `manage-gitignore`,
owner `grammy-jiang`, repository `manage-gitignore`, workflow `release.yml`,
environment `pypi`. If the `pypi` job ever fails on a claim mismatch, `build`
and `github-release` still succeed, so the tag has already produced downloadable
artifacts and nothing was uploaded: fix the publisher and re-run the job. No
version is burned by a failed publish, only by a successful one.

Check `git ls-files -z | xargs -0 grep -lP '\x1b'` before tagging — or just run
the suite, which now does it. A file written through a colorizing shell looks
correct in a terminal and is corrupt on disk, and PyPI will not let you replace
a version once it is published.

## Provenance

Built with Claude Code, then reviewed across ten rounds by a nine-agent panel
(skill design, Python, testing, application security, shell, documentation,
software design, test design, UX). Around 390 findings were applied; a few were
declined because they would have broken the job — those declines are recorded in
the code comments where they apply, so a comment explaining why something looks
wrong is probably load-bearing. Read it before you tidy it away.
