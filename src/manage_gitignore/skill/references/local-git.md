# Non-ChatGPT environments — local Git path

Use this path for Claude Code, Codex CLI, GitHub Copilot CLI, and any host that
does not identify itself as ChatGPT.

Use `scripts/gitwork.py` for all Git mutations:

- `status --facts` for review
- `commit --message-file --facts` for committing only `.gitignore`
- `push-plan` then `push --facts` for controlled push behaviour
- `facts --facts` then `summary.py` for final reporting

Do not run `git add` / `git commit` / `git push` directly.
