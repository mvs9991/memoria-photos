"""The optional Claude layer: off by default, privacy-scoped, and never trusted blindly."""
import json
from types import SimpleNamespace

import pytest

from photointel.engine import people as people_mod
from photointel.pipeline.indexer import Indexer
from photointel.pipeline.post import run_post_stages
from photointel.search import llm as llm_mod
from photointel.search.engine import SearchEngine


@pytest.fixture
def ready(ctx, library):
    Indexer(ctx, workers=2).run(roots=[str(library)])
    conn = ctx.connect()
    run_post_stages(ctx, conn)
    pid = conn.execute("SELECT id FROM persons WHERE face_count > 0 ORDER BY face_count DESC").fetchone()[0]
    people_mod.rename_person(conn, pid, "Ghat")
    return ctx, conn, pid


class FakeClient:
    """Stands in for anthropic.Anthropic; records what would have been sent."""

    def __init__(self, payload: dict, stop_reason: str = "end_turn"):
        self.payload = payload
        self.stop_reason = stop_reason
        self.sent: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
            stop_reason=self.stop_reason,
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        )


def test_llm_disabled_by_default(ready):
    ctx, conn, pid = ready
    assert ctx.settings.llm_enabled is False
    assert llm_mod.llm_parse(ctx, conn, "anything") is None
    assert llm_mod.llm_available(ctx) is False


def test_llm_parse_maps_names_to_ids(ready, monkeypatch):
    ctx, conn, pid = ready
    ctx.settings.llm_enabled = True
    ctx.settings.anthropic_api_key = "test-key"
    fake = FakeClient({
        "people_all": ["Ghat"], "people_any": [], "places": [], "events": [], "tags": ["beach"],
        "date_from": "2024-01-01", "date_to": "2024-12-31", "visual_query": "sand and sea",
        "result_type": "photos", "sort": "relevance", "only_favorites": False,
        "only_screenshots": False, "trips_only": False, "reasoning": "Looked for Ghat at the seaside in 2024.",
    })
    monkeypatch.setattr(llm_mod, "_client", lambda _ctx: fake)
    q = llm_mod.llm_parse(ctx, conn, "that beach day with Ghat a couple of years back")
    assert q is not None
    assert q.persons_all == [pid]
    assert q.semantic_text == "sand and sea"
    assert q.source == "llm"
    assert any(c["kind"] == "ai" for c in q.interpretation)


def test_llm_never_invents_people(ready, monkeypatch):
    ctx, conn, pid = ready
    ctx.settings.llm_enabled = True
    ctx.settings.anthropic_api_key = "test-key"
    fake = FakeClient({
        "people_all": ["Someone Who Does Not Exist"], "people_any": [], "places": ["Atlantis"],
        "events": [], "tags": [], "date_from": None, "date_to": None, "visual_query": None,
        "result_type": "photos", "sort": "date_desc", "only_favorites": False,
        "only_screenshots": False, "trips_only": False, "reasoning": "x",
    })
    monkeypatch.setattr(llm_mod, "_client", lambda _ctx: fake)
    q = llm_mod.llm_parse(ctx, conn, "photos of someone")
    assert q.persons_all == []        # unknown names are dropped, not guessed at
    assert q.place_ids == []


def test_llm_payload_contains_no_photo_data(ready, monkeypatch):
    ctx, conn, pid = ready
    ctx.settings.llm_enabled = True
    ctx.settings.anthropic_api_key = "test-key"
    fake = FakeClient({
        "people_all": [], "people_any": [], "places": [], "events": [], "tags": [],
        "date_from": None, "date_to": None, "visual_query": "beach", "result_type": "photos",
        "sort": "relevance", "only_favorites": False, "only_screenshots": False,
        "trips_only": False, "reasoning": "x",
    })
    monkeypatch.setattr(llm_mod, "_client", lambda _ctx: fake)
    llm_mod.llm_parse(ctx, conn, "beach photos")
    sent = json.dumps(fake.sent[0], default=str)
    for leak in (".jpg", ".png", "DCIM", "sha256", "gps", str(ctx.paths.data)):
        assert leak.lower() not in sent.lower(), f"{leak} must not be sent to the LLM"
    # no image content blocks
    assert "base64" not in sent and '"image"' not in sent


def test_llm_refusal_is_handled(ready, monkeypatch):
    ctx, conn, pid = ready
    ctx.settings.llm_enabled = True
    ctx.settings.anthropic_api_key = "test-key"
    monkeypatch.setattr(llm_mod, "_client", lambda _ctx: FakeClient({}, stop_reason="refusal"))
    assert llm_mod.llm_parse(ctx, conn, "something") is None


def test_llm_failure_falls_back_to_rules(ready, monkeypatch):
    ctx, conn, pid = ready
    ctx.settings.llm_enabled = True
    ctx.settings.anthropic_api_key = "test-key"

    def boom(_ctx):
        raise RuntimeError("network down")

    monkeypatch.setattr(llm_mod, "_client", boom)
    res = SearchEngine(ctx).search(conn, "photos of Ghat")   # rule parser still answers
    assert res.total > 0


def test_llm_summary_falls_back_when_disabled(ready):
    ctx, conn, pid = ready
    ev = conn.execute("SELECT id FROM events LIMIT 1").fetchone()
    if ev is None:
        pytest.skip("no events in fixture library")
    text = llm_mod.llm_event_summary(ctx, conn, ev[0])
    assert isinstance(text, str) and text


def test_date_conversion():
    assert llm_mod._iso_to_ts(None) is None
    assert llm_mod._iso_to_ts("not a date") is None
    start = llm_mod._iso_to_ts("2024-05-17")
    end = llm_mod._iso_to_ts("2024-05-17", end_of_day=True)
    assert end - start == pytest.approx(86399, abs=1)
    year_end = llm_mod._iso_to_ts("2024", end_of_day=True)
    assert year_end > llm_mod._iso_to_ts("2024-12-01")
