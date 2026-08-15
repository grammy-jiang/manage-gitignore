# When the run carried an uncommitted change across

Read this only when Step 3 printed `Carried across your uncommitted change`, or
when Step 4's `status` shows a diff you were not expecting. It is here rather
than in `SKILL.md` because most runs never reach it, and the ones that do are
told so by the tool.

## What happened

`.gitignore` already held an edit of the user's that was never committed.

A run may only commit what that run wrote, so the rebuild was based on the
**committed** version of the file — not the one on disk. The commit is therefore
this run's own work and nothing else. The user's edit was then re-applied on top
in the work tree, and is put back **staged or unstaged exactly as it was found**,
so `git status` reads the same before and after.

The consequence worth understanding: from Step 3 onward, **the file on disk is
deliberately not what will be committed.** It holds more. Do not diff it by hand
and do not describe it as the change — Step 4's `status --facts` reports the
committed version, and that is the one to show.

## What to relay

- Every `+ kept your added rule:` line — their addition survived.
- Every `- honoured your removal of:` line — their deletion survived.
- Any `!` line, **loudly**. It means part of their edit was *inside* the template
  block, which is regenerated wholesale from the API, so that part could not be
  carried across. It is reported rather than silently dropped, and the user
  needs to know it is gone.

## Why re-applying is done rule by rule

A whole-file three-way merge conflicts on a deletion: the run rewrites the
region a deleted rule lived in, so the merge cannot tell "they removed this"
from "this moved". Re-applying at the level of the custom rules does not have
that problem, because this run owns the template block and the user owns
everything outside it.
