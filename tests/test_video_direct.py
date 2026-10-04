"""HEVC videos stream as they are to browsers that can play them, and a transcode runs once per video.

On the library's computer an 8 s 4K HEVC clip took 76 s to transcode before the first frame could play,
and a player's parallel range requests each started their own transcode of the same file.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from photointel.pipeline.indexer import Indexer
from tests.test_media_and_organisation import make_video


@pytest.fixture
def setup(ctx, tmp_path):
    from photointel.api.app import create_app

    root = tmp_path / "lib"
    make_video(root / "clip.mp4", seconds=1.0)
    Indexer(ctx, workers=1, enable_faces=False, enable_semantic=False).run(roots=[str(root)])
    conn = ctx.connect()
    vid = conn.execute("SELECT id FROM photos WHERE media_type = 'video'").fetchone()[0]
    conn.execute("UPDATE photos SET video_codec = 'hevc' WHERE id = ?", (vid,))   # as an iPhone clip would be
    conn.commit()
    conn.close()
    return TestClient(create_app(ctx)), vid, (root / "clip.mp4").read_bytes()


def test_direct_streams_the_original_hevc_file(setup, monkeypatch):
    client, vid, original = setup
    from photointel import video

    monkeypatch.setattr(video, "transcode_to_mp4", lambda *a, **k: pytest.fail("transcoded although direct was asked for"))
    r = client.get(f"/api/photos/{vid}/video?direct=1")
    assert r.status_code == 200 and r.content == original
    part = client.get(f"/api/photos/{vid}/video?direct=1", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100


def test_without_direct_hevc_is_transcoded_and_only_once(setup, monkeypatch):
    client, vid, _ = setup
    from photointel import video

    calls = []

    def slow_transcode(src, dest, max_side=1280):
        calls.append(1)
        time.sleep(0.6)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        return dest

    monkeypatch.setattr(video, "transcode_to_mp4", slow_transcode)
    results = []
    threads = [threading.Thread(target=lambda: results.append(client.get(f"/api/photos/{vid}/video").status_code))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [200] * 4
    assert len(calls) == 1, f"{len(calls)} transcodes of one video ran at once"
