"""A search must not fail when the stored photo embeddings and the loaded model disagree in size.

That happens when the model files change (a different model, or a server started without the folder that
holds the usual one) while the library still holds the old embeddings: every search answered 500 with
"matmul: ... size 768 is different from 16". Seen in the load-test library after a run with other models.
It now logs a warning and answers without the visual ranking until the photos are re-analysed.
"""
from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer


@pytest.fixture
def client(ctx, library):
    from photointel.api.app import create_app

    Indexer(ctx, workers=2).run(roots=[str(library)])
    return TestClient(create_app(ctx))


def test_a_model_of_another_size_does_not_break_search(ctx, client, monkeypatch):
    assert client.get("/api/search?q=beach").status_code == 200
    model = ctx.test_semantic_model
    real = model.encode_texts
    monkeypatch.setattr(model, "encode_texts", lambda texts: np.hstack([real(texts), np.ones((len(texts), 5), np.float32)]))
    for q in ("beach", "a dog", "2024"):
        r = client.get(f"/api/search?q={q}")
        assert r.status_code == 200, f"{q!r}: {r.status_code} {r.text[:200]}"
