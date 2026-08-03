"""gitignore.py — response validation, merging, detection, and the write path.

Most tests here pin a defect the review loop actually found; the docstring says
which, so a change that reintroduces one fails with a reason attached.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time

import pytest

import templates as gi
from conftest import API, api_block


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
