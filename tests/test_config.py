"""Tests for configuration loading.

The README instructs a reader to `cp .env.example .env` and then run the app.
For a long time nothing read that file, so following the documented quickstart
produced a 503 with no explanation. These tests exist so the instruction and the
behaviour cannot drift apart again.
"""

from __future__ import annotations

import os

import pytest

from app.config import _load_dotenv


@pytest.fixture
def env_file(tmp_path):
    def write(content: str):
        path = tmp_path / ".env"
        path.write_text(content)
        return path
    return write


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("LI_AT", "JSESSIONID", "API_KEY", "SOME_KEY"):
        monkeypatch.delenv(name, raising=False)


class TestLoadDotenv:
    def test_values_are_loaded(self, env_file):
        _load_dotenv(env_file("LI_AT=abc123\nJSESSIONID=ajax:9\n"))
        assert os.environ["LI_AT"] == "abc123"
        assert os.environ["JSESSIONID"] == "ajax:9"

    def test_the_real_environment_wins(self, env_file, monkeypatch):
        """A deployment injecting secrets must never be overridden by a file."""
        monkeypatch.setenv("API_KEY", "from-real-env")
        _load_dotenv(env_file("API_KEY=from-dotenv\n"))
        assert os.environ["API_KEY"] == "from-real-env"

    def test_export_prefix_is_accepted(self, env_file):
        """People paste the same lines they use in a shell."""
        _load_dotenv(env_file("export LI_AT=shell-style\n"))
        assert os.environ["LI_AT"] == "shell-style"

    @pytest.mark.parametrize("raw,expected", [
        ('LI_AT="quoted"', "quoted"),
        ("LI_AT='single'", "single"),
        ("LI_AT=bare", "bare"),
        ("LI_AT=  padded  ", "padded"),
    ])
    def test_quoting_and_whitespace(self, env_file, raw, expected):
        _load_dotenv(env_file(raw + "\n"))
        assert os.environ["LI_AT"] == expected

    def test_comments_and_blank_lines_are_skipped(self, env_file):
        _load_dotenv(env_file("# a comment\n\n  \nLI_AT=value\n"))
        assert os.environ["LI_AT"] == "value"

    def test_a_malformed_line_does_not_abort_the_file(self, env_file):
        """One bad line must not cost the reader every value after it."""
        _load_dotenv(env_file("BROKEN_NO_EQUALS\nLI_AT=still-loaded\n"))
        assert os.environ["LI_AT"] == "still-loaded"
        assert "BROKEN_NO_EQUALS" not in os.environ

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        _load_dotenv(tmp_path / "does-not-exist")  # must not raise

    def test_a_value_containing_equals_is_preserved(self, env_file):
        """Base64 secrets routinely end in '='."""
        _load_dotenv(env_file("LI_AT=abc=def==\n"))
        assert os.environ["LI_AT"] == "abc=def=="
