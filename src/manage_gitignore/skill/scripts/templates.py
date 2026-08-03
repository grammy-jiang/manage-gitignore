#!/usr/bin/env python3
"""Fetch a .gitignore from gitignore.io (Toptal API), preserving custom lines.

The template block returned by the API is written VERBATIM. If a .gitignore
already exists, any lines *outside* the API-generated block (the user's custom
additions) are carried over and de-duplicated against the fresh template block --
so updating templates never loses custom rules and never leaves duplicates.

The API block is delimited by the markers gitignore.io always emits:
    # Created by https://www.toptal.com/developers/gitignore/api/<templates>
    ...
    # End of https://www.toptal.com/developers/gitignore/api/<templates>
Anything before "# Created by" or after "# End of" is treated as custom. A
.gitignore with no such markers (hand-written) is treated as entirely custom.
Those two markers are matched exactly, byte for byte, because the API emits them
verbatim; a hand-retyped marker (different case or spacing) will not be
recognised and its block will be carried over as custom rules. That is the safe
direction of the trade -- loose matching would let an ordinary user comment be
mistaken for a block delimiter and silently swallow the lines around it.

Usage:
  gitignore.py --list                       # print every template name
  gitignore.py [--dir DIR] --detect         # report template block + custom rules
  gitignore.py [--dir DIR] [--force] T...    # fetch + merge + write .gitignore

TEMPLATEs may be space- or comma-separated (e.g. "node,python vim").

When .gitignore already carries an uncommitted change, the rebuild is based on
the COMMITTED file rather than on what is on disk. That is the only base for
which "the changes this run made" describes the resulting commit truthfully.
The user's own edit is not discarded: it is re-applied on top afterwards, and
put back staged or unstaged exactly as it was found, so `git status` reads the
same before and after. The work tree therefore holds MORE than will be
committed, and gitwork.py's `commit` is told both versions through the facts
file rather than taking whatever is on disk.

Re-applying is done at the level of the custom rules, not as a text merge of two
files that barely resemble each other -- the two regions have different owners.
This run rewrites the template block wholesale; the user owns everything outside
it. A whole-file three-way merge conflicts on a deletion, because every line it
would need as context was rewritten; a rule-level one does not. The one edit
that cannot be carried across is an edit *inside* the block, which is
regenerated from the API; that is reported, never silently swallowed.

An untracked .gitignore is not treated as pending at all: there is no committed
version for the result to be confused with, so the whole file is honestly this
run's own work. A .gitignore staged for deletion is refused (exit 4) -- there is
no rebuild that honours "this file should be gone".

Fetching uses curl (raw bytes; the template block is never edited), bounded by
--max-time/--max-filesize and pinned to https end-to-end. The response is only
accepted when its own "# Created by <API url>" header echoes exactly the
templates that were requested, its "# End of" marker is present, and nothing but
whitespace follows that marker -- the header is the host pin, so a redirect to
another origin cannot pass off a substitute block, and a proxy cannot append to
one. A symlinked .gitignore is refused outright rather than followed.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
from io import BufferedReader
from typing import NoReturn, TypedDict, cast

from gitwork import file_state, is_repo, version_at
from shared import (
    Facts,
    RecommendedTemplate,
    RecommendReport,
    TemplatesFacts,
    atomic_write_bytes,
    clean,
    has_suspicious_chars,
    preserved_mode,
    read_bytes_or_die,
    refuse_option_like,
    write_json_or_die,
)

API = "https://www.toptal.com/developers/gitignore/api"
CREATED = "# Created by "
ENDOF = "# End of "
MARK = "/api/"

FETCH_MAX_SECONDS = 20  # curl's own budget; the subprocess wait is this + slack
FETCH_MAX_BYTES = 2_000_000  # a full multi-template block is a few KB

# Patterns broad enough to ignore (nearly) the whole tree. Legitimate templates
# never contain these, so they are surfaced for human review before committing.
BROAD_PATTERNS = {"*", "**", "*.*", "/", "/*", "**/*", ".", "./"}

# Templates every repo gets, regardless of content. This list is the single
# authoritative home for that policy -- SKILL.md describes it, --recommend
# emits it, and nothing re-derives it by hand.
ALWAYS_ON = ("git", "macos", "linux", "windows", "vim", "emacs", "visualstudiocode")


class _DetectRuleRequired(TypedDict):
    """The one key every rule must carry. Split out so the rest can be optional
    on 3.10 as well: `NotRequired` is 3.11+, and a total=False base plus a total
    subclass is the spelling that works on every version this supports."""

    name: str


class DetectRule(_DetectRuleRequired, total=False):
    """One content-based recommendation rule.

    Fires when any `markers` basename or any `globs` pattern is present, every
    `requires` basename is also present, and no `suppressed_by` template already
    fired. The matched entry becomes the reported reason.
    """

    markers: tuple[str, ...]
    globs: tuple[str, ...]
    requires: tuple[str, ...]
    suppressed_by: tuple[str, ...]


DETECT_RULES: tuple[DetectRule, ...] = (
    {
        "name": "node",
        "markers": ("package.json", "node_modules"),
        "globs": ("*.mjs", "*.js", "*.ts"),
    },
    {
        "name": "python",
        "markers": ("pyproject.toml", "setup.py", "requirements.txt"),
        "globs": ("*.py",),
    },
    {"name": "rust", "markers": ("Cargo.toml",)},
    {"name": "go", "markers": ("go.mod",)},
    {"name": "maven", "markers": ("pom.xml",)},
    {"name": "gradle", "markers": ("build.gradle", "build.gradle.kts")},
    {"name": "java", "globs": ("*.java",), "suppressed_by": ("maven", "gradle")},
    {"name": "jetbrains+all", "markers": (".idea",)},
    {"name": "django", "markers": ("manage.py",), "requires": ("settings.py",)},
    {"name": "rails", "markers": ("Gemfile",), "requires": ("routes.rb",)},
    {"name": "flutter", "markers": ("pubspec.yaml",)},
    {"name": "unity", "markers": ("ProjectSettings",), "requires": ("Assets",)},
)

SCAN_MAX_DEPTH = 3  # deep enough for src/app/settings.py, shallow enough to stay cheap


class PendingVersions(TypedDict):
    """The versions of .gitignore in play when it carries an uncommitted change.

    `index` is None when nothing is staged for it, `work` when the file is not
    on disk (staged for deletion).
    """

    state: str
    head: str
    index: str | None
    work: str | None


class CarriedReport(TypedDict):
    """What the user's own uncommitted edit turned out to be."""

    added: list[str]
    removed: list[str]
    block_edited: bool


EXIT_ERROR = 1
EXIT_UNKNOWN_TEMPLATE = 3  # the one recoverable failure: re-ask and retry
EXIT_DIRTY_GITIGNORE = 4  # the file already holds changes this run did not make


def die(msg: str, code: int = EXIT_ERROR) -> NoReturn:
    print(f"gitignore: {msg}", file=sys.stderr)
    sys.exit(code)


def is_pattern_line(line: str) -> bool:
    """True for a real ignore rule -- not a blank line and not a comment."""
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def count_patterns(lines: list[str]) -> int:
    """How many of these lines are real rules. One definition, four callers."""
    return sum(1 for line in lines if is_pattern_line(line))


def decode_utf8(raw: bytes, what: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        die(f"{what} is not valid UTF-8: {exc}")


def fetch_bytes(path: str) -> bytes:
    url = f"{API}/{path}"
    # No -L. Redirects are not followed at all: --proto-redir would pin only the
    # scheme, leaving a redirect free to move the request to another host, and
    # the list endpoint has no in-body header to catch that. These two endpoints
    # do not redirect in normal operation, so a redirect is an error worth
    # surfacing rather than something to chase.
    cmd = [
        "curl",
        "-fsS",
        "--proto",
        "=https",
        "--max-time",
        str(FETCH_MAX_SECONDS),
        "--max-filesize",
        str(FETCH_MAX_BYTES),
        url,
    ]
    # curl's --max-filesize only acts on a declared Content-Length, so a chunked
    # response would be bounded by --max-time alone. Read the stream here with a
    # hard cap and kill curl the moment it is exceeded.
    deadline = time.monotonic() + FETCH_MAX_SECONDS + 10
    body = bytearray()
    # stderr goes to a file, not a second pipe: draining only stdout while curl
    # fills a stderr pipe would deadlock once that pipe's buffer filled.
    with tempfile.TemporaryFile() as errfile:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errfile)
        except FileNotFoundError:
            die("curl not found")
        # `with proc:` closes the pipe and reaps the child on every exit path,
        # including the die() calls below -- otherwise a capped or timed-out
        # fetch leaks the descriptor it was reading.
        with proc:
            assert proc.stdout is not None
            # Waiting on readiness rather than calling a blocking read is what
            # makes the clock authoritative: a curl that produces NO output at
            # all would otherwise sit in read() past every deadline, which is
            # exactly the stalled case this bound exists for.
            selector = selectors.DefaultSelector()
            selector.register(proc.stdout, selectors.EVENT_READ)
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        proc.kill()
                        die(f"download exceeded {FETCH_MAX_SECONDS + 10}s for {url}")
                    if not selector.select(timeout=min(1.0, remaining)):
                        continue  # nothing readable yet; re-check the clock
                    # read1: hand back whatever is available instead of
                    # blocking for a full buffer. Popen types stdout as IO[bytes];
                    # with stdout=PIPE and no text mode it is a BufferedReader.
                    chunk = cast("BufferedReader", proc.stdout).read1(65536)
                    if not chunk:
                        break  # EOF
                    body += chunk
                    if len(body) > FETCH_MAX_BYTES:
                        proc.kill()
                        die(f"response from {url} exceeded {FETCH_MAX_BYTES} bytes")
                proc.wait(timeout=max(1.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                die(f"download timed out after {FETCH_MAX_SECONDS + 10}s for {url}")
            finally:
                selector.close()
        if proc.returncode != 0:
            errfile.seek(0)
            detail = errfile.read().decode("utf-8", "replace").strip()
            die(f"download failed for {url}: {detail or f'curl exit {proc.returncode}'}")
    return bytes(body)


def fetch_text(path: str) -> str:
    return decode_utf8(fetch_bytes(path), f"response from {API}/{path}")


def strip_bom(text: str) -> str:
    """Drop a leading BOM. A UTF-8 file saved by many editors carries one, and
    it would otherwise ride along as part of the first template name."""
    return text.lstrip("\ufeff")


def read_bytes(path: str) -> bytes:
    """Read a file: never a symlink, never a FIFO, never a directory."""
    return read_bytes_or_die(path, die)


def read_text(path: str) -> str:
    return decode_utf8(read_bytes(path), path)


def check_api_block(api_text: str, want: list[str]) -> None:
    """Reject any response that is not exactly the gitignore.io block requested.

    Checked: the header URL's origin, the exact requested template set, the
    presence of the "# End of" marker, and that nothing but whitespace follows
    it. Everything fails closed and nothing is rewritten -- so what gets written
    stays byte-identical to what the API sent.
    """
    lines = api_text.splitlines()
    first = lines[0] if lines else ""
    if not first.startswith(CREATED) or MARK not in first:
        die("unexpected response (not gitignore API output)")
    url = first[len(CREATED) :].strip()
    if not url.startswith(API + "/"):
        die(f"response header names an unexpected URL: {clean(url)}")
    got = {t.strip().lower() for t in url.split(MARK, 1)[1].split(",") if t.strip()}
    if got != set(want):
        die(
            "response is for different templates than requested "
            f"(requested: {','.join(sorted(want))}; "
            f"got: {','.join(clean(g) for g in sorted(got))})"
        )
    end = next((i for i, line in enumerate(lines) if line.startswith(ENDOF) and MARK in line), None)
    if end is None:
        die("response block is truncated (no '# End of' marker)")
    trailing = [line for line in lines[end + 1 :] if line.strip()]
    if trailing:
        die(f"response has unexpected trailing data after '# End of': {clean(trailing[0])[:80]!r}")


def api_pattern_sections(api_text: str) -> dict[str, str]:
    """Map each pattern in the template block to the '### Name ###' it sits under.

    Used to tell the user *which* template already covers a custom rule that was
    dropped as a duplicate.
    """
    sections: dict[str, str] = {}
    current = ""
    for line in api_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("###") and stripped.endswith("###") and len(stripped) > 6:
            current = stripped.strip("#").strip()
            continue
        if not is_pattern_line(line):
            continue
        sections.setdefault(stripped, current or "template")
    return sections


def risky_patterns(api_text: str) -> tuple[list[str], list[str]]:
    """Return (negations, broad patterns) in the template block, for human review.

    Negations are normal in real templates (e.g. "!.vscode/settings.json"), and
    this is deliberately NOT a block: it surfaces the two shapes that could
    un-ignore a secret or ignore the whole tree, so they are read in the diff
    rather than skimmed past.
    """
    negations: list[str] = []
    broad: list[str] = []
    for line in api_text.splitlines():
        stripped = line.strip()
        if not is_pattern_line(line):
            continue
        if stripped.startswith("!"):
            negations.append(stripped)
        elif stripped in BROAD_PATTERNS:
            broad.append(stripped)
    return negations, broad


def scan_repo(root: str) -> list[tuple[str, str]]:
    """Return [(basename, path-relative-to-root)] for entries down to SCAN_MAX_DEPTH.

    Skips .git. Directories are included as entries so a rule can match on
    "node_modules" or "ProjectSettings".
    """
    found: list[tuple[str, str]] = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = [d for d in dirnames if d != ".git"]
        if depth >= SCAN_MAX_DEPTH:
            dirnames[:] = []
        # Sorted so the reported reason is the same on every run: os.walk's
        # order is filesystem-dependent, and "reproducible and auditable" has to
        # mean the same marker file wins every time.
        for name in sorted(dirnames) + sorted(filenames):
            entry = os.path.normpath(os.path.join(rel, name))
            found.append((name, entry))
    return found


REASON_MAX_LEN = 80  # a path longer than this is truncated for display


def classify(wanted: list[str], recommended: dict[str, str], previous: list[str]) -> TemplatesFacts:
    """Group templates by *why* each is in the set. One precedence, one place.

    always_on beats recommended beats carried_over beats added, so a template is
    reported under exactly one heading and the categories always partition the
    set. `--recommend` and the run facts must agree, so both call this.
    """
    always = [t for t in wanted if t in ALWAYS_ON]
    rec: list[RecommendedTemplate] = [
        {"name": t, "reason": recommended[t]}
        for t in wanted
        if t in recommended and t not in always
    ]
    classified = set(always) | {r["name"] for r in rec}
    carried = [t for t in wanted if t in previous and t not in classified]
    classified |= set(carried)
    groups: TemplatesFacts = {
        "always_on": always,
        "recommended": rec,
        "carried_over": carried,
        "added": [t for t in wanted if t not in classified],
        "removed": [t for t in previous if t not in wanted],
    }
    return groups


def recommend(root: str) -> list[RecommendedTemplate]:
    """Deterministically map repo contents to template recommendations.

    Returns [{"name", "reason"}] -- reason is the exact entry that triggered the
    rule, so the choice is reproducible and auditable rather than eyeballed.
    """
    entries = scan_repo(root)
    hits: list[RecommendedTemplate] = []
    fired: set[str] = set()
    for rule in DETECT_RULES:
        if any(s in fired for s in rule.get("suppressed_by", ())):
            continue
        why = next((p for n, p in entries if n in rule.get("markers", ())), None)
        if why is None:
            for pattern in rule.get("globs", ()):
                why = next((p for n, p in entries if fnmatch.fnmatch(n, pattern)), None)
                if why is not None:
                    break
        if why is None:
            continue
        if any(not any(n == req for n, _ in entries) for req in rule.get("requires", ())):
            continue
        fired.add(rule["name"])
        # `why` is a repo-controlled path shown to a human and to the agent.
        # Neutralised and length-capped so it stays a label and cannot be
        # mistaken for instructions.
        label = clean(why)
        if len(label) > REASON_MAX_LEN:
            label = label[: REASON_MAX_LEN - 1] + "\u2026"
        hits.append({"name": rule["name"], "reason": label})
    return hits


def count_esc(data: bytes) -> int:
    """ESC bytes in the written file. 0 expected; anything else means corruption."""
    return data.count(b"\x1b")


def verify_written(target: str, want: list[str], kept: list[str]) -> tuple[list[str], bytes]:
    """Re-read what was written and check it against what was intended.

    Returns a list of problems (empty == verified). This is the check SKILL.md
    used to ask a human to eyeball in the diff: markers present, template set
    exact, every preserved custom rule still there, no ANSI corruption.
    """
    raw = read_bytes(target)  # no-follow: a symlink swapped in post-write is fatal
    return verify_bytes(raw, want, kept, target), raw


def verify_bytes(raw: bytes, want: list[str], kept: list[str], what: str) -> list[str]:
    """The checks themselves, on bytes rather than a path.

    Split out because two different versions have to pass them: the one on disk,
    and the one about to be committed, which are no longer the same file once an
    uncommitted change is being carried across.
    """
    problems: list[str] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"{what} is not valid UTF-8 after writing: {exc}"]

    esc = count_esc(raw)
    if esc:
        problems.append(f"{esc} ESC byte(s) in the written file (ANSI corruption)")
    if has_suspicious_chars(text):
        # Bidi overrides and zero-width joiners reorder or hide a rule with no
        # ESC in sight. The block is written verbatim by design, so this is
        # reported rather than stripped -- but "verified clean" must not be
        # claimed over it.
        suspect = [ln for ln in text.splitlines() if has_suspicious_chars(ln)]
        problems.append(
            f"{len(suspect)} line(s) contain bidi/zero-width characters, "
            f"first: {clean(suspect[0])!r}"
        )
    got_templates, got_custom = split_existing(text)
    if not got_templates:
        problems.append("no template block markers found in the written file")
    elif set(got_templates) != set(want):
        problems.append(
            f"template block lists {','.join(got_templates)}, expected {','.join(want)}"
        )
    present = {line.strip() for line in got_custom}
    for line in kept:
        if line.strip() and line.strip() not in present:
            problems.append(f"custom rule lost during write: {line.strip()}")
    return problems


def split_regions(text: str) -> tuple[list[str], list[str], list[str]]:
    """Return (templates, block_lines, custom_lines) for an existing .gitignore.

    The two regions this file is made of, and the only place that split is
    decided. templates and block_lines are empty when there is no API block --
    a hand-written file is custom from top to bottom.
    """
    lines = text.splitlines()
    start = end = None
    templates: list[str] = []
    for i, line in enumerate(lines):
        if start is None and line.startswith(CREATED) and MARK in line:
            start = i
            raw = line.split(MARK, 1)[1].strip()
            templates = [t.strip() for t in raw.split(",") if t.strip()]
        elif start is not None and line.startswith(ENDOF) and MARK in line:
            end = i
            break
    if start is None or end is None:
        return [], [], lines
    return templates, lines[start : end + 1], lines[:start] + lines[end + 1 :]


def split_existing(text: str) -> tuple[list[str], list[str]]:
    """(templates, custom_lines) -- the two regions callers usually want."""
    templates, _, custom = split_regions(text)
    return templates, custom


def reapply_custom(
    kept: list[str], base_custom: list[str], their_custom: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Re-apply an edit made to the custom rules on top of this run's own result.

    The file has two owners. This run owns the template block, and rewrites it
    wholesale; the user owns the custom rules. Because those regions are
    disjoint, an uncommitted edit can be carried across as *its own change* --
    the difference between the committed custom rules and the user's -- rather
    than as a text merge of two files that barely resemble each other. A plain
    three-way merge of the whole file conflicts on a deletion, because the
    surrounding lines it needs as context were all rewritten. This does not.

    kept        the custom rules this run committed (deduplicated)
    base_custom the custom rules as committed, before the user touched them
    their_custom the custom rules in the user's version

    Returns (result, added, removed).
    """
    ops = difflib.SequenceMatcher(a=base_custom, b=their_custom, autojunk=False).get_opcodes()
    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in ops:
        if tag in ("delete", "replace"):
            removed.extend(base_custom[i1:i2])
        if tag in ("insert", "replace"):
            added.extend(their_custom[j1:j2])

    result = list(kept)
    for line in removed:
        if line in result:  # already dropped as a duplicate is not an error
            result.remove(line)
    # Appended rather than placed: the positions in `kept` no longer mean what
    # they meant in `base_custom` once duplicates have been dropped, and a rule
    # appended to the end of a .gitignore behaves the same as one in the middle.
    result.extend(added)
    return result, added, removed


def dedup_custom(custom: list[str], api_text: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Drop custom pattern lines already in api_text or repeated within custom.

    Comments and blank lines are kept; only real ignore patterns are compared
    (by stripped value). Returns (kept_lines, [(removed_line, covered_by)]).
    """
    api_patterns = api_pattern_sections(api_text)
    kept: list[str] = []
    removed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in custom:
        stripped = line.strip()
        is_pattern = is_pattern_line(line)
        if is_pattern and stripped in api_patterns:
            removed.append((line, api_patterns[stripped]))
            continue
        if is_pattern and stripped in seen:
            removed.append((line, "an earlier custom rule"))
            continue
        if is_pattern:
            seen.add(stripped)
        kept.append(line)
    while kept and not kept[0].strip():
        kept.pop(0)
    while kept and not kept[-1].strip():
        kept.pop()
    return kept, removed


def normalize_templates(args: list[str]) -> list[str]:
    """Split, lowercase and de-duplicate the requested template names.

    A name starting with "-" is refused outright: argparse would read it as an
    option, so `--facts-out=/etc/x` arriving as a "template" must never get that
    far. Callers should also pass `--` before the list.
    """
    want: list[str] = []
    seen: set[str] = set()
    for arg in args:
        # Split on commas AND whitespace: a single quoted argument such as
        # "node, python vim" is documented to work, not just separate argv words.
        for part in re.split(r"[,\s]+", arg):
            name = strip_bom(part.strip()).lower()
            if name:
                refuse_option_like(name, "template name", die)
            if name and name not in seen:
                seen.add(name)
                want.append(name)
    return want


def validate(want: list[str]) -> None:
    valid = {
        line.strip().lower()
        for line in fetch_text("list?format=lines").splitlines()
        if line.strip()
    }
    unknown = [name for name in want if name not in valid]
    if not unknown:
        return
    # Every name here came from the caller and is about to be relayed verbatim.
    msg = ["unknown template(s): " + " ".join(clean(n) for n in unknown)]
    for name in unknown:
        # Substring hits catch "java" -> "javascript"; close matches catch typos
        # ("pyhton", "dajngo") that substring containment misses entirely.
        near = sorted(v for v in valid if name in v)[:5]
        for guess in difflib.get_close_matches(name, sorted(valid), n=5, cutoff=0.7):
            if guess not in near:
                near.append(guess)
        if near:
            suggestions = ", ".join(clean(g) for g in near[:5])
            msg.append(f'  "{clean(name)}" -> did you mean: {suggestions}')
    msg.append("list all names with: --list")
    # Its own exit code: this is the only failure the caller can recover from,
    # and it must not have to match on message text to know that.
    die("\n".join(msg), EXIT_UNKNOWN_TEMPLATE)


def refuse_symlink(target: str) -> None:
    """A symlinked .gitignore is never read or replaced -- see read_text."""
    if os.path.islink(target):
        die(f"{target} is a symlink; refusing to read or replace it")


def capture_pending(root: str, work: str | None) -> PendingVersions | None:
    """The versions of .gitignore that exist when it has an uncommitted change.

    None when there is nothing to preserve: not a repo, a clean file, or a file
    that is not in HEAD at all (untracked, or added and never committed). In
    that last case a first run is exactly what this tool is for -- there is no
    committed version for the result to be confused with, so the whole file is
    honestly this run's own work.

    Otherwise the run must keep three things apart, and git already stores all
    three: what is committed, what is staged, and what is on disk. The rebuild
    starts from the committed one, because that is the only base for which "the
    changes this run made" is a truthful description of the resulting commit.
    The other two come back afterwards as the user's own uncommitted work.

    `work` is passed in rather than re-read: the caller already has those bytes,
    and reading the file twice would leave a window for it to change in between.
    """
    if not is_repo(root):
        return None
    state = file_state(root)
    if state not in ("staged", "modified"):
        return None
    head = version_at(root, "HEAD")
    if head is None:
        return None
    index = version_at(root, "")
    if index is None:
        # Staged for deletion: the user's pending change is "this file should be
        # gone". There is no version of "rebuild the templates" that honours
        # that, and quietly writing a fresh file would undo it without saying so.
        die(
            ".gitignore is staged for deletion. This run would recreate it, "
            "silently undoing that.\n"
            "  Finish the deletion (commit it) or unstage it "
            "(`git restore --staged .gitignore`), then re-run.",
            EXIT_DIRTY_GITIGNORE,
        )
    return {"state": state, "head": head, "index": index, "work": work}


def atomic_write(target: str, data: bytes) -> None:
    """Replace target in one step, keeping its permissions.

    mkstemp creates the temp file 0600; without restoring a mode the .gitignore
    would be narrowed to owner-only on every run.
    """
    refuse_symlink(target)
    if os.path.isdir(target):
        die(f"{target} is a directory, not a file")
    atomic_write_bytes(target, data, mode=preserved_mode(target))


def cmd_list(args: argparse.Namespace) -> None:
    """--list: print every template name, or just how many there are."""
    # API-controlled text going straight to a terminal gets the same
    # treatment as every other untrusted string in this tool.
    catalogue = [n for n in fetch_text("list?format=lines").splitlines() if n.strip()]
    if args.count:
        # A number the caller can use directly. A failed fetch already died
        # above, so this can never report 0 for "could not reach the API".
        print(len(catalogue))
        return
    for name in catalogue:
        print(clean(name))
    return


def cmd_recommend(args: argparse.Namespace, target: str) -> None:
    """--recommend: scan the repo and print the proposed set as JSON."""
    if not os.path.isdir(args.dir):
        die(f"target dir not found: {args.dir}")
    previous: list[str] = []
    custom_count = 0
    if os.path.lexists(target):
        refuse_symlink(target)
        raw_previous, existing_custom = split_existing(read_text(target))
        previous = [clean(t) for t in raw_previous]
        custom_count = count_patterns(existing_custom)
    found = recommend(args.dir)
    proposed = list(ALWAYS_ON)
    proposed += [h["name"] for h in found if h["name"] not in proposed]
    proposed += [t for t in previous if t not in proposed]
    report: RecommendReport = {
        "always_on": list(ALWAYS_ON),
        "recommended": found,
        "previous": previous,
        # Computed by classify(), not left for the caller to derive:
        # "previous, minus anything already always-on or recommended".
        "carried_over": classify(proposed, {h["name"]: h["reason"] for h in found}, previous)[
            "carried_over"
        ],
        "custom_lines": custom_count,
        "proposed": proposed,
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return


def cmd_detect(args: argparse.Namespace, target: str) -> None:
    """--detect: report the template block and custom rules already in place."""
    if not os.path.isdir(args.dir):
        die(f"target dir not found: {args.dir}")
    if not os.path.lexists(target):
        print("gitignore: none")
        return
    refuse_symlink(target)
    templates, detected_custom = split_existing(read_text(target))
    meaningful = [line for line in detected_custom if line.strip()]
    # Counted the same way as "custom pattern lines kept" below -- patterns
    # only -- so the two numbers in a run summary are comparable.
    pattern_count = count_patterns(meaningful)
    # Every value below is repo-controlled: neutralise it like any other
    # untrusted display text, and say so when something was there.
    print(
        "templates: " + (",".join(clean(t) for t in templates) if templates else "(hand-written)")
    )
    print(f"custom_lines: {pattern_count}")
    if any(has_suspicious_chars(line) for line in meaningful):
        print("note: some lines contain control or text-reordering characters")
    for line in meaningful:
        print("  | " + clean(line))
    return


def cmd_write(args: argparse.Namespace, target: str) -> None:
    """The write path: fetch, merge, de-duplicate, write, verify, record facts."""
    if args.templates_file is not None:
        if args.templates:
            die("--templates-file and positional template names are mutually exclusive")
        # A file the caller wrote directly: no shell ever parsed these names, so
        # nothing here depends on the caller quoting them correctly.
        want = normalize_templates(strip_bom(read_text(args.templates_file)).splitlines())
    else:
        want = normalize_templates(args.templates)
    if not want:
        die("no templates given (try --list)")
    # Cheap local checks before the network round-trip in validate(): a bad --dir
    # should not cost two API calls to discover.
    if not os.path.isdir(args.dir):
        die(f"target dir not found: {args.dir}")
    # A symlinked .gitignore is refused on its own terms, before anything asks
    # git about it -- git would be answering about the target.
    refuse_symlink(target)
    validate(want)

    existed = os.path.lexists(target)
    work_text: str | None = None
    before_digest = ""
    if existed:
        if not args.force:
            die(f"{target} exists -- re-run with --force to overwrite")
        # One read: the digest and the parsed rules must describe the same bytes,
        # or the mid-fetch check below would be comparing against a different
        # file than the custom rules came from.
        before_bytes = read_bytes(target)
        before_digest = hashlib.sha256(before_bytes).hexdigest()
        work_text = decode_utf8(before_bytes, target)

    # The rebuild is based on what is COMMITTED, not on what is on disk, whenever
    # those differ. That is the only base for which "the changes this run made"
    # describes the resulting commit truthfully. The user's own edit is not
    # discarded -- it is re-applied afterwards, and put back staged or unstaged
    # exactly as it was found.
    pending = capture_pending(args.dir, work_text)
    base_text = pending["head"] if pending else (work_text or "")
    prev_templates, custom = split_existing(base_text)
    prev_custom_patterns = count_patterns(custom)
    # Recommendations are recomputed here, not taken on trust, so the summary's
    # "why was this template included" grouping is derived from the same scan
    # that produced it rather than from anything re-entered by hand.
    recommended = recommend(args.dir)

    joined = ",".join(want)
    # Not fetch_text(): the verbatim write needs the original bytes, so both the
    # bytes and the decoded text are kept rather than re-encoding the string.
    api_bytes = fetch_bytes(joined)
    api_text = decode_utf8(api_bytes, f"response from {API}/{joined}")
    check_api_block(api_text, want)

    kept, removed = dedup_custom(custom, api_text) if custom else ([], [])

    # The --force gate was evaluated before the fetch, which takes seconds. A
    # .gitignore created in that window would otherwise be clobbered by a run
    # the user only authorised for a repo that had none.
    if os.path.lexists(target) and not existed and not args.force:
        die(f"{target} appeared during this run -- re-run with --force to overwrite")
    if existed and hashlib.sha256(read_bytes(target)).hexdigest() != before_digest:
        die(
            f"{target} changed while this run was fetching -- its custom rules were read "
            "before that change and would be lost. Re-run to pick up the new content."
        )

    def assemble(custom_lines: list[str]) -> bytes:
        """The block plus these custom rules. No custom rules means the API bytes
        verbatim, byte-identical, which is the case the block deserves."""
        if not custom_lines:
            return api_bytes
        return (api_text.rstrip("\n") + "\n\n" + "\n".join(custom_lines) + "\n").encode("utf-8")

    committed_bytes = assemble(kept)
    carried: CarriedReport = {"added": [], "removed": [], "block_edited": False}
    if pending:
        work_custom, added, dropped = reapply_custom(
            kept, custom, split_existing(pending["work"] or "")[1]
        )
        carried = {
            "added": added,
            "removed": dropped,
            # An edit inside the block cannot survive: the block is regenerated
            # wholesale from the API. Reported, never silently swallowed.
            "block_edited": split_regions(pending["work"] or "")[1]
            != split_regions(pending["head"])[1],
        }
        disk_bytes = assemble(work_custom)
        verify_custom = work_custom
    else:
        disk_bytes = committed_bytes
        verify_custom = kept

    atomic_write(target, disk_bytes)

    # Verify what actually landed on disk before reporting success. A write that
    # lost a custom rule or picked up ANSI bytes is a hard failure here, not
    # something for a human to spot in the diff.
    problems, written = verify_written(target, want, verify_custom)
    # And separately verify the bytes that will be committed. Once a pending
    # change is being carried, those are a different file from the one on disk,
    # so checking only the disk would leave the committed version unchecked.
    if pending:
        problems += verify_bytes(committed_bytes, want, kept, "the version to be committed")
    if problems:
        die("post-write verification FAILED:\n  - " + "\n  - ".join(problems))

    negations, broad = risky_patterns(api_text)

    if args.facts_out is not None:
        groups = classify(want, {h["name"]: h["reason"] for h in recommended}, prev_templates)
        facts: Facts = {
            "scan": {
                "gitignore": "existing" if existed else "none",
                "prev_templates_count": len(prev_templates),
                "custom_lines": prev_custom_patterns,
                "detected": [clean(f"{h['name']} ({h['reason']})") for h in recommended],
            },
            "templates": {**groups, "total": len(want)},
            "merge": {
                "verbatim": not kept,
                "esc_bytes": count_esc(written),
                "custom_kept": count_patterns(kept),
                "custom_removed": [
                    {"line": clean(line), "covered_by": clean(by)} for line, by in removed
                ],
            },
            "write": {
                "path": target,
                "mode": "overwrite" if existed else "new",
                "reason": "file existed" if existed else "no .gitignore yet",
            },
            # The same warnings the console prints, carried into the summary so
            # they survive into a log rather than only the live terminal.
            "review": {
                "negations": [clean(n) for n in negations],
                "broad": [clean(b) for b in broad],
            },
            "net": {"prev_count": len(prev_templates), "new_count": len(want)},
            # Binds the bytes this run verified to the bytes gitwork.py will
            # commit: anything that rewrites .gitignore in between is caught
            # rather than committed on the strength of the path alone.
            # written_sha256 is the sha of what this run will COMMIT. Without a
            # pending change that is also what is on disk; with one the two are
            # deliberately different files, and worktree_sha256 pins the other.
            "internal": {"written_sha256": hashlib.sha256(committed_bytes).hexdigest()},
        }
        if pending:
            # The hand-off that lets `commit` commit this run's work only: the
            # work tree holds more than that, so it cannot just take what is on
            # disk, and it has to be told what to put back afterwards.
            #
            # An index that matches HEAD carries nothing worth restoring -- after
            # the commit it already holds the committed version -- so it is left
            # alone rather than re-staged to the same bytes.
            restore_index = ""
            if pending["index"] is not None and pending["index"] != pending["head"]:
                index_custom = reapply_custom(kept, custom, split_existing(pending["index"])[1])[0]
                restore_index = assemble(index_custom).decode("utf-8")
            facts["internal"] = {
                **facts["internal"],
                "pending_state": pending["state"],
                "worktree_sha256": hashlib.sha256(written).hexdigest(),
                "commit_text": committed_bytes.decode("utf-8"),
                "restore_worktree": disk_bytes.decode("utf-8"),
                "restore_index": restore_index,
            }
    # Announce the primary write BEFORE the facts file: if the facts write then
    # fails, the caller already knows .gitignore itself landed and verified, and
    # the error reads as the scoped, separate problem it is.
    print(f"Wrote {target}")
    print("Verified: template block intact, custom rules preserved, 0 ESC bytes")
    if pending:
        # Say it plainly and early: the file on disk is deliberately NOT what
        # will be committed, and a reader who assumes otherwise will misread the
        # diff in the next step.
        print(
            f"Carried across your uncommitted change ({clean(pending['state'])}): "
            "rebuilt from the committed .gitignore, your edit re-applied on top."
        )
        print("  the commit will contain this run's rebuild only")
        for line in carried["added"]:
            print(f"  + kept your added rule: {clean(line)}")
        for line in carried["removed"]:
            print(f"  - honoured your removal of: {clean(line)}")
        if carried["block_edited"]:
            print(
                "  ! your edit touched the template block itself, which is "
                "regenerated wholesale -- that part could not be carried across"
            )

    if args.facts_out is not None:
        # Atomic: a half-written facts file would fail the next step with a JSON
        # error instead of whatever actually went wrong here.
        write_json_or_die(args.facts_out, dict(facts), die)
    print(f"Templates: {joined}")
    if prev_templates:
        print(f"Previous templates: {','.join(prev_templates)}")
    kept_pattern_count = count_patterns(kept)
    print(f"Custom pattern lines kept: {kept_pattern_count}")
    if removed:
        print(f"Duplicate custom lines removed: {len(removed)}")
        for line, covered_by in removed:
            # covered_by comes from an API "### Name ###" header and `line` from
            # the repo's own file: both are untrusted display text, exactly like
            # the facts JSON treats them.
            print(f"  - {clean(line)}  (covered by {clean(covered_by)})")

    if args.facts_out is not None:
        print(f"Facts: {args.facts_out}")

    if negations or broad:
        # A plain list here; render_summary.py owns the wording that describes
        # what these patterns do, so the two cannot drift apart.
        print("Review before committing (from the template block):")
        for line in [*negations, *broad]:
            print(f"  {clean(line)}")

    print(f"Edit later: https://www.toptal.com/developers/gitignore?templates={joined}")


def main() -> None:
    """Parse arguments, then hand straight to the handler that owns the mode."""
    parser = argparse.ArgumentParser(add_help=True, description=__doc__, allow_abbrev=False)
    parser.add_argument("--dir", default=".", help="target repo root")
    parser.add_argument("--force", action="store_true", help="overwrite existing")
    parser.add_argument("--facts-out", metavar="PATH", help="write the run's summary facts as JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="print all templates")
    parser.add_argument(
        "--count",
        action="store_true",
        help="with --list: print only how many templates the catalogue has",
    )
    mode.add_argument("--detect", action="store_true", help="report existing setup")
    mode.add_argument(
        "--recommend",
        action="store_true",
        help="scan the repo and print the proposed template set as JSON",
    )
    parser.add_argument(
        "--templates-file",
        metavar="PATH",
        help="read template names from a file, one per line (never shell-parsed)",
    )
    parser.add_argument("templates", nargs="*")
    args = parser.parse_args()

    if args.count and not args.list:
        # Checked unconditionally: outside a report mode this used to be ignored
        # while the run went on to write a file the caller did not ask for.
        die("--count only applies to --list")

    report_mode = args.list or args.detect or args.recommend
    # Report modes take no template names: silently ignoring them would look
    # like a write that never happened.
    if report_mode:
        flag = "list" if args.list else "detect" if args.detect else "recommend"
        if args.templates:
            die(
                f"--{flag} takes no template names (got: {' '.join(args.templates)}); "
                "run it alone, then re-run to write"
            )
        if args.facts_out is not None:
            die(f"--{flag} produces no run facts; drop --facts-out (nothing would be written)")
        if args.force:
            die(f"--{flag} writes nothing; drop --force (it would have no effect)")
        if args.templates_file is not None:
            die(f"--{flag} takes no templates; drop --templates-file")

    if args.list:
        cmd_list(args)
        return

    target = os.path.join(args.dir, ".gitignore")
    if args.facts_out is not None and os.path.abspath(args.facts_out) == os.path.abspath(target):
        die(
            f"--facts-out must not be {target}: it would overwrite the very file "
            "this run writes and verifies"
        )

    if args.recommend:
        cmd_recommend(args, target)
        return

    if args.detect:
        cmd_detect(args, target)
        return

    cmd_write(args, target)


if __name__ == "__main__":
    main()
