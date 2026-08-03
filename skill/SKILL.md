---
name: manage-gitignore
description: Build a repository's .gitignore from gitignore.io, keeping the template block verbatim and the repo's own custom rules intact, then review the diff and — with confirmation — commit and push it. Use when asked to create, refresh, or replace a .gitignore, add ignore rules for a language / editor / OS, inspect an existing .gitignore, or "get a gitignore from gitignore.io".
allowed-tools:
  - Read
  - Write
  - Bash
  - AskUserQuestion
---

# manage-gitignore

A `.gitignore` here is two things: the **template block** the API returns between
its `# Created by …` / `# End of …` markers, written verbatim, and the **custom
rules** outside it, preserved and de-duplicated against the fresh block.

## Division of labour

**Anything a program can decide, a program decides.** The `manage-gitignore`
command owns every mechanical step — scanning, merging, verifying, all summary
numbers, tracked-vs-untracked, the diff, the commit, and what a push may do.

**Yours:** ask the user, judge their answers, write the commit message, relay
what the tools say. That is all. Never re-derive a tool's answer by eye, never
reformat its output into your own numbers, and never run `git add`/`commit`/`push`
yourself — `manage-gitignore git` is the only path to a mutation, and it fails
closed.

`<repo>` below is the repository being worked on. If `manage-gitignore` is not on
PATH, say so and stop; do not fall back to hand-written git or a hand-written
`.gitignore`.

**Any non-zero exit stops that action** — report it verbatim. The one exception
is Step 3's exit 3, documented there.

**If AskUserQuestion is unavailable** (headless), stop at the first choice and
say which confirmation is needed. Never assume an answer.

## Step 0 — Inspect only

If the user only wants to *see* what a repo ignores:

```bash
manage-gitignore templates --dir "<repo>" --detect
```

Relay it and stop. Nothing is written, so there is nothing to review or commit.

## Step 1 — Scan

```bash
manage-gitignore templates --dir "<repo>" --recommend
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
file with the Write tool**, one name per line, at a `mktemp` path outside the
repo — free-text names must never reach a command line, for the same reason
commit messages go through a file.

Asked how many templates exist: `templates --list --count` prints the number and
fails loudly rather than reporting `0` for an unreachable API. To browse, drop
`--count` and summarise by category rather than pasting hundreds of names.

## Step 3 — Write

```bash
manage-gitignore templates --dir "<repo>" --force \
  --facts-out "<facts.json>" --templates-file "<templates.txt>"
```

Always pass `--force`: with no existing file it does nothing, and with one it is
safe precisely because the tool carries the custom rules across.

**Pick `<facts.json>` once, here, and pass that same path to every `--facts`
later.** A different path is not an error — it silently loses everything recorded
so far. Keep it outside the repo, out of the diff being reviewed.

You delete both temp files once each command returns: `rm -f "<templates.txt>"`,
and `rm -f "<msgfile>"` after Step 4. The tools never unlink their inputs.

The tool verifies its own write before reporting success — block intact, template
set exact, every custom rule present, no ANSI, bidi or zero-width characters.

**On a non-zero exit:**

- **exit 3, unknown template name** — the only recoverable case. Re-ask *for the
  rejected names only*, quoting the near matches it printed (`"pythonn" not
  found — did you mean python?`), write a fresh `<templates.txt>`, and re-run.
  **Retry once.** If that fails, report the near matches and stop.
- **anything else** — nothing usable was written. Report it and end the run: no
  Step 4, and no Step 5 summary for a run that produced no file.

On success relay its report: custom rules kept, each duplicate dropped with the
template that covers it, and any `Review before committing` lines — negations
(`!…`, which un-ignore a path) or patterns broad enough to ignore the tree.
**Those flags cover the template block only, never the carried-over custom
rules**, so their absence certifies nothing about the rest of the diff.

## Step 4 — Review, commit, push (`.gitignore` ONLY)

Never stage, commit, or suggest committing any other file.

### 1. Show the diff

```bash
manage-gitignore git --dir "<repo>" status
```

It picks the right command for the file's state and returns the real diff — show
it. For an `untracked` file that diff is a one-line stub, so also show the file
itself (`cat`, or the Read tool); on a first run the Step 3 flags are otherwise
the only review surface. If `suspicious_characters` is true, say so: the terminal
may not be rendering what the file says.

Two outcomes skip the rest of Step 4 — go to Step 5 with
`--choice "not committed"`, no `--hash`, and a `--note` saying which:

- `is_repo: false` → `--note "not a git repo"`
- `changed: false` → `--note "no change: .gitignore already matched"`

Read the diff for what *the user* should weigh — a flagged negation, a broad
pattern, a custom rule they will be surprised to see gone.

### 2. Ask

Assemble everything the answer depends on first, so the user approves the actual
change and not an intention:

- **Draft the commit message now, on one line**, and show it. The summary records
  only the subject, so a body would be approved and never shown back. If the user
  supplies several lines, say only the first is recorded and confirm it.
- **Say the file is already written.** *Don't commit* leaves the change on disk;
  it does not undo it. Name the right discard for the `state` `status` reported:
  `modified`/`staged` with history → `git checkout -- .gitignore`; `untracked`
  (the common first run) → `rm .gitignore`; staged with no commits yet →
  `git reset -- .gitignore && rm .gitignore`.
- **Name where a push would go**, from the tool rather than by re-deriving it:

  ```bash
  manage-gitignore git --dir "<repo>" push-plan
  ```

  Say its `guidance` sentence — it already names the destination *and its URL*,
  because a remote's nickname says nothing about where code goes. If
  `suspicious_characters` is true, say that too. If `permits_push` is false, say
  a push looks unlikely and why; the three options below never change.

Then AskUserQuestion — exactly these, never an "also commit other changes"
option: **Commit + push** / **Commit only** (local) / **Don't commit**.

### 3. Commit

Only on *Commit + push* or *Commit only*. Write **the exact text shown in item
2** to a `mktemp` file — do not redraft it — then:

```bash
manage-gitignore git --dir "<repo>" commit \
  --message-file "<msgfile>" --facts "<facts.json>"
```

It stages only `.gitignore`, commits with `--only` so the rest of the index
survives, and proves the commit holds that one file *and the content this run
verified*.

**Read `verdict`:**

- `ok` — keep the returned `hash` for Step 5.
- anything else — **do not push.** The JSON carries `remedy` (what the user can
  run) and `record_choice` / `record_note` (what Step 5 must record). Relay the
  remedy; **never run it yourself** — discarding a commit that exists is the
  user's call. Do not pass the hash to Step 5.

A non-zero exit with no verdict means nothing was committed and the index is as
you found it. Report it and stop.

### 4. Push

Only if the user chose a push option.

```bash
manage-gitignore git --dir "<repo>" push-plan
```

- `permits_push: false` → report `guidance` and go to Step 5. None of these is an
  error to fix; `stop-up-to-date` is a success.
- `action: "diverged"` → [references/push-safety.md](references/push-safety.md).
  Keep `upstream_sha`; the force needs it.
- otherwise:

```bash
manage-gitignore git --dir "<repo>" push --facts "<facts.json>"
```

`push` recomputes the plan and executes only what it permits, by explicit
refspec, so `push.default=matching` can never widen it. It refuses to force
outside a diverged branch.

A `no-upstream` plan whose `remote` is `null` (several remotes, no `origin`)
needs one more question: show each candidate **with its URL** from `remote_urls`,
then pass `--remote`.

**A push that did not happen appears two ways** — JSON with `pushed: false`, or a
non-zero exit with no JSON. Treat both the same: report it, and go to Step 5 with
no push recorded.

## Step 5 — Summary

```bash
manage-gitignore git --dir "<repo>" facts --facts "<facts.json>" \
  --choice "commit + push" --hash "<hash>" --note "<why, when needed>"
```

`--choice` is the one value no repository state can supply. Record what
*happened*, not what was asked for:

| what happened | `--choice` | `--note` |
|---|---|---|
| committed and pushed | `commit + push` | — |
| committed, no push (`permits_push: false`, `pushed: false`, skipped, or a failed push) | `commit only` | the plan's `guidance`, or the reported error |
| commit refused, or the user said *Don't commit* | `not committed` | the error, when there was one |
| bad commit (`verdict` ≠ `ok`) | the JSON's `record_choice` | its `record_note` |
| Step 4 item 1 shortcut | `not committed` | the note given there |

Omit `--hash` unless `verdict` was `ok`; it is verified, not believed. `--note`
repeats, and appends without touching computed fields — never hand-edit the file.
The commit and push lines are already in it: `commit --facts` and `push --facts`
wrote them.

```bash
manage-gitignore summary "<facts.json>"
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
- Never run `git add`/`commit`/`push` directly.
- This skill modifies and commits **only** `.gitignore`.
- Commit messages go through a file and `--message-file`. Never a heredoc or
  `-m "$(...)"` — some shells inject ANSI bytes into both, and those end up
  stored in the commit.
- Never push without explicit confirmation, and never force without the separate
  confirmation in [references/push-safety.md](references/push-safety.md).
