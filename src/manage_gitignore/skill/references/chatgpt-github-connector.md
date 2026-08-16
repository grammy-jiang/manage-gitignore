# The GitHub connector path (ChatGPT only)

Read this only when your host's runtime context identifies ChatGPT or ChatGPT
Work. Every other host uses Step 4 of `SKILL.md` as written.

In ChatGPT the credentials to reach GitHub belong to the connector, and only you
can call it — `scripts/gitwork.py` cannot. So the write is yours to make. Nothing
else is: `connector-plan` computes what the write must send, and
`connector-record` refuses to call the result a success unless that is exactly
what came back. You carry values between two gates you cannot move.

Local `gitwork.py commit` and `push` are not used here. Running the local commit
first produces two independently authored commits and leaves the checkout
diverged from the branch you just wrote to.

## Before offering the choice

1. Find the GitHub tools by capability and provenance (`@GitHub`) rather than by
   one generated function name — those names are not a stable API.
2. Confirm the connector with a harmless read of the exact target repository.
3. Check that the returned permissions include push.

If the connector is missing, unselected, disconnected, or unauthorised, or if
push permission is absent: say so, ask the user to install/select/connect
`@GitHub`, and **stop there**. Do not fall back to shell git — in ChatGPT it has
no credentials, and the run would end with a local commit the user did not ask
for and a push that could never work.

## Which choices this path covers

**Commit + push only.** It is the choice that has to reach GitHub, and the one
this file exists for.

- **Commit only** stays a local `gitwork.py commit`, unchanged. It is by
  definition a request *not* to publish, so routing it through the connector
  would do the one thing it asks you not to do.
- **Don't commit** leaves the generated file on disk, as everywhere else.

## The write

Resolve the canonical `owner/name`, the branch, and the branch's current head
through the connector first. Then:

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" connector-plan \
  --facts "<facts.json>" --expect-head "<branch head>" --branch "<branch>"
```

It fails, and the run stops, if `.gitignore` is not the file this run verified,
or if the local checkout is not at that branch head. The second is not a
formality: Step 3 rebuilt from the *local* file, so a checkout behind the branch
would drop every custom rule added on the remote since — and it would do it
silently, because the branch head has not moved by the time you write.

It returns `blob_sha`, `expected_parent`, `content_base64` and `content_sha256`.
Use those, not your own reading of the file. Then, through the connector:

1. Re-read the branch head and check it still equals `expected_parent`.
2. Create a blob from `content_base64`, base64-encoded.
3. Create a tree on the branch's base tree, replacing or adding **only**
   `.gitignore`, mode `100644`.
4. Create a commit with the approved one-line message, that tree, and
   `expected_parent` as its single parent.
5. Update the branch ref with `force=false`.

If the branch moved, the ref update is not a fast-forward, or any tool asks for
further authorisation: stop and report the exact state. Never force.

The simpler `create_file` / `update_file` actions may be used only if they
preserve the bytes exactly, and for an existing file only with the previously
fetched blob SHA supplied, so a concurrent change fails instead of being
overwritten. `connector-record` checks the result either way.

## Recording it

Read the new commit back through the connector — its parent, and the paths it
changed — then:

```bash
python3 "<skill-dir>/scripts/gitwork.py" --dir "<repo>" connector-record \
  --facts "<facts.json>" --blob-sha "<blob the connector stored>" \
  --parent "<the commit's parent>" --changed-path "<each path it changed>" \
  --commit-sha "<new commit>" --repository "<owner/name>" \
  --branch "<branch>" --commit-url "<canonical URL>"
```

Pass `--changed-path` once per path the commit actually changed, whatever they
are. Do not filter the list down to `.gitignore` — that check is the point of
passing it, and a list you have already trimmed cannot fail it.

- **exit 0** — verified. The facts file now holds the commit and the push.
- **exit 7** — the write is not the one that was approved. The reason says which
  value disagreed. The remote commit may well exist; what has failed is this
  run's ability to vouch for it. Relay the reason, do not retry the write, and
  go to Step 5 — the outcome is already recorded.

Then Step 5 as usual, with `--requested-action "commit + push"`. Pass no
`--hash`: that is the local transport's, and there is no local commit here.
