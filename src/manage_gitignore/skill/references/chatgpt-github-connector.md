# ChatGPT / ChatGPT Work — GitHub connector path

Use this path only when host runtime context identifies ChatGPT.

1. Discover GitHub connector tools by capability (`@GitHub`), not by one fixed
   generated MCP function name.
2. Probe with a harmless repository read for the exact target.
3. Require `permissions.push == true` before offering **Commit + push**.
4. If connector install/selection/connection/authorization is missing, ask the
   user to enable/connect `@GitHub` and stop there. Do **not** fall back to
   local shell Git.

Then run the connector workflow:

1. Resolve canonical `owner/name`, URL, branch, and branch head.
2. Read `.gitignore` on that branch and keep the baseline blob SHA (or absence).
3. Generate locally with `templates.py` as usual; keep verified bytes unchanged.
4. Ask approval with exact repo URL, branch, diff, and one-line message.
5. On **Commit + push**, commit through the connector (Git Data flow preferred):
   - re-check branch head matches approval baseline;
   - create blob from verified bytes; verify blob SHA;
   - create tree replacing only `.gitignore`;
   - create commit with approved message and approved parent;
   - update ref with `force=false`.
6. Verify remote result before success:
   - commit exists, parent matches expected head;
   - exactly one changed path: `.gitignore`;
   - fetched `.gitignore` bytes/hash match verified payload;
   - branch points to/contains the commit.
7. Record summary facts with `gitwork.py facts` using:
   `--requested-action`, `--transport chatgpt-github-connector`,
   `--commit-status/--commit-sha/--commit-url`, and
   `--push-status/--push-repository/--push-branch/--push-reason`.

Never run local `gitwork.py commit` or `gitwork.py push` for ChatGPT
**Commit + push**.
