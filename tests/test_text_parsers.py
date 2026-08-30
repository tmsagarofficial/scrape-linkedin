"""Tests for the display-string regex layer.

Every input here is a verbatim string extracted from a real captured RSC
response, not an invented example, so a passing suite means the parsers work on
what LinkedIn actually sends.
"""

import pytest

from app.linkedin.text_parsers import (
    parse_date_range_string,
    parse_duration_string,
    parse_location_string,
)


class TestParseDurationString:
    def test_employment_type_and_duration(self):
        assert parse_duration_string("Full-time · 4 yrs 1 mo") == {
            "employment_type": "full_time",
            "years": 4,
            "months": 1,
            "total_months": 49,
            "text": "Full-time · 4 yrs 1 mo",
        }

    def test_company_then_employment_type(self):
        result = parse_duration_string("Ravenn · Internship")
        assert result["employment_type"] == "internship"
        assert result["total_months"] is None

    def test_months_only(self):
        result = parse_duration_string("3 mos")
        assert (result["years"], result["months"], result["total_months"]) == (0, 3, 3)

    def test_years_only(self):
        result = parse_duration_string("3 yrs")
        assert (result["years"], result["months"], result["total_months"]) == (3, 0, 36)

    def test_singular_forms(self):
        assert parse_duration_string("1 yr 1 mo")["total_months"] == 13

    @pytest.mark.parametrize("value", [None, "", "   ", "wholly unexpected", 42])
    def test_never_raises_on_bad_input(self, value):
        result = parse_duration_string(value)
        assert result["employment_type"] is None
        assert result["total_months"] is None


class TestParseDateRangeString:
    def test_en_dash_year_only(self):
        """Education ranges use U+2013 and carry no month."""
        result = parse_date_range_string("2018 – 2022")
        assert result["start"] == {"year": 2018, "month": None}
        assert result["end"] == {"year": 2022, "month": None}
        assert result["is_current"] is False

    def test_ascii_hyphen_with_months(self):
        """Experience ranges use U+002D, which the en dash pattern would miss."""
        result = parse_date_range_string("Aug 2022 - Jul 2025 · 3 yrs")
        assert result["start"] == {"year": 2022, "month": 8}
        assert result["end"] == {"year": 2025, "month": 7}
        assert result["duration_months"] == 36
        assert result["is_current"] is False

    def test_present_is_open_ended(self):
        result = parse_date_range_string("Aug 2025 - Present · 1 yr 1 mo")
        assert result["start"] == {"year": 2025, "month": 8}
        assert result["end"] is None
        assert result["is_current"] is True
        assert result["duration_months"] == 13

    def test_multi_year_range(self):
        result = parse_date_range_string("Mar 2019 - May 2022 · 3 yrs 3 mos")
        assert result["start"] == {"year": 2019, "month": 3}
        assert result["end"] == {"year": 2022, "month": 5}
        assert result["duration_months"] == 39

    def test_single_point_in_time(self):
        result = parse_date_range_string("Aug 2020")
        assert result["start"] == {"year": 2020, "month": 8}
        assert result["end"] is None

    def test_hyphen_inside_text_is_not_a_range_split(self):
        """A degree name contains a hyphen but is not a date range."""
        result = parse_date_range_string(
            "Bachelor of Engineering - BE, Electrical, Electronics and "
            "Communications Engineering"
        )
        assert result["start"] is None
        assert result["end"] is None

    @pytest.mark.parametrize("value", [None, "", "   ", "not a date", 0])
    def test_never_raises_on_bad_input(self, value):
        result = parse_date_range_string(value)
        assert result["start"] is None
        assert result["is_current"] is False


class TestParseLocationString:
    def test_location_and_work_mode(self):
        assert parse_location_string("Hyderabad, Telangana, India · On-site") == {
            "location": "Hyderabad, Telangana, India",
            "work_mode": "on_site",
            "text": "Hyderabad, Telangana, India · On-site",
        }

    def test_location_without_work_mode(self):
        result = parse_location_string("Pune, Maharashtra, India")
        assert result["location"] == "Pune, Maharashtra, India"
        assert result["work_mode"] is None

    def test_unknown_trailing_segment_is_kept_not_dropped(self):
        """An employment type in the trailing slot must not be eaten."""
        result = parse_location_string("Ravenn · Internship")
        assert result["work_mode"] is None
        assert result["location"] == "Ravenn · Internship"

    @pytest.mark.parametrize("value", [None, "", "   ", []])
    def test_never_raises_on_bad_input(self, value):
        assert parse_location_string(value)["location"] is None
