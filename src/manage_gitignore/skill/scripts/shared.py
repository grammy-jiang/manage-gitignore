"""Helpers shared by the manage-gitignore scripts.

Four concerns live here because more than one script needs them and separate
copies would drift: neutralising repo-derived text, opening a file without
following a symlink, writing a file atomically while keeping its permissions,
and the shape of the JSON the tools hand to each other.

Imported by path: Python puts the running script's directory on sys.path, and
all four files sit together in the skill directory.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping
from typing import NoReturn, TypedDict

# Everything invisible or text-moving, written once and shared by both patterns
# below so they cannot drift apart:
#   - bidi overrides/isolates, zero-width and word joiners, BOM/ZWNBSP
#   - U+2028/U+2029, which str.splitlines() treats as line breaks
#   - variation selectors and the Unicode Tag block, the current vehicles for
#     smuggling ASCII inside an innocuous-looking string
_INVISIBLE = (
    "\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff"
    "\u2028\u2029"
    "\ufe00-\ufe0f\U000e0000-\U000e007f\U000e0100-\U000e01ef"
)

# C0 + DEL + the C1 block (C1 carries a single-codepoint CSI at U+009B), plus
# everything above. A filename may contain any of these, and a summary the
# reader trusts must not be forgeable by one.
CONTROL_CHARS = re.compile("[\u0000-\u001f\u007f-\u009f" + _INVISIBLE + "]")


def clean(value: object) -> str:
    """Stringify a value and neutralise anything that could forge output.

    Replaced with a space rather than deleted, so "a\\nb" reads as two words
    instead of silently becoming "ab".
    """
    text = value if isinstance(value, str) else str(value)
    return CONTROL_CHARS.sub(" ", text).strip()


# The same set minus tab, newline and carriage return. `clean` neutralises those
# because a newline inside a summary *field* forges a row; a newline inside a
# *file* is just a line ending, so scanning file content must not flag it.
SUSPICIOUS_CHARS = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f" + _INVISIBLE + "]"
)


def refuse_option_like(value: str, what: str, die: Callable[[str], NoReturn]) -> str:
    """Reject a value a command would read as an option rather than data.

    Template names, refs, remotes and branches all reach argv, and all can come
    from somewhere the user does not control. One guard, one wording.
    """
    if value.startswith("-"):
        die(f"refusing {what} that looks like an option: {value!r}")
    return value


def has_suspicious_chars(text: str) -> bool:
    """True if text carries a control or text-reordering character.

    Ordinary whitespace does not count. Used where stripping would be wrong (a
    .gitignore is written verbatim) but the reader still needs to be told the
    bytes are there.
    """
    return SUSPICIOUS_CHARS.search(text) is not None


class SymlinkRefused(OSError):
    """Raised instead of following a symlink at the final path component."""


class NotARegularFile(OSError):
    """Raised for a FIFO, device or directory where a plain file is required."""


class TooLarge(OSError):
    """Raised when a file this skill reads exceeds its size bound."""


MAX_READ_BYTES = 4_000_000  # a .gitignore or facts file is a few KB


def read_bytes_nofollow(path: str, max_bytes: int = MAX_READ_BYTES) -> bytes:
    """Read a file, refusing to follow a symlink at the final component.

    A .gitignore that is a symlink would otherwise let these tools read an
    arbitrary file (say ~/.ssh/id_rsa) and carry its contents into a commit.

    Bounded like the network fetch and the stdin path: nothing this skill reads
    is legitimately large, and an unbounded read is an unbounded allocation.
    """
    try:
        # O_NONBLOCK as well as O_NOFOLLOW: opening a FIFO with no writer BLOCKS,
        # so without it the refusal below is never reached -- the process simply
        # hangs, which reads as a slow run rather than a failure. On a regular
        # file O_NONBLOCK has no effect.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # what O_NOFOLLOW raises on a symlink
            raise SymlinkRefused(f"{path} is a symlink; refusing to follow it") from exc
        raise
    try:
        # Checked on the raw descriptor, BEFORE fdopen: a directory makes fdopen
        # itself raise, and O_NOFOLLOW stops a symlink but not a FIFO (reading
        # one blocks forever with no writer). Only a regular file is a .gitignore.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise NotARegularFile(f"{path} is not a regular file; refusing to read it")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise TooLarge(f"{path} is larger than {max_bytes} bytes; refusing to read it")
        return data


def read_bytes_or_die(path: str, die: Callable[[str], NoReturn]) -> bytes:
    """read_bytes_nofollow with every refusal turned into a caller's die().

    Both scripts need the same three-way translation; one copy keeps their error
    wording identical.
    """
    try:
        return read_bytes_nofollow(path)
    except (SymlinkRefused, NotARegularFile, TooLarge) as exc:
        die(str(exc))
    except OSError as exc:
        die(f"cannot read {path}: {exc}")


def atomic_write_bytes(target: str, data: bytes, *, mode: int | None = None) -> None:
    """Replace target in one step; a crash mid-write can never truncate it.

    mkstemp creates the temp file 0600, so `mode` is applied before the rename
    when the caller wants the destination to keep different permissions.
    """
    # os.replace() puts a regular file where the link was, rather than writing
    # through it -- so the link's target is never touched. Stated here because
    # callers rely on it for paths (facts files) with no symlink gate of their own.
    directory = os.path.dirname(target) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def default_file_mode() -> int:
    """0666 & ~umask -- what an ordinary tool would create a file as."""
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def preserved_mode(path: str) -> int:
    """The mode `path` already has, or the umask default when it does not exist.

    follow_symlinks=False: read the link's own mode rather than leaking the
    permission bits of whatever it points at into the file we write.
    """
    try:
        return stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode)
    except OSError:
        return default_file_mode()


def write_json(path: str, payload: Mapping[str, object]) -> None:
    """Write a facts file atomically, keeping its permissions.

    Several commands update this file in turn; a half-written one would fail the
    next step with a JSON error rather than the real cause. mkstemp creates 0600,
    so without restoring a mode every update would narrow the file.
    """
    text = json.dumps(payload, indent=2) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"), mode=preserved_mode(path))


def write_json_or_die(
    path: str, payload: Mapping[str, object], die: Callable[[str], NoReturn]
) -> None:
    """write_json with the failure turned into a caller's die().

    Mirrors read_bytes_or_die so both scripts report an unwritable facts file
    the same way.
    """
    try:
        write_json(path, payload)
    except OSError as exc:
        die(f"cannot write facts file {path}: {exc}")


# ── the facts contract ──────────────────────────────────────────────────────
# Three files touch this JSON: gitignore.py writes the file-side sections,
# gitwork.py adds the git-side ones, render_summary.py reads all of them. These
# TypedDicts are the single description of that shape, so `make verify`'s mypy
# run turns a renamed or mistyped key into an error instead of a silently
# missing row in the summary. Types only -- no behaviour lives here.


class ScanFacts(TypedDict, total=False):
    git_repo: bool
    gitignore: str  # existing | none
    prev_templates_count: int
    custom_lines: int
    detected: list[str]


class RecommendedTemplate(TypedDict):
    name: str
    reason: str


class TemplatesFacts(TypedDict, total=False):
    total: int
    always_on: list[str]
    recommended: list[RecommendedTemplate]
    carried_over: list[str]
    added: list[str]
    removed: list[str]


class RemovedRule(TypedDict):
    line: str
    covered_by: str


class MergeFacts(TypedDict, total=False):
    verbatim: bool
    esc_bytes: int
    custom_kept: int
    custom_removed: list[RemovedRule]


class ReviewFacts(TypedDict, total=False):
    """Patterns worth a human glance. Scoped to the fetched template block."""

    negations: list[str]
    broad: list[str]


class WriteFacts(TypedDict, total=False):
    path: str
    mode: str  # overwrite | new
    reason: str


class PushFacts(TypedDict, total=False):
    """Where a push landed, in pieces. render_summary composes the display."""

    sha: str
    remote: str
    branch: str


class CommitFacts(TypedDict, total=False):
    choice: str  # commit + push | commit only | not committed
    hash: str
    subject: str
    scope: str
    untouched: str
    push: PushFacts


class NetFacts(TypedDict, total=False):
    prev_count: int
    new_count: int
    diffstat: str


class PushPlan(TypedDict, total=False):
    """What `push-plan` emits. A superset: `action` says which keys are set."""

    action: str
    branch: str
    remote: str | None  # null on `no-upstream` when the caller must choose
    remotes: list[str]
    remote_url: str  # upstream actions: where a push would land
    remote_urls: dict[str, str]  # no-upstream: one per candidate remote
    merge_ref: str
    upstream_sha: str
    ahead: int
    behind: int
    would_drop: list[str]
    would_add: list[str]
    suspicious_characters: bool
    error: str
    guidance: str  # one sentence to tell the user; see gitwork.ACTION_GUIDANCE
    permits_push: bool  # whether `push` will attempt anything at all


class RecommendReport(TypedDict, total=False):
    """What `gitignore.py --recommend` emits."""

    always_on: list[str]
    recommended: list[RecommendedTemplate]
    previous: list[str]
    carried_over: list[str]
    custom_lines: int
    proposed: list[str]


class InternalFacts(TypedDict, total=False):
    """Tool-to-tool handshake values. Never rendered; not a display contract."""

    written_sha256: str
    # Present only when .gitignore already carried an uncommitted change. The
    # work tree then holds MORE than this run wrote -- this run's rebuild plus
    # the user's own edit re-applied on top -- so the commit cannot simply take
    # whatever is on disk. commit_text is what this run wrote and all it may
    # commit; the two restore_* values put the user's change back afterwards,
    # staged and unstaged kept apart exactly as they were found.
    pending_state: str  # the file_state seen before the run: staged | modified
    worktree_sha256: str  # the file left on disk, which is NOT written_sha256 here
    commit_text: str
    restore_worktree: str
    restore_index: str  # "" when nothing was staged, i.e. nothing to restore


# Stamped into the facts document by the run that creates it, and required by
# every command that later reads one. The point is not authenticity -- nothing
# here is a trust boundary -- it is catching a caller that passed a different
# path. A facts path that does not exist already fails loudly; one that exists
# and holds something else used to be merged into and reported as a success,
# silently losing everything recorded so far.
FACTS_TOOL = "manage-gitignore"


class Facts(TypedDict, total=False):
    tool: str
    scan: ScanFacts
    templates: TemplatesFacts
    merge: MergeFacts
    review: ReviewFacts
    write: WriteFacts
    commit: CommitFacts
    net: NetFacts
    notes: list[str]
    internal: InternalFacts
