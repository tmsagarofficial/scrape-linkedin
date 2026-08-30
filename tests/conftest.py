"""Shared test configuration.

AGENTS.md §8 requires the suite to pass with no credentials and no network.
That is easy to state and easy to violate by accident: a test that stubs one
code path while a second path quietly constructs its own HTTP client will reach
out to LinkedIn for real, and the only symptom is a slow test run.

That happened — a caller-session test built a client the stub did not cover and
spent two minutes retrying against LinkedIn with a fake cookie. This file turns
the requirement into something enforced rather than remembered.
"""

from __future__ import annotations

import pytest


class NetworkAccessAttempted(AssertionError):
    """Raised when a test tries to make a real HTTP request."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail loudly if any test reaches the network.

    Patched at the `curl_cffi` boundary, which is the only way this application
    talks to LinkedIn. A test that needs to exercise request behaviour should
    stub at the client or `_client_for` level instead.
    """
    try:
        from curl_cffi import requests as cffi
    except ImportError:  # pragma: no cover - dependency is declared
        return

    def blocked(*args, **kwargs):
        target = args[0] if args else kwargs.get("url", "<unknown>")
        raise NetworkAccessAttempted(
            f"a test attempted a real HTTP request to {target}.\n"
            "The suite must run offline (AGENTS.md §8). Stub the client, or "
            "app.main._client_for, rather than letting a real one be built."
        )

    for name in ("get", "post", "put", "delete", "head", "request"):
        if hasattr(cffi, name):
            monkeypatch.setattr(cffi, name, blocked)
    monkeypatch.setattr(cffi.Session, "request", blocked, raising=False)
