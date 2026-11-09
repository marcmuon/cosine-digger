"""Discogs caching and pacing through the public client and chain entry points."""

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import httpx
import pytest

from cosine_digger import chain, discogs, storage
from cosine_digger.discogs import _request
from cosine_digger.httpcache import DbHttpCache, MemoryHttpCache
from cosine_digger.models import SimilarTracksData, Track

URL = "https://api.discogs.com/releases/1"
KEY = "discogs:" + URL


@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 100.0

        def __init__(self):
            self.sleeps = []

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(discogs.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(discogs.time, "sleep", clock.sleep)
    return clock


def mock_network(monkeypatch, handler):
    monkeypatch.setattr(chain.httpx, "HTTPTransport", lambda: httpx.MockTransport(handler))


def test_miss_stores_streamed_body_and_hit_skips_budget_and_wait(monkeypatch, clock):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'{"title":"fixture"}'))

    mock_network(monkeypatch, send)
    cache = MemoryHttpCache()
    client, tracker = chain.make_rate_limited_client(budget=1, http_cache=cache)
    with client:
        first = _request(client, URL, {"page": 2}, "private-token", None)
        second = _request(client, URL, {"page": 2}, "private-token", None)
        assert first == second == {"title": "fixture"}
        assert cache.get(KEY + "?page=2") == '{"title":"fixture"}'
        assert cache.get(KEY) is None
        with pytest.raises(chain.BudgetExhausted):
            client.get(URL)
    assert len(calls) == 1
    assert calls[0].headers["Authorization"] == "Discogs token=private-token"
    assert tracker.remaining == 0
    assert clock.sleeps == []


@pytest.mark.parametrize("status", [404, 500])
def test_errors_are_not_cached(monkeypatch, clock, status):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(status, json={"message": "error"})

    mock_network(monkeypatch, send)
    cache = MemoryHttpCache()
    client, tracker = chain.make_rate_limited_client(budget=2, http_cache=cache)
    with client:
        assert client.get(URL).status_code == status
        assert client.get(URL).status_code == status
    assert len(calls) == 2
    assert cache.get(KEY) is None
    assert tracker.remaining == 0


@pytest.mark.parametrize("retry_status", [200, 429])
def test_429_retries_once_and_paces_from_retry(monkeypatch, clock, retry_status):
    responses = []

    def send(request):
        response = httpx.Response(429 if not responses else retry_status, json={})
        responses.append(response)
        return response

    mock_network(monkeypatch, send)
    cache = MemoryHttpCache()
    client, tracker = chain.make_rate_limited_client(budget=2, http_cache=cache)
    with client:
        assert client.get(URL).status_code == retry_status
        assert len(responses) == 2
        assert tracker.remaining == 0
        assert clock.sleeps == [5.0]
        assert all(response.is_closed for response in responses)
        assert cache.get(KEY) == ("{}" if retry_status == 200 else None)


def test_pacing_counts_request_duration_and_ignores_hits(monkeypatch, clock):
    def send(request):
        clock.now += 0.4
        return httpx.Response(200, json={})

    mock_network(monkeypatch, send)
    cache = MemoryHttpCache()
    client, _ = chain.make_rate_limited_client(http_cache=cache)
    with client:
        client.get(URL)
        assert clock.sleeps == []
        clock.now += 0.2
        client.get(URL)  # A cache hit must not move the pacing timestamp.
        client.get(URL + "?page=2")
        assert clock.sleeps == pytest.approx([0.5])
        clock.now += 2
        client.get(URL + "?page=3")
        assert clock.sleeps == pytest.approx([0.5])


def test_no_cache_still_paces_from_last_real_request(monkeypatch, clock):
    mock_network(monkeypatch, lambda request: httpx.Response(200, json={}))
    client, tracker = chain.make_rate_limited_client(budget=2)
    with client:
        client.get(URL)
        assert clock.sleeps == []
        client.get(URL)
    assert clock.sleeps == pytest.approx([1.1])
    assert tracker.remaining == 0


def test_broken_cache_does_not_break_requests(monkeypatch, clock, caplog):
    class BrokenCache:
        def get(self, key):
            raise RuntimeError("unavailable")

        def put(self, key, body, ttl_s):
            raise sqlite3.OperationalError("locked")

    mock_network(monkeypatch, lambda request: httpx.Response(200, json={"ok": True}))
    client, tracker = chain.make_rate_limited_client(budget=1, http_cache=BrokenCache())
    with client:
        assert client.get(URL).json() == {"ok": True}
    assert tracker.remaining == 0
    assert "HTTP cache read failed" in caplog.text
    assert "HTTP cache write failed" in caplog.text


def test_db_round_trip_expiry_and_cross_thread_access(tmp_path, monkeypatch):
    path = tmp_path / "cache.sqlite"
    now = 1000.0
    monkeypatch.setattr(storage.time, "time", lambda: now)
    cache = DbHttpCache(path)
    try:
        with ThreadPoolExecutor(max_workers=2) as callers:
            callers.submit(cache.put, KEY, '{"ok":true}', 10).result()
            assert callers.submit(cache.get, KEY).result() == '{"ok":true}'
        conn = storage.connect(path)
        try:
            assert storage.cache_get(conn, KEY, now=1009) == '{"ok":true}'
            assert storage.cache_get(conn, KEY, now=1010) is None
        finally:
            conn.close()
        now = 1010.0
        assert cache.get(KEY) is None
    finally:
        cache.close()
    cache.close()


def test_repeat_chain_dig_uses_persistent_cache_with_zero_budget(tmp_path, monkeypatch, clock):
    class FakeCosine:
        def similar(self, track_id, **kwargs):
            return SimilarTracksData(
                source_track=Track(id=track_id, name="Seed", artist="Seed", track="Seed"),
                similar_tracks=[Track(
                    id="discovery", name="Discovery", artist="Artist", track="Discovery",
                    score=0.9, external_link="https://www.discogs.com/release/1",
                )],
            )

    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(200, json={})

    mock_network(monkeypatch, send)
    path = tmp_path / "chain.sqlite"
    events = []
    for budget in (1, 0):
        cache = DbHttpCache(path)
        try:
            results = chain.chain_dig(
                ["seed"], FakeCosine(), max_discogs_calls=budget, http_cache=cache,
                on_progress=lambda event, data: events.append(event),
            )
            assert [result.track.id for result in results] == ["discovery"]
        finally:
            cache.close()
    assert len(calls) == 1
    assert "budget_exhausted" not in events
    assert clock.sleeps == []
    conn = storage.connect(path)
    try:
        assert conn.execute("SELECT ttl_s FROM http_cache WHERE key = ?", (KEY,)).fetchone() == (604800,)
    finally:
        conn.close()


def test_configured_discogs_key_wins_over_a_stale_environment_key():
    import httpx

    from cosine_digger.chain import _RetryOn429Transport
    seen = []

    def handler(request):
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200, json={})
    with httpx.Client(transport=_RetryOn429Transport(httpx.MockTransport(handler), token="CURRENT")) as client:
        client.get("https://api.discogs.com/labels/1", headers={"Authorization": "Discogs token=STALE"})
    assert seen == ["Discogs token=CURRENT"]


def test_parallel_discogs_requests_share_one_pace(monkeypatch):
    """Parallel album seeds (no client) and a chain client in the same process
    still take one Discogs slot each, PACE_S apart."""
    import threading
    import time

    monkeypatch.setattr(discogs, "PACE_S", 0.1)
    sent, lock = [], threading.Lock()

    def record(request):
        with lock:
            sent.append(time.monotonic())
        return httpx.Response(200, json={})

    monkeypatch.setattr(discogs.httpx, "get", lambda url, **kw: record(None))
    mock_network(monkeypatch, record)
    client, _ = chain.make_rate_limited_client(budget=10)
    with client, ThreadPoolExecutor(max_workers=8) as pool:
        jobs = [pool.submit(_request, None, f"{URL}?seed={i}", None, None, None) for i in range(4)]
        jobs += [pool.submit(client.get, f"{URL}?hop={i}") for i in range(4)]
        for job in jobs:
            job.result()
    sent.sort()
    gaps = [b - a for a, b in zip(sent, sent[1:])]
    # Unpaced, all eight go out together; paced, they span seven slots. The
    # gap bound leaves room for thread wake-up jitter.
    assert len(sent) == 8 and sent[-1] - sent[0] >= 0.65 and min(gaps) >= 0.03
