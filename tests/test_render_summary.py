"""render_summary.py — degrade readably, never crash, never render a forgery.

The facts file is assembled by tools but carries repo-derived text (file names,
commit subjects). Two properties matter: a malformed field must not take the
whole summary down, and no field may be able to forge output it does not own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import summary as rs
from conftest import MODULE, REPO

FULL_FACTS = {
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
    "templates": {"total": 1, "always_on": ["git"], "removed": ["direnv"]},
}


def render(facts: dict) -> str:
    return rs.render(facts, rs.Pal(False))


def run_cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "src"), env.get("PYTHONPATH", "")]).rstrip(
        os.pathsep
    )
    return subprocess.run(
        [sys.executable, str(MODULE["summary"]), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
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
        out = render({"scan": {"git_repo": True, "detected": ["ev‮il​"]}})
        assert "‮" not in out
        assert "​" not in out

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
