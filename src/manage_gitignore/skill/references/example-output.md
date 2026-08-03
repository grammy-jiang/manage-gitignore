# manage-gitignore — example run summary

What `manage-gitignore summary` prints at the end of a run (Step 5). Shown here plain;
on an interactive terminal the same output is colorized.

```text
manage-gitignore — run summary
==============================

SCAN
  repo        git repository
  .gitignore  existing — 11 templates, 0 custom
  detected    node (scripts/lint-mermaid.mjs)

TEMPLATES — 12 total
  always-on     git, macos, linux, windows, vim, emacs, visualstudiocode
  recommended   node  ← scripts/lint-mermaid.mjs
  carried-over  python, dotenv, jetbrains+all
  added         direnv
  removed       (none)

MERGE
  template block  verbatim — byte-identical to API, no ANSI control bytes (0 ESC)
  custom rules    0 kept, 0 removed

REVIEW — in the template block
  un-ignores  !*.svg

WRITE
  .gitignore  overwritten (--force; file existed)

COMMIT
  choice  commit + push
  commit  6e0a827  chore: add direnv to .gitignore
  scope   .gitignore only  (4 staged pre-commit files untouched)
  push    6e0a827 → origin/master

NET
  templates  11 → 12  +direnv
  diff       1 file changed, 7 insertions(+), 3 deletions(-)
```

## Reading it

- **SCAN** — what was found before anything changed: whether it is a git repo,
  whether a `.gitignore` already existed, and how it was composed (template
  count + custom-rule count).
- **TEMPLATES** — every template grouped by *why* it is in the set, so the choice
  is auditable: `always-on` (the fixed defaults), `recommended` (each with the
  marker file that triggered it), `carried-over` (from the previous file),
  `added`, `removed`.
- **MERGE** — proof the template block was written verbatim (`0 ESC` = no ANSI
  corruption), plus the custom-rule accounting: kept vs dropped-as-duplicate,
  each dropped line naming the template that already covers it.
- **REVIEW** — patterns in the *fetched template block* worth a human glance:
  negations that un-ignore a path, and anything broad enough to ignore the tree.
  It says nothing about the carried-over custom rules, so an empty or absent
  section is not a clean bill of health for the whole file.
- **WRITE / COMMIT / NET** — what landed on disk, what was committed (and what
  was deliberately left untouched), and the net change in templates and lines.

The JSON facts schema that produces this block is documented at the top of
`manage-gitignore summary`.

**Keeping this file honest:** every row above is literal `manage-gitignore summary`
output. If that renderer's wording changes, this example has to be regenerated
in the same commit — a worked example showing a format the code never emits is
worse than no example at all.
