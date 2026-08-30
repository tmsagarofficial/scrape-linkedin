"""Tests for profile URL parsing.

This is the 400 branch of the error taxonomy (AGENTS.md §5). A URL that is
well-formed but points at a company or a job must be rejected up front as a
client error, not fetched and then misreported as a 404.
"""

import pytest

from app.linkedin.urls import InvalidProfileURL, public_id_from_url


class TestAccepted:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("https://www.linkedin.com/in/williamhgates/", "williamhgates"),
            ("https://www.linkedin.com/in/williamhgates", "williamhgates"),
            ("http://linkedin.com/in/williamhgates/", "williamhgates"),
            ("www.linkedin.com/in/williamhgates", "williamhgates"),
            ("linkedin.com/in/williamhgates", "williamhgates"),
            ("https://uk.linkedin.com/in/williamhgates/", "williamhgates"),
            ("https://www.linkedin.com/in/williamhgates/?trk=nav", "williamhgates"),
            ("https://www.linkedin.com/in/first-last-123/", "first-last-123"),
            ("williamhgates", "williamhgates"),
        ],
    )
    def test_valid_inputs(self, value, expected):
        assert public_id_from_url(value) == expected

    def test_percent_encoded_slug_is_decoded(self):
        assert public_id_from_url("https://www.linkedin.com/in/jos%C3%A9/") == "josé"

    def test_locale_path_segments_are_tolerated(self):
        assert public_id_from_url(
            "https://www.linkedin.com/in/williamhgates/en"
        ) == "williamhgates"


class TestRejected:
    @pytest.mark.parametrize(
        "value",
        [
            "https://www.linkedin.com/company/microsoft/",
            "https://www.linkedin.com/school/mit/",
            "https://www.linkedin.com/jobs/view/123456/",
            "https://www.linkedin.com/feed/update/urn:li:activity:123/",
            "https://example.com/in/williamhgates/",
            "https://www.linkedin.com/",
            "https://www.linkedin.com/in/",
            "",
            "   ",
            None,
        ],
    )
    def test_invalid_inputs_raise(self, value):
        with pytest.raises(InvalidProfileURL):
            public_id_from_url(value)

    def test_company_url_names_what_it_saw(self):
        """The message should help a caller fix their request."""
        with pytest.raises(InvalidProfileURL, match="company"):
            public_id_from_url("https://www.linkedin.com/company/microsoft/")
