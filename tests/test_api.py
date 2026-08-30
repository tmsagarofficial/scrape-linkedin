"""Tests for the HTTP API.

Every branch of the AGENTS.md §5 error taxonomy is exercised, and the §7
fallback chain — live, fresh cache, stale cache, structured 503 — is checked
end to end.

No network and no credentials: the LinkedIn client is replaced with stubs, per
AGENTS.md §8.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.cache import ProfileCache
from app.linkedin.client import (
    FetchResult,
    ProfileNotFound,
    ProfileNotVisible,
    RedirectLoop,
    SessionExpired,
    UpstreamBlocked,
)
from app.linkedin.rsc_parser import TextNode
from app.main import app, require_api_key
import app.main as main

API_KEY = "demo-key"
HEADERS = {"X-API-Key": API_KEY}


def nodes_for(name: str = "Jordan Rivera") -> list[TextNode]:
    return [
        TextNode(text=name, section="profile-sticky-header"),
        TextNode(text="Staff Engineer", section="profile-sticky-header"),
        TextNode(text="Springfield, Ohio, United States", section="profile-top-card"),
        # A single-role entry renders title first, then "company · type".
        TextNode(text="Engineer", section="profile-card-experience",
                 entity_url="https://www.linkedin.com/company/1/"),
        TextNode(text="Globex · Full-time", section="profile-card-experience",
                 entity_url="https://www.linkedin.com/company/1/"),
        TextNode(text="Aug 2022 - Present · 2 yrs", section="profile-card-experience",
                 entity_url="https://www.linkedin.com/company/1/"),
    ]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """Give each test a private cache, budget and rate limiter."""
    from app.budget import DailyBudget

    monkeypatch.setattr(main, "cache", ProfileCache(":memory:", ttl_seconds=3600))
    monkeypatch.setattr(main, "budget", DailyBudget(":memory:", limit=1000))
    monkeypatch.setattr(main.settings, "api_key", API_KEY)
    main._outbound.clear()
    yield


@pytest.fixture
def api():
    return TestClient(app)


def stub_fetch(monkeypatch, result=None, error=None, record=None):
    """Stub every path that reaches LinkedIn.

    Both the shared client *and* `_client_for` are patched: a request carrying
    `X-LI-AT` builds a throwaway client, so stubbing only the shared one leaves
    that path live.
    """
    def _fetch(public_id, sections=(), *, complete=False, session=None):
        if record is not None:
            record.append(
                {"public_id": public_id, "complete": complete,
                 "caller_session": session}
            )
        if error:
            raise error
        return result or FetchResult(nodes=nodes_for(), components_used=["screen"])

    monkeypatch.setattr(main.client, "fetch_profile", _fetch)

    class _Stub:
        def __init__(self, caller=None):
            self._caller = caller

        def fetch_profile(self, public_id, sections=(), *, complete=False):
            return _fetch(
                public_id, sections, complete=complete, session=self._caller
            )

    monkeypatch.setattr(main, "_client_for", lambda session: _Stub(session))


class TestAuth:
    def test_401_without_a_key(self, api):
        assert api.get("/v1/profile?url=x").status_code == 401

    def test_401_with_a_wrong_key(self, api):
        response = api.get("/v1/profile?url=x", headers={"X-API-Key": "nope"})
        assert response.status_code == 401
        assert response.json()["detail"]["error"] == "unauthorized"

    def test_401_names_the_demo_key(self, api):
        """A reviewer who curls without reading must not hit a dead end."""
        detail = api.get("/v1/profile/x").json()["detail"]
        assert detail["demo_api_key"] == "demo-key"
        assert "X-LI-AT" in detail["live_data"]

    def test_401_hides_a_real_key(self, api, monkeypatch):
        """The hint is for the published demo key only."""
        monkeypatch.setattr(main.settings, "api_key", "s3cr3t-production-key")
        detail = api.get("/v1/profile/x", headers={"X-API-Key": "wrong"}).json()["detail"]
        assert "demo_api_key" not in detail
        assert "s3cr3t" not in str(detail)

    def test_health_needs_no_key(self, api):
        assert api.get("/health").status_code == 200


class TestBadRequest:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.linkedin.com/company/microsoft/",
            "https://example.com/in/someone/",
            "https://www.linkedin.com/in/",
        ],
    )
    def test_400_for_non_profile_urls(self, api, url):
        response = api.get(f"/v1/profile?url={url}", headers=HEADERS)
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "invalid_url"


class TestUpstreamErrors:
    def test_404_when_the_profile_does_not_exist(self, api, monkeypatch):
        stub_fetch(monkeypatch, error=ProfileNotFound("ghost"))
        response = api.get("/v1/profile/ghost", headers=HEADERS)
        assert response.status_code == 404

    def test_403_when_the_profile_is_not_visible(self, api, monkeypatch):
        stub_fetch(monkeypatch, error=ProfileNotVisible("private"))
        response = api.get("/v1/profile/private", headers=HEADERS)
        assert response.status_code == 403

    def test_503_when_the_session_is_dead_and_cache_is_empty(self, api, monkeypatch):
        stub_fetch(monkeypatch, error=SessionExpired())
        response = api.get("/v1/profile/someone", headers=HEADERS)
        assert response.status_code == 503
        assert "session" in response.json()["message"].lower()

    def test_503_when_upstream_blocks(self, api, monkeypatch):
        stub_fetch(monkeypatch, error=UpstreamBlocked("999 automation defence"))
        response = api.get("/v1/profile/someone", headers=HEADERS)
        assert response.status_code == 503
        assert "999" in response.json()["message"]

    def test_503_on_a_redirect_loop(self, api, monkeypatch):
        """A self-redirect is LinkedIn declining, not a network blip."""
        stub_fetch(monkeypatch, error=RedirectLoop("https://www.linkedin.com/x"))
        response = api.get("/v1/profile/someone", headers=HEADERS)
        assert response.status_code == 503
        assert "itself" in response.json()["message"]

    def test_never_returns_a_bare_500(self, api, monkeypatch):
        """§12: an unexpected exception must still produce a typed body."""
        def boom(public_id, sections=(), *, complete=False):
            raise RuntimeError("something unforeseen")
        monkeypatch.setattr(main.client, "fetch_profile", boom)
        # raise_server_exceptions=False lets the app's own handler run, which is
        # what a real deployment does.
        quiet = TestClient(app, raise_server_exceptions=False)
        response = quiet.get("/v1/profile/someone", headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["error"] == "internal_error"


class TestSuccess:
    def test_200_and_a_mapped_profile(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        response = api.get("/v1/profile/jordan-rivera", headers=HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert body["profile"]["name"]["full"] == "Jordan Rivera"
        assert body["profile"]["experience"][0]["company"]["name"] == "Globex"
        assert body["_meta"]["source"] == "live"

    def test_accepts_a_full_url(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        response = api.get(
            "/v1/profile?url=https://www.linkedin.com/in/jordan-rivera/",
            headers=HEADERS,
        )
        assert response.status_code == 200

    def test_206_when_a_section_could_not_be_fetched(self, api, monkeypatch):
        stub_fetch(monkeypatch, result=FetchResult(
            nodes=nodes_for(), components_used=["screen"],
            warnings=["section component part 7 failed: timeout"],
        ))
        response = api.get("/v1/profile/jordan-rivera", headers=HEADERS)
        assert response.status_code == 206
        assert response.json()["_meta"]["source"] == "partial"


class TestPartialResponsesDoNotPoisonTheCache:
    """A narrowed `?fields=` response must not replace a complete cached one.

    Observed in practice: a profile cached with five experience entries came
    back with zero after a later `?fields=skills` request overwrote it. The
    cache key does not encode the field set, so only complete responses are
    written.
    """

    def test_narrowed_response_is_not_cached(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        api.get("/v1/profile/x?fields=skills", headers=HEADERS)
        assert main.cache.get("x") is None

    def test_narrowed_response_says_it_was_not_cached(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        meta = api.get("/v1/profile/x?fields=skills", headers=HEADERS).json()["_meta"]
        assert any("not cached" in w for w in meta["warnings"])

    def test_a_full_response_is_still_cached(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        api.get("/v1/profile/x", headers=HEADERS)
        assert main.cache.get("x") is not None

    def test_a_complete_entry_survives_a_later_narrow_request(self, api, monkeypatch):
        """The regression this exists to prevent."""
        stub_fetch(monkeypatch)
        api.get("/v1/profile/x", headers=HEADERS)              # full, cached
        before = main.cache.get("x")[0]["profile"]["experience"]
        api.get("/v1/profile/x?fields=skills&refresh=true", headers=HEADERS)
        after = main.cache.get("x")[0]["profile"]["experience"]
        assert after == before, "a narrowed fetch degraded the cached profile"


class TestCompleteLists:
    """`?complete=true` swaps LinkedIn's truncated cards for full lists."""

    def test_flag_is_passed_through_to_the_client(self, api, monkeypatch):
        calls = []
        stub_fetch(monkeypatch, record=calls)
        api.get("/v1/profile/x?complete=true", headers=HEADERS)
        assert calls[-1]["complete"] is True

    def test_default_is_off(self, api, monkeypatch):
        calls = []
        stub_fetch(monkeypatch, record=calls)
        api.get("/v1/profile/x", headers=HEADERS)
        assert calls[-1]["complete"] is False

    def test_meta_records_which_mode_produced_the_response(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        assert api.get("/v1/profile/x", headers=HEADERS).json()["_meta"]["complete"] is False
        assert api.get(
            "/v1/profile/y?complete=true", headers=HEADERS
        ).json()["_meta"]["complete"] is True

    def test_a_truncated_cache_entry_does_not_satisfy_complete(self, api, monkeypatch):
        """Otherwise a cheap earlier request poisons every later full one."""
        calls = []
        stub_fetch(monkeypatch, record=calls)
        api.get("/v1/profile/x", headers=HEADERS)              # caches truncated
        api.get("/v1/profile/x?complete=true", headers=HEADERS)  # must refetch
        assert [c["complete"] for c in calls] == [False, True]

    def test_a_complete_cache_entry_serves_a_normal_request(self, api, monkeypatch):
        calls = []
        stub_fetch(monkeypatch, record=calls)
        api.get("/v1/profile/x?complete=true", headers=HEADERS)
        api.get("/v1/profile/x", headers=HEADERS)
        assert len(calls) == 1, "the fuller cached copy should have been reused"


class TestCaching:
    def test_second_request_is_served_from_cache(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        assert api.get("/v1/profile/jordan-rivera", headers=HEADERS).status_code == 200

        def fail(public_id, sections=(), *, complete=False):
            raise AssertionError("should not have called upstream")
        monkeypatch.setattr(main.client, "fetch_profile", fail)

        response = api.get("/v1/profile/jordan-rivera", headers=HEADERS)
        assert response.status_code == 200
        assert response.json()["_meta"]["source"] == "cache"

    def test_refresh_bypasses_the_cache(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        api.get("/v1/profile/jordan-rivera", headers=HEADERS)
        response = api.get("/v1/profile/jordan-rivera?refresh=true", headers=HEADERS)
        assert response.json()["_meta"]["source"] == "live"

    def test_stale_cache_is_served_when_the_fetch_fails(self, api, monkeypatch):
        """§7: stale-if-error at any age beats a 503."""
        stub_fetch(monkeypatch)
        api.get("/v1/profile/jordan-rivera", headers=HEADERS)

        main.cache.ttl_seconds = -1  # force every entry to read as expired
        stub_fetch(monkeypatch, error=SessionExpired())

        response = api.get("/v1/profile/jordan-rivera", headers=HEADERS)
        assert response.status_code == 200
        meta = response.json()["_meta"]
        assert meta["source"] == "cache"
        assert any("stale cache" in w for w in meta["warnings"])


class TestDailyBudget:
    """A published demo key plus a shared session needs a daily ceiling.

    A per-minute limit is not enough: 20/min is still ~28,800/day, which is
    nothing like human browsing and is what gets a LinkedIn account restricted.
    """

    def _tiny_budget(self, monkeypatch, limit=1):
        from app.budget import DailyBudget

        monkeypatch.setattr(main, "budget", DailyBudget(":memory:", limit=limit))

    def test_live_fetches_are_capped_per_day(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        self._tiny_budget(monkeypatch, limit=1)

        assert api.get("/v1/profile/one", headers=HEADERS).status_code == 200
        second = api.get("/v1/profile/two", headers=HEADERS)
        assert second.status_code == 429
        assert second.json()["detail"]["error"] == "daily_budget_exhausted"

    def test_cached_profiles_still_work_when_exhausted(self, api, monkeypatch):
        """Cache costs LinkedIn nothing, so the ceiling must not block it."""
        stub_fetch(monkeypatch)
        self._tiny_budget(monkeypatch, limit=1)
        api.get("/v1/profile/one", headers=HEADERS)          # spends the budget

        cached = api.get("/v1/profile/one", headers=HEADERS)
        assert cached.status_code == 200
        assert cached.json()["_meta"]["source"] == "cache"

    def test_stale_cache_beats_a_429(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        self._tiny_budget(monkeypatch, limit=1)
        api.get("/v1/profile/one", headers=HEADERS)
        main.cache.ttl_seconds = -1                          # force staleness

        response = api.get("/v1/profile/one", headers=HEADERS)
        assert response.status_code == 200
        assert any(
            "budget exhausted" in w for w in response.json()["_meta"]["warnings"]
        )

    def test_a_caller_session_is_exempt(self, api, monkeypatch):
        """Their credential, their risk — it must not spend our ceiling."""
        stub_fetch(monkeypatch)
        self._tiny_budget(monkeypatch, limit=0)

        assert api.get("/v1/profile/one", headers=HEADERS).status_code == 429
        with_own = api.get(
            "/v1/profile/two",
            headers={**HEADERS, "X-LI-AT": SECRET, "X-LI-JSESSIONID": "ajax:0000"},
        )
        assert with_own.status_code == 200

    def test_the_error_names_the_alternatives(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        self._tiny_budget(monkeypatch, limit=0)
        message = api.get("/v1/profile/x", headers=HEADERS).json()["detail"]["message"]
        assert "X-LI-AT" in message and "locally" in message

    def test_health_publishes_the_budget(self, api):
        stats = api.get("/health").json()["live_fetch_budget"]
        assert stats["limit_per_day"] > 0
        assert "remaining_today" in stats

    def test_budget_survives_a_restart(self, tmp_path):
        """Stored in SQLite: a crash loop must not reset the ceiling."""
        from app.budget import DailyBudget

        path = str(tmp_path / "b.db")
        first = DailyBudget(path, limit=2)
        assert first.consume() is True
        assert DailyBudget(path, limit=2).used() == 1


class TestRateLimiting:
    def test_429_with_retry_after(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        monkeypatch.setattr(main.settings, "rate_limit_per_min", 1)
        api.get("/v1/profile/one", headers=HEADERS)
        response = api.get("/v1/profile/two", headers=HEADERS)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"


#: A stand-in for a caller-supplied cookie.
#:
#: Deliberately *not* shaped like a real li_at. A realistic fixture would trip
#: scripts/scan_secrets.py, and the honest response to that is to keep
#: credential-shaped strings out of the repository rather than to teach the
#: scanner to ignore a pattern — the tests only need a distinctive string.
SECRET = "caller-cookie-stand-in-not-a-real-credential"


class TestCallerSuppliedSession:
    """`X-LI-AT` lets a reviewer use their own session when ours has expired.

    AGENTS.md §5 attaches three conditions, each tested here: never logged,
    never cached under, never persisted.
    """

    def _headers(self):
        return {**HEADERS, "X-LI-AT": SECRET, "X-LI-JSESSIONID": "ajax:0000"}

    def test_x_li_at_alone_is_rejected(self, api):
        """JSESSIONID supplies the csrf-token, so half a session is unusable."""
        response = api.get(
            "/v1/profile/x", headers={**HEADERS, "X-LI-AT": SECRET}
        )
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "incomplete_session"

    def test_response_is_not_cached(self, api, monkeypatch):
        """Their session may see profiles ours cannot; caching would leak it."""
        calls = []
        stub_fetch(monkeypatch, record=calls)
        api.get("/v1/profile/x", headers=self._headers())
        assert main.cache.get("x") is None
        assert api.get("/v1/cache", headers=HEADERS).json()["count"] == 0

    def test_it_does_not_read_from_the_shared_cache_either(self, api, monkeypatch):
        """A caller with their own session wants their view, not our copy."""
        calls = []
        stub_fetch(monkeypatch, record=calls)
        api.get("/v1/profile/x", headers=HEADERS)            # populates cache
        api.get("/v1/profile/x", headers=self._headers())    # must still fetch
        assert len(calls) == 2

    def test_the_secret_never_appears_in_the_response(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        response = api.get("/v1/profile/x", headers=self._headers())
        assert SECRET not in response.text

    def test_the_secret_is_not_written_to_the_request_log(
        self, api, monkeypatch, tmp_path
    ):
        from app.linkedin import request_log

        log = tmp_path / "request-log.jsonl"
        monkeypatch.setattr(request_log, "LOG_PATH", log)
        request_log.record(
            method="GET", url="https://www.linkedin.com/x",
            headers={"csrf-token": "ajax:0000"},
            cookies={"li_at": SECRET, "JSESSIONID": "ajax:0000"},
            status=200, response_headers={}, body=b"0:[]",
        )
        written = log.read_text()
        assert SECRET not in written
        assert "sha256:" in written

    def test_the_response_says_it_was_not_cached(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        meta = api.get("/v1/profile/x", headers=self._headers()).json()["_meta"]
        assert any("not cached" in w for w in meta["warnings"])

    def test_health_advertises_the_escape_hatch(self, api):
        body = api.get("/health").json()
        assert body["caller_session_supported"] is True
        assert "X-LI-AT" in body["note"]


class TestTransparency:
    """Pre-seeding is disclosed at the API surface, not only in the README."""

    def test_cache_listing_reports_what_is_stored(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        api.get("/v1/profile/jordan-rivera", headers=HEADERS)
        body = api.get("/v1/cache", headers=HEADERS).json()
        assert body["count"] == 1
        assert body["entries"][0]["public_id"] == "jordan-rivera"

    def test_listing_never_returns_profile_bodies(self, api, monkeypatch):
        """Transparency must not itself become a bulk disclosure."""
        stub_fetch(monkeypatch)
        api.get("/v1/profile/jordan-rivera", headers=HEADERS)
        body = api.get("/v1/cache", headers=HEADERS).json()
        assert "Jordan Rivera" not in api.get("/v1/cache", headers=HEADERS).text
        assert set(body["entries"][0]) == {
            "public_id", "age_seconds", "pre_seeded", "seeded_at",
        }

    def test_listing_requires_the_api_key(self, api):
        assert api.get("/v1/cache").status_code == 401

    def test_a_profile_can_be_removed(self, api, monkeypatch):
        stub_fetch(monkeypatch)
        api.get("/v1/profile/jordan-rivera", headers=HEADERS)
        removed = api.delete("/v1/cache/jordan-rivera", headers=HEADERS)
        assert removed.status_code == 200
        assert removed.json()["removed"] is True
        assert api.get("/v1/cache", headers=HEADERS).json()["count"] == 0

    def test_removing_something_absent_is_a_404(self, api):
        response = api.delete("/v1/cache/never-cached", headers=HEADERS)
        assert response.status_code == 404
        assert response.json()["removed"] is False

    def test_removal_validates_the_identifier(self, api):
        response = api.delete("/v1/cache/https://example.com/x", headers=HEADERS)
        assert response.status_code in (400, 404)

    def test_a_seeded_response_says_so(self, api, monkeypatch):
        """A consumer should not have to guess whether data was pre-collected."""
        stub_fetch(monkeypatch)
        api.get("/v1/profile/jordan-rivera", headers=HEADERS)
        payload, _ = main.cache.get("jordan-rivera")
        payload["_meta"]["seeded_at"] = "2026-01-01T00:00:00+00:00"
        main.cache.put("jordan-rivera", payload)

        meta = api.get("/v1/profile/jordan-rivera", headers=HEADERS).json()["_meta"]
        assert any("pre-seeded" in w for w in meta["warnings"])


class TestDocs:
    def test_profile_responses_are_documented(self, api):
        """`/docs` must show what comes back, not just what goes in.

        The handlers return JSONResponse directly, so FastAPI cannot infer a
        schema and the spec showed an untyped body with only 422 documented.
        """
        op = api.get("/openapi.json").json()["paths"]["/v1/profile"]["get"]
        documented = set(op["responses"])
        assert {"200", "206", "400", "401", "403", "404", "429", "503"} <= documented
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("ProfileResponse")

    def test_no_duplicate_operation_ids(self, api):
        """Two methods on one api_route share an operation id, which breaks
        client generators."""
        spec = api.get("/openapi.json").json()
        ids = [
            op["operationId"]
            for path in spec["paths"].values()
            for op in path.values()
            if "operationId" in op
        ]
        assert len(ids) == len(set(ids)), "duplicate operationId in the spec"

    def test_openapi_renders(self, api):
        assert api.get("/docs").status_code == 200
        assert "/v1/profile" in api.get("/openapi.json").json()["paths"]

    def test_head_is_accepted_on_probe_routes(self, api):
        """Platform health probes use HEAD, not GET.

        Render probes `HEAD /`. A GET-only route answers 405, the platform
        reads that as unhealthy and cycles the instance — which presents as an
        service serving roughly half its requests, indistinguishable from a
        crash loop until you read the access log.
        """
        for path in ("/", "/health"):
            assert api.request("HEAD", path).status_code == 200, path

    def test_index_page_renders(self, api):
        response = api.get("/")
        assert response.status_code == 200
        assert "parse_confidence" in response.text
