"""gitwork.py — state classification, the commit gate, and the push gate.

This is the module that mutates a repository, so the tests lean on real git
repos rather than mocks: a wrong answer here is a lost commit or a rewritten
remote branch. Several tests pin defects the review loop found in earlier
versions of this file; those say so in the docstring.
"""

from __future__ import annotations

import json
import subprocess

import pytest

import gitwork as gw
from conftest import git, init_repo, remote_head


@pytest.fixture
def facts_file(tmp_path):
    path = tmp_path / "facts.json"
    path.write_text(json.dumps({"merge": {"esc_bytes": 0}}), encoding="utf-8")
    return path


def write_gitignore(repo, text="node_modules/\n"):
    (repo / ".gitignore").write_text(text, encoding="utf-8")


def msg_file(tmp_path, text="chore: update .gitignore\n"):
    path = tmp_path / "msg.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ── file state ──────────────────────────────────────────────────────────────
class TestFileState:
    def test_clean_when_nothing_changed(self, repo):
        assert gw.file_state(str(repo)) == "clean"

    def test_untracked(self, repo):
        write_gitignore(repo)
        assert gw.file_state(str(repo)) == "untracked"

    def test_staged_new_file(self, repo):
        write_gitignore(repo)
        git(repo, "add", ".gitignore")
        assert gw.file_state(str(repo)) == "staged"

    def test_modified_but_not_staged(self, repo):
        """Regression: git() used to strip stdout, which drops porcelain's

        leading space and turns " M path" (modified) into "M path" (staged).
        The status command then showed an EMPTY --cached diff for a real change.
        """
        write_gitignore(repo)
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        write_gitignore(repo, "node_modules/\ndist/\n")
        assert gw.file_state(str(repo)) == "modified"

    def test_staged_with_further_unstaged_changes(self, repo):
        write_gitignore(repo)
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        write_gitignore(repo, "a/\n")
        git(repo, "add", ".gitignore")
        write_gitignore(repo, "a/\nb/\n")
        assert gw.file_state(str(repo)) == "staged"


class TestHasCommits:
    def test_false_on_an_unborn_head(self, empty_repo):
        assert gw.has_commits(str(empty_repo)) is False

    def test_true_once_a_commit_exists(self, repo):
        assert gw.has_commits(str(repo)) is True


# ── status ──────────────────────────────────────────────────────────────────
class TestStatus:
    @staticmethod
    def _status(run_script, repo):
        out = run_script("gitwork.py", "--dir", str(repo), "status")
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_a_clean_repo_reports_no_change(self, repo, run_script):
        """The no-op outcome the agent branches on to skip the commit question."""
        write_gitignore(repo)
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        data = self._status(run_script, repo)
        assert data["state"] == "clean"
        assert data["changed"] is False
        assert data["diff"] == ""

    def test_a_dot_git_directory_is_not_a_work_tree(self, repo):
        """git exits 0 there and prints "false"; the exit code alone would lie."""
        assert gw.is_repo(str(repo / ".git")) is False

    def test_reports_a_non_repo_without_failing(self, plain_dir, run_script):
        assert self._status(run_script, plain_dir)["is_repo"] is False

    def test_untracked_uses_status_short(self, repo, run_script):
        write_gitignore(repo)
        assert self._status(run_script, repo)["diff_command"] == "git status --short -- .gitignore"

    def test_tracked_diffs_against_head(self, repo, run_script):
        write_gitignore(repo)
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        write_gitignore(repo, "node_modules/\ndist/\n")
        assert self._status(run_script, repo)["diff_command"] == "git diff HEAD -- .gitignore"

    def test_mixed_state_diff_shows_the_unstaged_hunks_too(self, repo, run_script):
        """Regression: `commit --only` re-stages from the work tree, so a

        --cached diff would have shown the user less than what gets committed.
        """
        write_gitignore(repo, "a/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        write_gitignore(repo, "a/\nstaged-line/\n")
        git(repo, "add", ".gitignore")
        write_gitignore(repo, "a/\nstaged-line/\nlate-line/\n")
        diff = self._status(run_script, repo)["diff"]
        assert "staged-line/" in diff
        assert "late-line/" in diff

    def test_flags_a_diff_carrying_invisible_characters(self, repo, run_script):
        """The reviewer is approving what the terminal renders, not the bytes."""
        write_gitignore(repo, "a/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        write_gitignore(repo, "a/\nb\u202eevil/\n")
        out = run_script("gitwork.py", "--dir", str(repo), "status")
        assert json.loads(out.stdout)["suspicious_characters"] is True
        assert "WARNING" in out.stderr

    def test_an_ordinary_diff_is_not_flagged(self, repo, run_script):
        write_gitignore(repo, "a/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        write_gitignore(repo, "a/\nb/\n")
        out = run_script("gitwork.py", "--dir", str(repo), "status")
        assert json.loads(out.stdout)["suspicious_characters"] is False

    def test_unborn_head_shows_the_whole_file(self, empty_repo, run_script):
        """Regression: `git diff HEAD` is fatal in a repo with no commits.

        And --cached would hide an unstaged edit that `commit --only` still
        records, so a first commit is diffed against /dev/null instead.
        """
        write_gitignore(empty_repo, "staged/\n")
        git(empty_repo, "add", ".gitignore")
        write_gitignore(empty_repo, "staged/\nlate/\n")
        data = self._status(run_script, empty_repo)
        assert "--no-index" in data["diff_command"]
        assert "staged/" in data["diff"]
        assert "late/" in data["diff"]


# ── ref handling ────────────────────────────────────────────────────────────
class TestSafeRef:
    def test_rejects_a_ref_that_looks_like_an_option(self):
        """`--ref=--output=/tmp/x` would otherwise reach git as a flag."""
        with pytest.raises(SystemExit):
            gw.safe_ref("--output=/tmp/pwn")

    def test_passes_an_ordinary_ref_through(self):
        assert gw.safe_ref("HEAD") == "HEAD"


class TestCommitFiles:
    def test_an_unresolvable_ref_is_an_error_not_an_empty_commit(self, repo):
        """Regression: the rc was ignored, so a bad ref read as "touches nothing"."""
        with pytest.raises(SystemExit):
            gw.commit_files(str(repo), "deadbee")

    def test_lists_the_paths_a_commit_touched(self, repo):
        assert gw.commit_files(str(repo)) == ["seed.txt"]


class TestUndoHint:
    def test_first_commit_has_no_parent_to_reset_to(self, empty_repo):
        write_gitignore(empty_repo)
        git(empty_repo, "add", ".gitignore")
        git(empty_repo, "commit", "-qm", "first")
        assert "update-ref -d HEAD" in gw.undo_hint(str(empty_repo))

    def test_later_commits_use_reset_soft(self, repo):
        (repo / "second.txt").write_text("x\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "second")
        assert "reset --soft HEAD^" in gw.undo_hint(str(repo))


# ── commit ──────────────────────────────────────────────────────────────────
class TestCommit:
    def test_commits_only_gitignore_and_leaves_the_index_alone(self, repo, run_script, tmp_path):
        write_gitignore(repo)
        (repo / "other.txt").write_text("other\n", encoding="utf-8")
        git(repo, "add", "other.txt")
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert out.returncode == 0, out.stderr
        data = json.loads(out.stdout)
        assert data["files"] == [".gitignore"]
        assert data["only_gitignore"] is True
        staged = git(repo, "diff", "--cached", "--name-only").stdout.split()
        assert staged == ["other.txt"]

    def test_reports_what_it_left_untouched_as_a_formatted_phrase(self, repo, run_script, tmp_path):
        """The agent must never have to turn a raw count into prose."""
        write_gitignore(repo)
        (repo / "other.txt").write_text("x\n", encoding="utf-8")
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert json.loads(out.stdout)["untouched"] == "1 other file"

    def test_reports_several_untouched_files_in_the_plural(self, repo, run_script, tmp_path):
        write_gitignore(repo)
        for name in ("a.txt", "b.txt"):
            (repo / name).write_text("x\n", encoding="utf-8")
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert json.loads(out.stdout)["untouched"] == "2 other files"

    def test_facts_may_not_target_the_gitignore_itself(self, repo, run_script, tmp_path):
        write_gitignore(repo)
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "commit",
            "--message-file",
            str(msg_file(tmp_path)),
            "--facts",
            str(repo / ".gitignore"),
        )
        assert out.returncode == 1
        assert "must not be" in out.stderr

    def test_an_empty_message_file_is_refused_without_staging(self, repo, run_script, tmp_path):
        write_gitignore(repo)
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "commit",
            "--message-file",
            str(msg_file(tmp_path, "   \n\n")),
        )
        assert out.returncode == 1
        assert "message file is empty" in out.stderr
        assert git(repo, "diff", "--cached", "--name-only").stdout.strip() == ""

    def test_refuses_when_there_is_nothing_to_commit(self, repo, run_script, tmp_path):
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert out.returncode == 1
        assert "no changes to commit" in out.stderr

    def test_a_failed_commit_unstages_what_it_staged(self, repo, run_script):
        """Regression: `add` succeeded then `commit` failed, stranding the index."""
        write_gitignore(repo)
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", "/no/such/msg"
        )
        assert out.returncode == 1
        assert git(repo, "diff", "--cached", "--name-only").stdout.strip() == ""

    def test_records_its_own_commit_block_into_facts(self, repo, run_script, tmp_path, facts_file):
        write_gitignore(repo)
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "commit",
            "--message-file",
            str(msg_file(tmp_path)),
            "--facts",
            str(facts_file),
        )
        assert out.returncode == 0, out.stderr
        commit = json.loads(facts_file.read_text())["commit"]
        assert commit["hash"] == json.loads(out.stdout)["hash"]
        assert commit["subject"] == "chore: update .gitignore"
        assert commit["scope"] == ".gitignore only"

    def test_refuses_a_file_that_changed_since_it_was_verified(
        self, repo, run_script, tmp_path, facts_file
    ):
        """The write→commit binding: a path match alone is not enough."""
        import hashlib

        write_gitignore(repo)
        digest = hashlib.sha256((repo / ".gitignore").read_bytes()).hexdigest()
        facts_file.write_text(
            json.dumps({"internal": {"written_sha256": digest}}), encoding="utf-8"
        )
        write_gitignore(repo, "node_modules/\nTAMPERED\n")
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "commit",
            "--message-file",
            str(msg_file(tmp_path)),
            "--facts",
            str(facts_file),
        )
        assert out.returncode == 1
        assert "changed since it was written" in out.stderr
        assert git(repo, "log", "--oneline").stdout.count("\n") == 1  # nothing new

    def test_refuses_a_gitignore_swapped_for_a_symlink(
        self, repo, run_script, tmp_path, facts_file
    ):
        """The checksum re-read must not follow a link to an out-of-repo secret."""
        import hashlib

        write_gitignore(repo)
        digest = hashlib.sha256((repo / ".gitignore").read_bytes()).hexdigest()
        facts_file.write_text(
            json.dumps({"internal": {"written_sha256": digest}}), encoding="utf-8"
        )
        secret = tmp_path / "secret"
        secret.write_text("PRIVATE KEY\n", encoding="utf-8")
        (repo / ".gitignore").unlink()
        (repo / ".gitignore").symlink_to(secret)
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "commit",
            "--message-file",
            str(msg_file(tmp_path)),
            "--facts",
            str(facts_file),
        )
        assert out.returncode == 1
        assert "symlink" in out.stderr
        assert git(repo, "log", "--oneline").stdout.count("\n") == 1

    def test_refuses_a_gitignore_swapped_for_a_fifo(self, repo, run_script, tmp_path, facts_file):
        import hashlib
        import os as _os

        write_gitignore(repo)
        digest = hashlib.sha256((repo / ".gitignore").read_bytes()).hexdigest()
        facts_file.write_text(
            json.dumps({"internal": {"written_sha256": digest}}), encoding="utf-8"
        )
        (repo / ".gitignore").unlink()
        _os.mkfifo(repo / ".gitignore")
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "commit",
            "--message-file",
            str(msg_file(tmp_path)),
            "--facts",
            str(facts_file),
        )
        assert out.returncode == 1
        assert "not a regular file" in out.stderr

    def test_an_over_scoped_commit_still_reports_its_hash(
        self, repo, run_script, tmp_path, monkeypatch
    ):
        """The caller needs the hash to report — and undo — the bad commit."""
        write_gitignore(repo)
        (repo / "other.txt").write_text("x\n", encoding="utf-8")
        # A hook that sneaks another file into the commit being made.
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\ngit add other.txt\n", encoding="utf-8")
        hook.chmod(0o755)
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert out.returncode == 2
        data = json.loads(out.stdout)
        assert data["only_gitignore"] is False
        assert data["hash"]
        assert "other.txt" in data["files"]

    def test_accepts_a_file_matching_its_recorded_checksum(
        self, repo, run_script, tmp_path, facts_file
    ):
        import hashlib

        write_gitignore(repo)
        digest = hashlib.sha256((repo / ".gitignore").read_bytes()).hexdigest()
        facts_file.write_text(
            json.dumps({"internal": {"written_sha256": digest}}), encoding="utf-8"
        )
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "commit",
            "--message-file",
            str(msg_file(tmp_path)),
            "--facts",
            str(facts_file),
        )
        assert out.returncode == 0, out.stderr


# ── push planning ───────────────────────────────────────────────────────────
class TestGuidance:
    """SKILL.md says "say the plan's guidance" instead of carrying a 9-row table,
    so every action the tool can return must produce a usable sentence."""

    def test_every_action_has_guidance(self):
        for action in gw.ACTION_GUIDANCE:
            plan = gw.describe({"action": action})
            assert plan["guidance"]
            assert "{dest}" not in plan["guidance"]

    def test_no_action_is_missing_from_the_table(self, remote_pair, repo):
        """A new action with no entry would fall through to its own bare name."""
        for r in (repo, remote_pair[0]):
            action = gw.push_plan(str(r))["action"]
            assert action in gw.ACTION_GUIDANCE

    def test_permits_push_matches_what_push_will_attempt(self):
        assert gw.describe({"action": "fast-forward"})["permits_push"] is True
        assert gw.describe({"action": "diverged"})["permits_push"] is True
        assert gw.describe({"action": "stop-up-to-date"})["permits_push"] is False
        assert gw.describe({"action": "stop-behind-only"})["permits_push"] is False

    def test_an_upstream_destination_names_the_branch_and_url(self, remote_pair):
        work, bare = remote_pair
        plan = gw.describe(gw.push_plan(str(work)))
        assert "origin/main" in plan["guidance"]
        assert str(bare) in plan["guidance"]

    def test_a_first_push_destination_names_the_url(self, repo, make_bare):
        bare = make_bare("only")
        git(repo, "remote", "add", "only", str(bare))
        plan = gw.describe(gw.push_plan(str(repo)))
        assert plan["action"] == "no-upstream"
        assert str(bare) in plan["guidance"]

    def test_an_unsettled_destination_says_so(self, repo, make_bare):
        """Several remotes, no origin: the tool must not imply one destination."""
        for name in ("alpha", "beta"):
            git(repo, "remote", "add", name, str(make_bare(name)))
        plan = gw.describe(gw.push_plan(str(repo)))
        assert plan["remote"] is None
        assert "not settled yet" in plan["guidance"]


class TestCommitVerdict:
    """SKILL.md reads `verdict` instead of enumerating four outcomes twice."""

    def test_a_clean_commit_reports_ok(self, repo, run_script, tmp_path):
        write_gitignore(repo)
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        data = json.loads(out.stdout)
        assert data["verdict"] == "ok"
        assert data["content_matches"] is True

    def test_an_over_scoped_commit_says_what_to_record(self, repo, run_script, tmp_path):
        write_gitignore(repo)
        (repo / "other.txt").write_text("x\n", encoding="utf-8")
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\ngit add other.txt\n", encoding="utf-8")
        hook.chmod(0o755)
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        data = json.loads(out.stdout)
        assert data["verdict"] == "touched-extra-files"
        assert data["record_choice"] == "not committed"
        assert "touched extra files" in data["record_note"]
        assert data["remedy"]


class TestSafeMergeRef:
    """branch.<name>.merge is repo config, and it builds a push refspec."""

    def test_accepts_an_ordinary_branch_ref(self):
        assert gw.safe_merge_ref("refs/heads/feature/foo") == "refs/heads/feature/foo"

    @pytest.mark.parametrize(
        "ref",
        [
            "refs/heads/evil:refs/heads/main",  # a second refspec smuggled in
            "+refs/heads/main",  # a leading + forces the push
            "--upload-pack=/bin/sh",  # an option, not a ref
            "refs/tags/v1",  # not a branch
            "main",  # unqualified
        ],
    )
    def test_refuses_anything_that_is_not_a_plain_branch_ref(self, ref):
        with pytest.raises(SystemExit):
            gw.safe_merge_ref(ref)


class TestSafeToken:
    def test_rejects_a_remote_that_looks_like_an_option(self):
        """A repo's own config can name a remote whatever it likes."""
        with pytest.raises(SystemExit):
            gw.safe_token("--upload-pack=/bin/sh", "remote")

    def test_passes_an_ordinary_remote_through(self):
        assert gw.safe_token("origin", "remote") == "origin"


class TestGitHelper:
    def test_a_missing_git_binary_is_reported(self, repo, monkeypatch, capsys):
        def boom(*args, **kwargs):
            raise FileNotFoundError("no git")

        monkeypatch.setattr(gw.subprocess, "run", boom)
        with pytest.raises(SystemExit):
            gw.git(str(repo), "status")
        assert "git not found" in capsys.readouterr().err

    def test_a_long_stderr_is_truncated(self, repo, monkeypatch, capsys):
        """Remote-server text has no length it is entitled to."""
        import subprocess as sp

        def fake(*args, **kwargs):
            return sp.CompletedProcess(args=[], returncode=1, stdout="", stderr="x" * 5000)

        monkeypatch.setattr(gw.subprocess, "run", fake)
        with pytest.raises(SystemExit):
            gw.git(str(repo), "status", check=True)
        err = capsys.readouterr().err
        assert "truncated" in err
        assert len(err) < gw.MAX_ERR_LEN + 200

    def test_a_hung_git_call_is_reported(self, repo, monkeypatch, capsys):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(gw.subprocess, "run", boom)
        with pytest.raises(SystemExit):
            gw.git(str(repo), "status")
        assert "timed out" in capsys.readouterr().err


class TestPushPlan:
    def test_not_a_repo(self, plain_dir):
        assert gw.push_plan(str(plain_dir))["action"] == "stop-not-a-repo"

    def test_detached_head(self, repo):
        sha = git(repo, "rev-parse", "HEAD").stdout.strip()
        git(repo, "checkout", "-q", sha)
        assert gw.push_plan(str(repo))["action"] == "stop-detached-head"

    def test_no_remote(self, repo):
        assert gw.push_plan(str(repo))["action"] == "stop-no-remote"

    def test_no_upstream_with_a_single_remote_picks_it(self, repo, make_bare):
        git(repo, "remote", "add", "only", str(make_bare("b")))
        plan = gw.push_plan(str(repo))
        assert plan["action"] == "no-upstream"
        assert plan["remote"] == "only"

    def test_no_upstream_with_several_remotes_and_no_origin_defers(self, repo, make_bare):
        for name in ("alpha", "beta"):
            git(repo, "remote", "add", name, str(make_bare(name)))
        plan = gw.push_plan(str(repo))
        assert plan["action"] == "no-upstream"
        assert plan["remote"] is None  # the caller must ask

    def test_a_bidi_remote_name_is_flagged_on_the_no_upstream_path(self, repo, make_bare):
        """The user approves a push destination by reading these names."""
        git(repo, "remote", "add", "ev\u202eil", str(make_bare("x")))
        plan = gw.push_plan(str(repo))
        assert plan["action"] == "no-upstream"
        assert plan["suspicious_characters"] is True

    def test_an_ordinary_remote_name_is_not_flagged(self, repo, make_bare):
        git(repo, "remote", "add", "origin", str(make_bare("y")))
        assert gw.push_plan(str(repo))["suspicious_characters"] is False

    def test_no_upstream_carries_each_remotes_url(self, repo, make_bare):
        """So the caller never has to shell out for the destination."""
        bare = make_bare("origin")
        git(repo, "remote", "add", "origin", str(bare))
        plan = gw.push_plan(str(repo))
        assert plan["remote_urls"]["origin"] == str(bare)
        assert set(plan["remote_urls"]) == set(plan["remotes"])

    def test_the_push_url_wins_over_the_fetch_url(self, repo, make_bare):
        """git allows them to differ; the user must see where the push goes."""
        fetch_bare, push_bare = make_bare("fetchside"), make_bare("pushside")
        git(repo, "remote", "add", "origin", str(fetch_bare))
        git(repo, "config", "remote.origin.pushurl", str(push_bare))
        urls = gw.push_plan(str(repo))["remote_urls"]
        assert urls["origin"] == str(push_bare)
        assert urls["origin"] != str(fetch_bare)

    def test_an_ext_remote_cannot_execute_a_command(self, repo, tmp_path):
        """`ext::` remotes run a command named in repo-local config."""
        marker = tmp_path / "pwned"
        git(repo, "remote", "add", "evil", f'ext::sh -c "touch {marker}"')
        gw.git(str(repo), "ls-remote", "evil")
        assert not marker.exists()

    def test_no_upstream_prefers_origin_among_several_remotes(self, repo, make_bare):
        for name in ("alpha", "origin", "beta"):
            git(repo, "remote", "add", name, str(make_bare(name)))
        plan = gw.push_plan(str(repo))
        assert plan["action"] == "no-upstream"
        assert plan["remote"] == "origin"

    def test_an_upstream_plan_names_where_a_push_would_land(self, remote_pair):
        """A remote's nickname says nothing about the destination."""
        work, bare = remote_pair
        assert gw.push_plan(str(work))["remote_url"] == str(bare)

    def test_an_upstream_plan_prefers_the_push_url(self, remote_pair, make_bare):
        """git lets pushurl differ from url; the push follows pushurl."""
        work, _ = remote_pair
        elsewhere = make_bare("elsewhere")
        git(work, "config", "remote.origin.pushurl", str(elsewhere))
        assert gw.push_plan(str(work))["remote_url"] == str(elsewhere)

    def test_up_to_date(self, remote_pair):
        work, _ = remote_pair
        assert gw.push_plan(str(work))["action"] == "stop-up-to-date"

    def test_fast_forward(self, remote_pair):
        work, _ = remote_pair
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "local")
        plan = gw.push_plan(str(work))
        assert plan["action"] == "fast-forward"
        assert (plan["ahead"], plan["behind"]) == (1, 0)

    def test_behind_only(self, remote_pair, clone_of, tmp_path):
        work, bare = remote_pair
        other = clone_of(bare, tmp_path / "other")
        (other / "f.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs")
        git(other, "push", "-q")
        plan = gw.push_plan(str(work))
        assert plan["action"] == "stop-behind-only"
        assert (plan["ahead"], plan["behind"]) == (0, 1)

    def test_an_unreachable_remote_stops_before_comparing(self, remote_pair):
        """A failed fetch means the comparison would use stale data."""
        work, _ = remote_pair
        git(work, "remote", "set-url", "origin", "/no/such/repo.git")
        plan = gw.push_plan(str(work))
        assert plan["action"] == "stop-fetch-failed"
        assert plan["error"]

    def test_an_unreadable_comparison_stops_rather_than_guessing(self, remote_pair, monkeypatch):
        """If ahead/behind cannot be read, no push decision is safe."""
        work, _ = remote_pair
        real = gw.git

        def fake(repo, *args, **kwargs):
            if args[:1] == ("rev-list",) and "--left-right" in args:
                return (0, "garbage-with-no-tab", "")
            return real(repo, *args, **kwargs)

        monkeypatch.setattr(gw, "git", fake)
        assert gw.push_plan(str(work))["action"] == "stop-compare-failed"

    def test_a_bidi_commit_subject_is_flagged_on_a_diverged_branch(
        self, remote_pair, clone_of, tmp_path
    ):
        """This list is read to approve destroying commits."""
        work, bare = remote_pair
        other = clone_of(bare, tmp_path / "other")
        (other / "t.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs \u202e reversed")
        git(other, "push", "-q")
        (work / "m.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "mine")
        plan = gw.push_plan(str(work))
        assert plan["action"] == "diverged"
        assert plan["suspicious_characters"] is True

    def test_an_ordinary_diverged_branch_is_not_flagged(self, remote_pair, clone_of, tmp_path):
        work, bare = remote_pair
        other = clone_of(bare, tmp_path / "other")
        (other / "t.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs")
        git(other, "push", "-q")
        (work / "m.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "mine")
        assert gw.push_plan(str(work))["suspicious_characters"] is False

    def test_diverged_lists_what_a_force_would_drop(self, remote_pair, clone_of, tmp_path):
        work, bare = remote_pair
        other = clone_of(bare, tmp_path / "other")
        (other / "theirs.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs")
        git(other, "push", "-q")
        (work / "mine.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "mine")
        plan = gw.push_plan(str(work))
        assert plan["action"] == "diverged"
        assert (plan["ahead"], plan["behind"]) == (1, 1)
        assert len(plan["would_drop"]) == 1
        assert len(plan["would_add"]) == 1


# ── push execution ──────────────────────────────────────────────────────────
class TestPush:
    def test_fast_forward_pushes(self, remote_pair, run_script):
        work, bare = remote_pair
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "local")
        out = run_script("gitwork.py", "--dir", str(work), "push")
        assert out.returncode == 0, out.stderr
        data = json.loads(out.stdout)
        assert data["pushed"] is True and data["forced"] is False
        assert remote_head(bare) == git(work, "rev-parse", "HEAD").stdout.strip()

    def test_does_not_widen_to_other_branches(self, remote_pair, run_script):
        """push.default=matching would make a bare `git push` push every branch."""
        work, bare = remote_pair
        git(work, "config", "push.default", "matching")
        git(work, "branch", "sidecar")
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "local")
        assert run_script("gitwork.py", "--dir", str(work), "push").returncode == 0
        remote_branches = subprocess.run(
            ["git", "-C", str(bare), "branch", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
        ).stdout.split()
        assert remote_branches == ["main"]

    def test_refuses_to_force_a_branch_that_is_merely_behind(
        self, remote_pair, clone_of, run_script, tmp_path
    ):
        """A force here would delete remote commits and add none."""
        work, bare = remote_pair
        other = clone_of(bare, tmp_path / "other")
        (other / "f.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs")
        git(other, "push", "-q")
        before = remote_head(bare)
        out = run_script("gitwork.py", "--dir", str(work), "push", "--confirm-force")
        assert out.returncode == 3
        assert remote_head(bare) == before

    def test_refuses_a_diverged_push_without_confirmation(
        self, remote_pair, clone_of, run_script, tmp_path
    ):
        work, bare = remote_pair
        other = clone_of(bare, tmp_path / "other")
        (other / "t.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs")
        git(other, "push", "-q")
        (work / "m.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "mine")
        before = remote_head(bare)
        out = run_script("gitwork.py", "--dir", str(work), "push")
        assert out.returncode == 4
        assert json.loads(out.stdout)["pushed"] is False
        assert remote_head(bare) == before

    def test_forces_a_diverged_push_only_with_confirmation(
        self, remote_pair, clone_of, run_script, tmp_path
    ):
        work, bare = remote_pair
        other = clone_of(bare, tmp_path / "other")
        (other / "t.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs")
        git(other, "push", "-q")
        (work / "m.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "mine")
        approved_sha = gw.push_plan(str(work))["upstream_sha"]
        out = run_script(
            "gitwork.py",
            "--dir",
            str(work),
            "push",
            "--confirm-force",
            "--expect-remote",
            approved_sha,
        )
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["forced"] is True
        assert remote_head(bare) == git(work, "rev-parse", "HEAD").stdout.strip()

    def test_a_stale_lease_aborts_a_force_push(self, remote_pair, clone_of, run_script, tmp_path):
        """--force-with-lease, not --force: a remote that moved after the plan

        was computed must abort the push rather than be overwritten.
        """
        work, bare = remote_pair
        other = clone_of(bare, tmp_path / "other")
        (other / "t.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs")
        git(other, "push", "-q")
        (work / "m.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "mine")
        approved_sha = gw.push_plan(str(work))["upstream_sha"]  # what the user saw

        # A third party pushes again: the lease is stale before we act on it.
        (other / "t2.txt").write_text("z\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs-again")
        git(other, "push", "-q")
        newest = remote_head(bare)

        out = run_script(
            "gitwork.py",
            "--dir",
            str(work),
            "push",
            "--confirm-force",
            "--expect-remote",
            approved_sha,
        )
        assert out.returncode != 0
        assert remote_head(bare) == newest  # their second commit survived

    def test_force_without_the_approved_sha_is_refused(
        self, remote_pair, clone_of, run_script, tmp_path
    ):
        """A bare lease would be computed after this command's own fetch."""
        work, bare = remote_pair
        other = clone_of(bare, tmp_path / "other")
        (other / "t.txt").write_text("x\n", encoding="utf-8")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "theirs")
        git(other, "push", "-q")
        (work / "m.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "mine")
        before = remote_head(bare)
        out = run_script("gitwork.py", "--dir", str(work), "push", "--confirm-force")
        assert out.returncode == 6
        assert json.loads(out.stdout)["error"] == "missing-expect-remote"
        assert remote_head(bare) == before

    def test_an_up_to_date_branch_is_a_success_not_an_error(self, remote_pair, run_script):
        """stop-up-to-date is the one stop that is not a failure."""
        work, _ = remote_pair
        out = run_script("gitwork.py", "--dir", str(work), "push")
        assert out.returncode == 0
        assert json.loads(out.stdout)["pushed"] is False

    def test_an_explicit_remote_choice_is_honoured(self, repo, run_script, make_bare, facts_file):
        """The disambiguation success leg: several remotes plus --remote."""
        bares = {name: make_bare(name) for name in ("alpha", "beta")}
        for name, bare in bares.items():
            git(repo, "remote", "add", name, str(bare))
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "push",
            "--remote",
            "beta",
            "--facts",
            str(facts_file),
        )
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["pushed"] is True
        assert remote_head(bares["beta"]) == git(repo, "rev-parse", "HEAD").stdout.strip()
        assert json.loads(facts_file.read_text())["commit"]["push"]["remote"] == "beta"

    def test_ambiguous_remote_keeps_the_json_contract(self, repo, run_script, make_bare):
        for name in ("alpha", "beta"):
            git(repo, "remote", "add", name, str(make_bare(name)))
        out = run_script("gitwork.py", "--dir", str(repo), "push")
        assert out.returncode == 5
        assert json.loads(out.stdout)["error"] == "ambiguous-remote"

    def test_unknown_remote_is_rejected(self, repo, run_script, make_bare):
        git(repo, "remote", "add", "only", str(make_bare("b")))
        out = run_script("gitwork.py", "--dir", str(repo), "push", "--remote", "nope")
        assert out.returncode == 5
        assert json.loads(out.stdout)["error"] == "unknown-remote"

    def test_first_push_sets_upstream(self, repo, run_script, make_bare, facts_file):
        git(repo, "remote", "add", "only", str(make_bare("b")))
        out = run_script("gitwork.py", "--dir", str(repo), "push", "--facts", str(facts_file))
        assert out.returncode == 0, out.stderr
        assert git(repo, "rev-parse", "--abbrev-ref", "@{u}").stdout.strip() == "only/main"
        # A first push has no merge_ref yet; the recorded branch must still be right.
        push = json.loads(facts_file.read_text())["commit"]["push"]
        assert (push["remote"], push["branch"]) == ("only", "main")

    def test_a_slashed_branch_name_keeps_its_prefix(self, remote_pair, run_script, facts_file):
        """refs/heads/feature/foo is the branch "feature/foo", not "foo"."""
        work, _ = remote_pair
        git(work, "checkout", "-qb", "feature/foo")
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "local")
        git(work, "push", "-q", "-u", "origin", "feature/foo")
        (work / "g.txt").write_text("y\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "more")
        run_script("gitwork.py", "--dir", str(work), "push", "--facts", str(facts_file))
        push = json.loads(facts_file.read_text())["commit"]["push"]
        assert push["branch"] == "feature/foo"

    def test_records_the_push_line_from_verified_state(self, remote_pair, run_script, facts_file):
        work, _ = remote_pair
        (work / "f.txt").write_text("x\n", encoding="utf-8")
        git(work, "add", "-A")
        git(work, "commit", "-qm", "local")
        run_script("gitwork.py", "--dir", str(work), "push", "--facts", str(facts_file))
        push = json.loads(facts_file.read_text())["commit"]["push"]
        assert push["remote"] == "origin"
        assert push["branch"] == "main"
        assert push["sha"] == git(work, "rev-parse", "--short", "HEAD").stdout.strip()


# ── facts ───────────────────────────────────────────────────────────────────
class TestFacts:
    def test_the_choice_is_recorded_even_without_a_repo(self, plain_dir, run_script, facts_file):
        """The choice is the user's answer, not a repository fact."""
        out = run_script(
            "gitwork.py",
            "--dir",
            str(plain_dir),
            "facts",
            "--facts",
            str(facts_file),
            "--choice",
            "not committed",
        )
        assert out.returncode == 0, out.stderr
        facts = json.loads(facts_file.read_text())
        assert facts["commit"]["choice"] == "not committed"
        assert facts["scan"]["git_repo"] is False

    def test_records_the_choice_and_repo_flag(self, repo, run_script, facts_file):
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "facts",
            "--facts",
            str(facts_file),
            "--choice",
            "not committed",
        )
        assert out.returncode == 0, out.stderr
        facts = json.loads(facts_file.read_text())
        assert facts["commit"]["choice"] == "not committed"
        assert facts["scan"]["git_repo"] is True

    def test_rejects_an_invalid_choice(self, repo, run_script, facts_file):
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "facts",
            "--facts",
            str(facts_file),
            "--choice",
            "maybe",
        )
        assert out.returncode == 2

    def test_rejects_a_hash_that_looks_like_an_option(self, repo, run_script, facts_file):
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "facts",
            "--facts",
            str(facts_file),
            "--hash=--output=/tmp/pwn",
        )
        assert out.returncode == 1
        assert "looks like an option" in out.stderr

    def test_rejects_a_hash_that_does_not_resolve(self, repo, run_script, facts_file):
        """--hash is verified, not believed."""
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "facts",
            "--facts",
            str(facts_file),
            "--hash",
            "deadbee",
        )
        assert out.returncode == 1

    def test_rejects_a_hash_whose_commit_touches_other_files(self, repo, run_script, facts_file):
        (repo / "other.txt").write_text("x\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", "mixed")
        sha = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        out = run_script(
            "gitwork.py", "--dir", str(repo), "facts", "--facts", str(facts_file), "--hash", sha
        )
        assert out.returncode == 1
        assert "expected only .gitignore" in out.stderr

    def test_note_appends_without_disturbing_computed_fields(self, repo, run_script, facts_file):
        run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "facts",
            "--facts",
            str(facts_file),
            "--note",
            "one",
            "--note",
            "two",
        )
        facts = json.loads(facts_file.read_text())
        assert facts["notes"] == ["one", "two"]
        assert facts["merge"]["esc_bytes"] == 0

    def test_existing_notes_written_as_a_bare_string_are_preserved(
        self, repo, run_script, tmp_path
    ):
        """A hand-edited facts file is discouraged but must not lose data."""
        facts = tmp_path / "f.json"
        facts.write_text(json.dumps({"notes": "single existing note"}), encoding="utf-8")
        run_script(
            "gitwork.py", "--dir", str(repo), "facts", "--facts", str(facts), "--note", "two"
        )
        assert json.loads(facts.read_text())["notes"] == ["single existing note", "two"]

    def test_diffstat_for_an_untracked_file_counts_a_final_unterminated_line(self, repo):
        """Regression: a file with no trailing newline undercounted by one."""
        (repo / ".gitignore").write_bytes(b"a\nb\nc")  # 3 lines, no trailing \n
        assert gw.diffstat(str(repo), None) == "new file, 3 lines"

    def test_diffstat_for_an_unstaged_modification(self, repo, run_script, tmp_path):
        (repo / ".gitignore").write_text("a/\n", encoding="utf-8")
        run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        (repo / ".gitignore").write_text("a/\nb/\n", encoding="utf-8")
        assert "1 file changed" in gw.diffstat(str(repo), None)

    def test_diffstat_for_a_staged_change_under_an_unborn_head(self, empty_repo):
        """No HEAD to compare against, so the index is the only baseline."""
        (empty_repo / ".gitignore").write_text("a/\nb/\n", encoding="utf-8")
        git(empty_repo, "add", ".gitignore")
        assert "1 file changed" in gw.diffstat(str(empty_repo), None)

    def test_diffstat_for_a_committed_change(self, repo, run_script, tmp_path):
        (repo / ".gitignore").write_text("a/\n", encoding="utf-8")
        run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        sha = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        assert "1 file changed" in gw.diffstat(str(repo), sha)

    def test_diffstat_refuses_a_symlinked_untracked_file(self, repo, tmp_path):
        """The line count must not be derived from whatever the link points at."""
        secret = tmp_path / "secret"
        secret.write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
        (repo / ".gitignore").symlink_to(secret)
        assert gw.diffstat(str(repo), None) == ""

    def test_diffstat_is_empty_for_a_clean_repo(self, repo):
        assert gw.diffstat(str(repo), None) == ""

    def test_dir_is_accepted_after_the_subcommand_too(self, repo, run_script):
        """The docstring promises --dir REPO without saying where it must sit."""
        out = run_script("gitwork.py", "status", "--dir", str(repo))
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["is_repo"] is True

    @pytest.mark.parametrize("cmd", ["push", "facts"])
    def test_other_commands_also_refuse_a_facts_path_aimed_at_the_target(
        self, repo, run_script, cmd
    ):
        write_gitignore(repo)
        out = run_script("gitwork.py", "--dir", str(repo), cmd, "--facts", str(repo / ".gitignore"))
        assert out.returncode == 1
        assert "must not be" in out.stderr

    def test_hash_populates_the_commit_block_and_diffstat(
        self, repo, run_script, tmp_path, facts_file
    ):
        """The --hash success path, end to end."""
        write_gitignore(repo)
        commit = json.loads(
            run_script(
                "gitwork.py",
                "--dir",
                str(repo),
                "commit",
                "--message-file",
                str(msg_file(tmp_path)),
            ).stdout
        )
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "facts",
            "--facts",
            str(facts_file),
            "--hash",
            commit["hash"],
            "--choice",
            "commit only",
        )
        assert out.returncode == 0, out.stderr
        facts = json.loads(facts_file.read_text())
        assert facts["commit"]["hash"] == commit["hash"]
        assert facts["commit"]["subject"] == "chore: update .gitignore"
        assert facts["commit"]["scope"] == ".gitignore only"
        assert "1 file changed" in facts["net"]["diffstat"]

    def test_a_facts_file_that_is_not_an_object_is_rejected(self, repo, run_script, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("[1, 2, 3]", encoding="utf-8")
        out = run_script("gitwork.py", "--dir", str(repo), "facts", "--facts", str(bad))
        assert out.returncode == 1
        assert "must contain a JSON object" in out.stderr
        assert "Traceback" not in out.stderr

    def test_a_bad_facts_path_is_reported_not_traced(self, repo, run_script):
        out = run_script("gitwork.py", "--dir", str(repo), "facts", "--facts", "/no/such/f.json")
        assert out.returncode == 1
        assert "cannot read" in out.stderr
        assert "Traceback" not in out.stderr


# ── argument surface ────────────────────────────────────────────────────────
class TestCli:
    def test_push_plan_runs_as_a_subcommand(self, repo, run_script):
        out = run_script("gitwork.py", "--dir", str(repo), "push-plan")
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["action"] == "stop-no-remote"

    def test_a_missing_directory_is_reported(self, run_script):
        out = run_script("gitwork.py", "--dir", "/no/such/dir", "status")
        assert out.returncode == 1
        assert "directory not found" in out.stderr

    def test_commit_on_a_non_repo_is_refused(self, plain_dir, run_script, tmp_path):
        out = run_script(
            "gitwork.py",
            "--dir",
            str(plain_dir),
            "commit",
            "--message-file",
            str(msg_file(tmp_path)),
        )
        assert out.returncode == 1
        assert "not a git work tree" in out.stderr


def hook(repo, body: str) -> None:
    """Install an executable pre-commit hook.

    A hook is the realistic way the committed bytes end up different from the
    bytes this run verified -- lint-staged, a formatter, an over-eager
    `git add -A`. The gates exist for it, so it is what tests them.
    """
    path = repo / ".git" / "hooks" / "pre-commit"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


class TestCommitFailureLeavesNothingStaged:
    """`add` has already run by the time `commit` can fail. Leaving now would
    strand .gitignore in the index in a state the caller did not create."""

    def test_a_rejecting_hook_unstages_the_file_again(self, repo, run_script, tmp_path):
        write_gitignore(repo)
        hook(repo, "exit 1\n")
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert out.returncode == 1
        assert "commit failed" in out.stderr
        assert "was unstaged again" in out.stderr
        assert git(repo, "diff", "--cached", "--name-only").stdout.split() == []

    def test_the_failure_message_carries_the_hook_output(self, repo, run_script, tmp_path):
        write_gitignore(repo)
        hook(repo, "echo 'policy: no ignores today' >&2\nexit 1\n")
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert out.returncode == 1
        assert "policy: no ignores today" in out.stderr

    def test_nothing_is_committed(self, repo, run_script, tmp_path):
        write_gitignore(repo)
        before = git(repo, "rev-list", "--count", "HEAD").stdout.strip()
        hook(repo, "exit 1\n")
        run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert git(repo, "rev-list", "--count", "HEAD").stdout.strip() == before


class TestCommittedContentIsVerified:
    """The file list is not the content. A hook can commit different bytes under
    the same path, and `.gitignore only` would still be true of that commit."""

    def test_a_hook_that_rewrites_the_file_is_caught(self, repo, run_script, tmp_path):
        write_gitignore(repo, "node_modules/\n")
        hook(repo, "printf 'SOMETHING-ELSE/\\n' > .gitignore\ngit add .gitignore\n")
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert out.returncode == 2, out.stdout
        data = json.loads(out.stdout)
        assert data["verdict"] == "content-mismatch"
        assert data["content_matches"] is False
        assert data["record_choice"] == "not committed"

    def test_it_says_do_not_push_and_offers_an_undo(self, repo, run_script, tmp_path):
        write_gitignore(repo, "node_modules/\n")
        hook(repo, "printf 'SOMETHING-ELSE/\\n' > .gitignore\ngit add .gitignore\n")
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert "Do NOT push" in out.stderr
        assert "Do NOT push" in json.loads(out.stdout)["remedy"]

    def test_an_untouched_commit_reports_a_matching_verdict(self, repo, run_script, tmp_path):
        """The same gate, passing: the ok verdict must mean the check ran."""
        write_gitignore(repo)
        out = run_script(
            "gitwork.py", "--dir", str(repo), "commit", "--message-file", str(msg_file(tmp_path))
        )
        assert out.returncode == 0, out.stderr
        data = json.loads(out.stdout)
        assert data["verdict"] == "ok"
        assert data["content_matches"] is True


class TestCommitRecordsIntoFacts:
    def test_the_untouched_phrase_reaches_the_facts_file(
        self, repo, run_script, tmp_path, facts_file
    ):
        write_gitignore(repo)
        (repo / "other.txt").write_text("x\n", encoding="utf-8")
        out = run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "commit",
            "--message-file",
            str(msg_file(tmp_path)),
            "--facts",
            str(facts_file),
        )
        assert out.returncode == 0, out.stderr
        commit = json.loads(facts_file.read_text(encoding="utf-8"))["commit"]
        assert commit["untouched"] == "1 other file"
        assert commit["scope"]
        assert commit["hash"]

    def test_no_untouched_key_when_the_tree_was_otherwise_clean(
        self, repo, run_script, tmp_path, facts_file
    ):
        write_gitignore(repo)
        run_script(
            "gitwork.py",
            "--dir",
            str(repo),
            "commit",
            "--message-file",
            str(msg_file(tmp_path)),
            "--facts",
            str(facts_file),
        )
        assert "untouched" not in json.loads(facts_file.read_text(encoding="utf-8"))["commit"]


class TestFactsFileIsRead:
    """The facts file is written by these tools, but the path comes from the
    agent -- so being handed the wrong file must be a refusal, not a crash."""

    def test_malformed_json_is_refused(self, repo, run_script, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        out = run_script("gitwork.py", "--dir", str(repo), "facts", "--facts", str(bad))
        assert out.returncode == 1
        assert "cannot read facts file" in out.stderr
        assert "Traceback" not in out.stderr

    def test_undecodable_bytes_are_refused(self, repo, run_script, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_bytes(b'{"a": "\xff\xfe"}')
        out = run_script("gitwork.py", "--dir", str(repo), "facts", "--facts", str(bad))
        assert out.returncode == 1
        assert "cannot read facts file" in out.stderr

    def test_a_json_array_is_refused(self, repo, run_script, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("[]", encoding="utf-8")
        out = run_script("gitwork.py", "--dir", str(repo), "facts", "--facts", str(bad))
        assert out.returncode == 1
        assert "must contain a JSON object" in out.stderr

    def test_a_symlinked_facts_file_is_refused(self, repo, run_script, tmp_path):
        real = tmp_path / "real.json"
        real.write_text("{}", encoding="utf-8")
        link = tmp_path / "link.json"
        link.symlink_to(real)
        out = run_script("gitwork.py", "--dir", str(repo), "facts", "--facts", str(link))
        assert out.returncode == 1
        assert "symlink" in out.stderr


class TestFactsRefusesAnUnverifiedCommit:
    def test_a_hash_whose_content_moved_is_not_recorded(self, repo, run_script, tmp_path):
        """`facts --hash` must not stamp a commit as this run's result when the
        content it recorded is not what this run verified."""
        write_gitignore(repo, "node_modules/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add ignores")
        sha = git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
        write_gitignore(repo, "COMPLETELY-DIFFERENT/\n")  # the worktree moved on

        facts = tmp_path / "f.json"
        facts.write_text(json.dumps({"internal": {"written_sha256": "deadbeef"}}), encoding="utf-8")
        out = run_script(
            "gitwork.py", "--dir", str(repo), "facts", "--facts", str(facts), "--hash", sha
        )
        assert out.returncode == 1
        assert "refusing to record it" in out.stderr


class TestBlobComparison:
    """`blob_matches_worktree` gates both the commit path and the facts path, so
    they cannot drift on what "same content" means."""

    def test_true_when_the_commit_matches_the_worktree(self, repo):
        write_gitignore(repo, "node_modules/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        assert gw.blob_matches_worktree(str(repo), "HEAD") is True

    def test_false_when_the_worktree_moved_on(self, repo):
        write_gitignore(repo, "node_modules/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        write_gitignore(repo, "other/\n")
        assert gw.blob_matches_worktree(str(repo), "HEAD") is False

    def test_true_when_it_cannot_tell(self, repo):
        """No .gitignore in the commit and none on disk: unable to compare is
        not the same as mismatched, and the caller's other checks still run."""
        assert gw.blob_matches_worktree(str(repo), "HEAD") is True

    def test_trailing_newline_differences_are_not_content_differences(self, repo):
        """Compared as object ids, so no encoding or newline handling of ours
        can make two identical blobs look different."""
        write_gitignore(repo, "node_modules/\n")
        git(repo, "add", ".gitignore")
        git(repo, "commit", "-qm", "add")
        write_gitignore(repo, "node_modules/\n")
        assert gw.blob_matches_worktree(str(repo), "HEAD") is True


def test_an_unhandled_push_action_fails_loudly(repo, monkeypatch):
    """Defensive branch: if push_plan ever grows an action `push` does not know,
    it must stop rather than fall off the end and report success."""
    monkeypatch.setattr(
        gw, "push_plan", lambda *a, **k: {"action": "teleport", "permits_push": True}
    )
    args = type(
        "Args",
        (),
        {
            "dir": str(repo),
            "confirm_force": False,
            "expect_remote": None,
            "remote": None,
            "facts": None,
        },
    )()
    with pytest.raises(SystemExit) as excinfo:
        gw.cmd_push(args)
    assert excinfo.value.code == 1


def test_repos_are_isolated(tmp_path):
    """Guard the fixtures themselves: a commit in one must not appear in the other."""
    a = init_repo(tmp_path / "a")
    b = init_repo(tmp_path / "b")
    (a / "only-in-a.txt").write_text("x\n", encoding="utf-8")
    git(a, "add", "-A")
    git(a, "commit", "-qm", "a-only")

    assert not (b / "only-in-a.txt").exists()
    assert git(a, "rev-list", "--count", "HEAD").stdout.strip() == "2"
    assert git(b, "rev-list", "--count", "HEAD").stdout.strip() == "1"
    assert "a-only" not in git(b, "log", "--oneline").stdout
