"""_shared.py — the one sanitiser and the one no-follow reader.

Both are single implementations on purpose: three copies of "strip control
characters" would drift, and one of them would be the one that matters.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from manage_gitignore.shared import (
    NotARegularFile,
    SymlinkRefused,
    atomic_write_bytes,
    clean,
    has_suspicious_chars,
    read_bytes_nofollow,
    read_bytes_or_die,
)


class TestClean:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("plain", "plain"),
            ("x\x1b[31mred", "x [31mred"),  # ESC neutralised, not deleted
            ("a\nb", "a b"),  # newline cannot forge a row
            ("a\rb", "a b"),
            ("a\tb", "a b"),
            ("a\x00b", "a b"),
            ("  padded  ", "padded"),
        ],
    )
    def test_control_characters_become_spaces(self, raw, expected):
        assert clean(raw) == expected

    @pytest.mark.parametrize(
        "char",
        [
            "\u202e",  # RIGHT-TO-LEFT OVERRIDE
            "\u200b",  # ZERO WIDTH SPACE
            "\u2066",  # LEFT-TO-RIGHT ISOLATE
            "\ufeff",  # ZERO WIDTH NO-BREAK SPACE / BOM
            "\u2060",  # WORD JOINER
            "\u009b",  # C1 CSI: an escape sequence with no ESC byte in it
            "\u0085",  # C1 NEL
            "\u2028",  # LINE SEPARATOR -- str.splitlines() breaks on it
            "\u2029",  # PARAGRAPH SEPARATOR
        ],
    )
    def test_text_moving_characters_are_neutralised(self, char):
        """These reorder, hide, or split text without being C0 controls."""
        assert char not in clean(f"a{char}b")

    def test_replacement_not_deletion_keeps_words_apart(self):
        """Deleting would silently turn "a\\nb" into "ab" — a different string."""
        assert clean("a\nb") == "a b"

    @pytest.mark.parametrize("value", [42, 3.5, None, True, ["a"], {"k": "v"}])
    def test_non_strings_are_stringified(self, value):
        assert isinstance(clean(value), str)

    def test_ordinary_unicode_is_untouched(self):
        assert clean("日本語 — ok") == "日本語 — ok"


class TestHasSuspiciousChars:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("plain text", False),
            ("line one\nline two", False),  # ordinary whitespace is not suspicious
            ("col\tcol", False),
            ("crlf\r\n", False),
            ("bidi \u202e here", True),
            ("csi \u009b here", True),
            ("zero \u200b width", True),
            ("sep \u2028 here", True),
            ("nul \x00 here", True),
        ],
    )
    def test_only_text_moving_characters_count(self, text, expected):
        assert has_suspicious_chars(text) is expected


class TestReadBytesOrDie:
    """The wrapper both scripts use, so their wording cannot drift."""

    @staticmethod
    def _die(msg):
        raise SystemExit(msg)

    def test_returns_content_for_a_regular_file(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"ok")
        assert read_bytes_or_die(str(path), self._die) == b"ok"

    def test_a_symlink_keeps_its_own_wording(self, tmp_path):
        target = tmp_path / "t"
        target.write_bytes(b"x")
        link = tmp_path / "link"
        link.symlink_to(target)
        with pytest.raises(SystemExit) as excinfo:
            read_bytes_or_die(str(link), self._die)
        assert "symlink" in str(excinfo.value)

    def test_an_ordinary_oserror_gets_the_generic_wording(self, tmp_path):
        """Not a symlink and not a FIFO: a plain unreadable path."""
        missing = tmp_path / "no-such-dir" / "f"
        with pytest.raises(SystemExit) as excinfo:
            read_bytes_or_die(str(missing), self._die)
        assert "cannot read" in str(excinfo.value)


class TestWriteJson:
    def test_a_new_file_honours_the_umask(self, tmp_path):
        from manage_gitignore import shared as _shared

        target = tmp_path / "facts.json"
        old = os.umask(0o027)
        try:
            _shared.write_json(str(target), {"a": 1})
        finally:
            os.umask(old)
        assert stat.S_IMODE(target.stat().st_mode) == 0o640

    def test_an_existing_files_permissions_survive_a_rewrite(self, tmp_path):
        """Several commands update this file in turn; each must not narrow it."""
        from manage_gitignore import shared as _shared

        target = tmp_path / "facts.json"
        _shared.write_json(str(target), {"a": 1})
        os.chmod(target, 0o600)
        _shared.write_json(str(target), {"a": 2})
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert json.loads(target.read_text()) == {"a": 2}

    def test_a_symlinked_target_is_replaced_not_written_through(self, tmp_path):
        """os.replace() puts a regular file where the link was.

        Facts paths have no symlink gate of their own, so this is what keeps a
        --facts pointed at a symlink from clobbering the link's target.
        """
        from manage_gitignore import shared as _shared

        secret = tmp_path / "secret"
        secret.write_text("PRIVATE\n", encoding="utf-8")
        link = tmp_path / "facts.json"
        link.symlink_to(secret)
        _shared.write_json(str(link), {"a": 1})
        assert secret.read_text() == "PRIVATE\n"
        assert not link.is_symlink()
        assert json.loads(link.read_text()) == {"a": 1}

    def test_no_temp_file_is_left_behind(self, tmp_path):
        from manage_gitignore import shared as _shared

        _shared.write_json(str(tmp_path / "facts.json"), {"a": 1})
        assert list(tmp_path.glob(".tmp-*")) == []


class TestAtomicWriteBytes:
    def test_a_crash_mid_write_leaves_no_partial_file(self, tmp_path, monkeypatch):
        """The cleanup branch was asserted only by a comment."""
        from manage_gitignore import shared as _shared

        target = tmp_path / "out"
        target.write_bytes(b"original")

        def boom(*args, **kwargs):
            raise RuntimeError("disk went away")

        monkeypatch.setattr(_shared.os, "replace", boom)
        with pytest.raises(RuntimeError):
            atomic_write_bytes(str(target), b"new")
        assert target.read_bytes() == b"original"
        assert list(tmp_path.glob(".tmp-*")) == []


class TestReadBytesNofollow:
    def test_reads_a_regular_file(self, tmp_path):
        path = tmp_path / "f"
        path.write_bytes(b"hello\n")
        assert read_bytes_nofollow(str(path)) == b"hello\n"

    def test_refuses_a_symlink(self, tmp_path):
        """The whole point: a symlinked .gitignore must not be followed."""
        secret = tmp_path / "secret"
        secret.write_bytes(b"PRIVATE KEY\n")
        link = tmp_path / "link"
        link.symlink_to(secret)
        with pytest.raises(SymlinkRefused):
            read_bytes_nofollow(str(link))

    def test_a_symlink_refusal_is_still_an_oserror(self, tmp_path):
        """Callers that only catch OSError must not miss it."""
        secret = tmp_path / "secret"
        secret.write_bytes(b"x")
        link = tmp_path / "link"
        link.symlink_to(secret)
        with pytest.raises(OSError):
            read_bytes_nofollow(str(link))

    def test_refuses_a_fifo(self, tmp_path):
        """O_NOFOLLOW stops a symlink but not a FIFO: reading one blocks forever.

        A hang here would look like a slow run, not a failure, so it must be
        refused before the read.
        """
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(NotARegularFile):
            read_bytes_nofollow(str(fifo))

    def test_refuses_a_directory(self, tmp_path):
        target = tmp_path / "adir"
        target.mkdir()
        with pytest.raises(NotARegularFile):
            read_bytes_nofollow(str(target))

    def test_a_missing_file_raises_the_ordinary_error(self, tmp_path):
        with pytest.raises(OSError) as excinfo:
            read_bytes_nofollow(str(tmp_path / "nope"))
        assert not isinstance(excinfo.value, SymlinkRefused)

    def test_a_symlink_to_a_directory_is_also_refused(self, tmp_path):
        target = tmp_path / "dir"
        target.mkdir()
        link = tmp_path / "link"
        link.symlink_to(target)
        with pytest.raises(OSError):
            read_bytes_nofollow(str(link))

    def test_no_descriptor_is_leaked_on_success(self, tmp_path):
        """The fd is wrapped in a `with`, so repeated reads cannot exhaust them."""
        path = tmp_path / "f"
        path.write_bytes(b"x")
        for _ in range(200):
            read_bytes_nofollow(str(path))
