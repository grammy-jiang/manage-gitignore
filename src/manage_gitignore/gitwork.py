#!/usr/bin/env python3
"""Deterministic git operations for the manage-gitignore skill.

Everything here is a decision a program can make correctly every time: whether
the file is tracked, what the diff actually is, whether a commit touched only
.gitignore, and which of the push outcomes a branch is in. None of it is left to
be inferred from prose output, and none of it is re-derived by hand.

The one thing this script never does is decide on the user's behalf: it reports
the state and the single action that state permits. Asking the user, and passing
--confirm-force back, stays with the caller.

Subcommands (all take --dir REPO, default "."):
  status         is it a repo, is .gitignore tracked/changed -- and print the diff
  commit         add + commit ONLY .gitignore from a message file, then verify
  push-plan      classify the push situation; emit the one permitted action
  push           execute exactly what push-plan permits (--confirm-force to force)
  facts          merge git-side facts into the JSON gitignore.py --facts-out wrote

Every subcommand prints JSON to stdout (plus human-readable text to stderr where
useful) and exits non-zero on any failure worth stopping for.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from typing import NoReturn, cast

from manage_gitignore.shared import (
    Facts,
    PushPlan,
    clean,
    has_suspicious_chars,
    read_bytes_nofollow,
    read_bytes_or_die,
    refuse_option_like,
    write_json_or_die,
)

TARGET = ".gitignore"
MAX_ERR_LEN = 400  # git stderr can carry arbitrary remote-server text

# Exit codes. Callers are told to branch on the JSON, but a distinct status per
# refusal keeps a shell wrapper honest too.
EXIT_ERROR = 1
EXIT_BAD_COMMIT = 2  # committed, but not what this run intended
EXIT_NOT_PUSHED = 3  # a stop-* action: nothing to push
EXIT_NEEDS_FORCE = 4  # diverged, or the approved remote state moved
EXIT_REMOTE_CHOICE = 5  # ambiguous or unknown remote
EXIT_NEEDS_EXPECT = 6  # a force was asked for without the approved sha


def die(msg: str) -> NoReturn:
    print(f"gitwork: {msg}", file=sys.stderr)
    sys.exit(1)


def emit(payload: Mapping[str, object]) -> None:
    json.dump(dict(payload), sys.stdout, indent=2)
    sys.stdout.write("\n")


def git(
    repo: str,
    *args: str,
    check: bool = False,
    strip: bool = True,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    """Run a git command in repo. Returns (rc, stdout, stderr).

    check=True turns a non-zero exit into a hard stop -- used for the commands
    whose failure must never be walked past (commit, push, fetch).

    strip=False keeps stdout byte-exact. Required for --porcelain, whose first
    two columns are positional: stripping turns " M path" (modified, unstaged)
    into "M path" (staged), which would then show an empty --cached diff for a
    file that really did change.
    """
    # Fail closed on transport. GIT_TERMINAL_PROMPT=0: a credential prompt would
    # hang a headless run. protocol.ext.allow=never: `ext::` remotes execute a
    # command named in repo-local config, which is code execution from a
    # checked-out repo. protocol.file.allow=user keeps ordinary local remotes
    # (a path or file:// URL) working when this tool invokes git directly, while
    # still refusing them when git itself would be following a submodule or
    # recursive clone. Hooks are deliberately NOT disabled -- a repo's hooks are
    # part of how its owner wants commits made, and a rejecting hook is already
    # a handled outcome.
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    hardening = [
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.file.allow=user",
    ]
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *hardening, *args],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",  # a non-UTF-8 locale must not crash a git read
            timeout=120,
        )
    except FileNotFoundError:
        die("git not found")
    except subprocess.TimeoutExpired:
        die(f"git {' '.join(args)} timed out after 120s")
    # git's stderr can carry text straight from a remote server, so it is
    # neutralised before it is printed or stored, like any other display string.
    err = clean(proc.stderr) if proc.stderr.strip() else ""
    if len(err) > MAX_ERR_LEN:
        err = err[:MAX_ERR_LEN] + " …(truncated)"
    if check and proc.returncode != 0:
        die(f"git {' '.join(args)} failed (exit {proc.returncode}): {err}")
    out = proc.stdout.strip() if strip else proc.stdout.rstrip("\n")
    return proc.returncode, out, err


def is_repo(repo: str) -> bool:
    rc, out, _ = git(repo, "rev-parse", "--is-inside-work-tree")
    return rc == 0 and out == "true"  # inside a .git dir this exits 0 and says "false"


def require_repo(repo: str) -> None:
    if not is_repo(repo):
        die(f"{repo} is not a git work tree")


def porcelain(repo: str) -> str:
    """`git status --porcelain -- .gitignore`, or "" when there is no change.

    Unstripped: the leading column is meaningful (see git()).
    """
    _, out, _ = git(repo, "status", "--porcelain", "--", TARGET, strip=False)
    return out


def has_commits(repo: str) -> bool:
    """False on an unborn HEAD -- a repo with no commit yet.

    `git diff HEAD` is a fatal error there, so anything comparing against HEAD
    has to ask first.
    """
    rc, _, _ = git(repo, "rev-parse", "--verify", "-q", "HEAD")
    return rc == 0


def file_state(repo: str) -> str:
    """One of: untracked, staged, modified, clean.

    Porcelain codes are XY: X is the index status, Y the work-tree status. Both
    dirty ("MM") counts as staged with more on top -- reported as "staged" so the
    diff shown is the one that would be committed.
    """
    line = porcelain(repo)
    if not line.strip():
        return "clean"
    code = line[:2]
    if code == "??":
        return "untracked"
    return "modified" if code[0] in " " else "staged"


# ── status ──────────────────────────────────────────────────────────────────
def diff_command(repo: str, state: str, *, stat: bool = False) -> list[str]:
    """The git command that shows what a commit would land, for this state.

    One definition, used by `status` (what the human approves) and by the
    diffstat in the summary (what is reported) -- they must never disagree
    about which comparison is authoritative.

    For anything tracked that is HEAD, not the index: `commit` re-stages from
    the working tree, so HEAD..worktree is exactly what gets committed, and a
    --cached diff would hide unstaged hunks. On an unborn HEAD there is no HEAD
    to compare against, so the index (or the work tree) is the only option.
    """
    if state == "untracked":
        return ["status", "--short", "--", TARGET]
    stat_flag = ["--stat"] if stat else []
    if not has_commits(repo):
        # No HEAD to compare against. --cached would show only what is staged,
        # but `commit --only` re-stages from the working tree, so an unstaged
        # edit would be committed without ever appearing in the reviewed diff.
        # Diffing against /dev/null shows the whole file, which is exactly what
        # a first commit records.
        if stat:
            return ["diff", "--no-index", "--stat", "--", os.devnull, TARGET]
        return ["diff", "--no-index", "--", os.devnull, TARGET]
    return ["diff", "HEAD", *stat_flag, "--", TARGET]


def cmd_status(args: argparse.Namespace) -> int:
    repo = args.dir
    if not is_repo(repo):
        emit({"is_repo": False, "state": None, "diff": None})
        return 0
    state = file_state(repo)
    cmd = diff_command(repo, state)
    rc, diff, err = git(repo, *cmd)
    # `git diff --no-index` exits 1 to mean "these differ", which is the normal
    # case for a first commit, not a failure.
    tolerated = {0, 1} if "--no-index" in cmd else {0}
    if rc not in tolerated:
        die(f"git {' '.join(cmd)} failed: {err}")
    # The diff is read by a human to approve a change, so it is shown verbatim
    # -- rewriting it would defeat the point. But .gitignore content is partly
    # API-supplied and partly repo-supplied, so an ESC or a bidi override could
    # make the rendered diff disagree with the bytes. Flag that rather than
    # silently pass it through.
    suspicious = has_suspicious_chars(diff)
    if diff:
        if suspicious:
            print(
                "gitwork: WARNING - this diff contains control or text-reordering "
                "characters; what your terminal shows may not be what the file "
                "says. Inspect with `git diff --  .gitignore | cat -v`.",
                file=sys.stderr,
            )
        print(diff, file=sys.stderr)
    emit(
        {
            "is_repo": True,
            "state": state,
            "diff_command": "git " + " ".join(cmd),
            "diff": diff,
            "changed": state != "clean",
            "suspicious_characters": suspicious,
        }
    )
    return 0


# ── commit ──────────────────────────────────────────────────────────────────
def safe_token(value: str, what: str) -> str:
    """Reject a remote/branch git would read as an option.

    These come from repository config, which a checked-out repo can set: a
    remote literally named "--upload-pack=..." would otherwise reach `git push`
    as a flag.
    """
    return refuse_option_like(value, what, die)


def safe_ref(ref: str) -> str:
    """Reject a ref git would read as an option.

    --ref/--hash come from the caller; "--output=/etc/x" would otherwise be
    handed to git as a flag rather than a revision.
    """
    return refuse_option_like(ref, "ref", die)


def commit_files(repo: str, ref: str = "HEAD") -> list[str]:
    """Paths touched by a commit. A failed lookup is an error, not an empty commit."""
    rc, out, err = git(repo, "show", "--name-only", "--format=", safe_ref(ref))
    if rc != 0:
        die(f"cannot read commit {ref}: {err or f'git exit {rc}'}")
    return [line for line in out.splitlines() if line.strip()]


def undo_hint(repo: str, ref: str = "HEAD") -> str:
    """How to undo `ref` -- `<ref>^` does not exist for a first commit."""
    rc, count, _ = git(repo, "rev-list", "--count", safe_ref(ref))
    if rc == 0 and count.isdigit() and int(count) <= 1:
        return f"`git update-ref -d {ref}` removes it (there is no parent commit)"
    return f"`git reset --soft {ref}^` undoes it and restores the index"


def remote_push_url(repo: str, name: str) -> str:
    """Where `git push <name>` actually goes.

    pushurl when set, else url: git lets them differ, and showing the fetch URL
    would have the user approve a destination the push never reaches.
    """
    rc, pushurl, _ = git(repo, "config", "--get", f"remote.{name}.pushurl")
    if rc == 0 and pushurl:
        return pushurl
    _, url, _ = git(repo, "config", "--get", f"remote.{name}.url")
    return url


def safe_merge_ref(ref: str) -> str:
    """A branch ref from repo config, shape-checked before it builds a refspec.

    merge_ref is interpolated into `HEAD:<ref>` and into a --force-with-lease
    argument. A ':' or a leading '+' there would change what the push means, and
    the value comes from branch.<name>.merge, which a checked-out repo controls.
    """
    refuse_option_like(ref, "upstream ref", die)
    if not re.fullmatch(r"refs/heads/[^:\s+][^:\s]*", ref):
        die(f"refusing upstream ref of unexpected shape: {ref!r}")
    return ref


def blob_matches_worktree(repo: str, ref: str) -> bool:
    """Does `ref`'s recorded TARGET have the same content as the working tree?

    Compared as git object ids, so no encoding or trailing-newline handling can
    make two identical blobs look different. Used by both the commit gate and
    the facts gate, which must not drift on what "same content" means.
    """
    rc_ref, recorded, _ = git(repo, "rev-parse", f"{ref}:{TARGET}")
    rc_tree, current, _ = git(repo, "hash-object", "--", os.path.join(repo, TARGET))
    if rc_ref != 0 or rc_tree != 0:
        return True  # cannot tell; the caller's other checks still apply
    return recorded == current


def current_short_sha(repo: str) -> str:
    """HEAD's abbreviated sha. Four callers wanted this; one definition."""
    _, sha, _ = git(repo, "rev-parse", "--short", "HEAD")
    return sha


def commit_scope() -> str:
    """The scope string recorded in the facts. Written once, read twice."""
    return f"{TARGET} only"


def scope_violation(repo: str, files: list[str]) -> str | None:
    """The message for a commit that touched more than TARGET, else None.

    Only cmd_commit calls this; it stays a function because the message and the
    undo hint belong together.
    """
    if files == [TARGET]:
        return None
    return f"commit touches {files} -- expected only {TARGET}. Do NOT push; {undo_hint(repo)}."


def load_facts(path: str) -> Facts:
    # Through the same no-follow reader as .gitignore: a facts path is caller-
    # supplied, so it can be a symlink or a FIFO just as easily.
    raw = read_bytes_or_die(path, die)
    try:
        facts = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        die(f"cannot read facts file {path}: {exc}")
    if not isinstance(facts, dict):
        die("facts file must contain a JSON object")
    # The file was written by these same tools, so its shape is Facts; json.load
    # simply cannot say so. Every read site still uses .get() with a default.
    return cast("Facts", facts)


def refuse_facts_alias(repo: str, facts_path: str | None) -> None:
    """A facts path pointing at .gitignore would destroy the file being managed."""
    if facts_path is None:
        return
    if os.path.abspath(facts_path) == os.path.abspath(os.path.join(repo, TARGET)):
        die(f"--facts must not be {TARGET}: writing facts there would destroy it")


def save_facts(path: str, facts: Facts) -> None:
    """Atomic: several commands update this file in turn, and a half-written one
    would fail the next step with a JSON error rather than the real cause."""
    write_json_or_die(path, dict(facts), die)


def cmd_commit(args: argparse.Namespace) -> int:
    """Stage and commit ONLY .gitignore, leaving the rest of the index intact."""
    repo = args.dir
    require_repo(repo)
    refuse_facts_alias(repo, args.facts)
    if not os.path.lexists(args.message_file):
        die(f"message file not found: {args.message_file}")
    # Same guard as every other file this skill reads: a message file that is a
    # symlink or a FIFO is refused rather than followed.
    message = read_bytes_or_die(args.message_file, die).decode("utf-8", "replace")
    if not message.strip():
        die(f"message file is empty: {args.message_file}")

    # Before ANY git call: git itself will hash the working-tree file, and on a
    # FIFO that blocks forever. Refusing a non-regular target here turns a hang
    # into an error, and does it before the index has been touched.
    target_path = os.path.join(repo, TARGET)
    if os.path.lexists(target_path):
        read_bytes_or_die(target_path, die)

    if file_state(repo) == "clean":
        die(f"{TARGET} has no changes to commit")

    # Bind what is about to be committed to the bytes gitignore.py verified. A
    # path match is not enough: anything could have rewritten .gitignore, or
    # replaced it with a symlink, between the write and here.
    if args.facts is not None:
        expected = (load_facts(args.facts).get("internal") or {}).get("written_sha256")
        if expected:
            actual = hashlib.sha256(read_bytes_or_die(os.path.join(repo, TARGET), die)).hexdigest()
            # A window remains between this check and `git add` below: another
            # process could rewrite the file in between. Closing it entirely
            # needs the content staged from a held descriptor, which git offers
            # no porcelain for; the check still turns the common case (a stale
            # or edited file) from a silent commit into a refusal.
            if actual != expected:
                die(
                    f"{TARGET} changed since it was written and verified "
                    f"(sha256 {actual[:12]} != {expected[:12]}); refusing to commit it"
                )

    # Everything else that is dirty right now: reported so the summary can say
    # what the commit deliberately left alone. Unstripped, so the XY columns stay
    # aligned and ln[3:] really is the path on every line including the first.
    # --no-renames keeps every line "XY path" so ln[3:] is a plain path; with
    # renames a line reads "R  old -> new" and the comparison would miss.
    _, all_status, _ = git(repo, "status", "--porcelain", "--no-renames", strip=False)
    untouched = [ln for ln in all_status.splitlines() if ln[3:].strip() != TARGET]

    # The blob id of exactly the bytes just verified, taken before staging; the
    # committed object is compared against it once the commit exists.
    _, staged_oid, _ = git(repo, "hash-object", "--", os.path.join(repo, TARGET))

    git(repo, "add", "--", TARGET, check=True)
    # `-F -` with the bytes already validated above: git never re-reads the
    # caller's path, so what was checked and what is committed are the same.
    rc, _, err = git(repo, "commit", "--only", "-F", "-", "--", TARGET, stdin=message)
    if rc != 0:
        # `add` succeeded, so leaving now would strand the file in the index in a
        # state the caller did not create. Put the index back -- and if even that
        # fails, say so rather than asserting a cleanup that did not happen.
        reset_rc, _, reset_err = git(repo, "reset", "-q", "--", TARGET)
        if reset_rc == 0:
            die(f"commit failed (exit {rc}); {TARGET} was unstaged again: {err or 'no stderr'}")
        die(
            f"commit failed (exit {rc}): {err or 'no stderr'} -- AND the cleanup "
            f"reset also failed: {reset_err or f'exit {reset_rc}'}. {TARGET} may still "
            "be staged; check `git status` before doing anything else."
        )

    files = commit_files(repo)
    sha = current_short_sha(repo)
    problem = scope_violation(repo, files)
    if problem:
        # Still JSON on stdout: the caller needs the hash to report (and undo)
        # the commit that should not have happened.
        emit({"hash": sha, "files": files, "only_gitignore": False})
        print(f"gitwork: {problem}", file=sys.stderr)
        return EXIT_BAD_COMMIT
    if staged_oid:
        # The file list is not the content: a hook (or a race) could commit
        # different bytes under the same path.
        rc_oid, committed_oid, _ = git(repo, "rev-parse", f"{sha}:{TARGET}")
        if rc_oid != 0 or committed_oid != staged_oid:
            emit({"hash": sha, "files": files, "only_gitignore": True, "content_matches": False})
            print(
                f"gitwork: commit {sha} recorded {TARGET} with content that is not what "
                f"this run wrote and verified. Do NOT push; {undo_hint(repo)}.",
                file=sys.stderr,
            )
            return EXIT_BAD_COMMIT

    _, raw_subject, _ = git(repo, "log", "-1", "--format=%s")
    subject = clean(raw_subject)
    n = len(untouched)
    phrase = f"{n} other file{'' if n == 1 else 's'}" if n else None
    result = {
        "hash": sha,
        "subject": subject,
        "files": files,
        "only_gitignore": True,
        "untouched_count": n,
        "untouched": phrase,
    }
    # Recorded here, at the moment the numbers are true. Nothing downstream has
    # to re-observe a working tree that has since moved, or reword a raw count.
    if args.facts:
        facts = load_facts(args.facts)
        commit = facts.setdefault("commit", {})
        commit.update({"hash": sha, "subject": subject, "scope": commit_scope()})
        if phrase:
            commit["untouched"] = phrase
        save_facts(args.facts, facts)
        result["facts"] = args.facts
    emit(result)
    return 0


# ── push ────────────────────────────────────────────────────────────────────
def push_plan(repo: str) -> PushPlan:
    """Classify the push situation. `action` names the ONE permitted next step.

    stop-*      nothing to push; the reason says why
    fast-forward  plain push to the tracked ref
    no-upstream   first push; needs a remote choice and -u
    diverged      only a force would land -- needs explicit confirmation
    """
    if not is_repo(repo):
        return {"action": "stop-not-a-repo"}
    rc, _, _ = git(repo, "symbolic-ref", "-q", "HEAD")
    if rc != 0:
        return {"action": "stop-detached-head"}
    _, branch, _ = git(repo, "branch", "--show-current")

    rc, _, _ = git(repo, "rev-parse", "--abbrev-ref", "@{u}")
    if rc != 0:
        _, remotes_out, _ = git(repo, "remote")
        remotes = [r for r in remotes_out.splitlines() if r.strip() and not r.startswith("-")]
        if not remotes:
            return {"action": "stop-no-remote", "branch": branch}
        remote = remotes[0] if len(remotes) == 1 else ("origin" if "origin" in remotes else None)
        # The PUSH url, which can differ from the fetch url: showing the fetch url
        # would have the user approve a destination the push never goes to.
        urls = {name: remote_push_url(repo, name) for name in remotes}
        return {
            "action": "no-upstream",
            "branch": branch,
            "remotes": remotes,
            # Paired here so a caller never has to shell out for the one thing
            # that says where the code actually goes.
            "remote_urls": urls,
            "remote": remote,  # null => the caller must ask which one
            # Remote and branch names are repo-controlled and are about to be
            # shown as a push destination, so flag them like any other display
            # text the user acts on.
            "suspicious_characters": has_suspicious_chars(" ".join([*remotes, branch])),
        }

    # Upstream exists: refresh, then read ahead/behind. A failed fetch means the
    # comparison would be against stale data, so it is a hard stop.
    rc, _, err = git(repo, "fetch")
    if rc != 0:
        return {"action": "stop-fetch-failed", "branch": branch, "error": err}
    _, remote, _ = git(repo, "config", "--get", f"branch.{branch}.remote")
    _, merge_ref, _ = git(repo, "config", "--get", f"branch.{branch}.merge")
    _, upstream_sha, _ = git(repo, "rev-parse", "@{u}")
    remote_url = remote_push_url(repo, remote)
    rc, counts, _ = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}")
    if rc != 0 or "\t" not in counts:
        return {"action": "stop-compare-failed", "branch": branch}
    ahead, behind = (int(n) for n in counts.split("\t", 1))
    base: PushPlan = {
        "branch": branch,
        "remote": remote,
        "merge_ref": merge_ref,
        # Computed for every action, not just the exotic ones: fast-forward is
        # the common push, and it names a destination too.
        # Where a push would land, surfaced for every upstream action -- a
        # fast-forward and a force-push both get confirmed against a real URL
        # rather than a bare remote name.
        "remote_url": remote_url,
        "suspicious_characters": has_suspicious_chars(
            " ".join([branch, remote, merge_ref, remote_url])
        ),
        # The remote commit this comparison was made against. A force-push must
        # be leased against THIS sha -- the one whose consequences were shown to
        # the user -- not against whatever the remote holds by the time the push
        # runs. See cmd_push.
        "upstream_sha": upstream_sha,
        "ahead": ahead,
        "behind": behind,
    }
    if behind == 0:
        if ahead == 0:
            base["action"] = "stop-up-to-date"
            return base
        base["action"] = "fast-forward"
        return base
    if ahead == 0:
        # Nothing local to add: a force here would delete remote commits and
        # contribute none. Never offered.
        base["action"] = "stop-behind-only"
        return base
    _, drop, _ = git(repo, "log", "--oneline", "HEAD..@{u}")
    _, add, _ = git(repo, "log", "--oneline", "@{u}..HEAD")
    base["action"] = "diverged"
    base["would_drop"] = drop.splitlines()
    base["would_add"] = add.splitlines()
    # Those subject lines are read to approve an irreversible act, so they join
    # the names already checked in `base`.
    base["suspicious_characters"] = base.get("suspicious_characters", False) or (
        has_suspicious_chars(drop + add)
    )
    return base


def cmd_push_plan(args: argparse.Namespace) -> int:
    plan = push_plan(args.dir)
    emit(plan)
    return 0


def record_push(args: argparse.Namespace, plan: PushPlan, sha: str) -> None:
    """Store where the push landed, from verified state -- never from free text.

    Kept as its three pieces rather than a sentence: render_summary owns every
    display string, exactly as it does for the commit hash and subject.
    """
    if not args.facts:
        return
    facts = load_facts(args.facts)
    ref = plan.get("merge_ref") or plan.get("branch") or ""
    # removeprefix, not rsplit: "refs/heads/feature/foo" is the branch
    # "feature/foo", and splitting on the last slash would call it "foo".
    branch = ref.removeprefix("refs/heads/")
    facts.setdefault("commit", {})["push"] = {
        "sha": sha,
        # A push only happens once a remote is settled, so this is never null here.
        "remote": str(plan["remote"]),
        "branch": branch,
    }
    save_facts(args.facts, facts)


def cmd_push(args: argparse.Namespace) -> int:
    """Execute exactly the action push-plan permits -- nothing else.

    The plan is recomputed here rather than taken as an argument, so a stale or
    hand-edited plan cannot talk this into a push the current state forbids.
    """
    repo = args.dir
    refuse_facts_alias(repo, args.facts)
    plan = push_plan(repo)
    action = plan["action"]

    if action.startswith("stop-"):
        emit({**plan, "pushed": False})
        print(f"gitwork: not pushing ({action})", file=sys.stderr)
        return 0 if action == "stop-up-to-date" else EXIT_NOT_PUSHED

    if action == "fast-forward":
        # Explicit refspec: under push.default=matching a bare `git push` would
        # push every matching branch, not just this one.
        git(
            repo,
            "push",
            safe_token(str(plan["remote"]), "remote"),
            f"HEAD:{safe_merge_ref(plan['merge_ref'])}",
            check=True,
        )
        sha = current_short_sha(repo)
        record_push(args, plan, sha)
        emit({**plan, "pushed": True, "forced": False})
        return 0

    if action == "no-upstream":
        # Same JSON contract as every other outcome, so a caller never has to
        # parse stderr to find out what happened.
        remote = args.remote or plan["remote"]
        if not remote:
            emit({**plan, "pushed": False, "error": "ambiguous-remote"})
            names = ", ".join(clean(r) for r in plan["remotes"])
            warn = (
                " (some names contain characters that can misrepresent themselves)"
                if plan.get("suspicious_characters")
                else ""
            )
            print(
                f"gitwork: several remotes ({names}){warn}; pass --remote to choose",
                file=sys.stderr,
            )
            return EXIT_REMOTE_CHOICE
        if remote not in plan["remotes"]:
            emit({**plan, "pushed": False, "error": "unknown-remote"})
            names = ", ".join(clean(r) for r in plan["remotes"])
            print(
                f"gitwork: unknown remote {clean(remote)!r} (have: {names})",
                file=sys.stderr,
            )
            return EXIT_REMOTE_CHOICE
        git(
            repo,
            "push",
            "-u",
            safe_token(remote, "remote"),
            safe_token(str(plan["branch"]), "branch"),
            check=True,
        )
        sha = current_short_sha(repo)
        record_push(args, {**plan, "remote": remote}, sha)
        emit({**plan, "remote": remote, "pushed": True, "forced": False})
        return 0

    if action == "diverged":
        if not args.confirm_force:
            emit({**plan, "pushed": False})
            print(
                "gitwork: branch has diverged -- a force-push would DROP "
                f"{plan['behind']} remote commit(s). Refusing without --confirm-force. "
                "See references/push-safety.md.",
                file=sys.stderr,
            )
            return EXIT_NEEDS_FORCE
        # A bare --force-with-lease leases against the remote-tracking ref, which
        # push_plan just refreshed with `git fetch` -- so it would authorise
        # dropping commits that appeared AFTER the user saw the plan, which is
        # precisely what the lease is supposed to prevent. Lease explicitly
        # against the sha whose consequences were shown and approved.
        if not args.expect_remote:
            emit({**plan, "pushed": False, "error": "missing-expect-remote"})
            print(
                "gitwork: --confirm-force also requires --expect-remote <sha>, the "
                "`upstream_sha` from the push-plan the user approved. Without it the "
                "lease would be computed after this command's own fetch and protect "
                "nothing. See references/push-safety.md.",
                file=sys.stderr,
            )
            return EXIT_NEEDS_EXPECT
        if args.expect_remote != plan["upstream_sha"]:
            emit({**plan, "pushed": False, "error": "remote-moved"})
            print(
                f"gitwork: the remote moved since that plan was made "
                f"({args.expect_remote[:12]} -> {plan['upstream_sha'][:12]}). "
                "Re-run push-plan and re-confirm: the commits a force would drop "
                "are no longer the ones the user agreed to drop.",
                file=sys.stderr,
            )
            return EXIT_NEEDS_FORCE
        git(
            repo,
            "push",
            f"--force-with-lease={safe_merge_ref(plan['merge_ref'])}:{safe_ref(args.expect_remote)}",
            safe_token(str(plan["remote"]), "remote"),
            f"HEAD:{safe_merge_ref(plan['merge_ref'])}",
            check=True,
        )
        sha = current_short_sha(repo)
        record_push(args, plan, sha)
        emit({**plan, "pushed": True, "forced": True})
        return 0

    die(f"unhandled push action: {action}")


# ── facts ───────────────────────────────────────────────────────────────────
def diffstat(repo: str, commit_hash: str | None) -> str:
    """The diffstat for whichever end state .gitignore is actually in."""
    if commit_hash:
        _, out, _ = git(repo, "show", "--stat", "--format=", commit_hash, "--", TARGET)
        return out
    state = file_state(repo)
    if state in ("staged", "modified"):
        _, out, _ = git(repo, *diff_command(repo, state, stat=True))
        return out
    if state == "untracked":
        # git reports nothing for an untracked file, so count the lines directly
        # -- through the same no-follow reader as everywhere else, so a symlink
        # or FIFO cannot sneak in through the summary path.
        try:
            body = read_bytes_nofollow(os.path.join(repo, TARGET))
        except OSError:  # SymlinkRefused and NotARegularFile both subclass it
            return ""
        # A final line without a trailing newline still counts.
        lines = body.count(b"\n") + (1 if body and not body.endswith(b"\n") else 0)
        return f"new file, {lines} lines"
    return ""


def cmd_facts(args: argparse.Namespace) -> int:
    """Merge git-side facts into the JSON gitignore.py --facts-out produced.

    The only thing accepted from the caller here is --choice, which records a
    human answer no repository state can supply. Everything else is re-derived,
    including a --hash, which is verified rather than believed.
    """
    repo = args.dir
    refuse_facts_alias(repo, args.facts)
    facts = load_facts(args.facts)
    if args.note:
        # Appended through the tool so the rest of the file is never rewritten by
        # hand -- a hand-merge is how computed fields get dropped.
        raw = facts.get("notes")
        prior = list(raw) if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
        facts["notes"] = [str(n) for n in prior] + args.note
    facts.setdefault("scan", {})["git_repo"] = is_repo(repo)
    # The choice is the user's answer, not a repository fact: record it even
    # when there is no repo, which is exactly when it says "not committed".
    commit = facts.setdefault("commit", {})
    if args.choice:
        commit["choice"] = args.choice
    if is_repo(repo):
        if args.hash:
            files = commit_files(repo, args.hash)  # dies if the hash does not resolve
            if files != [TARGET]:
                die(
                    f"commit {args.hash} touches {files} -- expected only {TARGET}; "
                    "refusing to record it as this run's commit"
                )
            expected = (facts.get("internal") or {}).get("written_sha256")
            if expected and not blob_matches_worktree(repo, safe_ref(args.hash)):
                # The same gate cmd_commit applies: a commit whose recorded
                # content is not what this run verified must not be presented as
                # this run's result.
                die(
                    f"commit {args.hash} recorded {TARGET} with content that is not "
                    "what this run wrote and verified; refusing to record it"
                )
            _, raw_subject, _ = git(repo, "log", "-1", "--format=%s", safe_ref(args.hash))
            subject = clean(raw_subject)
            commit.setdefault("hash", args.hash)
            commit.setdefault("subject", subject)
            commit.setdefault("scope", commit_scope())
        stat = diffstat(repo, args.hash)
        if stat:
            facts.setdefault("net", {})["diffstat"] = stat.strip().splitlines()[-1].strip()

    save_facts(args.facts, facts)
    emit({"facts": args.facts, "merged": True})
    return 0


def main() -> int:
    # --dir is accepted both before and after the subcommand; the docstring
    # promises `--dir REPO` without saying where it has to sit. Two parents,
    # because a subparser copy carrying its own default would silently overwrite
    # a value already parsed by the main parser.
    before = argparse.ArgumentParser(add_help=False)
    before.add_argument("--dir", default=".", help="repository root")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--dir", default=argparse.SUPPRESS, help="repository root"
    )  # SUPPRESS: only set when actually given, so the pre-subcommand value survives

    def subcommand(name: str, **kwargs) -> argparse.ArgumentParser:
        """A subparser that inherits --dir and, crucially, allow_abbrev=False.

        add_parser does not inherit the parent's setting, so without this an
        abbreviated option would be accepted after the subcommand but not before.
        """
        return sub.add_parser(name, parents=[common], allow_abbrev=False, **kwargs)

    parser = argparse.ArgumentParser(description=__doc__, parents=[before], allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True)

    subcommand("status", help="repo/tracked state plus the actual diff")

    p = subcommand("commit", help=f"commit ONLY {TARGET} from a message file")
    p.add_argument("--message-file", required=True)
    p.add_argument("--facts", help="also record the commit block into this facts JSON")

    subcommand("push-plan", help="classify the push situation")

    p = subcommand("push", help="execute exactly what push-plan permits")
    p.add_argument("--confirm-force", action="store_true", help="required to force a diverged push")
    p.add_argument(
        "--expect-remote",
        metavar="SHA",
        help="the approved plan's upstream_sha; the force is leased against it",
    )
    p.add_argument("--remote", help="which remote, when the branch has no upstream")
    p.add_argument("--facts", help="also record the push line into this facts JSON")

    p = subcommand("facts", help="merge git facts into a facts JSON file")
    p.add_argument("--facts", required=True)
    p.add_argument(
        "--note",
        action="append",
        default=[],
        help="append a free-form note (repeatable); the only prose field",
    )
    p.add_argument("--hash", help="the commit this run produced (verified, not trusted)")
    p.add_argument(
        "--choice",
        choices=("commit + push", "commit only", "not committed"),
        help="the user's answer -- the one fact no repo state can supply",
    )

    args = parser.parse_args()
    if not os.path.isdir(args.dir):
        die(f"directory not found: {args.dir}")
    handlers = {
        "status": cmd_status,
        "commit": cmd_commit,
        "push-plan": cmd_push_plan,
        "push": cmd_push,
        "facts": cmd_facts,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
