# Force-push confirmation procedure

Read this when `git push-plan` returned `action: "diverged"` — the branch
is both ahead of and behind its upstream, so a normal push is rejected and only a
force would land.

You do not need to work out whether that is the situation: `push-plan` already
did, and it emits `diverged` **only** when `ahead > 0 and behind > 0`. Every
other state has its own action, and `git push` refuses to force in any of
them — `--confirm-force` on a `stop-behind-only` branch is ignored, not obeyed.

## Why this needs its own confirmation

A force-push rewrites the remote branch. The commits currently on the remote that
are not in your local history are **dropped**, and the branch is reset to yours.
For anyone who already fetched those commits this is irreversible from your side.
The user agreeing to "push" is not agreement to "rewrite history" — that is a
separate decision and needs a separate answer.

## Procedure

1. **Show the divergence** from the plan JSON — do not summarize it, print the
   commits:
   - `would_drop` — the commits a force-push removes from the remote.
   - `would_add` — your local commits that replace them.
   - `ahead` / `behind` — the counts.

   If the plan's `suspicious_characters` is `true`, say so before printing: these
   subject lines carry control or text-reordering characters, so what the
   terminal renders may not be what the commits say — and this is the list the
   user is about to approve destroying.

2. **Explain the consequence plainly**, in one sentence: force-pushing drops
   those `behind` remote commit(s) and resets the upstream branch to your local
   history — irreversible for anyone who already has them.

3. **Ask with AskUserQuestion**, exactly two options — put the cost in the
   label itself, using `behind` from the plan, so the risk does not depend on
   the user having read the paragraph above:
   - **Force-push (drops N remote commits)**
   - **Skip push (keep local only)**

4. **Only on an explicit "Force-push":**

   ```bash
   manage-gitignore git --dir "<repo>" push \
     --confirm-force --expect-remote "<upstream_sha>" --facts "<facts.json>"
   ```

   `<upstream_sha>` is the `upstream_sha` field of the plan you just showed the
   user. `--facts` is what records the push into the summary — without it a
   successful force-push renders as "not pushed".

   `--expect-remote` is not optional and not decoration. Copy it from the
   `upstream_sha` of the very plan whose `would_drop` list you showed the user —
   that value is what makes the lease mean anything:

   - A bare `--force-with-lease` leases against the remote-tracking ref. But
     `push` recomputes the plan first, and that recomputation runs `git fetch` —
     which *refreshes* the tracking ref. The lease would then authorise dropping
     commits that appeared after the user agreed, which is the one thing it
     exists to prevent.
   - With `--expect-remote`, the lease is pinned to the state the user actually
     approved. If anyone pushed in the meantime, the command refuses (exit 4,
     `error: remote-moved`) and tells you to re-run `push-plan` and re-ask —
     because the commits a force would now destroy are not the ones that were
     agreed to.
   - Omitting it is refused outright (exit 6, `error: missing-expect-remote`).

   The push itself goes by explicit refspec derived from the branch's own
   tracking config — the upstream just compared against, not an assumed
   `origin`, and never whatever `push.default=matching` would have expanded a
   bare `git push` into. It is always `--force-with-lease`, never `--force`.

   Check the exit status:

   - **exit 4, `error: remote-moved`** — someone pushed between the plan and the
     confirmation. Nothing was destroyed. Go back to step 1 of this procedure:
     re-run `push-plan`, show the *new* divergence, and ask again. The old answer
     does not carry over; it was about different commits. **Retry this at most
     once.** If it happens a second time in the same run, stop: the remote is
     moving faster than the confirmation can complete, and that is for the user
     to sort out, not for this skill to race.
   - **any other non-zero exit** — the push did not happen. Report the error
     verbatim, then continue to Step 5 with `--choice "commit only"` and a
     `--note` quoting it. The commit still exists and the summary must say so.

5. **On "Skip push":** report exactly the state — committed locally, not pushed,
   local and remote diverge — so the user is not left assuming the change reached
   the remote.

**Whichever outcome above applies, continue to Step 5 of SKILL.md** for the final
summary. If the push was skipped or refused, pass `--choice "commit only"` (not
the user's original "commit + push") and a `--note` giving the reason.
