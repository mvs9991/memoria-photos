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


def test_content_security_policy_is_sent_and_allows_the_pages_own_inline_script(app):
    import base64
    import hashlib
    import re

    from photointel.api.app import WEB_DIST

    c = TestClient(app)
    csp = c.get("/").headers.get("content-security-policy")
    assert csp and "default-src 'self'" in csp
    assert "object-src 'none'" in csp and "frame-ancestors 'self'" in csp
    # Scripts are this server's only, plus the page's own inline bootstrap by its hash — never a blanket
    # 'unsafe-inline', which would let injected script run.
    script_src = next(p for p in csp.split(";") if p.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src
    if WEB_DIST.exists():
        html = (WEB_DIST / "index.html").read_text(encoding="utf-8")
        for body in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S):
            want = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
            assert f"'sha256-{want}'" in script_src, "the inline script's hash is not in the policy"
    assert c.get("/api/stats").headers.get("content-security-policy") == csp


def test_wrong_guesses_from_one_device_do_not_lock_another_out(app):
    attacker = TestClient(app, client=("203.0.113.9", 5000))
    for i in range(10):
        assert attacker.post("/api/auth/login", json={"password": f"guess{i}"}).status_code == 401
    assert attacker.post("/api/auth/login", json={"password": "right-pass"}).status_code == 429   # still limited
    owner = TestClient(app, client=("192.168.1.50", 5000))
    assert owner.post("/api/auth/login", json={"password": "right-pass"}).status_code == 200
