"""Closing the Locked folder must end the access, even for a copy of the cookie.

"Close" used to delete the cookie in the browser and nothing else. A copy made while the folder was
open (a script, a second device, a saved request) kept opening it for the rest of its 15 minutes,
which defeats the point of pressing Close on a shared or borrowed computer.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from photointel import auth


@pytest.fixture
def app(ctx):
    from photointel.api.app import create_app

    return create_app(ctx)


def browser(app) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def open_folder(client: TestClient) -> str:
    r = client.post("/api/locked/open", json={"pin": "2468"})
    assert r.status_code == 200, r.text
    token = client.cookies.get(auth.LOCKED_COOKIE)
    assert token
    return token


@pytest.fixture
def pin_set(app):
    assert browser(app).post("/api/locked/pin", json={"new": "2468"}).status_code == 200


def can_open(app, token: str) -> bool:
    """Does a brand-new client presenting only this cookie get into the folder?"""
    c = browser(app)
    c.cookies.set(auth.LOCKED_COOKIE, token)
    return c.get("/api/locked/photos").status_code == 200


def test_a_copied_cookie_stops_working_when_the_folder_is_closed(app, pin_set):
    owner = browser(app)
    token = open_folder(owner)
    assert can_open(app, token), "sanity: the token works before it is closed"
    assert owner.post("/api/locked/close").status_code == 200
    assert not can_open(app, token), "a copy of the cookie still opened the folder after Close"


def test_closing_one_browser_does_not_close_another(app, pin_set):
    """Tokens are per browser: pressing Close on the desktop must not slam the phone's folder shut."""
    desktop, phone = browser(app), browser(app)
    t_desktop = open_folder(desktop)
    time.sleep(0.01)                                   # tokens carry the millisecond they were issued
    t_phone = open_folder(phone)
    assert t_desktop != t_phone
    desktop.post("/api/locked/close")
    assert not can_open(app, t_desktop)
    assert can_open(app, t_phone)


def test_the_folder_can_be_opened_again_after_closing(app, pin_set):
    owner = browser(app)
    old = open_folder(owner)
    owner.post("/api/locked/close")
    time.sleep(0.01)
    new = open_folder(owner)
    assert new != old
    assert can_open(app, new) and not can_open(app, old)


def test_closing_without_a_valid_cookie_records_nothing(ctx, app, pin_set):
    """Garbage must not grow the revocation list."""
    c = browser(app)
    c.cookies.set(auth.LOCKED_COOKIE, "locked:1.deadbeef")
    assert c.post("/api/locked/close").status_code == 200
    assert not (ctx.paths.data / "locked.revoked").exists()
    assert browser(app).post("/api/locked/close").status_code == 200       # no cookie at all
    assert not (ctx.paths.data / "locked.revoked").exists()


def test_revoking_twice_does_not_duplicate(ctx, app, pin_set):
    owner = browser(app)
    token = open_folder(owner)
    owner.post("/api/locked/close")
    auth.revoke_locked_token(ctx.paths.data, token)                       # already dead: a no-op
    lines = (ctx.paths.data / "locked.revoked").read_text().splitlines()
    assert len(lines) == 1


def test_old_revocations_are_dropped_once_the_token_would_have_expired(ctx, monkeypatch):
    data = ctx.paths.data
    path = data / "locked.revoked"
    long_ago = int((time.time() - (auth.LOCKED_MINUTES * 60 + 600)) * 1000)
    path.write_text(f"{long_ago} abc123\n", encoding="utf-8")
    token = auth.make_locked_token(data)
    auth.revoke_locked_token(data, token)
    lines = path.read_text().splitlines()
    assert len(lines) == 1 and "abc123" not in lines[0], "an entry for an expired token should have been pruned"
    assert not auth.valid_locked_token(data, token)


def test_changing_the_pin_still_ends_every_token(ctx, app, pin_set):
    """The earlier guarantee must survive: a new PIN invalidates tokens issued before it."""
    owner = browser(app)
    token = open_folder(owner)
    time.sleep(0.01)
    assert owner.post("/api/locked/pin", json={"current": "2468", "new": "1357"}).status_code == 200
    assert not can_open(app, token)
