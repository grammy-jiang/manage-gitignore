"""gitignore.py — response validation, merging, detection, and the write path.

Most tests here pin a defect the review loop actually found; the docstring says
which, so a change that reintroduces one fails with a reason attached.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import time

import pytest

import templates as gi
from conftest import API, api_block, git, init_repo


# ── response validation (fails closed) ──────────────────────────────────────
class TestCheckApiBlock:
    def test_accepts_the_requested_block(self):
        gi.check_api_block(api_block(["git", "node"]), ["git", "node"])

    def test_rejects_non_api_output(self):
        with pytest.raises(SystemExit):
            gi.check_api_block("<html>hello</html>\n", ["git"])

    def test_rejects_a_block_for_different_templates(self):
        """A response must be for exactly the templates requested."""
        with pytest.raises(SystemExit):
            gi.check_api_block(api_block(["node"], header_names=["node", "python"]), ["node"])

    def test_rejects_a_foreign_origin_in_the_header(self):
        """The header URL is the host pin; a redirect cannot substitute a block."""
        text = api_block(["git"]).replace(API, "https://evil.example/gitignore/api")
        with pytest.raises(SystemExit):
            gi.check_api_block(text, ["git"])

    def test_rejects_a_created_by_header_without_the_api_marker(self):
        """Both halves of the header guard, each as the sole failing one."""
        text = "# Created by something else entirely\n*.orig\n"
        with pytest.raises(SystemExit):
            gi.check_api_block(text, ["git"])

    def test_rejects_a_truncated_block(self):
        with pytest.raises(SystemExit):
            gi.check_api_block(api_block(["git"], omit_end=True), ["git"])

    def test_rejects_content_appended_after_the_end_marker(self):
        """A proxy appending "*" then "!keep-me" would otherwise be written verbatim."""
        with pytest.raises(SystemExit):
            gi.check_api_block(api_block(["git"], trailing="*\n!keep-me"), ["git"])

    def test_tolerates_blank_lines_after_the_end_marker(self):
        gi.check_api_block(api_block(["git"], trailing="\n   \n"), ["git"])

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<html>hello</html>\n", "unexpected response (not gitignore API output)"),
            (
                "# Created by https://evil.example/api/git\n",
                "response header names an unexpected URL: https://evil.example/api/git",
            ),
            (
                api_block(["node"], header_names=["node", "python"]),
                "response is for different templates than requested "
                "(requested: node; got: node,python)",
            ),
            (
                api_block(["git"], omit_end=True),
                "response block is truncated (no '# End of' marker)",
            ),
            (
                api_block(["git"], trailing="*\n!keep-me"),
                "response has unexpected trailing data after '# End of': '*'",
            ),
            # Long enough to reach the slice. With a one-character line -- which
            # is what the first version of this test used -- `[:80]` and `[:81]`
            # produce identical output, so the bound went unchecked.
            (
                api_block(["git"], trailing="z" * 100),
                "response has unexpected trailing data after '# End of': " + repr("z" * 80),
            ),
        ],
    )
    def test_each_refusal_says_which_check_failed(self, text, expected, capsys):
        """Survived the --all-functions audit: every one of these messages,
        emptied, and the tests above went on passing.

        They assert that *a* SystemExit happened, which five different refusals
        satisfy equally. The message is the entire difference between "the API
        changed shape" and "something is impersonating the API", and it is the
        only thing a user gets, so it is asserted rather than assumed.
        """
        with pytest.raises(SystemExit):
            gi.check_api_block(text, ["git"] if "node" not in text else ["node"])
        assert capsys.readouterr().err.strip().endswith(expected)


# ── parsing an existing file ────────────────────────────────────────────────
class TestSplitExisting:
    def test_a_hand_written_file_is_entirely_custom(self):
        assert gi.split_existing("*.log\nbuild/\n") == ([], ["*.log", "build/"])

    def test_a_header_with_no_end_marker_is_treated_as_entirely_custom(self):
        """Half a block is not a block: keep every line rather than guess."""
        text = f"# Created by {API}/git\n*.orig\n"
        templates, custom = gi.split_existing(text)
        assert templates == []
        assert custom == [f"# Created by {API}/git", "*.orig"]

    def test_reads_templates_and_keeps_lines_outside_the_block(self):
        text = "# lead\n" + api_block(["git", "node"]) + "\n# mine\nsecret.env\n"
        templates, custom = gi.split_existing(text)
        assert templates == ["git", "node"]
        assert "secret.env" in custom
        assert "# lead" in custom
        assert "node_modules/" not in custom

    def test_git_templates_own_created_by_comment_is_not_a_marker(self):
        """The real `git` template contains "# Created by git for backups".

        Treating it as the block marker would swallow surrounding lines. Only a
        marker carrying the /api/ path counts.
        """
        text = api_block(["git"]) + "\n# mine\nkeep.me\n"
        assert "# Created by git for backups" in text
        templates, custom = gi.split_existing(text)
        assert templates == ["git"]
        assert "keep.me" in custom
        assert "*.orig" not in custom


# ── de-duplication and attribution ──────────────────────────────────────────
class TestDedupCustom:
    api = api_block(["node", "python"])

    def test_drops_a_custom_rule_the_template_already_covers(self):
        kept, removed = gi.dedup_custom(["node_modules/", "mine/"], self.api)
        assert kept == ["mine/"]
        assert [line for line, _ in removed] == ["node_modules/"]

    def test_the_first_section_wins_when_two_cover_the_same_pattern(self):
        """Attribution has to be deterministic when templates overlap."""
        api = (
            "# Created by https://www.toptal.com/developers/gitignore/api/a,b\n"
            "### Alpha ###\nshared/\n\n### Beta ###\nshared/\n\n"
            "# End of https://www.toptal.com/developers/gitignore/api/a,b\n"
        )
        _, removed = gi.dedup_custom(["shared/"], api)
        assert removed[0][1] == "Alpha"

    def test_names_the_template_section_that_covers_a_dropped_rule(self):
        """covered_by must be computed here, not guessed downstream."""
        _, removed = gi.dedup_custom(["__pycache__/"], self.api)
        assert removed[0][1] == "Python"

    def test_drops_a_rule_repeated_within_the_custom_block(self):
        kept, removed = gi.dedup_custom(["mine/", "mine/"], self.api)
        assert kept == ["mine/"]
        assert removed[0][1] == "an earlier custom rule"

    def test_keeps_comments_and_does_not_dedup_them(self):
        kept, _ = gi.dedup_custom(["# note", "# note", "a/"], self.api)
        assert kept.count("# note") == 2

    def test_strips_surrounding_blank_lines(self):
        kept, _ = gi.dedup_custom(["", "  ", "a/", "", ""], self.api)
        assert kept == ["a/"]


class TestNormalizeTemplates:
    def test_a_leading_bom_is_stripped(self):
        """A UTF-8 file saved by many editors starts with one; it is not part
        of the first template name."""
        assert gi.normalize_templates(["\ufeffgit"]) == ["git"]

    def test_rejects_a_name_that_looks_like_an_option(self):
        """argparse would read it as a flag, before any catalogue check."""
        with pytest.raises(SystemExit):
            gi.normalize_templates(["--facts-out=/etc/x"])

    @pytest.mark.parametrize(
        ("argv", "expected"),
        [
            (["Node, python vim", "git"], ["node", "python", "vim", "git"]),
            (["a", "b,a"], ["a", "b"]),
            ([" , ,node, "], ["node"]),
            (["NODE"], ["node"]),
        ],
    )
    def test_splits_on_commas_and_whitespace_lowercased_deduped(self, argv, expected):
        """The docstring promises "space- or comma-separated" within one argument."""
        assert gi.normalize_templates(argv) == expected


# ── repo scanning (the rule table that used to live in prose) ───────────────
class TestRecommend:
    @staticmethod
    def _tree(root, *relpaths: str):
        root.mkdir(parents=True, exist_ok=True)
        for rel in relpaths:
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")
        return root

    @classmethod
    def _names(cls, root):
        return [h["name"] for h in gi.recommend(str(root))]

    def test_marker_file_fires_a_rule_and_is_reported_as_the_reason(self, tmp_path):
        root = self._tree(tmp_path / "a", "package.json")
        assert gi.recommend(str(root)) == [{"name": "node", "reason": "package.json"}]

    def test_glob_fires_a_rule_when_no_marker_file_exists(self, tmp_path):
        hits = gi.recommend(str(self._tree(tmp_path / "b", "src/app.py")))
        assert hits[0]["name"] == "python"
        assert hits[0]["reason"].endswith("app.py")

    def test_requires_must_also_be_present(self, tmp_path):
        assert "django" not in self._names(self._tree(tmp_path / "c", "manage.py"))
        assert "django" in self._names(self._tree(tmp_path / "d", "manage.py", "app/settings.py"))

    @pytest.mark.parametrize(
        ("marker", "expected"),
        [
            ("node_modules", "node"),
            ("app.mjs", "node"),
            ("main.ts", "node"),
            ("pyproject.toml", "python"),
            ("setup.py", "python"),
            ("requirements.txt", "python"),
            ("build.gradle.kts", "gradle"),
        ],
    )
    def test_each_alternative_in_a_rule_can_fire_on_its_own(self, tmp_path, marker, expected):
        """Each-Choice coverage: every OR-branch satisfied by itself."""
        root = tmp_path / f"alt-{marker.replace('.', '_')}"
        assert expected in self._names(self._tree(root, marker))

    def test_gradle_also_suppresses_plain_java(self, tmp_path):
        names = self._names(self._tree(tmp_path / "gr", "build.gradle", "src/Main.java"))
        assert "gradle" in names
        assert "java" not in names

    def test_suppressed_by_wins(self, tmp_path):
        """A Maven project must not also be tagged plain `java`."""
        names = self._names(self._tree(tmp_path / "e", "pom.xml", "src/Main.java"))
        assert "maven" in names
        assert "java" not in names

    def test_java_still_fires_without_a_build_tool(self, tmp_path):
        assert "java" in self._names(self._tree(tmp_path / "f", "src/Main.java"))

    def test_a_marker_directory_fires_a_rule(self, tmp_path):
        """DETECT_RULES markers match directory names too, not just files."""
        root = tmp_path / "dirmarker"
        (root / ".idea").mkdir(parents=True)
        assert "jetbrains+all" in self._names(root)

    def test_git_directory_is_never_scanned(self, tmp_path):
        assert self._names(self._tree(tmp_path / "g", ".git/hooks/package.json")) == []

    @pytest.mark.parametrize(
        ("marker", "expected"),
        [
            ("Cargo.toml", "rust"),
            ("go.mod", "go"),
            ("pom.xml", "maven"),
            ("build.gradle", "gradle"),
            (".idea", "jetbrains+all"),
            ("pubspec.yaml", "flutter"),
        ],
    )
    def test_each_single_marker_rule_fires(self, tmp_path, marker, expected):
        root = self._tree(tmp_path / expected.replace("+", "_"), marker)
        assert expected in self._names(root)

    @pytest.mark.parametrize(
        ("name", "present", "required"),
        [("rails", "Gemfile", "config/routes.rb"), ("unity", "ProjectSettings", "Assets")],
    )
    def test_a_requires_gate_needs_both_halves(self, tmp_path, name, present, required):
        assert name not in self._names(self._tree(tmp_path / f"{name}-half", present))
        assert name in self._names(self._tree(tmp_path / f"{name}-full", present, required))

    def test_a_long_reason_is_truncated_with_an_ellipsis(self, tmp_path):
        deep = "a-very-long-directory-name-that-keeps-going/" * 2 + "package.json"
        hits = gi.recommend(str(self._tree(tmp_path / "long", deep)))
        assert len(hits[0]["reason"]) == gi.REASON_MAX_LEN
        assert hits[0]["reason"].endswith("\u2026")

    @pytest.mark.parametrize("over", [0, 1])
    def test_the_reason_is_truncated_only_past_the_limit(self, tmp_path, over):
        """Boundary: exactly at the cap is untouched, one past it is trimmed."""
        tail = "/package.json"
        pad = gi.REASON_MAX_LEN + over - len(tail)
        deep = ("d" * pad) + tail
        hits = gi.recommend(str(self._tree(tmp_path / f"len{over}", deep)))
        reason = hits[0]["reason"]
        assert len(reason) == gi.REASON_MAX_LEN
        assert reason.endswith("\u2026") is bool(over)

    def test_the_reported_reason_is_stable_across_runs(self, tmp_path):
        """os.walk order is filesystem-dependent; the reason must not be."""
        root = self._tree(tmp_path / "stable", "a.py", "b.py", "c.py")
        assert len({gi.recommend(str(root))[0]["reason"] for _ in range(5)}) == 1

    def test_the_depth_limit_is_a_boundary_not_a_cliff(self, tmp_path):
        """At SCAN_MAX_DEPTH the marker is found; one level deeper it is not."""
        at_limit = "/".join(["d"] * gi.SCAN_MAX_DEPTH + ["package.json"])
        too_deep = "/".join(["d"] * (gi.SCAN_MAX_DEPTH + 1) + ["package.json"])
        assert "node" in self._names(self._tree(tmp_path / "at", at_limit))
        assert "node" not in self._names(self._tree(tmp_path / "past", too_deep))


# ── flagged patterns ────────────────────────────────────────────────────────
class TestClassify:
    """One precedence: always_on > recommended > carried_over > added."""

    def test_always_on_wins_even_when_a_rule_also_recommends_it(self):
        groups = gi.classify(["git"], {"git": "some-marker"}, [])
        assert groups["always_on"] == ["git"]
        assert groups["recommended"] == []

    def test_recommended_beats_carried_over(self):
        groups = gi.classify(["node"], {"node": "package.json"}, ["node"])
        assert [r["name"] for r in groups["recommended"]] == ["node"]
        assert groups["carried_over"] == []

    def test_carried_over_beats_added(self):
        groups = gi.classify(["rust"], {}, ["rust"])
        assert groups["carried_over"] == ["rust"]
        assert groups["added"] == []

    def test_anything_left_is_added(self):
        assert gi.classify(["dotenv"], {}, [])["added"] == ["dotenv"]

    def test_dropped_templates_are_reported_as_removed(self):
        assert gi.classify([], {}, ["java"])["removed"] == ["java"]

    def test_the_categories_partition_the_set(self):
        wanted = ["git", "node", "rust", "dotenv"]
        groups = gi.classify(wanted, {"node": "package.json"}, ["rust"])
        seen = (
            groups["always_on"]
            + [r["name"] for r in groups["recommended"]]
            + groups["carried_over"]
            + groups["added"]
        )
        assert sorted(seen) == sorted(wanted)


class TestDecodeUtf8:
    def test_invalid_utf8_in_an_existing_file_is_reported(self, tmp_path):
        target = tmp_path / ".gitignore"
        target.write_bytes(b"\xff\xfe not utf-8\n")
        with pytest.raises(SystemExit):
            gi.read_text(str(target))

    def test_invalid_utf8_from_the_api_is_reported(self, api):
        (api.dir / "block").write_bytes(b"\xff\xfe\n")
        with pytest.raises(SystemExit):
            gi.fetch_text("git")


class TestRiskyPatterns:
    def test_reports_negations_without_blocking_them(self):
        """Real templates legitimately negate — the vim template has !*.svg.

        Hard-blocking these (the first review's suggestion) would break the skill.
        """
        negations, broad = gi.risky_patterns(api_block(["vim"]))
        assert any(n.startswith("!*.svg") for n in negations)
        assert broad == []

    @pytest.mark.parametrize("pattern", sorted(gi.BROAD_PATTERNS))
    def test_every_broad_pattern_is_reported(self, pattern):
        assert gi.risky_patterns(f"### X ###\n{pattern}\n")[1] == [pattern]

    def test_an_ordinary_pattern_is_not_reported(self):
        """The boundary: specific globs are exactly what a template is for."""
        assert gi.risky_patterns("### X ###\n*.log\n") == ([], [])

    def test_ignores_comments(self):
        assert gi.risky_patterns("# !not-a-rule\n") == ([], [])


# ── post-write verification ─────────────────────────────────────────────────
class TestVerifyWritten:
    @staticmethod
    def _write(tmp_path, text: str) -> str:
        target = tmp_path / ".gitignore"
        target.write_text(text, encoding="utf-8")
        return str(target)

    def test_clean_write_reports_no_problems(self, tmp_path):
        problems, raw = gi.verify_written(self._write(tmp_path, api_block(["git"])), ["git"], [])
        assert problems == []
        assert raw.startswith(b"# Created by")

    def test_detects_a_lost_custom_rule(self, tmp_path):
        problems, _ = gi.verify_written(
            self._write(tmp_path, api_block(["git"])), ["git"], ["mine/"]
        )
        assert any("custom rule lost" in p for p in problems)

    def test_detects_ansi_corruption(self, tmp_path):
        text = api_block(["git"]).replace("*.orig", "*.o\x1b[31mrig")
        problems, _ = gi.verify_written(self._write(tmp_path, text), ["git"], [])
        assert any("ESC" in p for p in problems)

    def test_detects_a_template_set_that_does_not_match(self, tmp_path):
        problems, _ = gi.verify_written(
            self._write(tmp_path, api_block(["git"])), ["git", "node"], []
        )
        assert any("expected" in p for p in problems)

    def test_detects_bidi_or_zero_width_characters(self, tmp_path):
        """A rule can be reordered on screen with no ESC byte anywhere."""
        text = api_block(["git"]).replace("*.orig", "*.o\u202erig")
        problems, _ = gi.verify_written(self._write(tmp_path, text), ["git"], [])
        assert any("bidi/zero-width" in p for p in problems)

    def test_reports_both_ansi_and_bidi_when_both_are_present(self, tmp_path):
        """Independent checks: one problem must not mask the other."""
        text = (
            api_block(["git"]).replace("*.orig", "*.o\x1b[31mrig").replace("*.rej", "*.r\u202eej")
        )
        problems, _ = gi.verify_written(self._write(tmp_path, text), ["git"], [])
        assert any("ESC" in p for p in problems)
        assert any("bidi/zero-width" in p for p in problems)

    def test_detects_a_file_that_is_not_utf8_after_writing(self, tmp_path):
        """Returns a problem list rather than raising, so the caller reports it."""
        target = tmp_path / ".gitignore"
        target.write_bytes(b"\xff\xfe not utf-8\n")
        problems, _ = gi.verify_written(str(target), ["git"], [])
        assert any("not valid UTF-8" in p for p in problems)

    def test_detects_a_missing_block(self, tmp_path):
        problems, _ = gi.verify_written(self._write(tmp_path, "just custom\n"), ["git"], [])
        assert any("no template block" in p for p in problems)


# ── atomic write ────────────────────────────────────────────────────────────
class TestAtomicWrite:
    def test_new_file_is_not_created_owner_only(self, tmp_path):
        """mkstemp creates 0600; without restoring a mode every run narrowed it."""
        target = tmp_path / ".gitignore"
        gi.atomic_write(str(target), b"x\n")
        assert stat.S_IMODE(target.stat().st_mode) != 0o600

    def test_a_new_file_honours_the_umask(self, tmp_path):
        """0666 & ~umask -- the same rule an ordinary tool would follow."""
        target = tmp_path / ".gitignore"
        old = os.umask(0o027)
        try:
            gi.atomic_write(str(target), b"x\n")
        finally:
            os.umask(old)
        assert stat.S_IMODE(target.stat().st_mode) == 0o640

    def test_existing_permissions_are_preserved(self, tmp_path):
        target = tmp_path / ".gitignore"
        target.write_text("old\n", encoding="utf-8")
        os.chmod(target, 0o600)
        gi.atomic_write(str(target), b"new\n")
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert target.read_bytes() == b"new\n"

    def test_no_temp_files_are_left_behind(self, tmp_path):
        gi.atomic_write(str(tmp_path / ".gitignore"), b"x\n")
        assert list(tmp_path.glob(".gitignore.*")) == []

    def test_refuses_a_directory_target(self, tmp_path):
        target = tmp_path / "adir"
        target.mkdir()
        with pytest.raises(SystemExit):
            gi.atomic_write(str(target), b"x\n")

    def test_refuses_a_symlinked_target(self, tmp_path):
        secret = tmp_path / "secret"
        secret.write_text("token\n", encoding="utf-8")
        (tmp_path / ".gitignore").symlink_to(secret)
        with pytest.raises(SystemExit):
            gi.atomic_write(str(tmp_path / ".gitignore"), b"x\n")
        assert secret.read_text() == "token\n"


class TestSymlinkRefusal:
    def test_read_text_refuses_to_follow_a_symlink(self, tmp_path):
        """A symlinked .gitignore would carry its target into a commit."""
        secret = tmp_path / "id_rsa"
        secret.write_text("PRIVATE KEY\n", encoding="utf-8")
        (tmp_path / ".gitignore").symlink_to(secret)
        with pytest.raises(SystemExit):
            gi.read_text(str(tmp_path / ".gitignore"))


# ── a change this run did not make ──────────────────────────────────────────
HAND_WRITTEN = "# hand written\nmy-secret-dir/\n"


@pytest.fixture
def tracked_repo(tmp_path, api):
    """A git repo whose .gitignore is committed and clean."""
    root = init_repo(tmp_path / "tracked")
    (root / "package.json").write_text("{}", encoding="utf-8")
    (root / ".gitignore").write_text(HAND_WRITTEN, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "add a hand-written gitignore")
    api.set_block(api_block(["git", "node"]))
    return root


class TestUncommittedChangeIsCarriedAcross:
    """A run may only commit what that run wrote -- and must give back the rest.

    Defect this pins: with an uncommitted line already in .gitignore, the merge
    absorbed it as a custom rule and the commit shipped it as this run's work.
    The summary counted it among "custom rules kept" and never distinguished it.

    The fix is not a refusal. The rebuild is based on the COMMITTED file, so the
    commit is honestly this run's own; the user's edit is then re-applied on top
    in the work tree, staged and unstaged kept apart exactly as they were found.
    """

    def write(self, run_script, root, facts=None):
        args = ["gitignore.py", "--dir", str(root), "--force"]
        if facts is not None:
            args += ["--facts-out", str(facts)]
        return run_script(*args, "git", "node")

    # ── the rebuild is based on what is committed ───────────────────────────
    def test_an_added_rule_is_not_in_the_committed_version(
        self, tracked_repo, run_script, tmp_path
    ):
        facts = tmp_path / "f.json"
        (tracked_repo / ".gitignore").write_text(HAND_WRITTEN + "notes.txt\n", encoding="utf-8")
        out = self.write(run_script, tracked_repo, facts)
        assert out.returncode == 0, out.stderr
        internal = json.loads(facts.read_text())["internal"]
        assert "notes.txt" not in internal["commit_text"]
        assert "my-secret-dir/" in internal["commit_text"]  # the committed rule survives

    def test_an_added_rule_is_still_in_the_work_tree(self, tracked_repo, run_script):
        """The whole point: the user does not lose their edit."""
        (tracked_repo / ".gitignore").write_text(HAND_WRITTEN + "notes.txt\n", encoding="utf-8")
        assert self.write(run_script, tracked_repo).returncode == 0
        assert "notes.txt" in (tracked_repo / ".gitignore").read_text(encoding="utf-8")

    def test_a_removed_rule_stays_removed_in_the_work_tree(self, tracked_repo, run_script):
        """The case a plain three-way text merge conflicts on."""
        (tracked_repo / ".gitignore").write_text("# hand written\n", encoding="utf-8")
        assert self.write(run_script, tracked_repo).returncode == 0
        text = (tracked_repo / ".gitignore").read_text(encoding="utf-8")
        _, custom = gi.split_existing(text)
        assert "my-secret-dir/" not in [line.strip() for line in custom]

    def test_a_removed_rule_is_still_in_the_committed_version(
        self, tracked_repo, run_script, tmp_path
    ):
        facts = tmp_path / "f.json"
        (tracked_repo / ".gitignore").write_text("# hand written\n", encoding="utf-8")
        self.write(run_script, tracked_repo, facts)
        internal = json.loads(facts.read_text())["internal"]
        assert "my-secret-dir/" in internal["commit_text"]

    def test_the_run_says_it_carried_something_across(self, tracked_repo, run_script):
        """Silence here would let a reader assume the diff is the whole story."""
        (tracked_repo / ".gitignore").write_text(HAND_WRITTEN + "notes.txt\n", encoding="utf-8")
        out = self.write(run_script, tracked_repo)
        assert "Carried across your uncommitted change (modified)" in out.stdout
        assert "kept your added rule: notes.txt" in out.stdout

    def test_a_removal_is_reported_too(self, tracked_repo, run_script):
        (tracked_repo / ".gitignore").write_text("# hand written\n", encoding="utf-8")
        out = self.write(run_script, tracked_repo)
        assert "honoured your removal of: my-secret-dir/" in out.stdout

    # ── staged and unstaged are kept apart ──────────────────────────────────
    def test_a_staged_edit_is_recorded_for_restoring(self, tracked_repo, run_script, tmp_path):
        facts = tmp_path / "f.json"
        (tracked_repo / ".gitignore").write_text(HAND_WRITTEN + "staged.txt\n", encoding="utf-8")
        git(tracked_repo, "add", ".gitignore")
        out = self.write(run_script, tracked_repo, facts)
        assert out.returncode == 0, out.stderr
        internal = json.loads(facts.read_text())["internal"]
        assert internal["pending_state"] == "staged"
        assert "staged.txt" in internal["restore_index"]
        assert "staged.txt" not in internal["commit_text"]

    def test_an_unstaged_edit_records_no_index_to_restore(self, tracked_repo, run_script, tmp_path):
        """Nothing was staged, so the index must be left clean afterwards."""
        facts = tmp_path / "f.json"
        (tracked_repo / ".gitignore").write_text(HAND_WRITTEN + "notes.txt\n", encoding="utf-8")
        self.write(run_script, tracked_repo, facts)
        internal = json.loads(facts.read_text())["internal"]
        assert internal["restore_index"] == ""

    def test_staged_and_unstaged_are_recorded_separately(self, tracked_repo, run_script, tmp_path):
        """Porcelain "MM": three distinct versions, and all three matter."""
        facts = tmp_path / "f.json"
        (tracked_repo / ".gitignore").write_text(HAND_WRITTEN + "one.txt\n", encoding="utf-8")
        git(tracked_repo, "add", ".gitignore")
        (tracked_repo / ".gitignore").write_text(
            HAND_WRITTEN + "one.txt\ntwo.txt\n", encoding="utf-8"
        )
        self.write(run_script, tracked_repo, facts)
        internal = json.loads(facts.read_text())["internal"]
        assert "one.txt" in internal["restore_index"] and "two.txt" not in internal["restore_index"]
        assert (
            "one.txt" in internal["restore_worktree"] and "two.txt" in internal["restore_worktree"]
        )
        assert "one.txt" not in internal["commit_text"]

    def test_the_two_written_versions_really_do_differ(self, tracked_repo, run_script, tmp_path):
        """If these were equal the whole mechanism would be a no-op."""
        facts = tmp_path / "f.json"
        (tracked_repo / ".gitignore").write_text(HAND_WRITTEN + "notes.txt\n", encoding="utf-8")
        self.write(run_script, tracked_repo, facts)
        internal = json.loads(facts.read_text())["internal"]
        assert internal["commit_text"] != internal["restore_worktree"]
        on_disk = (tracked_repo / ".gitignore").read_text(encoding="utf-8")
        assert on_disk == internal["restore_worktree"]
        assert internal["worktree_sha256"] == hashlib.sha256(on_disk.encode()).hexdigest()
        assert (
            internal["written_sha256"]
            == hashlib.sha256(internal["commit_text"].encode()).hexdigest()
        )

    # ── an edit inside the block cannot survive, and says so ────────────────
    def test_an_edit_inside_the_template_block_is_reported(self, tracked_repo, run_script):
        """The block is regenerated wholesale; an edit to it is not carried."""
        first = run_script("gitignore.py", "--dir", str(tracked_repo), "--force", "git", "node")
        assert first.returncode == 0, first.stderr
        git(tracked_repo, "add", ".gitignore")
        git(tracked_repo, "commit", "-qm", "templated")
        text = (tracked_repo / ".gitignore").read_text(encoding="utf-8")
        (tracked_repo / ".gitignore").write_text(
            text.replace("node_modules/", "node_modules/  # mine"), encoding="utf-8"
        )
        out = self.write(run_script, tracked_repo)
        assert out.returncode == 0, out.stderr
        assert "your edit touched the template block itself" in out.stdout

    # ── the cases that must behave exactly as before ────────────────────────
    def test_a_clean_tracked_gitignore_records_nothing_pending(
        self, tracked_repo, run_script, tmp_path
    ):
        facts = tmp_path / "f.json"
        out = self.write(run_script, tracked_repo, facts)
        assert out.returncode == 0, out.stderr
        assert "commit_text" not in json.loads(facts.read_text())["internal"]
        assert "Carried across" not in out.stdout

    def test_an_untracked_gitignore_is_this_runs_own_work(self, tmp_path, api, run_script):
        """No committed version to be confused with, so the whole file is ours."""
        root = init_repo(tmp_path / "untracked-ignore")
        (root / "package.json").write_text("{}", encoding="utf-8")
        (root / ".gitignore").write_text(HAND_WRITTEN, encoding="utf-8")
        api.set_block(api_block(["git", "node"]))
        facts = tmp_path / "f.json"
        out = self.write(run_script, root, facts)
        assert out.returncode == 0, out.stderr
        assert "commit_text" not in json.loads(facts.read_text())["internal"]
        assert "my-secret-dir/" in (root / ".gitignore").read_text(encoding="utf-8")

    def test_a_repo_with_no_gitignore_is_written(self, tmp_path, api, run_script):
        root = init_repo(tmp_path / "no-ignore")
        (root / "package.json").write_text("{}", encoding="utf-8")
        api.set_block(api_block(["git", "node"]))
        assert self.write(run_script, root).returncode == 0

    def test_outside_a_repo_nothing_is_carried(self, cli_repo, run_script, tmp_path):
        """No history, so the file on disk is the only version there is."""
        facts = tmp_path / "f.json"
        (cli_repo / ".gitignore").write_text(HAND_WRITTEN, encoding="utf-8")
        out = self.write(run_script, cli_repo, facts)
        assert out.returncode == 0, out.stderr
        assert "commit_text" not in json.loads(facts.read_text())["internal"]

    def test_a_symlink_is_still_refused(self, tracked_repo, run_script):
        """Ordering: the symlink refusal runs before anything asks git, which
        would otherwise answer about the link's target."""
        secret = tracked_repo / "id_rsa"
        secret.write_text("PRIVATE KEY\n", encoding="utf-8")
        (tracked_repo / ".gitignore").unlink()
        (tracked_repo / ".gitignore").symlink_to(secret)
        out = self.write(run_script, tracked_repo)
        assert out.returncode == gi.EXIT_ERROR
        assert "symlink" in out.stderr


class TestReapplyCustom:
    """The merge itself, without a repository around it."""

    def test_an_addition_lands_on_the_new_result(self):
        result, added, removed = gi.reapply_custom(["a", "b"], ["a", "b"], ["a", "b", "c"])
        assert result == ["a", "b", "c"] and added == ["c"] and removed == []

    def test_a_removal_is_honoured(self):
        result, added, removed = gi.reapply_custom(["a", "b"], ["a", "b"], ["b"])
        assert result == ["b"] and removed == ["a"] and added == []

    def test_removing_something_already_deduplicated_is_not_an_error(self):
        """kept has already lost the line; the user removing it too is agreement."""
        result, _, removed = gi.reapply_custom(["b"], ["a", "b"], ["b"])
        assert result == ["b"] and removed == ["a"]

    def test_an_unchanged_edit_changes_nothing(self):
        result, added, removed = gi.reapply_custom(["a"], ["a", "b"], ["a", "b"])
        assert result == ["a"] and added == [] and removed == []

    def test_a_replacement_is_both_a_removal_and_an_addition(self):
        result, added, removed = gi.reapply_custom(["a", "b"], ["a", "b"], ["a", "z"])
        assert removed == ["b"] and added == ["z"] and result == ["a", "z"]

    def test_the_users_additions_keep_their_order(self):
        result, added, _ = gi.reapply_custom(["a"], ["a"], ["a", "x", "y"])
        assert added == ["x", "y"] and result == ["a", "x", "y"]


class TestSplitRegions:
    def test_a_hand_written_file_is_all_custom(self):
        templates, block, custom = gi.split_regions("a\nb\n")
        assert templates == [] and block == [] and custom == ["a", "b"]

    def test_the_block_is_returned_separately(self):
        text = api_block(["git", "node"]) + "\ncustom/\n"
        templates, block, custom = gi.split_regions(text)
        assert templates == ["git", "node"]
        assert block[0].startswith("# Created by") and block[-1].startswith("# End of")
        assert "custom/" in custom

    def test_split_existing_still_agrees_with_it(self):
        """One parser, two callers -- they must not drift."""
        text = api_block(["git", "node"]) + "\ncustom/\n"
        templates, custom = gi.split_existing(text)
        assert (templates, custom) == (gi.split_regions(text)[0], gi.split_regions(text)[2])


# ── name validation ─────────────────────────────────────────────────────────
class TestValidate:
    def test_known_names_pass(self, api):
        gi.validate(["node", "python"])

    def test_no_templates_at_all_is_refused(self, cli_repo, run_script):
        out = run_script("gitignore.py", "--dir", str(cli_repo))
        assert out.returncode == 1
        assert "no templates given" in out.stderr

    def test_a_templates_file_of_blank_lines_is_refused(self, cli_repo, run_script, tmp_path):
        listing = tmp_path / "t.txt"
        listing.write_text("\n  \n\n", encoding="utf-8")
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--templates-file", str(listing))
        assert out.returncode == 1
        assert "no templates given" in out.stderr

    def test_a_typo_gets_a_close_match_suggestion(self, api, capsys):
        """Substring containment alone finds nothing for "pyhton"."""
        with pytest.raises(SystemExit):
            gi.validate(["pyhton"])
        assert "did you mean" in capsys.readouterr().err

    def test_an_unknown_name_is_rejected(self, api):
        with pytest.raises(SystemExit):
            gi.validate(["definitely-not-a-template"])

    def test_a_substring_of_a_real_name_is_suggested(self, api, capsys):
        """ "jav" is not a template, but "java" and "javascript" contain it."""
        api.set_list(["javascript", "java", "node"])
        with pytest.raises(SystemExit):
            gi.validate(["jav"])
        err = capsys.readouterr().err
        assert "did you mean" in err
        assert "javascript" in err


# ── fetch bounds ────────────────────────────────────────────────────────────
class TestFetchBounds:
    def test_curl_is_invoked_with_its_safety_flags(self, api):
        """The bounds only hold if they actually reach curl's command line."""
        gi.fetch_bytes("node")
        argv = api.invocations()[-1]
        assert "--proto" in argv and "=https" in argv
        assert "-L" not in argv  # redirects are never followed
        # Adjacency matters: the value must belong to the flag it bounds.
        assert argv[argv.index("--max-time") + 1] == str(gi.FETCH_MAX_SECONDS)
        assert argv[argv.index("--max-filesize") + 1] == str(gi.FETCH_MAX_BYTES)

    def test_the_curl_command_line_is_exactly_this(self, api):
        """Survived the --all-functions audit: `'-fsS' -> ''`.

        The test above checks `--proto`, the absence of `-L`, and that the two
        bounds reach the flags they belong to -- but nothing named `-fsS`, so
        deleting it changed no test. `-f` is the load-bearing letter: without
        it curl prints the server's error page as the response body and exits
        0, so a 404 or a captive portal becomes the text this tool goes on to
        treat as a template block.

        Asserted as the whole command rather than flag by flag, which is what
        the audit says actually holds: every element checked individually still
        leaves the ones nobody thought to name.
        """
        gi.fetch_bytes("node")
        argv = api.invocations()[-1]
        # argv[0] is the stub's absolute path, since it is what PATH resolved.
        assert os.path.basename(argv[0]) == "curl"
        assert argv[1:] == [
            "-fsS",
            "--proto",
            "=https",
            "--max-time",
            str(gi.FETCH_MAX_SECONDS),
            "--max-filesize",
            str(gi.FETCH_MAX_BYTES),
            f"{gi.API}/node",
        ]

    def test_a_response_exactly_at_the_cap_is_accepted(self, api, monkeypatch):
        """The cap is a ceiling, not an off-by-one rejection."""
        block = api_block(["git"])
        api.set_block(block)
        monkeypatch.setattr(gi, "FETCH_MAX_BYTES", len(block.encode()))
        assert gi.fetch_bytes("git").decode() == block

    def test_a_missing_curl_is_reported_clearly(self, api, monkeypatch, tmp_path):
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        with pytest.raises(SystemExit):
            gi.fetch_bytes("node")

    def test_a_failing_fetch_is_reported_not_swallowed(self, api):
        api.set_mode("fail")
        with pytest.raises(SystemExit):
            gi.fetch_bytes("node")

    def test_one_byte_over_the_cap_is_rejected(self, api, monkeypatch):
        """A byte-precise boundary, not just an unbounded flood."""
        monkeypatch.setattr(gi, "FETCH_MAX_BYTES", 4096)
        api.set_block("x" * (gi.FETCH_MAX_BYTES + 1))
        with pytest.raises(SystemExit):
            gi.fetch_bytes("git")

    def test_an_unbounded_response_is_cut_off(self, api, monkeypatch):
        """curl's --max-filesize is inert without a Content-Length header."""
        api.set_mode("flood")
        monkeypatch.setattr(gi, "FETCH_MAX_BYTES", 200_000)
        with pytest.raises(SystemExit):
            gi.fetch_bytes("node")

    def test_a_stalled_response_is_abandoned(self, api, monkeypatch):
        """A curl that never returns must not hang the caller forever."""
        api.set_mode("hang", hang_seconds=60)
        monkeypatch.setattr(gi, "FETCH_MAX_SECONDS", 1)
        started = time.monotonic()
        with pytest.raises(SystemExit):
            gi.fetch_bytes("node")
        assert time.monotonic() - started < 30  # far below the stub's 60s sleep

    def test_the_list_endpoint_is_reachable_end_to_end(self, api, run_script):
        out = run_script("gitignore.py", "--list")
        assert out.returncode == 0, out.stderr
        assert "node" in out.stdout.split()
        assert "python" in out.stdout.split()


# ── the CLI, end to end, offline ────────────────────────────────────────────
@pytest.fixture
def cli_repo(plain_dir, api):
    (plain_dir / "package.json").write_text("{}", encoding="utf-8")
    api.set_block(api_block(["git", "node"]))
    return plain_dir


class TestCli:
    def test_write_then_detect_round_trips(self, cli_repo, run_script):
        out = run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        assert out.returncode == 0, out.stderr
        assert "Verified:" in out.stdout
        det = run_script("gitignore.py", "--dir", str(cli_repo), "--detect")
        assert "templates: git,node" in det.stdout

    def test_second_write_without_force_refuses(self, cli_repo, run_script):
        run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        out = run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        assert out.returncode == 1
        assert "--force" in out.stderr

    def test_refresh_preserves_custom_rules(self, cli_repo, run_script):
        run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        target = cli_repo / ".gitignore"
        target.write_text(
            target.read_text() + "\n# mine\nkeep.me\nnode_modules/\n", encoding="utf-8"
        )
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--force", "git", "node")
        assert out.returncode == 0, out.stderr
        assert "keep.me" in target.read_text()
        assert "(covered by Node)" in out.stdout

    def test_report_modes_reject_stray_template_names(self, cli_repo, run_script):
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--detect", "node")
        assert out.returncode == 1
        assert "takes no template names" in out.stderr

    def test_list_and_detect_are_mutually_exclusive(self, cli_repo, run_script):
        assert run_script("gitignore.py", "--list", "--detect").returncode == 2

    def test_missing_dir_fails_before_any_network_call(self, cli_repo, run_script, api):
        api.set_mode("fail")  # a fetch would now produce a different error
        out = run_script("gitignore.py", "--dir", "/no/such/dir", "--force", "node")
        assert out.returncode == 1
        assert "target dir not found" in out.stderr

    def test_recommend_emits_the_proposed_set_as_json(self, cli_repo, run_script):
        data = json.loads(run_script("gitignore.py", "--dir", str(cli_repo), "--recommend").stdout)
        assert data["recommended"] == [{"name": "node", "reason": "package.json"}]
        assert data["always_on"] == list(gi.ALWAYS_ON)
        assert set(gi.ALWAYS_ON).issubset(data["proposed"])

    def test_facts_out_classifies_every_template_by_why_it_is_there(
        self, cli_repo, run_script, api, tmp_path
    ):
        run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        api.set_block(api_block(["git", "node", "dotenv"]))
        facts_path = tmp_path / "facts.json"
        out = run_script(
            "gitignore.py",
            "--dir",
            str(cli_repo),
            "--force",
            "--facts-out",
            str(facts_path),
            "git",
            "node",
            "dotenv",
        )
        assert out.returncode == 0, out.stderr
        facts = json.loads(facts_path.read_text())
        assert facts["templates"]["always_on"] == ["git"]
        assert facts["templates"]["recommended"] == [{"name": "node", "reason": "package.json"}]
        assert facts["templates"]["added"] == ["dotenv"]
        assert facts["templates"]["removed"] == []

    def test_facts_out_records_esc_bytes_and_template_counts(
        self, cli_repo, run_script, api, tmp_path
    ):
        run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        api.set_block(api_block(["git", "node", "dotenv"]))
        facts_path = tmp_path / "facts.json"
        run_script(
            "gitignore.py",
            "--dir",
            str(cli_repo),
            "--force",
            "--facts-out",
            str(facts_path),
            "git",
            "node",
            "dotenv",
        )
        facts = json.loads(facts_path.read_text())
        assert facts["merge"]["esc_bytes"] == 0
        assert facts["net"] == {"prev_count": 2, "new_count": 3}

    def test_facts_records_a_template_carried_over_from_the_previous_file(
        self, cli_repo, run_script, api, tmp_path
    ):
        """`rust` is in the old file and matched by no rule here, so it is carried."""
        api.set_block(api_block(["git", "node", "rust"]))
        run_script("gitignore.py", "--dir", str(cli_repo), "git", "node", "rust")
        facts_path = tmp_path / "facts.json"
        run_script(
            "gitignore.py",
            "--dir",
            str(cli_repo),
            "--force",
            "--facts-out",
            str(facts_path),
            "git",
            "node",
            "rust",
        )
        assert json.loads(facts_path.read_text())["templates"]["carried_over"] == ["rust"]

    @pytest.mark.parametrize("mode", ["--list", "--detect", "--recommend"])
    @pytest.mark.parametrize("flag", ["--facts-out", "--force", "--templates-file", "positional"])
    def test_report_modes_refuse_every_write_only_flag(
        self, cli_repo, run_script, tmp_path, mode, flag
    ):
        """Accepting any of these would look like a write that never happened."""
        extra = {
            "--facts-out": ["--facts-out", str(tmp_path / "f.json")],
            "--force": ["--force"],
            "--templates-file": ["--templates-file", str(tmp_path / "t.txt")],
            "positional": ["node"],
        }[flag]
        out = run_script("gitignore.py", "--dir", str(cli_repo), mode, *extra)
        assert out.returncode == 1, out.stdout
        assert not (tmp_path / "f.json").exists()

    def test_recommend_folds_in_an_existing_file(self, cli_repo, run_script, api):
        """previous/custom_lines/proposed must reflect what is already there."""
        api.set_block(api_block(["git", "rust"]))
        run_script("gitignore.py", "--dir", str(cli_repo), "git", "rust")
        target = cli_repo / ".gitignore"
        target.write_text(target.read_text() + "\n# mine\nmy.rule\n", encoding="utf-8")
        data = json.loads(run_script("gitignore.py", "--dir", str(cli_repo), "--recommend").stdout)
        assert data["previous"] == ["git", "rust"]
        assert data["custom_lines"] == 1
        assert "rust" in data["proposed"]

    def test_recommend_refuses_a_symlinked_gitignore(self, cli_repo, run_script, tmp_path):
        secret = tmp_path / "secret"
        secret.write_text("token\n", encoding="utf-8")
        (cli_repo / ".gitignore").symlink_to(secret)
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--recommend")
        assert out.returncode == 1
        assert "symlink" in out.stderr

    def test_templates_can_come_from_a_file_instead_of_argv(self, cli_repo, run_script, tmp_path):
        """No shell parses these names, so quoting cannot be got wrong."""
        listing = tmp_path / "templates.txt"
        listing.write_text("git\nnode\n", encoding="utf-8")
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--templates-file", str(listing))
        assert out.returncode == 0, out.stderr
        assert "Templates: git,node" in out.stdout

    def test_a_templates_file_and_positional_names_are_mutually_exclusive(
        self, cli_repo, run_script, tmp_path
    ):
        listing = tmp_path / "templates.txt"
        listing.write_text("git\n", encoding="utf-8")
        out = run_script(
            "gitignore.py", "--dir", str(cli_repo), "--templates-file", str(listing), "node"
        )
        assert out.returncode == 1
        assert "mutually exclusive" in out.stderr

    def test_an_existing_gitignore_edited_mid_run_is_not_overwritten(
        self, cli_repo, run_script, api
    ):
        """Its custom rules were read before the edit; writing now would lose them."""
        api.set_block(api_block(["git"]))
        run_script("gitignore.py", "--dir", str(cli_repo), "git")
        target = cli_repo / ".gitignore"
        api.race_creates(target)  # the stub rewrites it during the template fetch
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--force", "git")
        assert out.returncode == 1
        assert "changed while this run was fetching" in out.stderr
        assert target.read_text() == "someone else got here first\n"

    def test_facts_out_may_not_target_the_gitignore_itself(self, cli_repo, run_script):
        target = cli_repo / ".gitignore"
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--facts-out", str(target), "git")
        assert out.returncode == 1
        assert "must not be" in out.stderr

    def test_facts_scan_block_describes_the_prior_state(self, cli_repo, run_script, api, tmp_path):
        api.set_block(api_block(["git"]))
        run_script("gitignore.py", "--dir", str(cli_repo), "git")
        target = cli_repo / ".gitignore"
        target.write_text(target.read_text() + "\n# mine\nmy.rule\n", encoding="utf-8")
        api.set_block(api_block(["git", "node"]))
        facts_path = tmp_path / "facts.json"
        run_script(
            "gitignore.py",
            "--dir",
            str(cli_repo),
            "--force",
            "--facts-out",
            str(facts_path),
            "git",
            "node",
        )
        assert json.loads(facts_path.read_text())["scan"] == {
            "gitignore": "existing",
            "prev_templates_count": 1,
            "custom_lines": 1,
            "detected": ["node (package.json)"],
        }

    def test_facts_scan_reports_no_prior_file(self, cli_repo, run_script, api, tmp_path):
        api.set_block(api_block(["git"]))
        facts_path = tmp_path / "facts.json"
        run_script("gitignore.py", "--dir", str(cli_repo), "--facts-out", str(facts_path), "git")
        scan = json.loads(facts_path.read_text())["scan"]
        assert scan["gitignore"] == "none"
        assert scan["prev_templates_count"] == 0

    def test_count_is_refused_outside_list_mode(self, cli_repo, run_script):
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--detect", "--count")
        assert out.returncode == 1
        assert "--count only applies to --list" in out.stderr

    def test_list_count_reports_a_bare_number(self, cli_repo, run_script):
        out = run_script("gitignore.py", "--list", "--count")
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().isdigit()

    def test_list_count_fails_loudly_rather_than_reporting_zero(self, cli_repo, run_script, api):
        """A failed fetch must not look like an empty catalogue."""
        api.set_mode("fail")
        out = run_script("gitignore.py", "--list", "--count")
        assert out.returncode != 0
        assert out.stdout.strip() != "0"

    def test_recommend_reports_the_carried_over_category(self, cli_repo, run_script, api):
        api.set_block(api_block(["git", "rust"]))
        run_script("gitignore.py", "--dir", str(cli_repo), "git", "rust")
        data = json.loads(run_script("gitignore.py", "--dir", str(cli_repo), "--recommend").stdout)
        assert data["carried_over"] == ["rust"]

    def test_flagged_patterns_reach_both_the_console_and_the_facts(
        self, cli_repo, run_script, api, tmp_path
    ):
        """risky_patterns had no path to the CLI under test."""
        api.set_block(api_block(["vim"]))
        facts_path = tmp_path / "facts.json"
        out = run_script(
            "gitignore.py", "--dir", str(cli_repo), "--facts-out", str(facts_path), "vim"
        )
        assert out.returncode == 0, out.stderr
        assert "Review before committing" in out.stdout
        assert "!*.svg" in out.stdout
        review = json.loads(facts_path.read_text())["review"]
        assert any(n.startswith("!*.svg") for n in review["negations"])

    def test_merge_and_write_facts_describe_a_real_merge(self, cli_repo, run_script, api, tmp_path):
        """The non-verbatim merge path, asserted through the facts it produces."""
        api.set_block(api_block(["node"]))
        run_script("gitignore.py", "--dir", str(cli_repo), "node")
        target = cli_repo / ".gitignore"
        target.write_text(
            target.read_text() + "\n# mine\nkeep.me\nnode_modules/\n", encoding="utf-8"
        )
        facts_path = tmp_path / "facts.json"
        run_script(
            "gitignore.py",
            "--dir",
            str(cli_repo),
            "--force",
            "--facts-out",
            str(facts_path),
            "node",
        )
        facts = json.loads(facts_path.read_text())
        assert facts["merge"]["verbatim"] is False
        assert facts["merge"]["esc_bytes"] == 0
        assert facts["merge"]["custom_kept"] == 1
        assert facts["merge"]["custom_removed"] == [{"line": "node_modules/", "covered_by": "Node"}]
        assert facts["write"] == {
            "path": str(target),
            "mode": "overwrite",
            "reason": "file existed",
        }

    def test_detect_notes_suspicious_characters(self, cli_repo, run_script, api):
        api.set_block(api_block(["git"]))
        run_script("gitignore.py", "--dir", str(cli_repo), "git")
        target = cli_repo / ".gitignore"
        target.write_text(target.read_text() + "\n# mine\nbad\u202erule\n", encoding="utf-8")
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--detect")
        assert "control or text-reordering characters" in out.stdout

    def test_a_gitignore_appearing_mid_run_is_not_clobbered(self, cli_repo, run_script, api):
        """The --force gate is decided before a fetch that takes seconds.

        Another process creating .gitignore in that window must not have its
        file overwritten by a run the user only authorised for an empty repo.
        """
        target = cli_repo / ".gitignore"
        assert not target.exists()
        api.set_block(api_block(["git"]))
        api.race_creates(target)
        out = run_script("gitignore.py", "--dir", str(cli_repo), "git")
        assert out.returncode == 1
        assert "appeared during this run" in out.stderr
        assert target.read_text() == "someone else got here first\n"

    def test_facts_records_a_removed_template(self, cli_repo, run_script, api, tmp_path):
        run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        api.set_block(api_block(["git"]))
        facts_path = tmp_path / "facts.json"
        run_script(
            "gitignore.py",
            "--dir",
            str(cli_repo),
            "--force",
            "--facts-out",
            str(facts_path),
            "git",
        )
        assert json.loads(facts_path.read_text())["templates"]["removed"] == ["node"]

    def test_facts_sha256_matches_the_file_on_disk(self, cli_repo, run_script, tmp_path):
        """gitwork commit re-checks this; a wrong value would block every commit."""
        facts_path = tmp_path / "facts.json"
        run_script(
            "gitignore.py", "--dir", str(cli_repo), "--facts-out", str(facts_path), "git", "node"
        )
        facts = json.loads(facts_path.read_text())
        expected = hashlib.sha256((cli_repo / ".gitignore").read_bytes()).hexdigest()
        assert facts["internal"]["written_sha256"] == expected

    def test_unknown_template_name_stops_before_writing(self, cli_repo, run_script):
        """Its own exit code: the caller branches on status, not on message text."""
        out = run_script("gitignore.py", "--dir", str(cli_repo), "nope-not-real")
        assert out.returncode == gi.EXIT_UNKNOWN_TEMPLATE
        assert out.returncode != gi.EXIT_ERROR
        assert "unknown template" in out.stderr
        assert not (cli_repo / ".gitignore").exists()

    def test_a_trailing_data_response_is_never_written(self, cli_repo, run_script, api):
        api.set_block(api_block(["git", "node"], trailing="*"))
        out = run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        assert out.returncode == 1
        assert not (cli_repo / ".gitignore").exists()

    def test_detect_on_a_symlink_refuses(self, cli_repo, run_script, tmp_path):
        secret = tmp_path / "secret"
        secret.write_text("token\n", encoding="utf-8")
        (cli_repo / ".gitignore").symlink_to(secret)
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--detect")
        assert out.returncode == 1
        assert "symlink" in out.stderr

    def test_detect_reports_nothing_for_a_fresh_repo(self, cli_repo, run_script):
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--detect")
        assert out.returncode == 0
        assert "gitignore: none" in out.stdout

    def test_detect_counts_patterns_not_comments(self, cli_repo, run_script):
        """scan.custom_lines must be comparable with merge.custom_kept."""
        run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        target = cli_repo / ".gitignore"
        target.write_text(target.read_text() + "\n# a comment\nreal.rule\n", encoding="utf-8")
        out = run_script("gitignore.py", "--dir", str(cli_repo), "--detect")
        assert "custom_lines: 1" in out.stdout


def _write_args(repo, templates):
    """The Namespace cmd_write reads, with every flag it consults set."""
    return argparse.Namespace(
        dir=str(repo),
        templates=list(templates),
        templates_file=None,
        force=True,
        facts_out=None,
    )


class TestMissingTargetDirectory:
    """Every entry point checks --dir before anything expensive: a typo should
    cost nothing, and must never be reported as an empty repository."""

    def test_recommend_refuses_a_missing_dir(self, run_script, tmp_path):
        out = run_script("gitignore.py", "--dir", str(tmp_path / "nope"), "--recommend")
        assert out.returncode == 1
        assert "target dir not found" in out.stderr

    def test_detect_refuses_a_missing_dir(self, run_script, tmp_path):
        out = run_script("gitignore.py", "--dir", str(tmp_path / "nope"), "--detect")
        assert out.returncode == 1
        assert "target dir not found" in out.stderr

    def test_write_refuses_a_missing_dir_before_touching_the_network(
        self, run_script, tmp_path, api
    ):
        out = run_script("gitignore.py", "--dir", str(tmp_path / "nope"), "git")
        assert out.returncode == 1
        assert "target dir not found" in out.stderr

    def test_a_file_given_as_dir_is_refused(self, run_script, tmp_path):
        afile = tmp_path / "afile"
        afile.write_text("x\n", encoding="utf-8")
        out = run_script("gitignore.py", "--dir", str(afile), "--detect")
        assert out.returncode == 1


class TestFetchFailures:
    """curl carries its own --max-time, but a curl that hangs without producing
    output would leave the subprocess wait blocking. Driven in-process so the
    budget can be shortened; as a subprocess this would cost
    FETCH_MAX_SECONDS + 10 seconds of real time to prove."""

    def test_a_hanging_download_is_killed_and_reported(self, api, monkeypatch):
        monkeypatch.setattr(gi, "FETCH_MAX_SECONDS", 1)
        api.set_mode("hang", hang_seconds=30)
        start = time.monotonic()
        with pytest.raises(SystemExit) as excinfo:
            gi.fetch_bytes("list")
        assert excinfo.value.code == 1
        assert time.monotonic() - start < 25, "the wait was not bounded"

    def test_a_failing_download_is_reported(self, api):
        api.set_mode("fail")
        with pytest.raises(SystemExit) as excinfo:
            gi.fetch_bytes("list")
        assert excinfo.value.code == 1

    def test_a_curl_that_finishes_writing_but_never_exits_is_killed(self, api, monkeypatch):
        """The other stall, and the one the read deadline cannot catch: stdout
        reaches EOF so the read loop completes, and only the wait is left to
        time out. Without that bound the process would sit here forever.

        FETCH_MAX_SECONDS is set so the deadline has all but elapsed by the time
        the wait begins, leaving it its one-second floor.
        """
        monkeypatch.setattr(gi, "FETCH_MAX_SECONDS", -9)
        api.set_mode("linger", hang_seconds=30)
        start = time.monotonic()
        with pytest.raises(SystemExit) as excinfo:
            gi.fetch_bytes("list")
        assert excinfo.value.code == 1
        assert time.monotonic() - start < 25, "the wait was not bounded"


class TestPostWriteVerification:
    """The last gate before success is claimed: the file is re-read and checked
    against what was intended. Everything downstream trusts that check, so if it
    fails the run must fail rather than report a verified write."""

    def test_a_failed_verification_stops_the_run(self, cli_repo, monkeypatch):
        """The branch exists for a bug no fixture can conjure, so the check is
        forced to fail; its job is to turn that bug into a non-zero exit."""
        monkeypatch.setattr(
            gi, "verify_written", lambda target, want, kept: (["block marker missing"], b"")
        )
        with pytest.raises(SystemExit) as excinfo:
            gi.cmd_write(_write_args(cli_repo, ["git", "node"]), str(cli_repo / ".gitignore"))
        assert excinfo.value.code == 1

    def test_verification_passes_on_a_normal_write(self, cli_repo, run_script):
        out = run_script("gitignore.py", "--dir", str(cli_repo), "git", "node")
        assert out.returncode == 0, out.stderr
        assert "Verified" in out.stdout


class TestGapsFoundByMutationAudit:
    """Lines the suite ran without checking, found by `python3 tests/mutate.py`.

    Each of these pins a mutation that survived: the code was changed to
    something wrong, every test still passed, and nobody would have noticed.
    The docstrings name the mutation so a future audit's report can be read
    against this class rather than rediscovered.
    """

    def test_a_rule_is_attributed_to_the_section_it_sits_under(self):
        """Survived: `startswith("###")` and `endswith("###")` both emptied.

        With either marker emptied, every line reads as a section header, so no
        pattern is ever recorded and nothing is reported as covered. The section
        name is what the user is told when a custom rule of theirs is dropped --
        "covered by Node" -- so getting it wrong is a visible defect, and it was
        entirely unchecked.
        """
        sections = gi.api_pattern_sections(api_block(["node", "python"]))
        assert sections["node_modules/"] == "Node"
        assert sections["__pycache__/"] == "Python"

    def test_a_header_needs_both_markers_and_something_between_them(self):
        """Survived: `startswith("###")`, `endswith("###")` and `len > 6`.

        All three describe the same judgement -- what counts as a section
        header -- and no test distinguished a header from a line that merely
        resembles one. Each half of the test below is a line the old suite let
        the parser classify either way:

        `### not a real header` opens like one and does not close; with the
        `endswith` check gone it would become a header and re-attribute every
        rule after it. `trailing marker ###` closes like one and does not open;
        with `startswith` gone it would stop being recorded as a rule at all.
        `######` is six characters that strip to nothing, and `###X###` is seven
        that strip to `X` -- which is exactly where `> 6` draws the line.
        """
        block = "\n".join(
            [
                f"# Created by {API}/node",
                "### Node ###",
                "node_modules/",
                "### not a real header",
                "trailing marker ###",
                "######",
                "npm-debug.log*",
                f"# End of {API}/node",
            ]
        )
        sections = gi.api_pattern_sections(block)
        assert sections["node_modules/"] == "Node"
        assert sections["trailing marker ###"] == "Node"
        assert sections["npm-debug.log*"] == "Node"

        seven = f"# Created by {API}/node\n###X###\nrule.log\n# End of {API}/node\n"
        assert gi.api_pattern_sections(seven)["rule.log"] == "X"

    def test_a_rule_before_any_section_is_attributed_to_the_template(self):
        """Survived: the `or "template"` fallback emptied.

        A block whose first pattern precedes any `### Name ###` would otherwise
        be reported as covered by "" -- the user is told their rule was dropped,
        and not by what.
        """
        block = f"# Created by {API}/git\n*.orig\n# End of {API}/git\n"
        assert gi.api_pattern_sections(block)["*.orig"] == "template"

        kept, removed = gi.dedup_custom(["*.orig"], block)
        assert kept == []
        assert removed == [("*.orig", "template")]

    def test_a_custom_rule_touching_the_end_marker_is_not_swallowed(self):
        """Survived: `lines[end + 1 :]` widened to `lines[end + 2 :]`.

        Almost every file has a blank line after the block, so dropping one line
        there loses nothing visible -- which is exactly why no test caught it.
        A file whose first custom rule sits immediately after the marker loses
        that rule instead.
        """
        text = f"# Created by {API}/git\n*.orig\n# End of {API}/git\nmine.log\nalso-mine.log\n"
        _, _, custom = gi.split_regions(text)
        assert custom == ["mine.log", "also-mine.log"]

    def test_the_template_list_is_everything_after_the_first_marker(self):
        """Survived: `line.split(MARK, 1)` widened to `split(MARK, 2)`.

        With one `/api/` in the header the two are identical, which is why
        nothing noticed. With two -- a hand-written or hand-edited header, since
        no catalogue name contains a slash -- `maxsplit=1` keeps everything
        after the first marker as the name, and `maxsplit=2` silently truncates
        it at the second. That decides which templates the file is recorded as
        already having, and so what `carried_over` reports back to the user.
        """
        text = f"# Created by {API}/node/api/python\nrule.log\n# End of {API}/node/api/python\n"
        names, _, _ = gi.split_regions(text)
        assert names == ["node/api/python"]

    def test_a_long_rule_list_still_diffs_exactly(self):
        """Survives still: `autojunk=False` flipped to `True`, and kept anyway.

        SequenceMatcher's autojunk heuristic discards elements appearing in more
        than 1% of a sequence once it reaches 200 elements, which is why every
        other fixture here is too small to reach it. Several inputs designed to
        trigger it -- 300 rules, half of them duplicates, blank lines at 50%
        density, a user deleting every duplicate -- produce an identical diff
        either way, so I could not turn the mutation into an observable
        difference and am not going to pretend otherwise.

        `autojunk=False` stays: it is a deliberate "do not guess" on a function
        whose output decides which of somebody's rules are put back. This test
        stays too, for the thing it does check -- that a .gitignore with a few
        hundred rules diffs exactly, which nothing else covered.
        """
        base = [f"rule-{n}.log" for n in range(300)]
        theirs = [*base[:150], "inserted-by-the-user.log", *base[150:]]

        result, added, removed = gi.reapply_custom(list(base), base, theirs)
        assert added == ["inserted-by-the-user.log"]
        assert removed == []
        assert "inserted-by-the-user.log" in result

    def test_a_wrong_template_set_is_reported_with_both_lists(self):
        """Survived: the message text and its `","` separators emptied.

        This is the verification that runs after writing somebody's .gitignore.
        A test asserting only that *a* problem was reported leaves the report
        itself unchecked -- and the report is the entire product of a failed
        verification.
        """
        written = api_block(["node", "vim"])
        problems = gi.verify_bytes(written.encode(), ["git", "python"], [], "the written file")
        assert len(problems) == 1
        # Both lists, both separators, and the words that say which is which.
        # Two templates on each side deliberately: with one, the separator is
        # never used and emptying it changes nothing.
        assert problems[0] == "template block lists node,vim, expected git,python"

    def test_an_option_shaped_template_name_is_refused_by_name(self, capsys):
        """Survived: the `"template name"` label emptied.

        `refuse_option_like` builds its message from that label, so an emptied
        one leaves "refusing  that looks like an option" -- and the person who
        has to fix it is not told what kind of value was wrong. Asserting only
        that it exits leaves the entire message unchecked.
        """
        with pytest.raises(SystemExit):
            gi.normalize_templates(["--facts-out=/etc/passwd"])
        assert "template name" in capsys.readouterr().err
