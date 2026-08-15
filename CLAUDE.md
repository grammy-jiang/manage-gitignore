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
| `src/manage_gitignore/skill/references/` | on-demand detail (force-push procedure, question splitting, carry-across, worked example) |
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

## Design principles

**Anything that can be deterministic, is.** A result you cannot reproduce is a
result you cannot act on, and the difference usually costs nothing to remove.
Where this repository holds the line, and what it deliberately gave up:

- **Property tests are derandomised in the gate.** Hypothesis draws different
  inputs on every run by default — measured, before the fix: three runs of one
  property drew three different input sets. A green run then meant "150 random
  examples happened to pass", and a counterexample might never be seen again.
  `tests/conftest.py` registers a `gate` profile (`derandomize=True`) so a
  commit's result is a property of the commit. The search is not lost, it
  moves: `property-search.yml` runs the `explore` profile weekly with a free
  seed and a budget of 2,000, where a red run is news rather than a blocked
  merge. Determinism where a result must be trusted, randomness where discovery
  is wanted — not one setting pretending to do both.
- **Dev dependencies are exact, not floors.** A floor means CI installs whatever
  released that morning, so the same commit passes today and fails tomorrow on
  somebody else's unrelated pull request. `filterwarnings = ["error"]` makes
  that sharper still. Exact pins are only safe while something watches them, so
  Dependabot bumps them weekly after a cooldown, through the same gates.
- **The build is reproducible.** `release.yml` sets `SOURCE_DATE_EPOCH` from the
  tagged commit, so re-running the release on a tag rebuilds the same bytes and
  "this artifact is that tag" is checkable. `TestTheBuildIsReproducible` asserts
  the property, and its second test asserts a different epoch gives a different
  wheel — otherwise the first would pass on a build that ignored the variable.
- **Filesystem order never reaches a decision.** `scan_repo` sorts every
  directory and file listing, because `os.walk` order is filesystem-dependent
  and "which marker file explains this recommendation" must not be.
- **No set is iterated into output.** Every set in the scripts is a membership
  test or a comparison; where one reaches a message it is `sorted()` first.
  Python randomises string hashing per process, so an unsorted set in a summary
  would reorder between runs of the same input.

What is deliberately *not* deterministic: `-n auto` picks a worker count from
the machine, so the shape of a run varies even though `--dist loadfile` keeps
outcomes stable; and `mutate.py`'s per-mutant timeout is wall-clock derived, so
a heavily loaded machine can push a mutant into "unscored". Both fail loudly
rather than silently — the second is why unscored leaves the denominator.

**Anything a program can decide, a program decides.** The agent driving this
skill asks the user, judges the answers, writes the commit message, and relays
results — nothing else. Scanning, merging, verification, every number in the
summary, and every git mutation live in the scripts, where they are testable and
where a wrong answer is an exit code rather than a plausible sentence.

So when a step needs a new fact: return it from a script as a field in the JSON
that step already reads. Do not add a paragraph to `SKILL.md` telling the agent
to work it out. `SKILL.md` went from 538 to 275 lines by applying this once.

**`SKILL.md` is loaded on every run, so prose is the expensive place to keep a
decision** — it costs tokens each time and can be applied wrongly, where a field
costs nothing and cannot. A second pass found four still written as instructions,
and each became JSON: `discard_command` (which undo suits the file's state — `rm`
where `git checkout` was needed destroys a tracked file), `skip_reason` (why the
run stops, in the words the summary should carry), `diff_is_stub`, and the
outcome itself. That last one was a five-row table the agent applied from memory
of what it had just done; `facts` derives it from what `commit --facts` and
`push --facts` wrote down, so a push that failed can no longer be summarised as
`commit + push`. A refused commit records its own choice and note rather than
printing them for the agent to hand back.

Two rules follow, and they are the ones to apply to the next change:

- **If the answer is already in the JSON, do not restate it in prose.** A
  sentence telling the agent how to combine two fields is a function that belongs
  in the script.
- **Detail needed by a minority of runs belongs in `references/`.** Those are read
  on demand, so a paragraph there is free to every run that never needs it —
  which is why the carry-across explanation is `references/carried-across.md` and
  `SKILL.md` keeps only the trigger and the pointer.

The facts document is stamped `"tool": "manage-gitignore"`, and every command that
reads one refuses a document without it. A path that does not exist already failed
loudly; one that exists and holds something else was merged into and reported as
success, so the summary came out missing sections rather than visibly wrong. That
was a warning addressed to the agent; it is enforced now, which is the same move.

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

One thing does talk to gitignore.io, and it is not a test: `tests/check_api_contract.py`,
run weekly by `.github/workflows/api-contract.yml`. Stubbing `curl` everywhere
means the contract with the outside world is asserted only against fixtures
written here — if Toptal changed the markers or the URL, the skill would break
for every user with the whole suite green. The check calls `templates.fetch_text`
and `templates.check_api_block` rather than describing the contract again, so it
cannot drift from what the skill requires, and it is the only place the real
fetch path runs against the real service. It exits 2 rather than 1 when the API
is merely unreachable: a watcher that cried wolf at every network blip would be
switched off within a month.

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
`python3 tests/mutate.py`, about a minute on eight cores. `--all-functions`
covers the impure half too, and costs tens of minutes rather than one; see
"What the audits cover" below.

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

`gitwork.py` has had it too — `python3 tests/mutate.py --subject gitwork`, its
pure half only, for the same reason: the rest of that file runs git, and a
mutant there costs minutes rather than seconds.

It scored **32/43**. Every survivor was a string literal, and every one is shown
to somebody at a decision point — where a push would land, what kind of value
was refused, and the message saying a commit touched more than `.gitignore` and
must not be pushed. Dropping the parentheses around a URL leaves the URL in the
string, so a substring check still passed while the user was shown
`origin/maingit@github.com:x/y.git`. It now scores **43/43**.

`summary.py` and `shared.py` have had it as well. `shared` scored **2/5** and is
now **5/5** — and the three survivors were the audit's own fault rather than the
suite's: `shared.py` is imported by every other script, so with only
`tests/test_shared.py` selected, breaking `refuse_option_like` so that it
rejected *every* value survived. Its subject runs the whole suite now. Five
mutations is still under a minute.

`summary.py` scored **114/198**, and its survivors were the same shape as the
first two audits at four times the scale: string literals and **dict keys**.
`facts.get("commit")` mutated to `facts.get("")` makes a whole section vanish,
and every test went on passing because each looked for one row rather than at
the document. So the tests look at the document now — six golden comparisons
against fixtures chosen to reach different branches. **189/198**, and **190/198**
once `tests/test_summary_properties.py` poisoned every externally-derived field
rather than three.

The eight that remain are recorded rather than chased, because each needs a
fixture built to defeat it rather than to describe a run:

| Line | Why it survives |
| --- | --- |
| `write.get("mode", "new")` | the default is only ever compared against `"overwrite"`, so emptying it changes nothing — equivalent |
| `scan.get("gitignore", "none")`, `("prev_templates_count", "?")`, `("custom_lines", 0)` | reached only when the key is absent *and* the surrounding state is present |
| `commit.get("choice", …)` | the fixtures supply the value the default already produces |
| `len(parts) > 1`, `strict=True`, `and` in the NET guard | need a fixture built around the boundary rather than around a run |

Four audits, one finding: what goes unchecked is not the logic, it is the
sentence the logic produces. Asserting that a message exists leaves every word
of it free.

#### What the audits cover, and what they do not

Everything above ran on the *pure* half of its subject, because that is what
keeps a run to a minute. `--all-functions` covers the rest, and has now been run
once against both scripts that have an impure half: **`gitwork.py` 585/742** and
**`templates.py` 425/538**, from 559 and 405 before the gaps were pinned. Both
runs are harness-clean — nothing unscored.

Three fixes to `mutate.py` came first, because the run could not finish:

- **The `if __name__ == "__main__":` guard is never mutated.** Flipping `==` to
  `!=` makes the module run `main()` when it is *imported*, so pytest exits
  during collection with INTERNALERROR — neither pass nor failure, and nothing
  about the mutant is learned. Excluded for the same reason docstrings are: a
  guaranteed non-result at the price of a full test run. The `"__main__"`
  literal stays mutable, because emptying it stops `python3 gitwork.py --status`
  doing anything and the suite drives exactly that.
- **An unscoreable mutant no longer aborts the run.** The first attempt lost
  seven workers' results to one such mutant at index 743 of 744. It is reported
  separately now and leaves the denominator: counting an INTERNALERROR as a kill
  is the same lie as counting a missing dependency as one.
- **A per-mutant timeout**, from the measured baseline rather than a constant,
  since the same audit runs on a laptop and on twenty cores. Without it a
  mutated loop bound stalls a worker for the rest of the run, silently.

The impure halves are where the safety properties live, and three of them were
unchecked. `check=True` survived on all three `git push` calls — with
`check=False` a rejected push is reported as `"pushed": true` and recorded in
the facts file, so the agent tells the user their work is on the remote when it
is not, and nothing had ever driven a push the remote refuses. Nothing looked at
`GIT_TERMINAL_PROMPT=0` or `protocol.ext.allow=never`. And curl's `-fsS` could be
deleted: without `-f` curl prints the server's error page as the body and exits
0, so a 404 becomes the text this tool treats as a template block. A test named
`--proto`, `-L` and both bounds, and still missed it — which is the argument for
asserting the whole command line rather than the flags somebody thought of.

Then a second pass, because "the rest are only diagnostics" is a category and
not a reading. Going through the *behavioural* survivors one at a time found
four more real ones — `check=True` on the two push legs the first pass had not
covered, `"forced"` on a first push, and `"pushed"` on the `remote-moved`
refusal — and one shape that had nothing to do with messages:

**Four constants were checked only against themselves.** `SCAN_MAX_DEPTH` built
both fixtures in its own boundary test; `REASON_MAX_LEN` was what the truncation
tests measured the result against; both recoverable exit codes were asserted as
`out.returncode == gi.EXIT_*`. Raise any of them and fixture and expectation move
together. The exit codes are the worst case, because they are a caller contract —
`SKILL.md` tells agents to branch on them rather than match message text — so a
silent change is a breaking change with a green suite. Each is now also written
out as a number, which is the only form that can disagree.

The survivors that remain are the same shape as every earlier audit, at the
largest scale yet: better than four in five are string literals. What is left is
deliberate. `main` in both files is argparse wiring, and a test asserting help
text is a second copy of the help text that will drift from the first. The rest
are diagnostics on paths whose *behaviour* is already checked — but note that
this exact sentence was written once before, and reading the list rather than
trusting it found nine more kills per script. Re-read it before believing it.

Recorded as equivalent rather than chased:

| Mutation | Why it cannot be reached |
| --- | --- |
| `or` → `and` in `check_api_block`'s header guard | with `and`, a `# Created by` line lacking `/api/` falls through to the URL check — and any string passing *that* check contains `/api/`, since `API` ends in `/api` |
| `FETCH_MAX_SECONDS + 10` → `- 10`, and `10 → 11` | the grace period on top of a 20s bound; no reachable input distinguishes them |
| `65536 → 65537` in the read loop | a chunk size, not a bound: the cap is checked against the accumulated body |
| `near[:5]` in `validate`'s substring pass | the outer `near[:5]` truncates the same list again, so the inner bound cannot reach the output |

Two of the tests written for this audit passed while proving nothing, and only
re-applying the mutation found it: one measured against `MAX_ERR_LEN` itself, so
`400 → 401` moved the test along with the code, and one used a one-character
line to exercise an 80-character slice. **A check that derives its expectation
from the thing it is checking cannot fail.** Verify a new test by breaking the
code it claims to protect.

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
