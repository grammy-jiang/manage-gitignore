"""render_summary.py — degrade readably, never crash, never render a forgery.

The facts file is assembled by tools but carries repo-derived text (file names,
commit subjects). Two properties matter: a malformed field must not take the
whole summary down, and no field may be able to forge output it does not own.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import summary as rs
from conftest import MODULE, script_command, script_env

FULL_FACTS = {
    "tool": "manage-gitignore",
    "scan": {
        "git_repo": True,
        "gitignore": "existing",
        "prev_templates_count": 11,
        "custom_lines": 2,
        "detected": ["node (package.json)"],
    },
    "templates": {
        "total": 12,
        "always_on": ["git", "vim"],
        "recommended": [{"name": "node", "reason": "package.json"}],
        "carried_over": ["python"],
        "added": ["dotenv"],
        "removed": [],
    },
    "merge": {
        "verbatim": True,
        "esc_bytes": 0,
        "custom_kept": 1,
        "custom_removed": [{"line": "node_modules/", "covered_by": "Node"}],
    },
    "write": {"path": ".gitignore", "mode": "overwrite", "reason": "file existed"},
    "commit": {
        "choice": "commit + push",
        "hash": "6e0a827",
        "subject": "chore: add dotenv",
        "scope": ".gitignore only",
        "untouched": "4 other files",
        "push": {"sha": "6e0a827", "remote": "origin", "branch": "main"},
    },
    "net": {"prev_count": 11, "new_count": 12, "diffstat": "+7 / -3"},
    "notes": ["history was reset before this run"],
}


FULL_FACTS_WITH_REMOVAL = {
    "tool": "manage-gitignore",
    "templates": {"total": 1, "always_on": ["git"], "removed": ["direnv"]},
}


def render(facts: dict) -> str:
    return rs.render(facts, rs.Pal(False))


def run_cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    """summary.py as SKILL.md's last step runs it.

    Built through conftest's helpers rather than by hand, so this path gets the
    same environment discipline and the same coverage instrumentation as every
    other script the suite drives.
    """
    return subprocess.run(
        script_command(MODULE["summary"], args),
        input=stdin,
        capture_output=True,
        text=True,
        env=script_env(),
    )


# ── the happy path ──────────────────────────────────────────────────────────
class TestFullRender:
    def test_every_section_appears(self):
        out = render(FULL_FACTS)
        for header in ("NOTES", "SCAN", "TEMPLATES", "MERGE", "WRITE", "COMMIT", "NET"):
            assert header in out

    def test_reasons_and_attribution_are_shown(self):
        out = render(FULL_FACTS)
        assert "← package.json" in out
        assert "(covered by Node)" in out

    def test_sections_with_no_data_are_skipped(self):
        out = render({"scan": {"git_repo": False}})
        assert "SCAN" in out
        assert "COMMIT" not in out
        assert "TEMPLATES" not in out

    def test_empty_facts_still_renders_a_title(self):
        assert "manage-gitignore" in render({})


# ── the schema is trusted; a wrong *file* is still rejected ─────────────────
class TestRejectsAWrongFile:
    """Shape coercion was removed: templates/gitwork build facts through typed
    dicts, and SKILL.md forbids hand-editing the file. What remains is the guard
    against being handed something that is not a facts file at all."""

    def test_a_json_array_is_rejected(self, tmp_path):
        path = tmp_path / "f.json"
        path.write_text("[]", encoding="utf-8")
        out = run_cli(str(path))
        assert out.returncode == 1
        assert "must be a JSON object" in out.stderr
        assert "Traceback" not in out.stderr

    @pytest.mark.parametrize("body", ["3", '"a string"', "null", "true"])
    def test_every_non_object_json_value_is_rejected(self, tmp_path, body):
        path = tmp_path / "f.json"
        path.write_text(body, encoding="utf-8")
        out = run_cli(str(path))
        assert out.returncode == 1
        assert "must be a JSON object" in out.stderr

    def test_malformed_json_names_the_problem(self, tmp_path):
        path = tmp_path / "f.json"
        path.write_text("{not json", encoding="utf-8")
        out = run_cli(str(path))
        assert out.returncode == 1
        assert "invalid JSON" in out.stderr
        assert "Traceback" not in out.stderr

    def test_undecodable_bytes_are_reported_not_raised(self, tmp_path):
        path = tmp_path / "f.json"
        path.write_bytes(b'{"a": "\xff\xfe"}')
        out = run_cli(str(path))
        assert out.returncode == 1
        assert "Traceback" not in out.stderr

    def test_a_missing_facts_file_is_an_error(self, tmp_path):
        out = run_cli(str(tmp_path / "absent.json"))
        assert out.returncode == 1
        assert "Traceback" not in out.stderr

    def test_a_symlinked_facts_file_is_refused(self, tmp_path):
        """Same no-follow reader as everywhere else: a facts path is
        caller-supplied and could point anywhere."""
        real = tmp_path / "real.json"
        real.write_text("{}", encoding="utf-8")
        link = tmp_path / "link.json"
        link.symlink_to(real)
        out = run_cli(str(link))
        assert out.returncode == 1
        assert "symlink" in out.stderr
        assert "Traceback" not in out.stderr

    def test_a_directory_is_refused(self, tmp_path):
        out = run_cli(str(tmp_path))
        assert out.returncode == 1
        assert "Traceback" not in out.stderr


class TestCliSuccess:
    """The command SKILL.md's last step runs. It is the only place the summary
    is ever produced, so its exit code and stream discipline are the contract."""

    def _facts(self, tmp_path):
        path = tmp_path / "facts.json"
        path.write_text(json.dumps(FULL_FACTS), encoding="utf-8")
        return path

    def test_renders_to_stdout_and_exits_zero(self, tmp_path):
        out = run_cli(str(self._facts(tmp_path)))
        assert out.returncode == 0
        assert out.stderr == ""
        for header in ("SCAN", "TEMPLATES", "MERGE", "WRITE", "COMMIT", "NET"):
            assert header in out.stdout

    def test_color_never_emits_no_escape_bytes(self, tmp_path):
        out = run_cli(str(self._facts(tmp_path)), "--color", "never")
        assert out.returncode == 0
        assert "\x1b" not in out.stdout

    def test_color_always_emits_escape_bytes(self, tmp_path):
        """Not a tty under capture, so `always` must override the tty check."""
        out = run_cli(str(self._facts(tmp_path)), "--color", "always")
        assert out.returncode == 0
        assert "\x1b" in out.stdout

    def test_color_auto_is_plain_when_not_a_tty(self, tmp_path):
        out = run_cli(str(self._facts(tmp_path)), "--color", "auto")
        assert out.returncode == 0
        assert "\x1b" not in out.stdout

    def test_an_unknown_color_choice_is_a_usage_error(self, tmp_path):
        out = run_cli(str(self._facts(tmp_path)), "--color", "chartreuse")
        assert out.returncode == 2

    def test_an_abbreviated_flag_is_not_accepted(self, tmp_path):
        """allow_abbrev=False: `--col` must not silently mean `--color`."""
        out = run_cli(str(self._facts(tmp_path)), "--col", "never")
        assert out.returncode == 2

    def test_no_arguments_is_a_usage_error(self):
        assert run_cli().returncode == 2

    def test_a_document_with_nothing_but_the_marker_still_renders(self, tmp_path):
        """Every section is optional; only the marker is not."""
        path = tmp_path / "facts.json"
        path.write_text(json.dumps({"tool": "manage-gitignore"}), encoding="utf-8")
        out = run_cli(str(path))
        assert out.returncode == 0
        assert "manage-gitignore" in out.stdout

    def test_a_document_this_tool_did_not_write_is_refused(self, tmp_path):
        """This is the closing report of a run. Rendering somebody else's
        document produces a confident summary of work that did not happen
        here -- and it is `{}` that used to be accepted, so the summary was
        empty rather than absent."""
        path = tmp_path / "facts.json"
        path.write_text("{}", encoding="utf-8")
        out = run_cli(str(path))
        assert out.returncode != 0
        assert "not a manage-gitignore facts file" in out.stderr


class TestColorDecision:
    """`use_color` reads the environment, so each rule needs pinning separately;
    a wrong answer here either strips colour from a terminal or writes escape
    bytes into a pipe."""

    def test_never_beats_everything(self, monkeypatch):
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert rs.use_color("never") is False

    def test_always_beats_a_non_tty(self):
        assert rs.use_color("always") is True

    def test_auto_honours_no_color(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        assert rs.use_color("auto") is False

    def test_auto_honours_force_color(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert rs.use_color("auto") is True

    def test_auto_is_on_for_a_real_terminal(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm")
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        assert rs.use_color("auto") is True

    def test_auto_is_off_for_a_dumb_terminal(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setenv("TERM", "dumb")
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
        assert rs.use_color("auto") is False

    def test_auto_is_off_when_piped(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
        assert rs.use_color("auto") is False


class TestSectionVariants:
    """Each of these is a branch the full-facts fixture never takes, and each
    would be a wrong sentence in front of a user rather than a crash."""

    def test_a_section_whose_every_row_is_absent_is_omitted(self):
        """emit_section skips rather than printing a header over nothing.

        Called directly: a caller reaching it with rows that are all None is the
        case worth pinning, and every facts shape that renders happens to filter
        those out earlier.
        """
        lines: list[str] = []
        rs.emit_section(lines, "WRITE", [("path", None), ("mode", None)], rs.Pal(False))
        assert lines == []

    def test_a_section_with_one_present_row_is_emitted(self):
        lines: list[str] = []
        rs.emit_section(lines, "WRITE", [("path", None), ("mode", "created")], rs.Pal(False))
        assert "WRITE" in "\n".join(lines)
        assert "created" in "\n".join(lines)
        assert "path" not in "\n".join(lines)

    def test_a_merged_block_does_not_claim_to_be_verbatim(self):
        out = render({"merge": {"verbatim": False, "custom_kept": 2}})
        assert "merged with custom rules" in out
        assert "verbatim" not in out

    def test_a_verbatim_block_says_so_with_its_esc_count(self):
        out = render({"merge": {"verbatim": True, "esc_bytes": 0}})
        assert "verbatim" in out
        assert "0 ESC" in out

    def test_a_new_file_is_reported_as_created_not_overwritten(self):
        out = render({"write": {"path": ".gitignore", "mode": "create"}})
        assert "created" in out
        assert "overwritten" not in out

    def test_an_overwrite_without_a_reason_still_renders(self):
        out = render({"write": {"path": ".gitignore", "mode": "overwrite"}})
        assert "overwritten" in out

    def test_a_commit_scope_without_untouched_files_says_nothing_extra(self):
        out = render(
            {"commit": {"choice": "commit", "hash": "abc1234", "scope": ".gitignore only"}}
        )
        assert ".gitignore only" in out
        assert "untouched" not in out

    def test_net_renders_a_diffstat_with_no_template_counts(self):
        out = render({"net": {"diffstat": "+7 / -3"}})
        assert "NET" in out
        assert "+7" in out
        assert "templates" not in out.split("NET", 1)[1]

    def test_net_renders_template_counts_with_no_diffstat(self):
        out = render({"net": {"prev_count": 1, "new_count": 2}, "templates": {"added": ["node"]}})
        assert "NET" in out
        assert "1 → 2" in out
        assert "+node" in out

    def test_an_empty_net_section_is_omitted(self):
        assert "NET" not in render({"net": {}})


# ── output cannot be forged ─────────────────────────────────────────────────
class TestSanitization:
    def test_an_escape_byte_cannot_reach_the_output(self):
        out = render({"commit": {"choice": "ok", "subject": "x\x1b[31mred", "hash": "abc"}})
        assert "\x1b" not in out

    def test_a_newline_cannot_forge_an_extra_row(self):
        """A file name really can contain a newline."""
        out = render({"scan": {"git_repo": True, "detected": ["a\nFORGED  line: hacked"]}})
        detected = [ln for ln in out.splitlines() if "FORGED" in ln]
        assert len(detected) == 1
        assert detected[0].lstrip().startswith("detected")

    def test_bidi_and_zero_width_characters_are_stripped(self):
        # Named codepoints, not literals: these characters render as nothing, so
        # writing them into the source would hide the fixture from review and
        # from grep, and the string is identical either way. `chr()` rather than
        # a `\u` escape because it survives every editor and every diff as plain
        # ASCII. `test_no_tracked_file_carries_an_invisible_character` in
        # tests/test_cli.py holds the whole repository to this.
        rlo, zwsp = chr(0x202E), chr(0x200B)  # bidi override, zero-width space
        out = render({"scan": {"git_repo": True, "detected": [f"ev{rlo}il{zwsp}"]}})
        assert rlo not in out
        assert zwsp not in out

    def test_ordinary_unicode_survives(self):
        assert "日本語" in render({"notes": ["日本語のメモ"]})


# ── alignment ───────────────────────────────────────────────────────────────
class TestAlignment:
    def test_multi_line_recommended_lines_up_under_the_value_column(self):
        """Regression: the continuation indent was a hand-counted literal."""
        facts = {
            "templates": {
                "total": 2,
                "recommended": [
                    {"name": "node", "reason": "package.json"},
                    {"name": "python", "reason": "pyproject.toml"},
                ],
            }
        }
        lines = render(facts).splitlines()
        first = next(i for i, line in enumerate(lines) if "recommended" in line)
        col = lines[first].index("node")
        assert lines[first + 1].index("python") == col

    def test_labels_of_different_lengths_share_one_value_column(self):
        lines = render(FULL_FACTS).splitlines()
        always = next(ln for ln in lines if "always-on" in ln)
        added = next(ln for ln in lines if ln.strip().startswith("added"))
        assert always.index("git") == added.index("dotenv")


# ── colour ──────────────────────────────────────────────────────────────────
class TestCommitAndPushRows:
    def test_push_is_composed_from_its_pieces(self):
        out = render(FULL_FACTS)
        assert "6e0a827 → origin/main" in out

    def test_no_push_row_when_nothing_was_committed(self):
        """ "not pushed" under "not committed" reads as a failure, not a non-event."""
        out = render({"commit": {"choice": "not committed"}})
        assert "not pushed" not in out


class TestRequestedActionAndOutcome:
    """Defect this pins: issue #55. A push that failed was rendered as a bare
    `choice  commit only`, which is the outcome standing in for the intent."""

    def test_a_failed_push_shows_what_was_asked_for_beside_what_happened(self):
        out = render(
            {
                "requested_action": "commit + push",
                "commit": {
                    "choice": "commit only",
                    "hash": "726bc13",
                    "subject": "chore: refresh .gitignore",
                    "push": {"status": "attempted", "remote": "origin", "branch": "main"},
                },
                "notes": ["fatal: could not read Username for 'https://github.com'"],
            }
        )
        assert "requested  commit + push" in out
        assert "choice     commit only" in out
        assert "push       push failed — see NOTES" in out

    def test_a_refusal_carries_the_reason_the_plan_gave(self):
        """`not attempted` has the tool's own words for why, so the row explains
        itself instead of pointing at NOTES for something no note may hold."""
        out = render(
            {
                "requested_action": "commit + push",
                "commit": {
                    "choice": "commit only",
                    "hash": "726bc13",
                    "push": {
                        "status": "not attempted",
                        "remote": "origin",
                        "branch": "main",
                        "reason": "branch has diverged; a force would drop 1 remote commit(s)",
                    },
                },
            }
        )
        assert "not pushed — branch has diverged" in out
        assert "see NOTES" not in out

    def test_an_ordinary_run_does_not_repeat_itself(self):
        """When the ask and the outcome agree there is nothing to reconcile, and
        a `requested` row would be a second copy of the row beneath it."""
        out = render({**FULL_FACTS, "requested_action": "commit + push"})
        assert "requested" not in out
        assert render(FULL_FACTS) == out

    def test_a_push_asked_for_and_never_reached_is_still_shown(self):
        """The commit itself was refused, so the outcome is "not committed" --
        which on its own would hide that a push had been wanted at all."""
        out = render(
            {
                "requested_action": "commit + push",
                "commit": {"choice": "not committed"},
                "notes": ["commit refused: .gitignore changed after it was verified"],
            }
        )
        assert "requested  commit + push" in out
        assert "push       not pushed — see NOTES" in out

    def test_delta_is_derived_from_the_template_lists(self):
        """One source: NET must not carry its own copy that can disagree."""
        out = render(FULL_FACTS)
        assert "+dotenv" in out

    def test_removed_templates_say_what_removal_means(self):
        assert "no longer ignored" in render(FULL_FACTS_WITH_REMOVAL)

    def test_the_review_section_surfaces_flagged_patterns(self):
        out = render({"review": {"negations": ["!*.svg"], "broad": ["*"]}})
        assert "REVIEW" in out
        assert "!*.svg" in out
        assert "*" in out


class TestColorApplication:
    def test_colour_is_actually_emitted_when_enabled(self):
        out = rs.render(FULL_FACTS, rs.Pal(True))
        assert "\x1b[" in out

    def test_the_diffstat_numbers_are_coloured(self):
        """The regex previously matched nothing git actually emits."""
        facts = {
            "net": {
                "prev_count": 1,
                "new_count": 1,
                "diffstat": "1 file changed, 7 insertions(+), 3 deletions(-)",
            }
        }
        out = rs.render(facts, rs.Pal(True))
        assert "\x1b[32m7 insertions(+)\x1b[0m" in out
        assert "\x1b[31m3 deletions(-)\x1b[0m" in out

    def test_the_compact_diffstat_form_is_also_coloured(self):
        facts = {"net": {"prev_count": 1, "new_count": 1, "diffstat": "+7 / -3"}}
        out = rs.render(facts, rs.Pal(True))
        assert "\x1b[32m+7\x1b[0m" in out
        assert "\x1b[31m-3\x1b[0m" in out

    def test_a_hyphen_inside_a_word_is_not_treated_as_a_deletion_count(self):
        """The negative lookbehind: "v-3" is a version, not a diff count."""
        facts = {"net": {"prev_count": 1, "new_count": 1, "diffstat": "v-3 unchanged"}}
        assert "\x1b[31m" not in rs.render(facts, rs.Pal(True))


class TestPushRow:
    def test_no_push_key_renders_as_not_pushed(self):
        out = render({"commit": {"choice": "commit only", "hash": "abc1234"}})
        assert "not pushed" in out

    def test_not_pushed_points_at_the_notes_that_explain_it(self):
        out = render({"commit": {"choice": "commit only"}, "notes": ["push skipped: offline"]})
        assert "see NOTES" in out

    def test_not_pushed_alone_has_no_dangling_pointer(self):
        assert "see NOTES" not in render({"commit": {"choice": "commit only"}})


class TestScanSection:
    def test_a_file_with_no_template_block_is_called_hand_written(self):
        out = render(
            {
                "scan": {
                    "git_repo": True,
                    "gitignore": "existing",
                    "prev_templates_count": 0,
                    "custom_lines": 3,
                }
            }
        )
        assert "(hand-written)" in out
        assert "0 templates" not in out


class TestTemplatesHeader:
    def test_the_header_has_no_count_when_total_is_absent(self):
        lines = render({"templates": {"always_on": ["git"]}}).splitlines()
        assert "TEMPLATES" in lines


class TestReviewSection:
    def test_an_empty_review_still_states_its_scope(self):
        """Silence must not read as a clean bill of health for the whole file."""
        out = render({"review": {"negations": [], "broad": []}})
        assert "REVIEW" in out
        assert "custom rules not scanned" in out


class TestColor:
    @pytest.mark.parametrize(
        ("mode", "env", "expected"),
        [
            ("always", {}, True),
            ("never", {}, False),
            ("auto", {"NO_COLOR": "1"}, False),
            ("auto", {"FORCE_COLOR": "1"}, True),
        ],
    )
    def test_mode_and_environment(self, mode, env, expected, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.delenv("FORCE_COLOR", raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert rs.use_color(mode) is expected

    def test_no_color_beats_force_color(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.setenv("FORCE_COLOR", "1")
        assert rs.use_color("auto") is False

    def test_alignment_is_identical_with_and_without_colour(self):
        plain = rs.render(FULL_FACTS, rs.Pal(False)).splitlines()
        colored = rs.render(FULL_FACTS, rs.Pal(True)).splitlines()
        assert len(plain) == len(colored)


# ── the CLI ─────────────────────────────────────────────────────────────────
class TestCli:
    def test_renders_a_facts_file(self, tmp_path):
        path = tmp_path / "f.json"
        path.write_text(json.dumps(FULL_FACTS), encoding="utf-8")
        out = run_cli(str(path), "--color", "never")
        assert out.returncode == 0
        assert "COMMIT" in out.stdout

    def test_a_missing_file_is_reported_not_traced(self):
        out = run_cli("/no/such/facts.json")
        assert out.returncode == 1
        assert "cannot read" in out.stderr
        assert "Traceback" not in out.stderr

    def test_invalid_json_is_reported(self, tmp_path):
        path = tmp_path / "f.json"
        path.write_text("{not json", encoding="utf-8")
        out = run_cli(str(path))
        assert out.returncode == 1
        assert "invalid JSON" in out.stderr

    def test_piped_output_carries_no_ansi(self, tmp_path):
        path = tmp_path / "f.json"
        path.write_text(json.dumps(FULL_FACTS), encoding="utf-8")
        assert "\x1b" not in run_cli(str(path)).stdout


class TestGapsFoundByMutationAudit:
    """Found by `python3 tests/mutate.py --subject summary`, which killed only
    114 of 198 mutations.

    Almost every survivor was a string literal or a dict key. Emptying a key --
    `facts.get("commit")` becoming `facts.get("")` -- makes a whole section
    vanish from the summary, and every test here went on passing, because each
    looked for one row rather than at the document.

    So this looks at the document. A golden comparison is brittle on purpose:
    the summary is the product, its wording and column layout are what the user
    reads, and `references/example-output.md` already promises to be regenerated
    whenever the renderer's wording changes. Something has to fail when it does.
    """

    EXPECTED = """\
manage-gitignore - run summary
==============================

NOTES
  • history was reset before this run

SCAN
  repo        git repository
  .gitignore  existing — 11 templates, 2 custom
  detected    node (package.json)

TEMPLATES — 12 total
  always-on     git, vim
  recommended   node  ← package.json
  carried-over  python
  added         dotenv
  removed       (none)

MERGE
  template block  verbatim — byte-identical to API, no ANSI control bytes (0 ESC)
  custom rules    1 kept, 1 removed
    removed       node_modules/  (covered by Node)

WRITE
  .gitignore  overwritten — replaced existing file, custom rules kept (file existed)

COMMIT
  choice  commit + push
  commit  6e0a827  chore: add dotenv
  scope   .gitignore only  (4 other files untouched)
  push    6e0a827 → origin/main

NET
  templates  11 → 12  +dotenv
  diff       +7 / -3
"""

    def test_the_whole_summary_is_what_it_has_always_been(self):
        assert render(FULL_FACTS) == self.EXPECTED


# A run that refused, on a machine that was not a repository: the branches
# FULL_FACTS never reaches -- absent values, a template set that only shrank,
# a file created rather than overwritten, and nothing committed.
REFUSED_FACTS = {
    "scan": {"git_repo": False, "gitignore": "absent", "detected": []},
    "templates": {
        "total": 1,
        "always_on": ["git"],
        "recommended": [],
        "carried_over": [],
        "added": [],
        "removed": ["python", "dotenv"],
    },
    "merge": {"verbatim": False, "esc_bytes": 3, "custom_kept": 0, "custom_removed": []},
    "review": {"negations": ["!keep.log"], "broad": ["*"]},
    "write": {"path": ".gitignore", "state": "new", "reason": "created"},
    "commit": {"choice": "not committed", "note": "user declined"},
    "net": {"prev_count": 3, "new_count": 1, "diffstat": "+0 / -12"},
}

# The one case where an empty REVIEW section is not silence but a statement.
NOTHING_FLAGGED_FACTS = {"review": {"negations": [], "broad": []}}


class TestGoldenSummariesForTheOtherBranches:
    """The audit's remaining survivors were all in paths one fixture cannot
    reach. Three documents rather than three dozen row assertions."""

    def test_a_refused_run_renders_as_it_always_has(self):
        assert (
            render(REFUSED_FACTS)
            == """\
manage-gitignore - run summary
==============================

SCAN
  repo        not a git repo
  .gitignore  none
  detected    (none)

TEMPLATES — 1 total
  always-on     git
  carried-over  (none)
  added         (none)
  removed       python, dotenv  (no longer ignored)

MERGE
  template block  merged with custom rules
  custom rules    0 kept, 0 removed

REVIEW — in the template block
  un-ignores  !keep.log
  very broad  *  (may ignore more than intended)

WRITE
  .gitignore  created (new file)

COMMIT
  choice  not committed

NET
  templates  3 → 1  -python -dotenv
  diff       +0 / -12
"""
        )

    def test_an_empty_review_says_what_was_checked(self):
        assert (
            render(NOTHING_FLAGGED_FACTS)
            == """\
manage-gitignore - run summary
==============================

REVIEW — in the template block
  flagged  none (custom rules not scanned)
"""
        )

    def test_an_empty_facts_file_still_renders_a_heading(self):
        assert (
            render({})
            == """\
manage-gitignore - run summary
==============================
"""
        )


# Every optional key omitted, so the defaults are what render, and a title of
# its own -- the one thing no other fixture supplies.
DEFAULTS_FACTS = {
    "title": "a run with a title of its own",
    "scan": {"git_repo": True, "gitignore": "something-unrecognised", "detected": ["x"]},
    "templates": {"total": 2, "always_on": ["git"], "added": ["node"]},
    "merge": {"verbatim": True, "custom_removed": [{"line": "*.log"}]},
    "write": {"mode": "overwrite", "reason": "the file was already there"},
    "commit": {},
    "net": {"prev_count": 1, "new_count": 2},
}

# A .gitignore somewhere other than the repository root, which is the only way
# the path in the WRITE row is not simply the default.
ELSEWHERE_FACTS = {"write": {"mode": "new", "path": "packages/api/.gitignore"}}

# Colour is output too: `names` marks added and removed templates differently,
# and with a colourless palette both branches produce identical text.
COLOURED_FACTS = {
    "templates": {
        "total": 3,
        "always_on": ["git"],
        "added": ["node", "vim"],
        "removed": ["python"],
        "carried_over": [],
    }
}


class TestGoldenSummariesForTheDefaults:
    """More of the audit's survivors. Each of these renders a value that the
    other fixtures happen to supply explicitly, so the fallback behind it was
    never exercised."""

    def test_the_fallbacks_render_as_they_always_have(self):
        assert (
            render(DEFAULTS_FACTS)
            == """\
a run with a title of its own
=============================

SCAN
  repo        git repository
  .gitignore  none
  detected    x

TEMPLATES — 2 total
  always-on     git
  carried-over  (none)
  added         node
  removed       (none)

MERGE
  template block  verbatim — byte-identical to API, no ANSI control bytes (0 ESC)
  custom rules    0 kept, 1 removed
    removed       *.log  (covered by template)

WRITE
  .gitignore  overwritten — replaced existing file, custom rules kept (the file was already there)

NET
  templates  1 → 2  +node
"""
        )

    def test_a_gitignore_outside_the_root_is_named_in_full(self):
        assert (
            render(ELSEWHERE_FACTS)
            == """\
manage-gitignore - run summary
==============================

WRITE
  packages/api/.gitignore  created (new file)
"""
        )

    def test_added_and_removed_templates_are_coloured_differently(self):
        """With `Pal(False)` the two branches are the same string, so nothing
        else in this file can tell them apart."""
        assert (
            rs.render(COLOURED_FACTS, rs.Pal(True))
            == "\x1b[1;36mmanage-gitignore - run summary\x1b[0m\n\x1b[36m==============================\x1b[0m\n\n\x1b[1;37mTEMPLATES — 3 total\x1b[0m\n  \x1b[36malways-on   \x1b[0m  git\n  \x1b[36mcarried-over\x1b[0m  \x1b[2m(none)\x1b[0m\n  \x1b[36madded       \x1b[0m  \x1b[32mnode\x1b[0m, \x1b[32mvim\x1b[0m\n  \x1b[36mremoved     \x1b[0m  \x1b[31mpython\x1b[0m\x1b[2m  (no longer ignored)\x1b[0m\n"
        )
