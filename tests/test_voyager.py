"""Tests for the Voyager Dash identity parser.

The fixture is shaped like a real Dash response but describes nobody: the point
is the structure, and a real record is personal data that must not be committed.
"""

from __future__ import annotations

import pytest

from app.linkedin.voyager import parse_identity

ROOT = "https://media.licdn.com/dms/image/v2/ABC/"
SEGMENT = "800_800/photo/0/1598359268633?e=1789603200&v=beta&t=sig"


def record(**overrides):
    base = {
        "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
        "firstName": "Jordan",
        "lastName": "Rivera",
        "headline": "Staff Engineer",
        "summary": "Builds distributed systems.",
        "publicIdentifier": "jordan-rivera",
        "multiLocaleFirstName": {"en_US": "Jordan", "es_ES": "Jordán"},
        "multiLocaleHeadline": {"en_US": "Staff Engineer"},
        "multiLocaleSummary": {"en_US": "Builds distributed systems."},
        "primaryLocale": {"language": "en", "country": "US"},
        "location": {"countryCode": "GB"},
        "premium": True,
        "showVerificationBadge": False,
        "websites": [{"category": "COMPANY", "url": "https://example.com/"}],
        "experienceCardUrn": "urn:li:fsd_profileCard:(ACoAA,EXPERIENCE,en_US)",
        "profilePicture": {
            "displayImage": {
                "vectorImage": {
                    "rootUrl": ROOT,
                    "artifacts": [
                        {"width": 100, "fileIdentifyingUrlPathSegment": "100_100/p?e=1789603200"},
                        {"width": 800, "fileIdentifyingUrlPathSegment": SEGMENT},
                    ],
                }
            }
        },
    }
    base.update(overrides)
    return {"data": {}, "included": [base]}


class TestIdentityFields:
    def test_authoritative_name_split(self):
        """LinkedIn supplies first/last; splitting a full name guesses."""
        name = parse_identity(record())["name"]
        assert (name.first, name.last) == ("Jordan", "Rivera")
        assert name.split_inferred is False

    def test_locale_variants_are_kept(self):
        """The brief requires surfacing locale variants where present."""
        out = parse_identity(record())
        assert out["name"].locales == {"en_US": "Jordan", "es_ES": "Jordán"}
        assert out["headline_locales"] == {"en_US": "Staff Engineer"}
        assert out["primary_locale"] == "en_US"

    def test_about_comes_from_summary(self):
        assert parse_identity(record())["about"] == "Builds distributed systems."

    def test_country_code_and_flags(self):
        out = parse_identity(record())
        assert out["country_code"] == "GB"
        assert out["is_premium"] is True
        assert out["is_verified"] is False

    def test_websites(self):
        sites = parse_identity(record())["websites"]
        assert [(w.url, w.category) for w in sites] == [
            ("https://example.com/", "COMPANY")
        ]

    def test_card_urns_are_pointers_not_content(self):
        """Sections are not in this record; only references to them."""
        assert "experienceCardUrn" in parse_identity(record())["_card_urns"]


class TestImages:
    def test_largest_rendition_is_primary(self):
        """A caller wanting a thumbnail can downscale; upscaling is impossible."""
        image = parse_identity(record())["images"]["profile"]
        assert image.width == 800
        assert image.url == ROOT + SEGMENT

    def test_expiry_is_parsed_from_the_signed_url(self):
        image = parse_identity(record())["images"]["profile"]
        assert image.expires_at is not None
        assert image.expires_at.startswith("2026-09-17")

    def test_all_sizes_are_offered(self):
        image = parse_identity(record())["images"]["profile"]
        assert [s["width"] for s in image.sizes] == [100, 800]

    def test_missing_image_is_omitted_not_faked(self):
        out = parse_identity(record(profilePicture=None))
        assert "profile" not in out.get("images", {})

    def test_malformed_expiry_leaves_it_null(self):
        broken = record()
        art = broken["included"][0]["profilePicture"]["displayImage"]["vectorImage"]["artifacts"]
        art[-1]["fileIdentifyingUrlPathSegment"] = "800_800/p?e=not-a-number"
        assert parse_identity(broken)["images"]["profile"].expires_at is None


class TestResilience:
    def test_missing_profile_entity_returns_empty(self):
        assert parse_identity({"included": []}) == {}

    def test_unrelated_payload_returns_empty(self):
        assert parse_identity({"included": [{"$type": "something.Else"}]}) == {}

    @pytest.mark.parametrize("payload", [{}, {"included": None}])
    def test_never_raises_on_bad_input(self, payload):
        assert parse_identity(payload) == {}

    def test_absent_optional_fields_are_simply_absent(self):
        """A field LinkedIn did not send must not appear as null."""
        out = parse_identity(record(websites=None, summary=None, premium=None))
        assert "websites" not in out
        assert "about" not in out
        assert "is_premium" not in out
