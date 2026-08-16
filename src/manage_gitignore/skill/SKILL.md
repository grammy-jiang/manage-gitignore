---
name: manage-gitignore
description: Build a repository's .gitignore from gitignore.io, keeping the template block verbatim and the repo's own custom rules intact, then review the diff and — with confirmation — commit and push it. Use when asked to create, refresh, or replace a .gitignore, add ignore rules for a language / editor / OS, inspect an existing .gitignore, or "get a gitignore from gitignore.io".
license: MIT
compatibility: Needs python3 3.10 or newer, plus git and curl on PATH, and outbound HTTPS to gitignore.io. Writes only the .gitignore of the repository it is pointed at, plus temporary files outside it. Runs in any agent that reads the Agent Skills format.
allowed-tools: Bash(python3:*) Bash(mktemp:*) Bash(rm:*) Read Write
metadata:
  homepage: https://github.com/grammy-jiang/manage-gitignore
---

# manage-gitignore

A `.gitignore` here is two things: the **template block** the API returns between
its `# Created by …` / `# End of …` markers, written verbatim, and the **custom
rules** outside it, preserved and de-duplicated against the fresh block.

## Division of labour

**Anything a program can decide, a program decides.** The scripts in `scripts/`
own every mechanical step — scanning, merging, verifying, all summary numbers,
tracked-vs-untracked, the diff, the commit, and what a push may do.

**Yours:** ask the user, judge their answers, write the commit message, relay
what the tools say. That is all. Never re-derive a tool's answer by eye, never
reformat its output into your own numbers, and never run `git add`/`commit`/`push`
yourself — `scripts/gitwork.py` is the only path to a mutation, and it fails
closed.

## Placeholders

`<skill-dir>` is the directory holding this SKILL.md —
`~/.claude/skills/manage-gitignore` under Claude Code,
`~/.agents/skills/manage-gitignore` under Codex or GitHub Copilot. `<repo>` is
the repository being worked on. Substitute both; never run a command with the
angle brackets still in it.

The scripts need only Python 3.10+, `git`, and `curl`. They import each other
from their own directory, so they run from wherever the skill is installed, with
nothing to install first. If one is missing, say so and stop; do not fall back to
hand-written git or a hand-written `.gitignore`.

**Any non-zero exit stops that action** — report it verbatim. The one exception
is Step 3's exit 3, documented there.

## What this skill needs from you

Two capabilities, named here by what they do rather than by any one agent's
tool names, because this skill runs under several:

- **Ask a question and wait for the answer.** Claude Code has AskUserQuestion,
  which renders the options as a menu; elsewhere, ask in prose and wait. Never
  assume an answer and never proceed on silence. **If no user can be reached at
  all** — a headless or non-interactive run — stop at the first choice and say
  which confirmation is missing.
- **Write and read a file directly.** Where a step says to write a file, use
  your file-write tool rather than a shell heredoc — the last rule under
  [Rules](#rules) says why that distinction matters.

The scripts reach gitignore.io over the network with `curl`, and put temporary
files outside the repository. **If a sandbox denies either**, report what it
said, verbatim, and stop. A `.gitignore` you wrote from memory is not the one
the user asked for.

## Step 0 — Inspect only

If the user only wants to *see* what a repo ignores:

```bash
python3 "<skill-dir>/scripts/templates.py" --dir "<repo>" --detect
```

Relay it and stop. Nothing is written, so there is nothing to review or commit.

## Step 1 — Scan

```bash
python3 "<skill-dir>/scripts/templates.py" --dir "<repo>" --recommend
```

Returns JSON: `always_on` (fixed policy), `recommended` (`[{name, reason}]`,
where `reason` is the file that triggered it), `previous`, `carried_over` (the
subset of `previous` not already covered — do not re-derive it), `custom_lines`,
and `proposed`, the starting set for Step 2. Do not scan the tree yourself or
second-guess a `reason`.

## Step 2 — Ask

**`always_on` is not up for a vote.** State it as a fact with its reason:
"always included (repo-independent hygiene — OS, editor and git artifacts turn up
whatever this project is written in): <the names Step 1 returned>."

**Say what unselecting costs, before the options**: anything in `carried_over` is
ignored *today*; dropping it makes those files visible to git again.

Ask about `recommended` (each with its `reason` — `node ← package.json`),
`carried_over` (labelled as such), a few extras you suggest, and a free-text
"Other" — *exact catalogue name(s), comma-separated*. A near-miss is rejected by
the tool, not quietly corrected. Never enumerate the catalogue in the question.

See [references/asking-the-user.md](references/asking-the-user.md) when the set
does not fit one question. **`carried_over` is never truncated.**

The final list is `always_on` plus whatever the user selected. **Write it to a
file with your file-write tool**, one name per line, at a `mktemp` path outside the
repo — free-text names must never reach a command line, for the same reason
commit messages go through a file.

Asked how many templates exist: `templates --list --count` prints the number and
fails loudly rather than reporting `0` for an unreachable API. To browse, drop
`--count` and summarise by category rather than pasting hundreds of names.

## Step 3 — Write

```bash
python3 "<skill-dir>/scripts/templates.py" --dir "<repo>" --force \
  --facts-out "<facts.json>" --templates-file "<templates.txt>"
```

Always pass `--force`: with no existing file it does nothing, and with one it is
safe precisely because the tool carries the custom rules across.

**Pick `<facts.json>` once, here, and pass that same path to every `--facts`
later**, outside the repo so it stays out of the diff being reviewed. A
different path is refused, not silently merged into.

You delete both temp files once each command returns: `rm -f "<templates.txt>"`,
and `rm -f "<msgfile>"` after Step 4. The tools never unlink their inputs.

The tool verifies its own write before reporting success — block intact, template
set exact, every custom rule present, no ANSI, bidi or zero-width characters.

**On a non-zero exit:**

- **exit 3, unknown template name** — the only recoverable case. Re-ask *for the
  rejected names only*, quoting the near matches it printed (`"pythonn" not
  found — did you mean python?`), write a fresh `<templates.txt>`, and re-run.
  **Retry once.** If that fails, report the near matches and stop.
- **exit 4, `.gitignore` is staged for deletion** — the user's pending change is
  "this file should be gone", and no rebuild honours that. Relay it and stop.
  They finish the deletion or unstage it; then the run starts again from Step 1.
- **anything else** — nothing usable was written. Report it and end the run: no
  Step 4, and no Step 5 summary for a run that produced no file.

On success relay its report: custom rules kept, each duplicate dropped with the
template that covers it, and any `Review before committing` lines — negations
(`!…`, which un-ignore a path) or patterns broad enough to ignore the tree.
**Those flags cover the template block only, never the carried-over custom
rules**, so their absence certifies nothing about the rest of the diff.

**If it says `Carried across your uncommitted change`**, read
[references/carried-across.md](references/carried-across.md) and follow it — the
file on disk is then deliberately not what will be committed.

Otherwise: do not diff the file by hand and do not describe it as the change.
Step 4's `status` reports the committed version, and it is the one to show.

## Step 4 — Review, commit, push (`.gitignore` ONLY)

Never stage, commit, or suggest committing any other file — and never another
change to this one. Step 3 refuses to start from a `.gitignore` that already
carries an uncommitted edit, so everything the diff shows here is this run's
work. That is what makes it honest to commit the whole file.

### 1. Show the diff

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" status --facts "<facts.json>"
```

**Always pass `--facts`.** When Step 3 carried an uncommitted change across, the
work tree holds that change too; without the facts file this would diff the work
tree and show the user their own edit as if this run had made it.

It picks the right command for the file's state and returns the real diff — show
it. If `diff_is_stub` is true the diff is a one-line "this file is new", so show
the file itself too (your file-read tool, or `cat`); on a first run the Step 3
flags are otherwise the only review surface. If `suspicious_characters` is true,
say so: the terminal may not be rendering what the file says.

If `skip_reason` is not null, the rest of Step 4 does not apply: go to Step 5
with `--note "<skip_reason>"` and no `--hash`.

Read the diff for what *the user* should weigh — a flagged negation, a broad
pattern, a custom rule they will be surprised to see gone.

### 2. Ask

Assemble everything the answer depends on first, so the user approves the actual
change and not an intention:

- **Draft the commit message now, on one line**, and show it. The summary records
  only the subject, so a body would be approved and never shown back. If the user
  supplies several lines, say only the first is recorded and confirm it.
- **Say the file is already written.** *Don't commit* leaves the change on disk;
  it does not undo it. Quote `status`'s `discard_command` as the way back — it
  is computed from the file's state, so do not compose one yourself.
- **Name where a push would go**, from the tool rather than by re-deriving it:

  ```bash
  python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push-plan
  ```

  Say its `guidance` sentence — it already names the destination *and its URL*,
  because a remote's nickname says nothing about where code goes. If
  `suspicious_characters` is true, say that too. If `permits_push` is false, say
  a push looks unlikely and why; the three options below never change.

Then ask — exactly these three, never an "also commit other changes" option:
**Commit + push** / **Commit only** (local) / **Don't commit**.

### 3. Commit

Only on *Commit + push* or *Commit only*. Write **the exact text shown in item
2** to a `mktemp` file — do not redraft it — then:

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" commit \
  --message-file "<msgfile>" --facts "<facts.json>"
```

It stages only `.gitignore`, commits with `--only` so the rest of the index
survives, and proves the commit holds that one file *and the content this run
verified*.

**Read `verdict`:**

- `ok` — keep the returned `hash` for Step 5.
- anything else — **do not push.** Relay its `remedy`; **never run it yourself**
  — discarding a commit that exists is the user's call. It has already written
  the outcome into the facts file, so Step 5 needs nothing extra from you. Do
  not pass the hash to Step 5.

A non-zero exit with no verdict means nothing was committed and the index is as
you found it. Report it and stop.

### 4. Push

Only if the user chose a push option.

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push-plan
```

- `permits_push: false` → report `guidance` and go to Step 5. None of these is an
  error to fix; `stop-up-to-date` is a success.
- `action: "diverged"` → [references/push-safety.md](references/push-safety.md).
  Keep `upstream_sha`; the force needs it.
- otherwise:

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" push --facts "<facts.json>"
```

`push` recomputes the plan and executes only what it permits, by explicit
refspec, so `push.default=matching` can never widen it. It refuses to force
outside a diverged branch.

A `no-upstream` plan whose `remote` is `null` (several remotes, no `origin`)
needs one more question: show each candidate **with its URL** from `remote_urls`,
then pass `--remote`.

**A push that did not happen appears two ways** — JSON with `pushed: false`, or a
non-zero exit with no JSON. Treat both the same: report it and go to Step 5.
`push --facts` has already written down which of the two it was, so there is
nothing for you to record; carry git's error text in `--note` when there is one.

## Step 5 — Summary

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" facts --facts "<facts.json>" \
  --requested-action "<what they chose>" --hash "<hash>" --note "<why, when needed>"
```

**What happened is derived, not declared.** `commit --facts` recorded the commit,
`push --facts` recorded the push and how far it got, and a refused commit
recorded its own outcome — so `facts` reads the answer off the document rather
than being told it. There is no choice for you to work out, and nothing to say
when the run went normally.

Pass only what the tools cannot know:

- `--requested-action` — the user's Step 4 answer, one of `commit + push`,
  `commit only`, `not committed`. **Always pass it**, including when the run
  ended early. It is the only thing here no command can read off the repository,
  and without it a push that failed is summarised as a run where none was ever
  wanted. It never overrides an outcome; the summary shows both when they differ.
- `--hash` — only when `verdict` was `ok`. It is verified, not believed.
- `--note` — only to carry a reason: a `skip_reason` from Step 4 item 1, the
  `guidance` from a plan that did not permit a push, or the error text from a
  push that failed. It repeats, and appends without touching computed fields.

Never hand-edit the file.

```bash
python3 "<skill-dir>/scripts/summary.py" "<facts.json>"
```

That output *is* the closing summary; do not hand-format a second one. Then
`rm -f "<facts.json>"`. A worked example is in
[references/example-output.md](references/example-output.md).

## Rules

- This skill manages **gitignore.io templates** plus the file's existing custom
  rules. A literal pattern ("ignore `*.log`") is not a template name — check
  `templates --list` in case it is, and if not, say plainly that this skill
  cannot append an arbitrary line and that doing so is an ordinary edit outside
  it. Never approximate with a nearby template.
- Never hand-write `.gitignore` or hand-edit the template block or custom rules.
  If the API is unreachable, say so; do not fake it.
- Never run `git add`/`commit`/`push` yourself. `scripts/gitwork.py` is the only
  path to a mutation, and it fails closed.
- This skill modifies and commits **only** `.gitignore`, and within it only the
  change this run made.
- Commit messages go through a file and `--message-file`. Never a heredoc or
  `-m "$(...)"` — some shells inject ANSI bytes into both, and those end up
  stored in the commit.
- Never push without explicit confirmation, and never force without the separate
  confirmation in [references/push-safety.md](references/push-safety.md).
