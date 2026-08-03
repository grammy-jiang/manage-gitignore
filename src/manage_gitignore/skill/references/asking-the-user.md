# Splitting the template question

AskUserQuestion allows at most **4 options per question**, and the free-text
"Other" always occupies one — so a question holds at most **3 substantive
entries**. Use as many questions as that takes, up to three.

| Situation | Questions |
|---|---|
| `recommended` + `carried_over` fit in 3 | **one** — plus extras if there is room, "Other" as the 4th option |
| they do not fit together | **two** — recommended + carried-over first, extras second, "Other" on the second |
| `recommended` alone needs all 3 | **three** — recommended, then carried-over, then extras with "Other" |

## What may be shortened, and what may not

**`carried_over` is never truncated.** Those templates are protecting files
right now; anything dropped from the set stops being ignored the moment the file
is written. If they do not fit, give them their own question — do not relegate
them to a free-text fallback the user has to remember to use.

**`recommended` and extras may be shortened**, keeping the highest-value
entries — but name every entry you left out, so the user can still add it via
"Other".

## Wording the "Other" option

Say what the tool will accept, so a near-miss is not a surprise:

> exact catalogue name(s), comma-separated — ask me to list them if unsure

`manage-gitignore templates` rejects any name that is not in the catalogue, and any name
beginning with `-`. It does not guess or correct.
