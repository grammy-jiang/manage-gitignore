#!/usr/bin/env python3
"""Render the manage-gitignore skill's end-of-run summary.

Reads a JSON facts file (see schema below) and prints a categorized, aligned
summary. Colorized on an interactive terminal; plain text (no ANSI) when output
is piped/redirected or color is disabled.

Color is ON when: stdout is a TTY, TERM != "dumb", and NO_COLOR is unset.
Override with --color=always|never|auto (default auto) or FORCE_COLOR / NO_COLOR.

Usage:
  python3 <skill-dir>/scripts/summary.py FACTS.json [--color auto|always|never]

The facts file is written by `templates --facts-out` and `git facts`, both of
which build it through the TypedDicts in shared.py. It is not hand-authored --
SKILL.md says so explicitly -- so this renderer trusts the shape and does not
carry coercion for values its own producers cannot emit. A malformed file is a
bug worth surfacing, not one to paper over.

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
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from shared import (  # one sanitiser, shared by every tool in this skill
    FACTS_TOOL,
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


def names(items: Sequence[object] | None, pal: Pal, kind: str | None = None) -> str:
    if not items:
        return pal.dim("(none)")
    if kind == "add":
        return ", ".join(pal.add(clean(n)) for n in items)
    if kind == "rem":
        return ", ".join(pal.rem(clean(n)) for n in items)
    return ", ".join(clean(n) for n in items)


def color_diffstat(text: object, pal: Pal) -> str:
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

LABEL_INDENT = 2  # leading spaces before a label
LABEL_GAP = 2  # spaces between the padded label and its value


def value_column(labels: Sequence[str]) -> int:
    """Where a section's values start, given its labels.

    emit_section lays rows out with this same arithmetic; sharing it is what
    keeps a wrapped value aligned with the column above it.
    """
    return LABEL_INDENT + max(len(label) for label in labels) + LABEL_GAP


def render(facts: Facts | Mapping[str, Any], pal: Pal) -> str:
    # `Any` in the values, not `object`: this walks a JSON document whose shape
    # the module docstring says it trusts, indexing and `.get`-ing its way down.
    # Typing the values as `object` would need a cast at every step, and a cast
    # asserts the same thing with more ceremony and no more checking.
    lines: list[str] = []
    title = clean(facts.get("title", "manage-gitignore - run summary"))
    lines.append(pal.title(title))
    lines.append(pal.rule("=" * len(title)))

    # NOTES (free-form context, e.g. a pre-run history reset)
    notes = [clean(n) for n in facts.get("notes") or [] if n]
    if notes:
        lines.append("")
        lines.append(pal.hdr("NOTES"))
        for note in notes:
            lines.append(f"  {pal.dim('•')} {note}")

    # SCAN
    scan = facts.get("scan") or {}
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
    tpl = facts.get("templates") or {}
    if tpl:
        rec = tpl.get("recommended") or []
        rec_val = None
        if rec:
            parts = []
            for item in rec:
                reason = item.get("reason")
                name = clean(item.get("name", ""))
                parts.append(f"{name}  {pal.dim(f'← {clean(reason)}')}" if reason else name)
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
    merge = facts.get("merge") or {}
    if merge:
        if merge.get("verbatim"):
            esc = merge.get("esc_bytes", 0)
            block = pal.ok(
                f"verbatim — byte-identical to API, no ANSI control bytes ({clean(esc)} ESC)"
            )
        else:
            block = "merged with custom rules"
        removed_items = merge.get("custom_removed") or []
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
    review = facts.get("review") or {}
    if review:
        review_rows: list[tuple[str, str | None]] = []
        review_rows += [("un-ignores", clean(x)) for x in review.get("negations") or []]
        review_rows += [
            ("very broad", f"{clean(x)}  {pal.dim('(may ignore more than intended)')}")
            for x in review.get("broad") or []
        ]
        if not review_rows:
            # An empty section would read as "the whole file is fine". Say what
            # was actually checked instead.
            review_rows = [("flagged", pal.dim("none (custom rules not scanned)"))]
        emit_section(lines, "REVIEW — in the template block", review_rows, pal)

    # WRITE
    write = facts.get("write") or {}
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
    commit = facts.get("commit") or {}
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
            if push:
                where = f"{clean(push.get('remote', ''))}/{clean(push.get('branch', ''))}"
                pushed = f"{pal.hashc(clean(push.get('sha', '')))} \u2192 {where}"
            else:
                pushed = pal.dim("not pushed")
                if notes:
                    # The explanation lives in NOTES; point at it from the row
                    # it explains rather than leaving the reader to connect them.
                    pushed += pal.dim(" — see NOTES")
            rows.append(("push", pushed))
        emit_section(lines, "COMMIT", rows, pal)

    # NET
    net = facts.get("net") or {}
    if net:
        tpl = facts.get("templates") or {}  # read locally: no section ordering
        rows = []
        if net.get("prev_count") is not None and net.get("new_count") is not None:
            # Built here from templates.added/removed rather than stored twice:
            # a second copy is a second thing that can disagree.
            bits = [f"+{clean(t)}" for t in tpl.get("added") or []]
            bits += [f"-{clean(t)}" for t in tpl.get("removed") or []]
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
    parser.add_argument("facts", help="path to the JSON facts file")
    parser.add_argument("--color", choices=["auto", "always", "never"], default="auto")
    args = parser.parse_args()

    def _die(msg: str) -> NoReturn:
        print(f"render_summary: {msg}", file=sys.stderr)
        sys.exit(1)

    # Same no-follow reader as everywhere else: a facts path is caller-supplied
    # and can be a symlink or a FIFO.
    try:
        raw = read_bytes_or_die(args.facts, _die).decode("utf-8")
    except UnicodeDecodeError as exc:
        _die(f"cannot read facts file: {exc}")

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
    # The same gate gitwork.py applies, and for the same reason: this is the
    # closing report of a run, so rendering somebody else's document produces a
    # confident summary of work that did not happen here.
    if facts.get("tool") != FACTS_TOOL:
        _die(f"{args.facts} is not a {FACTS_TOOL} facts file (no marker).")

    print(render(facts, Pal(use_color(args.color))))


if __name__ == "__main__":
    main()
