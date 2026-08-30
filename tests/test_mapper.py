"""Tests for the RSC-to-schema mapper.

Experience fixtures use the scrubbed Part1 capture, so the segmentation is
exercised against LinkedIn's real rendering. Edge cases that the single captured
profile does not contain are built as synthetic node runs.

Runs offline with no credentials, per AGENTS.md §8.
"""

from pathlib import Path

import pytest

from app.linkedin.rsc_parser import TextNode, iter_text, parse_flight
from app.normalize.mapper import build_profile
from app.schemas import Provenance

FIXTURE = Path(__file__).parent / "fixtures" / "rsc_below_activity_part1.txt"


def node(text, section, entity_url=None, component_key=None):
    return TextNode(text=text, section=section, entity_url=entity_url,
                    component_key=component_key)


@pytest.fixture(scope="module")
def part1_nodes():
    return list(iter_text(parse_flight(FIXTURE.read_text())))


@pytest.fixture(scope="module")
def mapped(part1_nodes):
    return build_profile("jordan-rivera", part1_nodes, endpoint_used="test")


class TestExperienceSegmentation:
    def test_grouped_employer_yields_one_entry_per_role(self, mapped):
        """Globex has two roles under one employer heading."""
        globex = [e for e in mapped.profile.experience if e.company.name == "Globex"]
        assert [e.title for e in globex] == ["SDE 2", "SDE"]

    def test_employer_level_fields_apply_to_every_role(self, mapped):
        """Type and location are hoisted above the roles and must be inherited."""
        globex = [e for e in mapped.profile.experience if e.company.name == "Globex"]
        assert all(e.employment_type == "full_time" for e in globex)
        assert all(e.location == "Springfield, Ohio, United States" for e in globex)
        assert all(e.work_mode == "on_site" for e in globex)

    def test_single_role_entry(self, mapped):
        role = next(e for e in mapped.profile.experience if e.title == "ML intern")
        assert role.company.name == "Nimbus Labs"
        assert role.employment_type == "internship"

    def test_company_without_employment_type(self, mapped):
        role = next(e for e in mapped.profile.experience if e.title == "Vice President")
        assert role.company.name == "Campus Entrepreneurship Cell"
        assert role.employment_type is None

    def test_current_role_is_flagged_and_has_no_end(self, mapped):
        current = next(e for e in mapped.profile.experience if e.is_current)
        assert current.title == "SDE 2"
        assert current.end is None
        assert current.start.year == 2025 and current.start.month == 8

    def test_durations_are_computed(self, mapped):
        sde = next(e for e in mapped.profile.experience if e.title == "SDE")
        assert sde.duration_months == 36

    def test_roles_are_in_rendered_order(self, mapped):
        titles = [e.title for e in mapped.profile.experience]
        assert titles == ["SDE 2", "SDE", "Vice President", "ML intern"]

    def test_company_id_is_captured(self, mapped):
        globex = next(e for e in mapped.profile.experience if e.company.name == "Globex")
        assert globex.company.public_id == "1000001"


class TestIdentityMerge:
    """Voyager identity is merged over SDUI where it is available."""

    IDENTITY = {
        "name": __import__("app.schemas", fromlist=["Name"]).Name(
            first="Jordan", last="Rivera", full="Jordan Rivera",
            locales={"en_US": "Jordan"}, split_inferred=False,
        ),
        "about": "From the identity record.",
        "primary_locale": "en_US",
        "country_code": "GB",
        "is_premium": True,
    }

    def test_typed_name_wins_over_a_whitespace_split(self):
        nodes = [node("Jordan Rivera", "profile-sticky-header")]
        merged = build_profile(
            "x", nodes, endpoint_used="t", identity=self.IDENTITY
        ).profile
        assert merged.name.split_inferred is False
        assert merged.name.locales == {"en_US": "Jordan"}

    def test_without_identity_the_split_is_marked_inferred(self):
        nodes = [node("Jordan Rivera", "profile-sticky-header")]
        plain = build_profile("x", nodes, endpoint_used="t").profile
        assert plain.name.split_inferred is True
        assert (plain.name.first, plain.name.last) == ("Jordan", "Rivera")

    def test_provenance_records_which_source_supplied_the_name(self):
        nodes = [node("Jordan Rivera", "profile-sticky-header")]
        with_id = build_profile("x", nodes, endpoint_used="t", identity=self.IDENTITY)
        without = build_profile("x", nodes, endpoint_used="t")
        assert with_id.meta.parse_confidence["name.first"] == Provenance.RAW
        assert without.meta.parse_confidence["name.first"] == Provenance.PARSED

    def test_identity_about_fills_a_gap_sdui_left(self):
        merged = build_profile(
            "x", [], endpoint_used="t", identity=self.IDENTITY
        ).profile
        assert merged.about == "From the identity record."

    def test_sdui_about_is_not_overwritten(self):
        """SDUI About is per-paragraph and richer; identity must not clobber it."""
        nodes = [
            node("About", "profile-card-about"),
            node("First paragraph.", "profile-card-about"),
            node("Second paragraph.", "profile-card-about"),
        ]
        merged = build_profile(
            "x", nodes, endpoint_used="t", identity=self.IDENTITY
        ).profile
        assert merged.about.startswith("First paragraph.")
        assert "Second paragraph." in merged.about

    def test_country_code_reaches_the_location(self):
        nodes = [
            node("Jordan Rivera", "profile-sticky-header"),
            node("Engineer", "profile-sticky-header"),
            node("London", "profile-top-card"),
            node("\u00b7", "profile-top-card"),
        ]
        merged = build_profile(
            "x", nodes, endpoint_used="t", identity=self.IDENTITY
        ).profile
        assert merged.location.raw == "London"
        assert merged.location.country_code == "GB"


class TestLinks:
    """Consumers should follow URLs, not reassemble them from ids."""

    def test_profile_url_is_built_from_the_public_id(self, mapped):
        assert mapped.profile.profile_url == (
            "https://www.linkedin.com/in/jordan-rivera/"
        )

    def test_company_url_is_surfaced(self, mapped):
        globex = next(
            e for e in mapped.profile.experience if e.company.name == "Globex"
        )
        assert globex.company.url == "https://www.linkedin.com/company/1000001/"

    def test_school_url_is_surfaced(self, mapped):
        assert mapped.profile.education[0].school_url == (
            "https://www.linkedin.com/school/2000001/"
        )

    def test_durable_id_becomes_a_urn(self):
        response = build_profile(
            "x", [], endpoint_used="t", profile_id="ACoAATEST"
        )
        assert response.profile.profile_id == "ACoAATEST"
        assert response.profile.profile_urn == "urn:li:fsd_profile:ACoAATEST"

    def test_urn_is_null_without_a_durable_id(self):
        response = build_profile("x", [], endpoint_used="t")
        assert response.profile.profile_urn is None


class TestEducation:
    def test_degree_and_field_are_split_on_the_comma(self, mapped):
        """The degree contains its own hyphen and must not be split on it."""
        education = mapped.profile.education[0]
        assert education.degree == "Bachelor of Engineering - BE"
        assert education.field.startswith("Electrical, Electronics")

    def test_year_only_dates_leave_month_null(self, mapped):
        """§6: partial dates are normal and must never be filled in."""
        education = mapped.profile.education[0]
        assert education.start.year == 2018 and education.start.month is None
        assert education.end.year == 2022 and education.end.month is None


class TestSecondProfileShapes:
    """Shapes found only on a second real profile.

    The first profile validated the two experience layouts. A second one
    surfaced three more constructs that silently corrupted output: inline skill
    annotations, per-role locations, and four-line certifications. These are
    regression tests for each.
    """

    def test_skill_annotations_do_not_split_an_employer(self):
        """"Skills for X at Y" renders between roles and carries no entity_url.

        Left in place it splits one employer into several groups, stranding
        every role after the first and dropping them with a warning.
        """
        url = "https://www.linkedin.com/company/89826258/"
        nodes = [
            node("EXAMPLE-ORG", "profile-card-experience", url),
            node("Full-time · 3 yrs 10 mos", "profile-card-experience", url),
            node("Example Lead", "profile-card-experience", url),
            node("May 2024 - Present · 2 yrs 4 mos", "profile-card-experience", url),
            node("Bangalore", "profile-card-experience", url),
            node("Skills for Example Lead at EXAMPLE-ORG", "profile-card-experience"),
            node("Collaborative Problem Solving and +4 skills", "profile-card-experience"),
            node("Example Vice Lead", "profile-card-experience", url),
            node("Aug 2023 - May 2024 · 10 mos", "profile-card-experience", url),
            node("Bengaluru, Karnataka, India", "profile-card-experience", url),
        ]
        response = build_profile("x", nodes, endpoint_used="t")
        experience = response.profile.experience

        assert [e.title for e in experience] == ["Example Lead", "Example Vice Lead"]
        assert all(e.company.name == "EXAMPLE-ORG" for e in experience)
        assert not [w for w in response.meta.warnings if "no recognisable" in w]

    def test_per_role_location_overrides_the_employer_level_one(self):
        url = "https://www.linkedin.com/company/1/"
        nodes = [
            node("Acme", "profile-card-experience", url),
            node("Full-time · 3 yrs", "profile-card-experience", url),
            node("Lead", "profile-card-experience", url),
            node("May 2024 - Present · 1 yr", "profile-card-experience", url),
            node("Bangalore", "profile-card-experience", url),
            node("Engineer", "profile-card-experience", url),
            node("Aug 2023 - May 2024 · 10 mos", "profile-card-experience", url),
            node("Chennai, Tamil Nadu, India", "profile-card-experience", url),
        ]
        experience = build_profile("x", nodes, endpoint_used="t").profile.experience
        assert experience[0].location == "Bangalore"
        assert experience[1].location == "Chennai, Tamil Nadu, India"

    def test_single_word_location_is_not_discarded(self):
        """A location is identified structurally, not by looking for a comma."""
        url = "https://www.linkedin.com/company/1/"
        nodes = [
            node("Acme", "profile-card-experience", url),
            node("Full-time · 2 yrs", "profile-card-experience", url),
            node("Lead", "profile-card-experience", url),
            node("May 2024 - Present · 1 yr", "profile-card-experience", url),
            node("Bangalore", "profile-card-experience", url),
            node("Engineer", "profile-card-experience", url),
            node("Aug 2023 - May 2024 · 10 mos", "profile-card-experience", url),
        ]
        experience = build_profile("x", nodes, endpoint_used="t").profile.experience
        assert experience[0].location == "Bangalore"

    def test_a_following_title_is_not_read_as_a_location(self):
        """The line after a date range is only a location if no role follows."""
        url = "https://www.linkedin.com/company/1/"
        nodes = [
            node("Acme", "profile-card-experience", url),
            node("Full-time · 4 yrs", "profile-card-experience", url),
            node("Chennai, Tamil Nadu, India · On-site", "profile-card-experience", url),
            node("Senior", "profile-card-experience", url),
            node("Aug 2025 - Present · 1 yr", "profile-card-experience", url),
            node("Junior", "profile-card-experience", url),
            node("Aug 2022 - Jul 2025 · 3 yrs", "profile-card-experience", url),
        ]
        experience = build_profile("x", nodes, endpoint_used="t").profile.experience
        assert [e.title for e in experience] == ["Senior", "Junior"]
        assert experience[0].location == "Chennai, Tamil Nadu, India"

    def test_standalone_work_mode_without_a_location(self):
        url = "https://www.linkedin.com/company/1/"
        nodes = [
            node("Associate IC1", "profile-card-experience", url),
            node("Acme Corp · Full-time", "profile-card-experience", url),
            node("Jul 2025 - Present · 1 yr 2 mos", "profile-card-experience", url),
            node("On-site", "profile-card-experience", url),
        ]
        entry = build_profile("x", nodes, endpoint_used="t").profile.experience[0]
        assert entry.company.name == "Acme Corp"
        assert entry.work_mode == "on_site"
        assert entry.location is None

    def test_certification_with_credential_id_and_url(self):
        section = "profile-card-licenses-and-certifications"
        nodes = [
            node("Palo Alto Networks Cybersecurity Foundation", section),
            node("Palo Alto Networks", section),
            node("Issued Jul 2026", section),
            node("Credential ID 6EDIYGC9PDCH", section),
            node("Show credential", "license-certifications-see-license-button",
                 "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fwww%2E"
                 "coursera%2Eorg%2Frecords%2F6EDIYGC9PDCH&urlhash=pADf"),
        ]
        cert = build_profile("x", nodes, endpoint_used="t").profile.certifications[0]
        assert cert.name == "Palo Alto Networks Cybersecurity Foundation"
        assert cert.authority == "Palo Alto Networks"
        assert cert.credential_id == "6EDIYGC9PDCH"
        assert cert.url == "https://www.coursera.org/records/6EDIYGC9PDCH"

    def test_credential_id_is_never_read_as_a_certification_name(self):
        section = "profile-card-licenses-and-certifications"
        nodes = [
            node("First Cert", section), node("Issuer A", section),
            node("Issued Jul 2026", section), node("Credential ID AAA", section),
            node("Second Cert", section), node("Issuer B", section),
            node("Issued Jan 2025", section), node("Credential ID BBB", section),
        ]
        certs = build_profile("x", nodes, endpoint_used="t").profile.certifications
        assert [c.name for c in certs] == ["First Cert", "Second Cert"]
        assert [c.credential_id for c in certs] == ["AAA", "BBB"]


class TestTopCardLocation:
    """Location is anchored on the separator, not on punctuation inside it."""

    def test_single_token_location_is_kept(self):
        """"United States" has no comma; a comma test drops it silently."""
        nodes = [
            node("Reid Hoffman", "profile-sticky-header"),
            node("Co-Founder, LinkedIn", "profile-sticky-header"),
            node("He/Him", "profile-top-card"),
            node("United States", "profile-top-card"),
            node("\u00b7", "profile-top-card"),
            node("Contact info", "profile-top-card"),
            node("2,788,442 followers", "profile-top-card"),
        ]
        profile = build_profile("x", nodes, endpoint_used="t").profile
        assert profile.location.raw == "United States"
        assert profile.follower_count == 2788442

    def test_multi_part_location_still_works(self):
        nodes = [
            node("Bill Gates", "profile-sticky-header"),
            node("Chair, Gates Foundation", "profile-sticky-header"),
            node("Gates Foundation", "profile-top-card"),
            node("Seattle, Washington, United States", "profile-top-card"),
            node("\u00b7", "profile-top-card"),
            node("Contact info", "profile-top-card"),
        ]
        profile = build_profile("x", nodes, endpoint_used="t").profile
        assert profile.location.raw == "Seattle, Washington, United States"

    def test_pronouns_are_captured_and_not_mistaken_for_a_location(self):
        nodes = [
            node("Jordan Rivera", "profile-sticky-header"),
            node("Engineer", "profile-sticky-header"),
            node("They/Them", "profile-top-card"),
            node("London", "profile-top-card"),
            node("\u00b7", "profile-top-card"),
            node("Contact info", "profile-top-card"),
        ]
        profile = build_profile("x", nodes, endpoint_used="t").profile
        assert profile.pronouns == "They/Them"
        assert profile.location.raw == "London"

    def test_headline_is_never_read_as_a_location(self):
        """The headline often contains a comma and must not win."""
        nodes = [
            node("Bill Gates", "profile-sticky-header"),
            node("Chair, Gates Foundation", "profile-sticky-header"),
            node("Chair, Gates Foundation", "profile-top-card"),
            node("Seattle, Washington", "profile-top-card"),
            node("\u00b7", "profile-top-card"),
        ]
        profile = build_profile("x", nodes, endpoint_used="t").profile
        assert profile.headline == "Chair, Gates Foundation"
        assert profile.location.raw == "Seattle, Washington"


class TestMissingTopCard:
    """LinkedIn intermittently omits the top card from the screen response.

    Observed returning it on one request and omitting it on the next for the
    same profile, HTTP 200 both times. Falling back to the public id silently
    would present a URL slug as the member's name.
    """

    def test_absent_top_card_is_reported_not_hidden(self):
        nodes = [node("Engineer", "profile-card-experience")]
        response = build_profile("raj-shamani", nodes, endpoint_used="t")
        assert "top_card" in response.meta.partial_fields
        assert any("top card absent" in w for w in response.meta.warnings)

    def test_public_id_is_still_returned_as_a_last_resort(self):
        """The response stays schema-valid; it just says the name is unreliable."""
        response = build_profile("raj-shamani", [], endpoint_used="t")
        assert response.profile.name.full == "raj-shamani"
        assert "top_card" in response.meta.partial_fields

    def test_present_top_card_raises_no_warning(self):
        nodes = [
            node("Raj Shamani", "profile-sticky-header"),
            node("Founder", "profile-sticky-header"),
        ]
        response = build_profile("raj-shamani", nodes, endpoint_used="t")
        assert response.profile.name.full == "Raj Shamani"
        assert "top_card" not in response.meta.partial_fields
        assert not any("top card absent" in w for w in response.meta.warnings)


class TestAbout:
    def test_about_text_is_mapped(self):
        nodes = [
            node("About", "profile-card-about"),
            node("Building distributed systems.", "profile-card-about"),
        ]
        profile = build_profile("x", nodes, endpoint_used="t").profile
        assert profile.about == "Building distributed systems."

    def test_every_paragraph_is_kept(self):
        """About renders one node per paragraph; taking the first truncates it."""
        nodes = [
            node("About", "profile-card-about"),
            node("Hello connections,", "profile-card-about"),
            node("I am a researcher in electronics.", "profile-card-about"),
            node("What drives me is solving root problems.", "profile-card-about"),
        ]
        about = build_profile("x", nodes, endpoint_used="t").profile.about
        assert about.startswith("Hello connections,")
        assert "researcher in electronics" in about
        assert "root problems" in about
        assert about.count("\n\n") == 2

    def test_about_padded_with_invisible_filler_is_null(self):
        """Members pad About with HANGUL FILLER to make it render blank."""
        nodes = [
            node("About", "profile-card-about"),
            node("\u3164\u3164\u3164\u3164", "profile-card-about"),
        ]
        assert build_profile("x", nodes, endpoint_used="t").profile.about is None

    def test_missing_about_is_null(self):
        assert build_profile("x", [], endpoint_used="t").profile.about is None


class TestHonors:
    """Honours render as a title plus three optional, self-identifying lines."""

    def test_full_entry(self):
        section = "profile-card-honors-and-awards"
        nodes = [
            node("Example Student Award", section),
            node("Issued by EXAMPLE-PUBLISHER · Dec 2024", section),
            node("Associated with EXAMPLE-COLLEGE", section),
            node("Received the prestigious award for academic excellence.", section),
        ]
        honor = build_profile("x", nodes, endpoint_used="t").profile.honors[0]
        assert honor.title == "Example Student Award"
        assert honor.issuer == "EXAMPLE-PUBLISHER"
        assert honor.issued.year == 2024 and honor.issued.month == 12
        assert honor.associated_with == "EXAMPLE-COLLEGE"
        assert honor.description.startswith("Received the prestigious")

    def test_title_only_entry(self):
        section = "profile-card-honors-and-awards"
        nodes = [node("Dean's List", section)]
        honor = build_profile("x", nodes, endpoint_used="t").profile.honors[0]
        assert honor.title == "Dean's List"
        assert honor.issuer is None and honor.issued is None

    def test_consecutive_entries_are_separated(self):
        section = "profile-card-honors-and-awards"
        nodes = [
            node("Award One", section, component_key="a"),
            node("Issued by Body A · Dec 2024", section, component_key="a"),
            node("Award Two", section, component_key="b"),
            node("Issued by Body B · Jan 2023", section, component_key="b"),
        ]
        honors = build_profile("x", nodes, endpoint_used="t").profile.honors
        assert [h.title for h in honors] == ["Award One", "Award Two"]
        assert [h.issuer for h in honors] == ["Body A", "Body B"]


class TestRecommendationsAndCauses:
    def test_recommendation_counts_are_read_from_the_tabs(self):
        """The card renders tab headers only, not the recommendation text."""
        nodes = [
            node("Recommendations", "profile-card-recommendations"),
            node("Received (2)", "profile-recommendations-details-view"),
            node("Given (5)", "profile-recommendations-details-view"),
        ]
        rec = build_profile("x", nodes, endpoint_used="t").profile.recommendations
        assert rec.received_count == 2
        assert rec.given_count == 5

    def test_bodies_are_parsed_into_received_and_given(self):
        sec = "unknown"
        nodes = [
            node("Received (1)", "profile-recommendations-details-view"),
            node("Given (1)", "profile-recommendations-details-view"),
            node("Received", sec),
            node("Alex Doe", sec), node("· 3rd+", sec),
            node("Chief Technologist", sec),
            node("February 26, 2025, the member was Alex's client", sec),
            node("Example first paragraph.", sec),
            node("He has great attention to detail.", sec),
            node("Given", sec),
            node("Sam Roe", sec), node("· 2nd", sec),
            node("Security Architect", sec),
            node("November 8, 2024, Sam managed the member directly", sec),
            node("Koby is a warrior-king of the industry.", sec),
        ]
        rec = build_profile("x", nodes, endpoint_used="t").profile.recommendations
        assert len(rec.received) == 1 and len(rec.given) == 1

        got = rec.received[0]
        assert got.author == "Alex Doe"
        assert got.author_degree == "3rd+"
        assert got.author_headline == "Chief Technologist"
        assert got.date.year == 2025 and got.date.month == 2
        assert got.text == (
            "Example first paragraph.\n\n"
            "He has great attention to detail."
        )
        assert rec.given[0].author == "Sam Roe"

    def test_pending_tab_is_not_counted_as_given(self):
        """Pending repeats the Received entries; it must not double-count."""
        sec = "unknown"
        nodes = [
            node("Received", sec),
            node("Alex Doe", sec), node("· 3rd+", sec), node("Tech", sec),
            node("Given", sec),
            node("Sam Roe", sec), node("· 2nd", sec), node("Arch", sec),
            node("Pending", sec),
            node("Alex Doe", sec), node("· 3rd+", sec), node("Tech", sec),
        ]
        rec = build_profile("x", nodes, endpoint_used="t").profile.recommendations
        assert len(rec.received) == 1
        assert len(rec.given) == 1

    def test_absent_recommendations_are_null_not_zero(self):
        """Zero would assert the profile has none; null says we do not know."""
        assert build_profile("x", [], endpoint_used="t").profile.recommendations is None

    def test_volunteer_causes_are_split(self):
        nodes = [
            node("Causes", "profile-card-volunteer-causes"),
            node("Education \u2022 Science and Technology",
                 "profile-card-volunteer-causes"),
        ]
        causes = build_profile("x", nodes, endpoint_used="t").profile.volunteer_causes
        assert causes == ["Education", "Science and Technology"]


class TestSkills:
    """The skills card interleaves skills with the credentials evidencing them.

    Verified against LinkedIn's own "Save as PDF" export, which lists only the
    bold entries as skills.
    """

    def test_credentials_are_not_reported_as_skills(self):
        nodes = [
            TextNode(text="Wireshark", section="profile-card-skills", emphasis="bold"),
            TextNode(text="Certified Network Security Practitioner",
                     section="profile-card-skills", emphasis="normal"),
            TextNode(text="Cyber", section="profile-card-skills", emphasis="bold"),
            TextNode(text="Cybersecurity Fundamentals",
                     section="profile-card-skills", emphasis="normal"),
        ]
        skills = build_profile("x", nodes, endpoint_used="t").profile.skills
        assert [s.name for s in skills] == ["Wireshark", "Cyber"]
        assert skills[0].credentials == ["Certified Network Security Practitioner"]
        assert skills[1].credentials == ["Cybersecurity Fundamentals"]

    def test_endorsement_cards_still_work_without_font_weight(self):
        """A card that renders endorsements declares no weight; fall back."""
        nodes = [
            TextNode(text="Machine Learning", section="profile-card-skills"),
            TextNode(text="Endorsements", section="profile-card-skills"),
            TextNode(text="3 endorsements", section="profile-card-skills"),
            TextNode(text="Android Development", section="profile-card-skills"),
            TextNode(text="2 endorsements", section="profile-card-skills"),
        ]
        skills = build_profile("x", nodes, endpoint_used="t").profile.skills
        assert [s.name for s in skills] == ["Machine Learning", "Android Development"]
        assert [s.endorsement_count for s in skills] == [3, 2]


class TestMultipleEducation:
    """Shapes first seen on a profile with two degrees."""

    def test_two_schools_become_two_entries(self):
        grad = "https://www.linkedin.com/school/3558/"
        under = "https://www.linkedin.com/school/7835/"
        nodes = [
            node("Northgate Institute of Technology", "education-lockup-view", grad),
            node("Master of Science - MS, Computer Science", "education-lockup-view", grad),
            node("Aug 2021 – Present", "education-lockup-view", grad),
            node("EXAMPLE-STATE-UNIVERSITY", "education-lockup-view", under),
            node("Bachelor of Science - BS, Computer Science", "education-lockup-view", under),
        ]
        education = build_profile("x", nodes, endpoint_used="t").profile.education
        assert [e.school for e in education] == [
            "Northgate Institute of Technology",
            "EXAMPLE-STATE-UNIVERSITY",
        ]
        assert education[0].school_url == grad
        assert education[1].school_url == under

    def test_ongoing_study_has_a_start_and_no_end(self):
        url = "https://www.linkedin.com/school/3558/"
        nodes = [
            node("Northgate Institute of Technology", "education-lockup-view", url),
            node("Master of Science - MS, Computer Science", "education-lockup-view", url),
            node("Aug 2021 – Present", "education-lockup-view", url),
        ]
        entry = build_profile("x", nodes, endpoint_used="t").profile.education[0]
        assert entry.start.year == 2021 and entry.start.month == 8
        assert entry.end is None

    def test_education_without_dates_is_still_returned(self):
        """§12: a missing date is null, not a reason to drop the entry."""
        url = "https://www.linkedin.com/school/7835/"
        nodes = [
            node("EXAMPLE-STATE-UNIVERSITY", "education-lockup-view", url),
            node("Bachelor of Science - BS, Computer Science", "education-lockup-view", url),
        ]
        entry = build_profile("x", nodes, endpoint_used="t").profile.education[0]
        assert entry.school == "EXAMPLE-STATE-UNIVERSITY"
        assert entry.degree == "Bachelor of Science - BS"
        assert entry.start is None and entry.end is None


class TestCertifications:
    def test_name_authority_and_issue_date(self, mapped):
        certs = mapped.profile.certifications
        assert certs[0].name == "Data Science Professional Certificate"
        assert certs[0].authority == "OpenCourse"
        assert certs[0].issued.year == 2020 and certs[0].issued.month == 8


class TestTruncation:
    def test_exact_total_is_recorded_when_stated(self, mapped):
        """'Show all 6 licenses' gives a real total."""
        coverage = mapped.meta.coverage["profile-card-licenses-and-certifications"]
        assert coverage.truncated is True
        assert coverage.total == 6
        assert coverage.returned == 2

    def test_total_stays_null_when_not_stated(self, mapped):
        """A bare 'Show all' means truncated-but-unknown, never a guess."""
        coverage = mapped.meta.coverage["profile-card-experience"]
        assert coverage.truncated is True
        assert coverage.total is None

    def test_truncation_is_surfaced_as_a_warning(self, mapped):
        joined = " ".join(mapped.meta.warnings)
        assert "2 of 6 entries" in joined
        assert "unstated" in joined

    def test_see_all_control_is_not_mistaken_for_content(self, mapped):
        """'Show all' must not appear as a skill or a certification."""
        assert all(s.name != "Show all" for s in mapped.profile.skills)
        assert all(c.name != "Show all" for c in mapped.profile.certifications)


class TestPagedListChrome:
    """A paged detail response carries chrome the card version does not."""

    SEC = "profile-card-licenses-and-certifications"

    def test_skills_annotation_is_not_read_as_a_certification(self):
        nodes = [
            node("Cybersecurity Fundamentals", self.SEC),
            node("IBM", self.SEC),
            node("Issued Jul 2023", self.SEC),
            node("Skills:", self.SEC),
            node("Ethical Hacking, Security, +7 skills", self.SEC),
            node("Microsoft Certified: Azure Fundamentals", self.SEC),
            node("Microsoft", self.SEC),
        ]
        certs = build_profile("x", nodes, endpoint_used="t").profile.certifications
        assert [c.name for c in certs] == [
            "Cybersecurity Fundamentals", "Microsoft Certified: Azure Fundamentals",
        ]
        assert certs[0].authority == "IBM"

    def test_build_version_string_is_ignored(self):
        """Paged responses trail a version like "0.1.51189"."""
        nodes = [
            node("Real Certificate", self.SEC),
            node("Some Authority", self.SEC),
            node("0.1.51189", self.SEC),
        ]
        certs = build_profile("x", nodes, endpoint_used="t").profile.certifications
        assert [c.name for c in certs] == ["Real Certificate"]

    def test_empty_state_copy_is_ignored(self):
        nodes = [
            node("Real Certificate", self.SEC),
            node("Some Authority", self.SEC),
            node("Nothing to see for now", self.SEC),
        ]
        certs = build_profile("x", nodes, endpoint_used="t").profile.certifications
        assert [c.name for c in certs] == ["Real Certificate"]

    def test_per_entry_expander_is_not_list_truncation(self):
        """"Show all 4 details" expands one skill, not the skills list."""
        nodes = [
            node("Wireshark", "profile-card-skills", component_key=None),
            node("Show all 4 details", "profile-card-skills"),
        ]
        response = build_profile("x", nodes, endpoint_used="t")
        assert response.meta.coverage["profile-card-skills"].truncated is False

    def test_a_complete_list_clears_the_truncation_flag(self):
        """The card renders "see all" whether or not we then paged the list."""
        # Each entry needs a name and an issue date, or two bare lines are read
        # as one certification's name and authority.
        nodes = []
        for i in range(3):
            nodes.append(node(f"Cert {i}", self.SEC))
            nodes.append(node(f"Issuer {i}", self.SEC))
            nodes.append(node("Issued Jan 2024", self.SEC))
        nodes.append(node("Show all 3 licenses", "license-certifications-see-all"))
        cov = build_profile("x", nodes, endpoint_used="t").meta.coverage[self.SEC]
        assert cov.total == 3
        assert cov.returned == 3
        assert cov.truncated is False


class TestProvenance:
    def test_regex_derived_fields_are_marked_parsed(self, mapped):
        confidence = mapped.meta.parse_confidence
        assert confidence["experience[0].start"] == Provenance.PARSED
        assert confidence["experience[0].duration_months"] == Provenance.PARSED

    def test_data_layer_is_declared(self, mapped):
        assert mapped.meta.data_layer == "rsc_parsed"


class TestTopCard:
    def test_duplicate_name_and_headline_are_collapsed(self):
        """The sticky header repeats the name and headline the top card renders."""
        nodes = [
            node("Jordan Rivera", "profile-sticky-header"),
            node("Staff Engineer at Globex", "profile-sticky-header"),
            node("View Jordan's verifications", "profile-top-card"),
            node("Globex · Northgate Institute", "profile-top-card"),
            node("Springfield, Ohio, United States", "profile-top-card"),
            node("1,234 followers", "profile-top-card"),
            node("500+", "profile-top-card"),
            node("connections", "profile-top-card"),
        ]
        profile = build_profile("jordan-rivera", nodes, endpoint_used="t").profile
        assert profile.name.full == "Jordan Rivera"
        assert profile.headline == "Staff Engineer at Globex"
        assert profile.location.raw == "Springfield, Ohio, United States"
        assert profile.follower_count == 1234
        assert profile.connection_count == "500+ connections"


class TestEmptyAndMalformed:
    def test_empty_input_produces_a_valid_response(self):
        response = build_profile("nobody", [], endpoint_used="t")
        assert response.profile.public_id == "nobody"
        assert response.profile.experience == []
        assert set(response.meta.partial_fields) >= {"experience", "skills"}

    def test_entry_without_a_date_is_reported_not_silently_dropped(self):
        """§12: never fabricate, but never omit silently either."""
        nodes = [
            node("Mystery Role", "profile-card-experience", "https://x/company/9/"),
            node("Some Company", "profile-card-experience", "https://x/company/9/"),
        ]
        response = build_profile("x", nodes, endpoint_used="t")
        assert response.profile.experience == []
        assert any("no recognisable date range" in w for w in response.meta.warnings)

    def test_no_raw_urns_leak_into_the_schema(self, mapped):
        """§6: never leak LinkedIn internals."""
        blob = mapped.model_dump_json()
        assert "urn:li:" not in blob
