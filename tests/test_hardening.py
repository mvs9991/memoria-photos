"""Security headers on every response, and the single-password login cannot be locked by one device."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from photointel import ratelimit
from photointel.auth import hash_password


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app

    ctx.settings.access_password_hash = hash_password("right-pass")
    ratelimit.reset_all()
    return create_app(ctx)


def test_every_response_carries_the_security_headers(app):
    c = TestClient(app)
    for path in ("/", "/api/stats", "/api/auth/status", "/nope.js"):
        h = c.get(path).headers
        assert h.get("x-content-type-options") == "nosniff", path
        assert h.get("x-frame-options") == "SAMEORIGIN", path
        assert h.get("referrer-policy") == "same-origin", path


def test_wrong_guesses_from_one_device_do_not_lock_another_out(app):
    attacker = TestClient(app, client=("203.0.113.9", 5000))
    for i in range(10):
        assert attacker.post("/api/auth/login", json={"password": f"guess{i}"}).status_code == 401
    assert attacker.post("/api/auth/login", json={"password": "right-pass"}).status_code == 429   # still limited
    owner = TestClient(app, client=("192.168.1.50", 5000))
    assert owner.post("/api/auth/login", json={"password": "right-pass"}).status_code == 200
