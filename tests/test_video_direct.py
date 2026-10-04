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


@pytest.mark.parametrize("size,rotation,expected", [
    ((320, 240), 90, (240, 320)),          # a portrait phone clip stored sideways comes out upright
    ((2000, 1000), 0, (1280, 640)),        # a large landscape clip is shrunk to the preview size
    ((1000, 2000), 270, (1280, 640)),
])
def test_the_transcode_is_upright_and_small(tmp_path, size, rotation, expected):
    import av

    from photointel.video import transcode_to_mp4

    src = make_video(tmp_path / "in.mov", size=size, rotation=rotation, codec="mpeg4", seconds=0.5)
    out = transcode_to_mp4(src, tmp_path / "out" / "x.mp4")
    with av.open(str(out)) as c:
        s = c.streams.video[0]
        assert (s.codec_context.width, s.codec_context.height) == expected
        assert s.codec_context.name == "h264"
    assert not list((tmp_path / "out").glob("*.part.mp4")), "a temporary file was left behind"
