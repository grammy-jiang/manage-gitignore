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
from collections.abc import Callable, Mapping
from typing import Any, NoReturn, cast

from shared import (
    FACTS_TOOL,
    Facts,
    PushFacts,
    PushPlan,
    atomic_write_bytes,
    clean,
    has_suspicious_chars,
    preserved_mode,
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
    # The constant, not a literal 1. They were two statements of one fact, and
    # the unreferenced one is the fact a mutation audit could change freely.
    sys.exit(EXIT_ERROR)


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
        # S603: no shell, and every element is a list item, so nothing here can
        # be reinterpreted as a command. `args` is built by this module; values
        # that come from outside -- a ref, a remote, a branch -- go through
        # `refuse_option_like` first, so none of them can even pose as a flag.
        # S607: `git` by name is deliberate. Resolving it to an absolute path
        # would pick one git and ignore the one the user's PATH says to use,
        # which is the wrong answer for a tool that runs inside their shell
        # environment; a PATH that already resolves to somebody else's git is a
        # compromise this cannot fix from here.
        proc = subprocess.run(  # noqa: S603
            ["git", "-C", repo, *hardening, *args],  # noqa: S607
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


def version_at(repo: str, spec: str) -> str | None:
    """`.gitignore` as of `spec`, or None when no version exists there.

    spec is "HEAD" for the committed file and "" for the staged one -- git spells
    the index as the empty side of `:path`. Text, not bytes: the callers parse it
    into custom rules and never write it back verbatim, so a normalised trailing
    newline changes nothing.
    """
    rc, out, _ = git(repo, "show", f"{spec}:{TARGET}", strip=False)
    if rc != 0:
        return None
    return out + "\n" if out else ""


def index_mode(repo: str) -> str:
    """The mode git has recorded for .gitignore, defaulting to a regular file."""
    rc, out, _ = git(repo, "ls-files", "--stage", "--", TARGET)
    if rc == 0 and out:
        return out.split()[0]
    return "100644"


def stage_content(repo: str, content: str) -> str:
    """Put `content` in the index as .gitignore without touching the work tree.

    Returns the blob sha. This is how a staged change survives a run: the work
    tree keeps the user's version throughout, and the index is set from the
    object store rather than by re-reading a file that no longer holds it.
    """
    rc, sha, err = git(repo, "hash-object", "-w", "--stdin", stdin=content)
    if rc != 0 or not sha:
        die(f"could not store .gitignore content: {err}")
    entry = f"{index_mode(repo)},{sha},{TARGET}"
    git(repo, "update-index", "--add", "--cacheinfo", entry, check=True)
    return sha


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


def discard_command(repo: str, state: str) -> str | None:
    """How the user undoes this run's write, for the state the file is in.

    Not a sentence for the agent to assemble. The right answer turns on whether
    the file is tracked and whether there is any commit to check out from, and
    both are facts this command already holds -- so an agent working it out from
    a table in prose is a decision that can go wrong for no reason. `rm` where
    `git checkout` was needed loses a file that was under version control.

    None means there is nothing to undo.
    """
    if state == "clean":
        return None
    if state == "untracked":
        return f"rm {TARGET}"
    if not has_commits(repo):
        # Tracked but nothing to restore from: unstage, then remove.
        return f"git reset -- {TARGET} && rm {TARGET}"
    return f"git checkout -- {TARGET}"


def cmd_status(args: argparse.Namespace) -> int:
    repo = args.dir
    if not is_repo(repo):
        emit(
            {
                "is_repo": False,
                "state": None,
                "diff": None,
                "changed": False,
                "discard_command": None,
                # Why the rest of the run is skipped, in the words the summary
                # should carry. Composed here so it cannot be paraphrased.
                "skip_reason": "not a git repo",
            }
        )
        return 0
    state = file_state(repo)
    carried = ""
    if getattr(args, "facts", None) is not None:
        refuse_facts_alias(repo, args.facts)
        carried = (load_facts(args.facts, repo).get("internal") or {}).get("commit_text") or ""
    if carried:
        # The work tree holds this run's rebuild plus the user's own uncommitted
        # edit. Diffing it would show the user their own change as if this run
        # had made it -- the exact confusion the carry-across exists to remove.
        # So the diff shown is HEAD against the version that will be committed.
        _, blob, err = git(repo, "hash-object", "-w", "--stdin", stdin=carried)
        if not blob:
            die(f"could not stage the version to be committed for diffing: {err}")
        cmd = ["diff", f"HEAD:{TARGET}", blob]
    else:
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
            "discard_command": discard_command(repo, state),
            # For an untracked file the diff is `status --short`: one line saying
            # the file is new, which is not a review surface. Said here rather
            # than left to the agent to infer from `state`.
            "diff_is_stub": state == "untracked",
            "skip_reason": None if state != "clean" else "no change: .gitignore already matched",
        }
    )
    return 0


# ── commit ──────────────────────────────────────────────────────────────────
def safe_token(value: str, what: str, on_refusal: Callable[[str], NoReturn] = die) -> str:
    """Reject a remote/branch git would read as an option.

    These come from repository config, which a checked-out repo can set: a
    remote literally named "--upload-pack=..." would otherwise reach `git push`
    as a flag.

    `on_refusal` exists so the push path can write down what it refused before
    stopping. Defaulting to `die` keeps every other caller unchanged.
    """
    return refuse_option_like(value, what, on_refusal)


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


def safe_merge_ref(ref: str, on_refusal: Callable[[str], NoReturn] = die) -> str:
    """A branch ref from repo config, shape-checked before it builds a refspec.

    merge_ref is interpolated into `HEAD:<ref>` and into a --force-with-lease
    argument. A ':' or a leading '+' there would change what the push means, and
    the value comes from branch.<name>.merge, which a checked-out repo controls.

    `on_refusal` as in safe_token: the push path records before it stops.
    """
    refuse_option_like(ref, "upstream ref", on_refusal)
    if not re.fullmatch(r"refs/heads/[^:\s+][^:\s]*", ref):
        on_refusal(f"refusing upstream ref of unexpected shape: {ref!r}")
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


def load_facts(path: str, repo: str | None = None) -> Facts:
    """Read a facts document, refusing one this run has no business in.

    `repo` is optional only because two callers reach here from a plain
    Namespace that carries no --dir; every command path supplies it.
    """
    # Through the same no-follow reader as .gitignore: a facts path is caller-
    # supplied, so it can be a symlink or a FIFO just as easily.
    raw = read_bytes_or_die(path, die)
    try:
        facts = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        die(f"cannot read facts file {path}: {exc}")
    if not isinstance(facts, dict):
        die("facts file must contain a JSON object")
    # A path that does not exist already dies above. This is the other half: a
    # path that exists and holds something else. Merging into it and reporting
    # success loses every number the run recorded, and the summary that follows
    # is missing sections rather than visibly wrong -- which is the hardest kind
    # of wrong to notice.
    if facts.get("tool") != FACTS_TOOL:
        die(
            f"{path} is not a {FACTS_TOOL} facts file (no marker). "
            "Pass the same --facts-out path the write step was given."
        )
    # The file was written by these same tools, so its shape is Facts; json.load
    # simply cannot say so. Every read site still uses .get() with a default.
    document = cast("Facts", facts)
    if repo is not None:
        require_facts_for(repo, path, document)
    return document


def require_facts_for(repo: str, path: str, facts: Facts) -> None:
    """Refuse a facts document that belongs to a different repository.

    The marker alone says "some run of this tool wrote this", which is not the
    question: a facts file left behind by a run against *another* repo carries
    that marker too, and merging into it produces a confident summary of work
    done somewhere else. `write.path` is the absolute target that run wrote, so
    comparing it against the target of this one is the binding.

    Absent means an older or partial document -- the marker check has already
    established provenance, and there is nothing here to disagree with.
    """
    recorded = (facts.get("write") or {}).get("path")
    if not recorded:
        return
    here = os.path.abspath(os.path.join(repo, TARGET))
    if os.path.abspath(str(recorded)) != here:
        die(
            f"{path} belongs to a different run: it records {clean(str(recorded))}, "
            f"but this command is working on {here}."
        )


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


def append_notes(facts: Facts, notes: list[str]) -> None:
    """Add to the notes list, whatever shape it is in.

    One definition because two commands write notes now, and a second copy of
    "read it, coerce it to a list, append" is a second place for them to
    disagree about what an existing string-valued `notes` means.
    """
    raw = facts.get("notes")
    prior = list(raw) if isinstance(raw, (list, tuple)) else ([raw] if raw else [])
    facts["notes"] = [str(n) for n in prior] + list(notes)


def record_refusal(args: argparse.Namespace, choice: str, note: str) -> None:
    """Write a refused commit's outcome into the facts document, if there is one.

    This was already computed here and printed as `record_choice` /
    `record_note` for the caller to hand back to `facts` in a later command.
    Handing it back is a step that can be skipped, and when it is, the summary
    reports a commit that the tool refused to stand behind. Writing it here
    removes the step rather than documenting it.
    """
    path = getattr(args, "facts", None)
    if path is None:
        return
    facts = load_facts(path, getattr(args, "dir", None))
    facts.setdefault("commit", {})["choice"] = choice
    append_notes(facts, [note])
    save_facts(path, facts)


def cmd_commit(args: argparse.Namespace) -> int:
    """Commit ONLY .gitignore, and only the part of it this run wrote.

    When the file carried an uncommitted change, gitignore.py left the work tree
    holding this run's rebuild *plus* that change re-applied -- which is what the
    user should still have afterwards, and is more than may be committed. So this
    swaps in the run's own version for the duration of the commit and puts the
    user's back the moment it is over, committed or not.

    The swap is a real file on disk rather than a synthetic commit-tree, because
    the repository's hooks have to see what is being committed; they are part of
    how its owner wants commits made.
    """
    repo = args.dir
    require_repo(repo)
    # Before the facts file is read for anything: a --facts pointing at
    # .gitignore itself would have this function write the file it is meant to
    # be committing.
    refuse_facts_alias(repo, args.facts)
    internal = (
        (load_facts(args.facts, repo).get("internal") or {}) if args.facts is not None else {}
    )
    carried = internal.get("commit_text") or ""
    if not carried:
        return commit_verified(args)

    target_path = os.path.join(repo, TARGET)
    on_disk = read_bytes_or_die(target_path, die)
    expected = internal.get("worktree_sha256") or ""
    if expected and hashlib.sha256(on_disk).hexdigest() != expected:
        die(
            f"{TARGET} changed since it was written; refusing to commit, and refusing "
            "to overwrite whatever is there now. Re-run from the write step."
        )
    mode = preserved_mode(target_path)
    atomic_write_bytes(target_path, carried.encode("utf-8"), mode=mode)
    try:
        return commit_verified(args)
    finally:
        # Unconditional: a refused or failed commit must still give the user
        # their file back. Restoring the index only when something was staged
        # keeps a clean index clean.
        atomic_write_bytes(
            target_path, (internal.get("restore_worktree") or "").encode("utf-8"), mode=mode
        )
        staged = internal.get("restore_index") or ""
        if staged:
            stage_content(repo, staged)


def commit_verified(args: argparse.Namespace) -> int:
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
        expected = (load_facts(args.facts, repo).get("internal") or {}).get("written_sha256")
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
        note = (
            f"commit {sha} was made but touched extra files; not recorded "
            "— see the reported undo command"
        )
        record_refusal(args, "not committed", note)
        # Still JSON on stdout: the caller needs the hash to report (and undo)
        # the commit that should not have happened.
        emit(
            {
                "hash": sha,
                "files": files,
                "only_gitignore": False,
                "verdict": "touched-extra-files",
                "remedy": problem,
                # Kept for a caller that passed no --facts. With one, the two
                # fields have already been written into it: relaying them back
                # by hand was a step that could be skipped, and the summary was
                # wrong when it was.
                "record_choice": "not committed",
                "record_note": note,
            }
        )
        print(f"gitwork: {problem}", file=sys.stderr)
        return EXIT_BAD_COMMIT
    if staged_oid:
        # The file list is not the content: a hook (or a race) could commit
        # different bytes under the same path.
        rc_oid, committed_oid, _ = git(repo, "rev-parse", f"{sha}:{TARGET}")
        if rc_oid != 0 or committed_oid != staged_oid:
            note = (
                f"commit {sha} recorded different content than was verified; "
                "not recorded — see the reported undo command"
            )
            record_refusal(args, "not committed", note)
            emit(
                {
                    "hash": sha,
                    "files": files,
                    "only_gitignore": True,
                    "content_matches": False,
                    "verdict": "content-mismatch",
                    "remedy": f"Do NOT push; {undo_hint(repo)}.",
                    "record_choice": "not committed",
                    "record_note": note,
                }
            )
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
        "content_matches": True,
        "verdict": "ok",
        "untouched_count": n,
        "untouched": phrase,
    }
    # Recorded here, at the moment the numbers are true. Nothing downstream has
    # to re-observe a working tree that has since moved, or reword a raw count.
    if args.facts:
        facts = load_facts(args.facts, repo)
        commit = facts.setdefault("commit", {})
        commit.update({"hash": sha, "subject": subject, "scope": commit_scope()})
        if phrase:
            commit["untouched"] = phrase
        save_facts(args.facts, facts)
        result["facts"] = args.facts
    emit(result)
    return 0


# ── push ────────────────────────────────────────────────────────────────────
# What each action means for a human, and whether `push` will do anything. These
# lived as two 9-row tables in SKILL.md -- restating, in prose, a decision this
# function already made. One authority; the doc reads `guidance` and `permits_push`.
ACTION_GUIDANCE = {
    "fast-forward": "it would go to {dest}",
    "stop-up-to-date": "{dest} already has this commit; nothing to push. Not a failure.",
    "no-upstream": "first push for this branch; it would go to {dest}",
    "diverged": (
        "{dest} has commits this branch does not. Pushing needs a force-push "
        "decision that can drop them -- see references/push-safety.md."
    ),
    "stop-behind-only": (
        "{dest} is ahead and there is nothing new to send. Once this commit "
        "lands the branch becomes diverged, and pushing would then need a "
        "force-push decision that can drop remote commits."
    ),
    "stop-no-remote": "no remote is configured, so a push has nowhere to go",
    "stop-detached-head": "not on a branch (detached HEAD); check one out first",
    "stop-fetch-failed": "could not reach the remote; check network and auth",
    "stop-compare-failed": "could not read ahead/behind, so no push decision is safe",
    "stop-not-a-repo": "not a git work tree",
}
PUSH_PERMITTED = {"fast-forward", "no-upstream", "diverged"}


def destination(plan: PushPlan) -> str:
    """Where a push would land, always naming the URL and not just a nickname.

    An upstream plan carries one remote; a no-upstream plan carries a candidate
    per remote, and may not have settled on one at all.
    """
    urls = plan.get("remote_urls") or {}
    remote = plan.get("remote")
    if plan.get("merge_ref"):  # an upstream exists: one destination, fully known
        branch = str(plan["merge_ref"]).removeprefix("refs/heads/")
        url = plan.get("remote_url")
        return f"{remote}/{branch}" + (f" ({url})" if url else "")
    if remote:  # first push, remote already settled
        return f"{remote}" + (f" ({urls[remote]})" if remote in urls else "")
    if urls:  # several candidates and no origin: nothing is settled yet
        listed = ", ".join(f"{n} ({u})" for n, u in sorted(urls.items()))
        return f"one of {listed} — not settled yet, a follow-up question will confirm"
    return "the remote"


def describe(plan: PushPlan) -> PushPlan:
    """Annotate a plan with the sentence to say and whether a push can happen."""
    action = str(plan.get("action", ""))
    plan["guidance"] = ACTION_GUIDANCE.get(action, action).format(dest=destination(plan))
    plan["permits_push"] = action in PUSH_PERMITTED
    return plan


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
    plan = describe(push_plan(args.dir))
    emit(plan)
    return 0


def record_push(
    args: argparse.Namespace,
    plan: PushPlan,
    *,
    status: str,
    sha: str = "",
    reason: str = "",
) -> None:
    """Store how far the push got, from verified state -- never from free text.

    Kept as its pieces rather than a sentence: render_summary owns every display
    string, exactly as it does for the commit hash and subject.

    Called *before* each push as well as after it. `git(..., check=True)` dies on
    a push the remote rejects, so a record written only on success is no record
    at all for the outcome the user most needs to see: the facts document said
    nothing, `cmd_facts` therefore derived "commit only", and a requested
    "commit + push" was reported as a run where no push had ever been wanted.
    Writing `attempted` first leaves the truth on disk whichever way the command
    ends, and the success path overwrites it with the sha.

    The refusal legs record too, with the plan's own words as `reason`. `remote`
    is optional here for the same reason: an ambiguous-remote refusal happens
    precisely because there is no settled remote to name.
    """
    if not args.facts:
        return
    facts = load_facts(args.facts, getattr(args, "dir", None))
    # A refusal cannot undo a push that already landed. The plan describes the
    # repository as it is *now*, and a retry sees a different one: our own landed
    # push makes it `stop-up-to-date`, another actor advancing the branch makes
    # it `stop-behind-only`, a force elsewhere makes it `diverged`. None of those
    # says anything about what this run did. Only an attempt can replace the
    # record of an attempt -- which is why the guard is on the status and not on
    # a list of actions, where every one left off would be this bug again.
    #
    # `attempted` deliberately still overwrites, and review argued it should not:
    # a second push of a SECOND commit, rejected, would replace a landed record
    # while the first commit is still on the remote. Declined, twice over. The
    # procedure makes one commit and at most one push -- push-safety's force is
    # the alternative to that push, not an extra one -- so reaching it means the
    # agent has left the procedure, and the document is no longer describing the
    # run it claims to. And the proposed test, "is the recorded sha still in the
    # upstream history", needs a git call on the pre-push write path plus a fetch
    # to be current; against a stale tracking ref it would assert `pushed` for a
    # sha a force had already removed. That is the same class of error in the
    # more dangerous direction. Revisit if this skill ever pushes twice.
    if status == "not attempted" and ((facts.get("commit") or {}).get("push") or {}).get("sha"):
        return
    ref = plan.get("merge_ref") or plan.get("branch") or ""
    # removeprefix, not rsplit: "refs/heads/feature/foo" is the branch
    # "feature/foo", and splitting on the last slash would call it "foo".
    branch = ref.removeprefix("refs/heads/")
    record: PushFacts = {"status": status}
    if sha:
        record["sha"] = sha
    if plan.get("remote"):
        record["remote"] = str(plan["remote"])
    if branch:
        record["branch"] = branch
    if reason:
        record["reason"] = reason
    facts.setdefault("commit", {})["push"] = record
    save_facts(args.facts, facts)


def cmd_push(args: argparse.Namespace) -> int:
    """Execute exactly the action push-plan permits -- nothing else.

    The plan is recomputed here rather than taken as an argument, so a stale or
    hand-edited plan cannot talk this into a push the current state forbids.
    """
    repo = args.dir
    refuse_facts_alias(repo, args.facts)
    plan = describe(push_plan(repo))
    action = plan["action"]

    def refuse_before_push(message: str) -> NoReturn:
        """Write down a refusal the argument guards made, then stop.

        Those guards run before git, so nothing was attempted -- but every other
        non-zero exit from here leaves a record saying which of the two outcomes
        it was, and SKILL.md tells the agent so. Without this, a preflight
        refusal is the one path that leaves nothing behind, and it would be
        reported as a push the remote rejected.
        """
        reason = clean(message)
        if len(reason) > MAX_ERR_LEN:  # as git's own stderr is bounded
            reason = reason[:MAX_ERR_LEN] + " …(truncated)"
        record_push(args, plan, status="not attempted", reason=reason)
        die(message)

    if action.startswith("stop-"):
        # `record_push` declines to overwrite a landed push with a refusal, so a
        # retry after this run already pushed keeps its record whichever stop-*
        # the current state produces.
        record_push(args, plan, status="not attempted", reason=str(plan.get("guidance") or action))
        emit({**plan, "pushed": False})
        print(f"gitwork: not pushing ({action})", file=sys.stderr)
        return 0 if action == "stop-up-to-date" else EXIT_NOT_PUSHED

    if action == "fast-forward":
        # Validated before the record, not inline in the git() call. These values
        # come from the repository's own config, so either guard can refuse one,
        # and refusing after the record would leave `attempted` on disk for a
        # push git never ran -- reported as a push that failed. `attempted` has
        # to keep meaning "git ran and did not come back".
        #
        # The reachable case is `branch.<name>.merge` set TWICE: git's own ref
        # resolution uses the first value, so `@{u}` still resolves and the plan
        # is a fast-forward, while `git config --get` -- what push_plan calls --
        # returns the last. A dash-prefixed *remote* cannot get here; git will
        # not resolve `@{u}` through one, and the no-upstream leg filters them.
        remote_arg = safe_token(str(plan["remote"]), "remote", refuse_before_push)
        merge_arg = safe_merge_ref(plan["merge_ref"], refuse_before_push)
        # Recorded before git runs, not only after -- see record_push.
        record_push(args, plan, status="attempted")
        # Explicit refspec: under push.default=matching a bare `git push` would
        # push every matching branch, not just this one.
        git(repo, "push", remote_arg, f"HEAD:{merge_arg}", check=True)
        sha = current_short_sha(repo)
        record_push(args, plan, status="pushed", sha=sha)
        emit({**plan, "pushed": True, "forced": False})
        return 0

    if action == "no-upstream":
        # Same JSON contract as every other outcome, so a caller never has to
        # parse stderr to find out what happened.
        remote = args.remote or plan["remote"]
        if not remote:
            record_push(
                args, plan, status="not attempted", reason="several remotes; none was chosen"
            )
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
            record_push(
                args,
                plan,
                status="not attempted",
                reason=f"unknown remote {clean(remote)!r}",
            )
            emit({**plan, "pushed": False, "error": "unknown-remote"})
            names = ", ".join(clean(r) for r in plan["remotes"])
            print(
                f"gitwork: unknown remote {clean(remote)!r} (have: {names})",
                file=sys.stderr,
            )
            return EXIT_REMOTE_CHOICE
        # Annotated: a dict display is checked against PushPlan in place, but
        # binding it to a bare name first widens it to dict[str, object].
        chosen: PushPlan = {**plan, "remote": remote}
        # Validated before the record, as on the fast-forward leg above.
        remote_arg = safe_token(remote, "remote", refuse_before_push)
        branch_arg = safe_token(str(plan["branch"]), "branch", refuse_before_push)
        record_push(args, chosen, status="attempted")
        git(repo, "push", "-u", remote_arg, branch_arg, check=True)
        sha = current_short_sha(repo)
        record_push(args, chosen, status="pushed", sha=sha)
        emit({**plan, "remote": remote, "pushed": True, "forced": False})
        return 0

    if action == "diverged":
        if not args.confirm_force:
            record_push(
                args,
                plan,
                status="not attempted",
                reason=f"branch has diverged; a force would drop {plan['behind']} remote commit(s)",
            )
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
            record_push(
                args,
                plan,
                status="not attempted",
                reason="a force needs the approved --expect-remote sha",
            )
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
            record_push(
                args,
                plan,
                status="not attempted",
                reason="the remote moved since that plan was approved",
            )
            emit({**plan, "pushed": False, "error": "remote-moved"})
            print(
                f"gitwork: the remote moved since that plan was made "
                f"({args.expect_remote[:12]} -> {plan['upstream_sha'][:12]}). "
                "Re-run push-plan and re-confirm: the commits a force would drop "
                "are no longer the ones the user agreed to drop.",
                file=sys.stderr,
            )
            return EXIT_NEEDS_FORCE
        # Validated before the record, as on the two legs above.
        merge_arg = safe_merge_ref(plan["merge_ref"], refuse_before_push)
        lease_arg = f"--force-with-lease={merge_arg}:{safe_ref(args.expect_remote)}"
        remote_arg = safe_token(str(plan["remote"]), "remote", refuse_before_push)
        record_push(args, plan, status="attempted")
        git(repo, "push", lease_arg, remote_arg, f"HEAD:{merge_arg}", check=True)
        sha = current_short_sha(repo)
        record_push(args, plan, status="pushed", sha=sha)
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

    Everything is re-derived, including a --hash, which is verified rather than
    believed -- and including the choice, which used to be the caller's to work
    out from a five-row table. It never needed to be: `commit --facts` records
    the hash, `push --facts` records the push, and a refused commit records its
    own choice, so what happened is already written down by the time this runs.
    Asking the caller to restate it invited a summary that reported the
    intention instead of the outcome -- "commit + push" over a push that failed.

    --choice remains, as an override for a caller that knows better, but it is
    no longer needed to get the common cases right.
    """
    repo = args.dir
    refuse_facts_alias(repo, args.facts)
    facts = load_facts(args.facts, repo)
    if args.note:
        # Appended through the tool so the rest of the file is never rewritten by
        # hand -- a hand-merge is how computed fields get dropped.
        append_notes(facts, args.note)
    if args.requested_action:
        # The one thing no command can read off the repository: the user's
        # answer. Everything below derives what *happened*; this is what was
        # *asked*, and the two are allowed to differ.
        facts["requested_action"] = args.requested_action
    facts.setdefault("scan", {})["git_repo"] = is_repo(repo)
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

    # Derived last, from what every earlier command wrote down. A commit with a
    # push recorded beside it was pushed; a commit without one was not; no
    # commit at all is "not committed", which is equally true of a refusal and
    # of the user declining. Nothing here can disagree with the rest of the
    # document, because it is computed from the rest of the document.
    #
    # This is the *outcome*, and it stays the outcome. A push that was attempted
    # and failed leaves `push.status` behind without a sha, so it lands here as
    # "commit only" -- correct, and no longer the whole story, because
    # `requested_action` records that a push was wanted and render_summary shows
    # both. The sha is the discriminator rather than the status because only the
    # success leg can produce one.
    if not commit.get("choice"):
        if not commit.get("hash"):
            commit["choice"] = "not committed"
        else:
            pushed = bool((commit.get("push") or {}).get("sha"))
            commit["choice"] = "commit + push" if pushed else "commit only"

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

    def subcommand(name: str, **kwargs: Any) -> argparse.ArgumentParser:
        """A subparser that inherits --dir and, crucially, allow_abbrev=False.

        add_parser does not inherit the parent's setting, so without this an
        abbreviated option would be accepted after the subcommand but not before.
        """
        return sub.add_parser(name, parents=[common], allow_abbrev=False, **kwargs)

    parser = argparse.ArgumentParser(description=__doc__, parents=[before], allow_abbrev=False)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = subcommand("status", help="repo/tracked state plus the actual diff")
    p.add_argument(
        "--facts",
        help=(
            "the run's facts JSON. Required whenever .gitignore carried an uncommitted "
            "change: without it the diff shown is the work tree, which holds that change "
            "too, rather than the version that will actually be committed."
        ),
    )

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
        "--requested-action",
        choices=("commit + push", "commit only", "not committed"),
        help="what the user asked for, recorded separately from what happened",
    )
    p.add_argument(
        "--choice",
        choices=("commit + push", "commit only", "not committed"),
        help="override the outcome derived from what the run recorded",
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
