"""The key that signs sessions is created once, whole, and the same for everyone who asks.

It used to be `if not exists: write 32 random bytes`, then read the file. Two logins at the same moment
(a phone and a computer) could each write their own key, so one device's session was signed with a key
that was replaced at once (logged straight out again), or read the file while it was still empty and sign
a session with an empty key, which anyone could forge.
"""
from __future__ import annotations

import threading

from photointel import auth


def test_concurrent_first_use_yields_one_whole_key(tmp_path):
    for round_ in range(15):
        data = tmp_path / f"lib{round_}"
        data.mkdir()
        auth._secret_cache.clear()
        start = threading.Barrier(24)
        got: list[bytes] = []
        lock = threading.Lock()

        def ask():
            start.wait()
            k = auth._secret(data)
            with lock:
                got.append(k)

        threads = [threading.Thread(target=ask) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert {len(k) for k in got} == {32}, f"a key was read before it was written: {sorted({len(k) for k in got})}"
        assert len(set(got)) == 1, f"{len(set(got))} different keys were handed out"
        assert (data / "secret.key").read_bytes() == got[0]


def test_a_session_survives_a_restart(tmp_path):
    token = auth.make_session(tmp_path)
    auth._secret_cache.clear()                      # a new server process reads the key from disk
    assert auth.valid_session(tmp_path, token)
