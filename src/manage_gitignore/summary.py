#!/usr/bin/env python3
"""Render the manage-gitignore skill's end-of-run summary.

Reads a JSON facts file (see schema below) and prints a categorized, aligned
summary. Colorized on an interactive terminal; plain text (no ANSI) when output
is piped/redirected or color is disabled.

Color is ON when: stdout is a TTY, TERM != "dumb", and NO_COLOR is unset.
Override with --color=always|never|auto (default auto) or FORCE_COLOR / NO_COLOR.

Usage:
  render_summary.py FACTS.json [--color auto|always|never]
  render_summary.py --color always FACTS.json   # force color (e.g. to preview)

This renderer is deliberately tolerant of malformed input: the facts file is
normally written by gitignore.py and gitwork.py, but it is also a documented,
hand-authorable format, so a wrong-typed or missing field degrades to a skipped
row rather than a traceback.

FACTS schema (all fields optional; sections with no data are skipped):
{
  "title": "manage-gitignore - run summary",
  "scan":   {"git_repo": true, "gitignore": "existing|none",
             "prev_templates_count": 11, "custom_lines": 0,
             "detected": ["node (scripts/lint-mermaid.mjs)"]},
  "templates": {"total": 12,
             "always_on": ["git", "..."],
             "recommended": [{"name": "node", "reason": "scripts/*.mjs"}],
             "carried_over": ["python", "dotenv"],
             "added": ["direnv"], "removed": []},
  "merge":  {"verbatim": true, "esc_bytes": 0,
             "custom_kept": 0,
             "custom_removed": [{"line": "node_modules/", "covered_by": "Node"}]},
  "review": {"negations": ["!*.svg"], "broad": ["*"]},
  "write":  {"path": ".gitignore", "mode": "overwrite|new", "reason": "file existed"},
  "commit": {"choice": "commit + push|commit only|not committed",
             "hash": "6e0a827", "subject": "chore: ...",
             "scope": ".gitignore only", "untouched": "4 staged files",
             "push": {"sha": "6e0a827", "remote": "origin", "branch": "main"}},
  "net":    {"prev_count": 11, "new_count": 12,
             "diffstat": "1 file changed, 7 insertions(+), 3 deletions(-)"},
  "notes":  ["free-form context shown near the top, e.g. a pre-run history reset"]
}

Keep "write.reason" short (e.g. "file existed"); put longer context in "notes".
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Sequence
from typing import NoReturn

from manage_gitignore.shared import (  # one sanitiser, shared by every tool in this skill
    Facts,
    clean,
    read_bytes_or_die,
)


def use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return sys.stdout.isatty() and os.environ.get("TERM", "") != "dumb"


class Pal:
    """ANSI palette; a no-op when color is off, so alignment is identical."""

    def __init__(self, on: bool) -> None:
        self.on = on

    def _w(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.on else text

    def title(self, s: str) -> str:
        return self._w(s, "1;36")

    def rule(self, s: str) -> str:
        return self._w(s, "36")

    def hdr(self, s: str) -> str:
        return self._w(s, "1;37")

    def label(self, s: str) -> str:
        return self._w(s, "36")

    def dim(self, s: str) -> str:
        return self._w(s, "2")

    def hashc(self, s: str) -> str:
        return self._w(s, "33")

    def add(self, s: str) -> str:
        return self._w(s, "32")

    def rem(self, s: str) -> str:
        return self._w(s, "31")

    def ok(self, s: str) -> str:
        return self._w(s, "32")


# Every C0 control plus DEL, and TAB/LF/CR with them: a newline inside a
# repo-derived value (a filename really can contain one) would otherwise let
# that value forge extra rows in a summary the reader trusts as tool output.
def as_list(value) -> list:
    """A facts list, coerced. A bare string would otherwise iterate per character."""
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value] if value else []


def as_dict(value) -> dict:
    """A facts section, or an empty one when it is not an object.

    A section written as a string or list would otherwise raise AttributeError on
    the first .get(); skipping it prints a shorter summary instead of no summary.
    """
    return value if isinstance(value, dict) else {}


def emit_section(
    lines: list[str], header: str, rows: Sequence[tuple[str, str | None]], pal: Pal
) -> None:
    """Append a titled section with a label-aligned body (skips empty sections)."""
    present = [(label, value) for label, value in rows if value is not None]
    if not present:
        return
    lines.append("")
    lines.append(pal.hdr(header))
    width = max(len(label) for label, _ in present)
    gap = value_column([label for label, _ in present]) - LABEL_INDENT - width
    for label, value in present:
        lines.append(f"{' ' * LABEL_INDENT}{pal.label(label.ljust(width))}{' ' * gap}{value}")


def names(items, pal: Pal, kind: str | None = None) -> str:
    # Anything that is not a list would otherwise be iterated: a string joins
    # character by character, a number raises. Coerce to a one-element list so a
    # sloppy facts file degrades to readable output instead of garbage or a crash.
    if not isinstance(items, (list, tuple)):
        items = [items] if items else []
    if not items:
        return pal.dim("(none)")
    if kind == "add":
        return ", ".join(pal.add(clean(n)) for n in items)
    if kind == "rem":
        return ", ".join(pal.rem(clean(n)) for n in items)
    return ", ".join(clean(n) for n in items)


def color_diffstat(text, pal: Pal) -> str:
    """Colour a diffstat, in either shape it can arrive in.

    `git diff --stat` says "6 insertions(+), 5 deletions(-)"; a hand-written or
    synthesised value may use the compact "+6 / -5". Both are coloured, so the
    renderer does not silently no-op on the form it actually receives.
    """
    text = clean(text)
    text = re.sub(r"\d+ insertions?\(\+\)", lambda m: pal.add(m.group()), text)
    text = re.sub(r"\d+ deletions?\(-\)", lambda m: pal.rem(m.group()), text)
    text = re.sub(r"\+\d+", lambda m: pal.add(m.group()), text)
    text = re.sub(r"(?<!\w)-\d+", lambda m: pal.rem(m.group()), text)
    return text


# The TEMPLATES section's labels, in order. Named once so the multi-line
# "recommended" continuation indent stays aligned with emit_section's column.
TEMPLATE_LABELS = ("always-on", "recommended", "carried-over", "added", "removed")

MAX_FACTS_BYTES = 2_000_000  # a real facts file is a few KB
LABEL_INDENT = 2  # leading spaces before a label
LABEL_GAP = 2  # spaces between the padded label and its value


def value_column(labels: Sequence[str]) -> int:
    """Where a section's values start, given its labels.

    emit_section lays rows out with this same arithmetic; sharing it is what
    keeps a wrapped value aligned with the column above it.
    """
    return LABEL_INDENT + max(len(label) for label in labels) + LABEL_GAP


def render(facts: Facts | dict, pal: Pal) -> str:
    lines: list[str] = []
    title = clean(facts.get("title", "manage-gitignore - run summary"))
    lines.append(pal.title(title))
    lines.append(pal.rule("=" * len(title)))

    # NOTES (free-form context, e.g. a pre-run history reset)
    raw_notes = facts.get("notes") or []
    if not isinstance(raw_notes, (list, tuple)):
        raw_notes = [raw_notes]
    notes = [clean(n) for n in raw_notes if n]
    if notes:
        lines.append("")
        lines.append(pal.hdr("NOTES"))
        for note in notes:
            lines.append(f"  {pal.dim('•')} {note}")

    # SCAN
    scan = as_dict(facts.get("scan"))
    if scan:
        gi = scan.get("gitignore", "none")
        if gi == "existing":
            count = scan.get("prev_templates_count", "?")
            # No template block found: --detect calls this "(hand-written)", and
            # the summary should not describe it as "0 templates" as if some
            # were simply dropped.
            shape = "(hand-written)" if count == 0 else f"{clean(count)} templates"
            gi_val = f"existing — {shape}, {clean(scan.get('custom_lines', 0))} custom"
        else:
            gi_val = "none"
        emit_section(
            lines,
            "SCAN",
            [
                ("repo", "git repository" if scan.get("git_repo") else pal.dim("not a git repo")),
                (".gitignore", gi_val),
                ("detected", names(scan.get("detected"), pal)),
            ],
            pal,
        )

    # TEMPLATES
    tpl = as_dict(facts.get("templates"))
    if tpl:
        rec = tpl.get("recommended") or []
        if not isinstance(rec, (list, tuple)):
            rec = [rec]
        rec_val = None
        if rec:
            parts = []
            for item in rec:
                if isinstance(item, dict):
                    reason = item.get("reason")
                    name = clean(item.get("name", ""))
                    parts.append(f"{name}  {pal.dim(f'← {clean(reason)}')}" if reason else name)
                else:
                    parts.append(clean(item))
            indent = " " * value_column(TEMPLATE_LABELS)
            rec_val = f"\n{indent}".join(parts) if len(parts) > 1 else parts[0]
        # Say what removal costs: a template leaving the set means those paths
        # stop being ignored, which is the consequence worth naming.
        removed_value = names(tpl.get("removed"), pal, "rem")
        if tpl.get("removed"):
            removed_value += pal.dim("  (no longer ignored)")
        total = tpl.get("total")
        header = "TEMPLATES" + (f" — {clean(total)} total" if total is not None else "")
        emit_section(
            lines,
            header,
            list(
                zip(
                    TEMPLATE_LABELS,
                    [
                        names(tpl.get("always_on"), pal),
                        rec_val,
                        names(tpl.get("carried_over"), pal),
                        names(tpl.get("added"), pal, "add"),
                        removed_value,
                    ],
                    strict=True,
                )
            ),
            pal,
        )

    # MERGE
    merge = as_dict(facts.get("merge"))
    if merge:
        if merge.get("verbatim"):
            esc = merge.get("esc_bytes", 0)
            block = pal.ok(
                f"verbatim — byte-identical to API, no ANSI control bytes ({clean(esc)} ESC)"
            )
        else:
            block = "merged with custom rules"
        # custom_removed is a list of {line, covered_by}, but a caller may pass a
        # plain count or a list of bare strings; render those rather than crash.
        raw_removed = merge.get("custom_removed") or []
        if isinstance(raw_removed, int):
            removed_items, removed_count = [], raw_removed
        else:
            if isinstance(raw_removed, (str, dict)):
                raw_removed = [raw_removed]
            removed_items = [i if isinstance(i, dict) else {"line": str(i)} for i in raw_removed]
            removed_count = len(removed_items)
        rows = [
            ("template block", block),
            ("custom rules", f"{clean(merge.get('custom_kept', 0))} kept, {removed_count} removed"),
        ]
        for item in removed_items:
            line = clean(item.get("line", ""))
            covered = clean(item.get("covered_by", "template"))
            rows.append(("  removed", f"{line}  {pal.dim(f'(covered by {covered})')}"))
        emit_section(lines, "MERGE", rows, pal)

    # REVIEW — scoped to the fetched template block, never the custom rules
    review = as_dict(facts.get("review"))
    if review:
        review_rows: list[tuple[str, str | None]] = []
        review_rows += [("un-ignores", clean(x)) for x in as_list(review.get("negations"))]
        review_rows += [
            ("very broad", f"{clean(x)}  {pal.dim('(may ignore more than intended)')}")
            for x in as_list(review.get("broad"))
        ]
        if not review_rows:
            # An empty section would read as "the whole file is fine". Say what
            # was actually checked instead.
            review_rows = [("flagged", pal.dim("none (custom rules not scanned)"))]
        emit_section(lines, "REVIEW — in the template block", review_rows, pal)

    # WRITE
    write = as_dict(facts.get("write"))
    if write:
        mode = write.get("mode", "new")
        if mode == "overwrite":
            reason = write.get("reason")
            val = f"overwritten {pal.dim('— replaced existing file, custom rules kept')}"
            if reason:
                val += f" {pal.dim(f'({clean(reason)})')}"
        else:
            val = f"created {pal.dim('(new file)')}"
        emit_section(lines, "WRITE", [(clean(write.get("path", ".gitignore")), val)], pal)

    # COMMIT
    commit = as_dict(facts.get("commit"))
    if commit:
        # One default, used by both the row and the push gate below: two
        # fallbacks drifting apart produced a self-contradictory summary.
        choice = clean(commit.get("choice", "not committed"))
        rows = [("choice", choice)]
        if commit.get("hash"):
            subj = clean(commit.get("subject", ""))
            rows.append(("commit", f"{pal.hashc(clean(commit['hash']))}  {subj}"))
        if commit.get("scope"):
            untouched = commit.get("untouched")
            scope = clean(commit["scope"])
            if untouched:
                scope += f"  {pal.dim(f'({clean(untouched)} untouched)')}"
            rows.append(("scope", scope))
        # Only meaningful once something was committed: "not pushed" under
        # choice "not committed" reads as a failure rather than a non-event.
        if choice != "not committed":
            push = commit.get("push")
            if isinstance(push, dict):
                where = f"{clean(push.get('remote', ''))}/{clean(push.get('branch', ''))}"
                pushed = f"{pal.hashc(clean(push.get('sha', '')))} \u2192 {where}"
            elif push:
                pushed = clean(push)  # a hand-written facts file
            else:
                pushed = pal.dim("not pushed")
                if notes:
                    # The explanation lives in NOTES; point at it from the row
                    # it explains rather than leaving the reader to connect them.
                    pushed += pal.dim(" — see NOTES")
            rows.append(("push", pushed))
        emit_section(lines, "COMMIT", rows, pal)

    # NET
    net = as_dict(facts.get("net"))
    if net:
        tpl = as_dict(facts.get("templates"))  # read locally: no section ordering
        rows = []
        if net.get("prev_count") is not None and net.get("new_count") is not None:
            # Built here from templates.added/removed rather than stored twice:
            # a second copy is a second thing that can disagree.
            bits = [f"+{clean(t)}" for t in as_list(tpl.get("added"))]
            bits += [f"-{clean(t)}" for t in as_list(tpl.get("removed"))]
            delta = " ".join(bits)
            prev, new = clean(net["prev_count"]), clean(net["new_count"])
            rows.append(("templates", f"{prev} → {new}  {pal.dim(delta)}".rstrip()))
        if net.get("diffstat"):
            rows.append(("diff", color_diffstat(net["diffstat"], pal)))
        emit_section(lines, "NET", rows, pal)

    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        allow_abbrev=False, description="Render manage-gitignore skill run summary."
    )
    parser.add_argument("facts", nargs="?", help="path to JSON facts file (else stdin)")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    args = parser.parse_args()

    # `is not None`, not truthiness: an empty --facts argument must be an error,
    # not a silent switch to stdin that then blocks forever on a terminal.
    def _die(msg: str) -> NoReturn:
        print(f"render_summary: {msg}", file=sys.stderr)
        sys.exit(1)

    if args.facts is not None:
        # Same no-follow reader as everywhere else: a facts path is caller-
        # supplied and can be a symlink or a FIFO.
        try:
            raw = read_bytes_or_die(args.facts, _die).decode("utf-8")
        except UnicodeDecodeError as exc:
            _die(f"cannot read facts file: {exc}")
    else:
        if sys.stdin.isatty():
            # Nothing is coming: a bare invocation on a terminal would otherwise
            # sit reading forever, looking like a hang rather than a usage error.
            print(
                "render_summary: no FACTS path given and stdin is a terminal "
                "(pass a file, or pipe JSON in)",
                file=sys.stderr,
            )
            sys.exit(1)
        # Bounded like the network fetch: a piped blob is no more trustworthy
        # than a downloaded one. Explicit UTF-8, not whatever the locale says.
        try:
            blob = sys.stdin.buffer.read(MAX_FACTS_BYTES + 1)
            if len(blob) > MAX_FACTS_BYTES:
                _die(f"facts on stdin exceeded {MAX_FACTS_BYTES} bytes")
            raw = blob.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _die(f"cannot read facts from stdin: {exc}")

    try:
        facts = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"render_summary: invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(facts, dict):
        print(
            f"render_summary: facts must be a JSON object, got {type(facts).__name__}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(render(facts, Pal(use_color(args.color))))


if __name__ == "__main__":
    main()
