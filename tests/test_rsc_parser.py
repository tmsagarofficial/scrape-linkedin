"""Tests for the RSC flight parser.

The fixture is a real captured `profileCardsBelowActivityPart1` response with a
synthetic identity substituted in (see `scripts/scrub_fixture.py`). Its record
graph, reference forms and separator characters are byte-for-byte what LinkedIn
sent, so these tests exercise the real wire format.

Runs offline with no credentials, per AGENTS.md §8.
"""

from pathlib import Path

import pytest

from app.linkedin.rsc_parser import ParsedFlight, iter_text, parse_flight

FIXTURE = Path(__file__).parent / "fixtures" / "rsc_below_activity_part1.txt"


@pytest.fixture(scope="module")
def flight() -> ParsedFlight:
    return parse_flight(FIXTURE.read_text())


@pytest.fixture(scope="module")
def nodes(flight):
    return list(iter_text(flight))


class TestParseFlight:
    def test_indexes_records_without_errors(self, flight):
        assert flight.errors == []
        assert len(flight.data) == 48
        assert len(flight.imports) == 12

    def test_separates_imports_from_data(self, flight):
        """`I[...]` records register components and carry no profile data."""
        assert flight.imports["3"] == "TracedComponent"
        assert flight.imports["5"] == "ReplaceableComponent"
        assert "3" not in flight.data

    def test_hex_record_ids(self, flight):
        """Ids are hexadecimal: '1a' is a distinct record, not decimal 26."""
        assert "1a" in flight.data
        assert "3b" in flight.imports

    def test_root_is_record_zero(self, flight):
        assert flight.root is not None
        assert flight.root[0] == "$"
        assert flight.root[1] == "div"

    def test_null_record_is_retained(self, flight):
        """Record `2:null` is a valid value, not a parse failure."""
        assert "2" in flight.data
        assert flight.data["2"] is None

    def test_malformed_lines_are_collected_not_raised(self):
        flight = parse_flight('0:["$","div",null,{"children":["ok"]}]\nnot-a-record\n9:{oops')
        assert len(flight.errors) == 2
        assert [n.text for n in iter_text(flight)] == ["ok"]

    def test_empty_body_is_survivable(self):
        flight = parse_flight("")
        assert flight.root is None
        assert list(iter_text(flight)) == []


class TestIterText:
    def test_extracts_only_rendered_text(self, nodes):
        """Class names, style tokens and telemetry must never leak in."""
        texts = [n.text for n in nodes]
        assert texts
        assert not any(t.startswith("_") for t in texts)
        assert not any("proto.sdui" in t for t in texts)
        assert not any("componentKey" in t for t in texts)

    def test_document_order_is_preserved(self, nodes):
        """Records arrive out of order; output must still be document order.

        Record `24` (a logo) is emitted after record `37` in the stream, so
        reading by ascending id would interleave education into experience.
        """
        texts = [n.text for n in nodes]
        assert texts.index("Experience") < texts.index("Globex")
        assert texts.index("Globex") < texts.index("Education")
        assert texts.index("Education") < texts.index(
            "Northgate Institute of Technology"
        )

    def test_roles_stay_with_their_employer(self, nodes):
        """A grouped position lists company first, then each role in order."""
        texts = [n.text for n in nodes]
        assert texts.index("Globex") < texts.index("SDE 2") < texts.index("SDE")

    def test_sections_are_labelled_from_linkedin_metadata(self, nodes):
        sections = {n.section for n in nodes}
        assert "profile-card-experience" in sections
        assert "profile-card-licenses-and-certifications" in sections

    def test_education_entry_is_complete(self, nodes):
        """The 4d acceptance gate: the education entry parses correctly."""
        education = [n for n in nodes if n.section == "education-lockup-view"]
        assert [n.text for n in education] == [
            "Northgate Institute of Technology",
            "Bachelor of Engineering - BE, Electrical, Electronics and "
            "Communications Engineering",
            "2018 – 2022",
        ]
        assert all(
            n.entity_url == "https://www.linkedin.com/school/2000001/"
            for n in education
        )

    def test_entity_url_groups_entries(self, nodes):
        """Entries about one employer share a navigation target."""
        experience = [
            n for n in nodes if n.section == "profile-card-experience" and n.entity_url
        ]
        by_url: dict[str, list[str]] = {}
        for node in experience:
            by_url.setdefault(node.entity_url, []).append(node.text)

        assert len(by_url) == 3
        globex = by_url["https://www.linkedin.com/company/1000001/"]
        assert "Globex" in globex and "SDE 2" in globex and "SDE" in globex

    def test_separator_characters_survive_intact(self, nodes):
        """The middle dot and en dash must not be normalised away."""
        texts = [n.text for n in nodes]
        assert "Full-time · 4 yrs 1 mo" in texts  # U+00B7 middle dot
        assert "2018 – 2022" in texts  # U+2013 en dash
        assert "Aug 2022 - Jul 2025 · 3 yrs" in texts  # U+002D hyphen


class TestNonContentStrings:
    """Not every string in a list slot is rendered text."""

    def test_serialised_json_is_not_emitted_as_text(self):
        """Some cards carry a JSON blob in a value slot.

        It satisfies the "string inside a list" rule but is plainly not display
        text, and would otherwise surface as a profile field.
        """
        import json
        blob = '{"threadlineDecoration":null,"key":"abc","semanticId":""}'
        record = ["$", "div", None, {"children": [blob, "real text"]}]
        flight = parse_flight("0:" + json.dumps(record))
        assert [n.text for n in iter_text(flight)] == ["real text"]

    def test_json_looking_prose_is_still_kept(self):
        flight = parse_flight('0:["$","p",null,{"children":["{not json"]}]')
        assert [n.text for n in iter_text(flight)] == ["{not json"]


class TestMultipleRoots:
    """A response is not always a single tree."""

    def test_content_under_a_second_unreferenced_root_is_found(self):
        """The recommendations card emits its bodies under a separate root.

        Nothing links it to record 0. Walking only record 0 returns the tab
        headers and silently discards every recommendation, with no error.
        """
        flight = parse_flight(
            '0:["$","div",null,{"children":["Received (2)"]}]\n'
            'd:["$","div",null,{"children":["the recommendation body"]}]'
        )
        assert flight.roots() == ["0", "d"]
        assert [n.text for n in iter_text(flight)] == [
            "Received (2)", "the recommendation body",
        ]

    def test_referenced_records_are_not_treated_as_roots(self):
        flight = parse_flight(
            '0:["$","div",null,{"children":["$L1"]}]\n'
            '1:["$","p",null,{"children":["once"]}]'
        )
        assert flight.roots() == ["0"]
        assert [n.text for n in iter_text(flight)] == ["once"]

    def test_record_zero_is_walked_first(self):
        flight = parse_flight(
            'd:["$","div",null,{"children":["second"]}]\n'
            '0:["$","div",null,{"children":["first"]}]'
        )
        assert flight.roots()[0] == "0"
        assert [n.text for n in iter_text(flight)] == ["first", "second"]


class TestVisible:
    """Invisible padding is empty, and must not be reported as content."""

    def test_hangul_filler_is_empty(self):
        from app.linkedin.rsc_parser import visible
        assert visible("\u3164\u3164\u3164") is None

    def test_zero_width_characters_are_empty(self):
        from app.linkedin.rsc_parser import visible
        assert visible("\u200b\ufeff") is None

    def test_real_text_survives(self):
        from app.linkedin.rsc_parser import visible
        assert visible("  Building things.  ") == "Building things."


class TestReferenceResolution:
    def test_resolves_bare_string_references(self):
        """A `$L` ref in a value slot, as in {"children": ["$L1"]}."""
        flight = parse_flight(
            '0:["$","div",null,{"children":["$L1"]}]\n1:["$","p",null,{"children":["hi"]}]'
        )
        assert [n.text for n in iter_text(flight)] == ["hi"]

    def test_resolves_component_tag_references(self):
        """A `$L` ref in the tag slot, as in ["$","$L1",null,{...}]."""
        flight = parse_flight(
            '0:["$","$L1",null,{"children":["hi"]}]\n1:I["abc",[],"Text"]'
        )
        assert [n.text for n in iter_text(flight)] == ["hi"]

    def test_descends_into_initial_content(self):
        """Profile cards hang off `initialContent`, not `children`."""
        flight = parse_flight(
            '0:["$","div",null,{"initialContent":"$L1"}]\n1:["$","p",null,{"children":["card"]}]'
        )
        assert [n.text for n in iter_text(flight)] == ["card"]

    def test_unescapes_doubled_dollar_literal(self):
        """RSC encodes a literal leading '$' as '$$'."""
        flight = parse_flight('0:["$","p",null,{"children":["$$5.00"]}]')
        assert [n.text for n in iter_text(flight)] == ["$5.00"]

    def test_ignores_sentinels(self):
        flight = parse_flight('0:["$","p",null,{"children":["$undefined","real"]}]')
        assert [n.text for n in iter_text(flight)] == ["real"]

    def test_dangling_reference_is_skipped(self):
        flight = parse_flight('0:["$","div",null,{"children":["$L99","kept"]}]')
        assert [n.text for n in iter_text(flight)] == ["kept"]

    def test_cycle_terminates(self):
        """A self-referential document must not recurse forever."""
        flight = parse_flight(
            '0:["$","div",null,{"children":["$L1"]}]\n'
            '1:["$","div",null,{"children":["$L1","done"]}]'
        )
        assert [n.text for n in iter_text(flight)] == ["done"]
