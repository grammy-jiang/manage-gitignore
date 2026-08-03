---
name: manage-gitignore
description: Build a repository's .gitignore from gitignore.io, keeping the template block verbatim and the repo's own custom rules intact, then review the diff and — with confirmation — commit and push it. Use when asked to create, refresh, or replace a .gitignore, add ignore rules for a language / editor / OS, inspect an existing .gitignore, or "get a gitignore from gitignore.io".
allowed-tools:
  - Read
  - Write
  - Bash
  - AskUserQuestion
---

# manage-gitignore (fetch from gitignore.io, keep custom rules)

Two things make up a `.gitignore` here, and the terms are used consistently below:

- **template block** — what the API returns, between its `# Created by …` and
  `# End of …` markers. Authoritative, written verbatim.
- **custom rules** — every line outside that block. Preserved and de-duplicated
  against the fresh template block.

## The division of labour — read this first

**Anything a program can decide, a program decides.** Two bundled tools own every
mechanical step; you run them and relay what they say.

| Decision | Owner |
|---|---|
| which templates the repo's contents suggest | `templates --recommend` |
| downloading, merging, de-duplicating, writing | `templates` (write mode) |
| whether the write is correct | `templates` post-write verify |
| every number in the summary | `templates --facts-out` + `git facts` |
| tracked vs untracked, and the diff itself | `git status` |
| committing only `.gitignore`, and proving it | `git commit` |
| which push (if any) the branch state permits | `git push-plan` / `git push` |

(Each cell is a `manage-gitignore` subcommand.)

**What is yours:** asking the user, judging their answers, writing the commit
message prose, and relaying results. That is all.

Never re-derive a tool's answer by eye, never reformat its output into your own
numbers, and never run a raw `git commit`/`git push` yourself — `manage-gitignore git` is
the only path to a mutation, and it fails closed.

One placeholder appears throughout: `<repo>` is the repository being worked on
(normally the current directory).

The tools are the `manage-gitignore` command, installed with the package
(`pipx install manage-gitignore`). If it is not on PATH, say so and stop — do
not fall back to hand-written git or a hand-written `.gitignore`.

```bash
manage-gitignore templates --list                       # all template names
manage-gitignore templates --dir "<repo>" --recommend   # scan → proposed set (JSON)
manage-gitignore templates --dir "<repo>" --detect      # what's in the current file
manage-gitignore git --dir "<repo>" "<subcommand>"      # see below
# subcommands: status, commit, push-plan, push, facts — plus `verify-commit
# [--ref R]`, an optional manual re-check that a commit touched only .gitignore.
# `commit` already does that itself; the flow below never needs it.
```

**Any non-zero exit stops the run at that point** — report the error verbatim, and
never hand-write the file or hand-run the git command to work around it.

**If AskUserQuestion is unavailable** (headless / non-interactive run), do not
proceed past any step that requires a choice. Stop and report which confirmation
is needed. Never assume the answer.

## Step 0 — Inspect only?

If the user only wants to *see* what a repo already ignores — not change it —
this is the whole job:

```bash
manage-gitignore templates --dir "<repo>" --detect
```

Relay its output (the templates the file was built from, and its custom rules)
and stop. Do not continue to Step 1; nothing is written, so there is nothing to
review, commit, or summarise.

## Step 1 — Scan

```bash
manage-gitignore templates --dir "<repo>" --recommend
```

One command; it walks the repo and returns JSON:

- `always_on` — the templates every repo gets (git, the three OSes, the three
  editors). Policy lives in `ALWAYS_ON` in the script, not in this file.
- `recommended` — `[{name, reason}]`, where `reason` is the exact file that
  triggered it (`package.json`, `manage.py`, …). Rules live in `DETECT_RULES`.
- `previous` — templates already in the file, so a refresh keeps the prior set.
- `custom_lines` — how many custom rules the current file has.
- `proposed` — the three merged, de-duplicated, in order. **This is the starting
  set for Step 2.**

Do not scan the tree yourself and do not second-guess a `reason`. If the repo is
not a git repo that is fine here — it only matters from Step 4 on.

## Step 2 — Ask the user

**Do not put `always_on` to a vote.** Those are fixed policy (`ALWAYS_ON` in the
script) — state them as a fact, listing the actual names Step 1 returned rather
than describing them from memory: "always included: <the always_on entries>."

**Say what unselecting costs, once, before the options.** Anything under
`previous`/carried-over is already ignored today; if the user does not select it
here it stops being ignored, and files it was hiding become visible to git. Put
that in the question text — not after the answer.

Ask about the rest, which is short enough for one question:

- each entry in `recommended`, shown **with its `reason`**, so the user can see
  why it is there (`node ← package.json`);
- each entry in `carried_over` (the scan already worked out which of `previous`
  is not covered by `always_on` or `recommended` — do not re-derive it), labelled
  as carried over;
- a few extras you suggest — `dotenv`, `jetbrains+all`, a cloud or CI template.
  These are illustrative, not a defined set: the authoritative lists are
  `ALWAYS_ON` and `DETECT_RULES` in the script, and the catalogue behind `--list`.
- the free-text "Other" option, for any catalogue name (comma-separated).

See [references/asking-the-user.md](references/asking-the-user.md) for how to
split this across questions when the set does not fit one. The rule that matters
here: **`carried_over` is never truncated** — those rules protect files *today*,
and anything dropped from the set stops being ignored.

Word the "Other" option so its constraint is visible: *exact catalogue name(s),
comma-separated — ask me to list them if unsure*. A near-miss is rejected by the
tool, not quietly corrected.

Never enumerate the whole catalogue *in the question itself*. Listing or counting
it on request is a separate action — see below.

The final template list is `always_on` plus whatever the user selected.

**Free-text names are untrusted input, so they never touch a command line.**
Write the final list to a file with the **Write tool**, one name per line, and
pass that file in Step 3 with `--templates-file`. Nothing the user typed is then
parsed by a shell, so nothing depends on your quoting it correctly — the same
reason commit messages go through a file.

`manage-gitignore templates` still rejects any name that is not in the API catalogue, and any
name beginning with `-` (which argparse would otherwise read as an option).

**If the user asks how many templates exist**, ask the tool:

```bash
manage-gitignore templates --list --count
```

It prints the number alone, and a failed fetch is a non-zero exit rather than a
`0` you could mistake for an empty catalogue.

To let the user browse, drop `--count`. There are hundreds of names: summarize by
category, or point at the `Edit later:` URL the write step prints, rather than
pasting the whole list into your reply.

## Step 3 — Write

```bash
manage-gitignore templates --dir "<repo>" --force \
  --facts-out "<facts.json>" --templates-file "<templates.txt>"
```

`<templates.txt>` is the file you wrote in Step 2 — one template name per line,
at a `mktemp` path outside the repo. **You delete it**
(`rm -f "<templates.txt>"` — quoted, or the shell reads `<` as a redirect) once
this command has returned; the same goes for `"<msgfile>"` after Step 4's
commit. The tools never unlink their inputs.

Always pass `--force`. It is not a judgement call: with no existing file it does
nothing, and with one it is safe precisely because the tool carries the custom
rules across — without it the tool refuses rather than clobber.

**Pick `<facts.json>` once, here, and pass that exact same path to every
`--facts` in Steps 4 and 5.** The commit's checksum check and the final summary
both read this file; a different path in a later step is not an error, it just
silently loses everything recorded so far. A path beside the repo (not inside it)
keeps it out of the very diff being reviewed.

`--facts-out` records everything Step 5 needs, including a checksum of the exact
bytes it wrote and verified. Step 4's commit re-checks that checksum, so anything
that rewrites `.gitignore` in between is caught rather than committed. Do not
transcribe any of those numbers by hand.

The tool verifies its own write before reporting success (template block intact,
template set exact, every custom rule still present, zero ANSI bytes). A failure
there is a hard stop, not something to eyeball later.

**If it exits non-zero**, one case is recoverable and the rest end the run:

- *Unknown template name(s)* — **recoverable**, and the only one; it exits with
  status **3**, so branch on the exit code rather than on the message. It prints
  near matches: re-ask *only for the rejected names* (keep the accepted ones),
  **quoting those near matches in the question** (`"pythonn" not found — did you
  mean python?`), not just the bare rejected name. Then write a fresh
  `<templates.txt>` with the Write tool — the accepted names plus the corrections,
  since the old one was already deleted — and re-run this step. **Retry at most once.** On a second failure, stop and
  report the near matches instead of asking a third time.
- *Download failed / timed out / unexpected response* — report verbatim. Do not
  retry blindly, do not hand-write the file.
- *`.gitignore` is a symlink* — it refuses to follow one (a symlink could point
  anywhere and its contents would be merged in as custom rules). Report it;
  resolving it is the user's call.
- *Post-write verification failed* — report the listed problems verbatim.

The first case is recoverable **only if the retry succeeds** — then continue to
Step 4 as normal. If the retry is exhausted, or for any of the other three cases,
nothing usable was written: report and end the run — do not go on to Step 4, and
do not render a Step 5 summary for a run that produced no file.

On success, relay its report: the custom rules kept, each duplicate dropped with
the template section that covers it, and any `Review before committing` lines —
negations (`!…`, which un-ignore a path) or patterns broad enough to ignore the
tree. Negations are normal in real templates; surface them so the user sees what
is being un-ignored rather than skimming past.

**Those flags cover the fetched template block only, never the carried-over
custom rules.** An absence of flags does not certify the whole diff — read the
Step 4 diff on its own merits.

## Step 4 — Review, commit, push (`.gitignore` ONLY)

This skill touches `.gitignore` and nothing else. Never stage, commit, or suggest
committing any other file, staged or not.

1. **Show the diff.**

   ```bash
   manage-gitignore git --dir "<repo>" status
   ```

   It picks the right command for the file's actual state: untracked →
   `git status --short`; tracked in a repo that has a commit → `git diff HEAD`,
   which is exactly what item 3 of this step will commit, staged and unstaged
   hunks together;
   tracked on an unborn HEAD (no commits yet) → `git diff --cached` or
   `git diff`, since there is no HEAD to compare against. It runs that and
   returns the real diff. Show it.

   For an untracked `.gitignore` that one line is a status stub, not content:
   also show the file itself (`cat "<repo>/.gitignore"`, or the Read tool) so
   there is something to actually review. On a first run the Step 3 negation and
   broad-pattern flags are otherwise the only review surface.

   Two outcomes skip the rest of Step 4 — go straight to Step 5 with
   `--choice "not committed"`, no `--hash`, and a `--note` saying which of the
   two it was (Step 5 lists the wording). The run still ends with the same
   rendered summary, and its COMMIT section will show `not committed` alongside
   that note rather than an unexplained bare line:

   - `is_repo: false` — not a repo, so there is nothing to commit or push.
   - `changed: false` — the templates resolved to what the file already held.
     Say so ("no change; .gitignore was already up to date") rather than asking
     a commit question with an empty diff behind it.

   Read the diff against the tool's own Step 3 report: the write was already
   verified mechanically, so what you are looking for is anything *the user*
   should weigh — a flagged negation, a broad pattern, a custom rule they will be
   surprised to see gone.

   If `suspicious_characters` is `true`, the diff contains control or
   text-reordering characters and what a terminal renders may not be what the
   file says. Pass that warning on before asking anything.

2. **Ask** — but first assemble everything the answer depends on, so the user
   is approving the actual change and not an intention:

   - **Draft the commit message now** and show it in the question text. Approving
     "Commit + push" should approve the message that will be used, not a message
     written afterwards.
   - **Say the file is already written.** `.gitignore` has *already* been
     rewritten on disk, so **Don't commit** leaves the change uncommitted rather
     than undoing it — `git checkout -- .gitignore` is what discards it.
     Otherwise "Don't commit" reads as "don't do it", and the file changed anyway.
   - **Name the push destination by asking `push-plan`**, not by re-deriving it:

     ```bash
     manage-gitignore git --dir "<repo>" push-plan
     ```

     Item 4 runs it again and `push` recomputes it a third time — that is the
     design, and it keeps one definition of where a push would go. Read the
     `action`:

     | `action` | what to say |
     |---|---|
     | `fast-forward`, `stop-up-to-date` | it would go to `<remote>/<branch>` |
     | `no-upstream` with a `remote` | first push; it would go to that remote — name its URL from `remote_urls`, as for the multi-remote case |
     | `no-upstream` with `remote: null` | several remotes and no `origin` — say the destination is not settled yet and a follow-up question will confirm it |
     | `diverged` | the remote already has commits this branch does not; a push would need a separate force-push decision (see [references/push-safety.md](references/push-safety.md)) |
     | `stop-behind-only` | the remote is ahead; a plain push has nothing to send yet — and once this commit lands the branch becomes **diverged**, so pushing would then need a force-push decision that can drop remote commits |
     | `stop-no-remote` | no remote configured; a push would have nowhere to go |
     | `stop-detached-head`, `stop-fetch-failed`, `stop-compare-failed` | report the reason; a push will not happen |

     This is **messaging only** — the three options in the question never change.
     Say in the question text that a push looks unlikely and why; if the user
     still picks "Commit + push", item 4 reports the `stop-*` and moves on.

     **A non-zero `rev-parse @{u}` is not an error** — it is how "no upstream"
     is signalled, and `push-plan` already accounts for it. Only a genuinely
     failed git invocation stops the run.

     For a `no-upstream` plan the URLs come back in the same JSON, as
     `remote_urls` — use them rather than shelling out. If the plan's
     `suspicious_characters` is `true`, say so: a remote or branch name carries
     characters that can misrepresent themselves on screen.

   AskUserQuestion, exactly these options — never add an "also commit other
   changes" option:
   - **Commit + push**
   - **Commit only** (local)
   - **Don't commit**

3. **Commit** — only if the answer was **Commit + push** or **Commit only**. On
   **Don't commit**, skip straight to Step 5 with nothing committed.

   **Keep the message to a single line.** The summary records only the subject,
   so a body would be approved and committed but never shown back — and the point
   of showing the message in item 2 is that the summary can prove what landed.

   Write **the exact text shown in item 2's question** to a file with the
   **Write tool** — a temp path outside the repo (`mktemp`), so it never lands in
   the very diff being committed. Do not redraft it here; the message that gets
   committed must be the one the user approved. Then:

   ```bash
   manage-gitignore git --dir "<repo>" commit \
     --message-file "<msgfile>" --facts "<facts.json>"
   ```

   It stages only `.gitignore`, commits with `--only` so the rest of the index
   survives, then proves the commit touched exactly one file **and recorded the
   content this run verified**. Four outcomes other than plain success — **do not
   push on any of them.** The first two exit non-zero with nothing committed; the
   other two both exit 2, so check **both** JSON fields rather than the exit code
   alone: `only_gitignore` (false = touched extra files) and `content_matches`
   (false = recorded different bytes, *even when `only_gitignore` is true*):

   - *it refused before staging anything* (missing message file, nothing to
     commit, checksum mismatch): the index is exactly as you found it. Report
     the error and stop.
   - *staging succeeded but the commit failed* (a rejecting hook, say): the tool
     unstages `.gitignore` again before exiting, so the index is back as it was.
     Report the error and stop.
   - *the commit succeeded but touched more than `.gitignore`* (exit 2, JSON
     `only_gitignore: false`): its stderr names the offending files and the exact
     undo command for this repo (`git reset --soft HEAD^`, or
     `git update-ref -d HEAD` if it was the first commit). Report both and
     **stop** — do not pass this hash to Step 5's `--hash` (`facts` refuses it
     anyway); let the user decide whether to undo.
   - *the commit recorded different content than was verified* (exit 2, JSON
     `content_matches: false` with `only_gitignore: true`): a hook or a race put
     other bytes in the commit. **Check this field even when `only_gitignore` is
     true.** Same handling: report it with the printed undo command, do not push,
     do not pass the hash to Step 5.

   **Never run the undo command yourself** in either of the last two cases.
   Report it and let the user choose; discarding a commit that exists is their
   call, not this skill's.

   Delete the message file once this has returned: `rm -f "<msgfile>"`.

   `--facts` makes it record its own hash, subject, scope and the count of files
   it left alone — measured at the moment those numbers are true. Keep the
   returned `hash` for Step 5's `--hash`; do not retype anything else.

4. **Push** (only if the user chose a push option).

   ```bash
   manage-gitignore git --dir "<repo>" push-plan
   ```

   `action` is the one thing the branch state permits — you do not work this out:

   | `action` | what it means | what you do |
   |---|---|---|
   | `fast-forward` | ahead, not behind | run `push` |
   | `no-upstream` | first push for this branch | `remote` non-null → just `push` (the user already chose to push); `remote` null → ask which remote, then `push --remote R` |
   | `diverged` | ahead **and** behind | see [references/push-safety.md](references/push-safety.md) — keep the plan's `upstream_sha`, the force needs it |
   | `stop-behind-only` | behind, nothing local to add | nothing to push; the remote is ahead — pull/rebase first if they want to |
   | `stop-up-to-date` | remote already has it | nothing to push; this is success, not a failure |
   | `stop-detached-head` | not on a branch | report: they are on a detached HEAD; check a branch out first |
   | `stop-no-remote` | no remote configured | report: committed locally; add a remote to push |
   | `stop-fetch-failed` | could not reach the remote | report the fetch error: check network/auth, then retry |
   | `stop-compare-failed` | could not read ahead/behind | report and stop; the comparison is unreliable |

   (`stop-not-a-repo` also exists but cannot appear here — item 1 already routed
   that case to Step 5.)

   **Only `fast-forward` and `no-upstream` lead to the `push` command below.**
   `diverged` goes through `references/push-safety.md`. Every `stop-*` means
   report the reason and move to Step 5 — none of them is an error to fix, and
   `stop-up-to-date` in particular is a normal, successful outcome. After any
   push, confirm the result from the JSON's `pushed` field, not from the exit
   code alone.

   ```bash
   # fast-forward, or no-upstream when push-plan already named the remote:
   manage-gitignore git --dir "<repo>" push --facts "<facts.json>"

   # no-upstream with several remotes and no origin (plan's `remote` is null):
   manage-gitignore git --dir "<repo>" push \
     --remote "<remote>" --facts "<facts.json>"
   ```

   `push` recomputes the plan itself and executes only what that plan permits, by
   explicit refspec (so `push.default=matching` can never widen it to other
   branches). It refuses a diverged push unless `--confirm-force` is passed, and
   refuses to force in any other state at all.

   `no-upstream` only needs a second question when `remote` comes back `null`
   (several remotes, none named `origin`). Show each candidate **with its URL**
   from the plan's `remote_urls` — `fork → git@github.com:me/repo.git`, not a
   bare name — because the name alone does not say where the code is about to go. If the plan's `suspicious_characters` is `true`, say so first: a remote
   or branch name carries characters that can misrepresent themselves on screen.
   Then pass `--remote`. With a single remote, or an `origin`, the choice in
   item 2 is the only confirmation needed.

   **A push that does not happen shows up in one of two ways**, and both mean
   the same thing: either JSON with `pushed: false` (a refusal this tool decided),
   or a non-zero exit with no JSON at all (git itself rejected the push). Do not
   rely on the exit code alone, and do not rely on the JSON alone. Either way:
   report the error and go to Step 5 with no push recorded.

## Step 5 — Final summary

1. Merge the git-side facts into the JSON Step 3 wrote:

   ```bash
   manage-gitignore git --dir "<repo>" facts --facts "<facts.json>" \
     --choice "commit + push" --hash "<hash>"
   ```

   That fills `scan.git_repo`, the diffstat (picking the command that matches the
   file's end state), and the template delta. `--hash` is verified, not believed:
   if it does not resolve, or touches more than `.gitignore`, this fails rather
   than recording it. Omit `--hash` when nothing was committed.

   A worked example for the commonest note-bearing case — committed, push
   refused because the branch diverged:

   ```bash
   manage-gitignore git --dir "<repo>" facts --facts "<facts.json>" \
     --choice "commit only" --hash "<hash>" \
     --note "not pushed: branch diverged, user kept it local"
   ```

   `--choice` is the one value no repository state can supply, so it is the one
   value you pass. It takes exactly three values — map the user's answer:
   **Commit + push** → `commit + push`; **Commit only (local)** → `commit only`;
   **Don't commit** → `not committed`. Use `not committed` too when no question
   was ever asked, via either Step 4 item 1 shortcut (not a repo, or no change).

   Record what *happened*, not what was originally asked for. Add a `--note`
   giving the reason in each of these cases:

   - **nothing was committed** (Step 4 item 3's first two outcomes, or the user
     answered "Don't commit") → `not committed`, with
     `--note "commit failed: <the reported error>"` when it was a failure;
   - **a commit exists but is not this run's result** (`only_gitignore: false`
     *or* `content_matches: false`) → `not committed`, with a note naming which
     and quoting the reported undo command. No `--hash`. For example:
     `--note "commit <hash> recorded different content than was verified; not
     recorded — see the reported undo command"`.
   - **a commit was made but touched more than `.gitignore`** (third outcome) →
     `not committed` **and** a note that says so without claiming failure, e.g.
     `--note "commit <hash> was made but touched extra files; not recorded — see
     the reported undo command"`. Do not pass `--hash`: `facts` refuses it, and
     the summary must not present that commit as this run's result.
   - **committed but not pushed** — a `stop-*`, `pushed: false`, **Skip push**,
     or any non-zero exit from the push → `commit only`, with a note naming the
     actual cause: `--note "not pushed: <the plan's action or the reported
     error>"`. The four causes are different; do not reuse one wording for all
     of them.
   - **the two Step 4 item 1 shortcuts** (not a repo, or no change) →
     `not committed` with `--note "not a git repo"` or
     `--note "no change: .gitignore already matched"`, so the summary's
     "not committed" line is never unexplained.

   Otherwise the summary shows "commit + push" beside a bare "not pushed", or a
   commit hash for a commit that never happened.

   The commit and push lines are already in the file: `commit --facts` and
   `push --facts` wrote them. Do not re-supply them.

2. Add any free-form context with `--note` (repeatable) on that same command —
   it appends, leaving every computed field untouched:

   ```bash
   manage-gitignore git --dir "<repo>" facts --facts "<facts.json>" \
     --note "history was reset before this run"
   ```

   Do not edit the facts file by hand. Rewriting it with the Write tool means
   re-emitting every field the tools computed, and dropping one is silent.

3. Render — this output *is* the closing summary; do not hand-format a second one:

   ```bash
   manage-gitignore summary "<facts.json>"
   ```

   Color is automatic (TTY vs pipe, `NO_COLOR`, `FORCE_COLOR`); `--color
   always|never` only to force.

A worked example of the rendered output, and how to read it, is in
[references/example-output.md](references/example-output.md).

## Rules

- Never hand-write `.gitignore`, and never edit, reorder, reformat, or hand-merge
  the template block or the custom rules — `manage-gitignore templates` owns both. If the API is
  unreachable, say so; do not fake it.
- Never run `git add`, `git commit`, or `git push` directly. `manage-gitignore git` is the
  only path to a mutation: it is the thing that guarantees the commit holds one
  file and that a force-push was explicitly confirmed.
- This skill modifies and commits **only** `.gitignore`.
- Commit messages: write the message to a file with the Write tool and pass
  `--message-file`. Never build one with a shell heredoc or `-m "$(...)"` — some
  shell setups (colorizing wrappers, `$PROMPT_COMMAND` hooks) inject ANSI control
  bytes into heredocs and command substitution, and those bytes end up stored in
  the commit object where they are awkward to remove.
- Never push without explicit user confirmation, and never force without the
  separate confirmation in `references/push-safety.md`.
