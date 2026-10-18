"""Shared pytest fixtures for the test suite."""

import pytest

from app.ratelimit import rate_limiter
from app.routers.web import login_limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Keep per-IP limits from leaking across tests that share a client IP."""
    rate_limiter.reset()
    login_limiter.reset()
    yield
    rate_limiter.reset()
    login_limiter.reset()
