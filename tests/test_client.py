from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

import cosine_digger.client as client_module
from cosine_digger.client import normalize_lookup_url
from cosine_digger.client import CosineClient, CosineHTTPError


@pytest.mark.parametrize(
    ("input_url", "expected"),
    [
        (
            "https://www.discogs.com/release/10903346-Tolouse-Low-Trax-Jeidem-Fall?srsltid=abc",
            "https://www.discogs.com/release/10903346-Tolouse-Low-Trax-Jeidem-Fall",
        ),
        (
            "https://discogs.com/release/10903346",
            "https://www.discogs.com/release/10903346",
        ),
        (
            "https://www.discogs.com/sell/release/10903346",
            "https://www.discogs.com/release/10903346",
        ),
        (
            "https://www.discogs.com/master/12345-something",
            "https://www.discogs.com/master/12345-something",
        ),
        (
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ&feature=share",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://youtu.be/dQw4w9WgXcQ?t=42",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        (
            "https://m.soundcloud.com/artist/track?utm_source=share",
            "https://soundcloud.com/artist/track",
        ),
        (
            "https://soundcloud.com/artist/track/",
            "https://soundcloud.com/artist/track",
        ),
    ],
)
def test_normalize_lookup_url(input_url: str, expected: str) -> None:
    assert normalize_lookup_url(input_url) == expected


def test_normalize_lookup_url_passthrough() -> None:
    url = "https://example.com/artist/track"
    assert normalize_lookup_url(url) == url


@pytest.mark.parametrize("url", [
    "https://notyoutube.com/watch?v=abc",
    "https://youtube.com.example.org/watch?v=abc",
    "https://notdiscogs.com/release/123",
    "https://notsoundcloud.com/artist/track?key=value",
])
def test_normalization_preserves_unrelated_domains(url):
    assert normalize_lookup_url(url) == url


def test_similar_accepts_null_optional_pagination_metadata():
    client = object.__new__(CosineClient)
    client._get = lambda *args, **kwargs: {
        "data": {
            "source_track": {"id": "seed", "name": "Seed", "artist": "Artist", "track": "Seed"},
            "similar_tracks": [{"id": "result", "name": "Result", "artist": "Other", "track": "Result"}],
        },
        "meta": None,
    }
    assert [track.id for track in client.similar("seed").similar_tracks] == ["result"]


@pytest.mark.parametrize("limit", [0])
def test_similar_rejects_nonpositive_limits_before_network(limit):
    client = object.__new__(CosineClient)
    with pytest.raises(ValueError, match="positive"):
        client.similar("seed", limit=limit)


@pytest.mark.parametrize("limit,expected_offsets", [(5, [0, 2, 4]), (10, [0, 2, 4, 5])])
def test_similar_short_pages_do_not_skip_tracks_and_empty_page_stops(limit, expected_offsets):
    import httpx

    tracks = [{"id": str(i), "name": f"Track {i}", "artist": "Artist", "track": f"Track {i}"}
              for i in range(5)]
    offsets = []

    def respond(request):
        assert request.url.path == "/api/v1/tracks/seed/similar"
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        assert len(offsets) <= 4, "pagination did not terminate"
        size = min(2, int(request.url.params["limit"]))
        return httpx.Response(200, json={
            "data": {"source_track": tracks[0], "similar_tracks": tracks[offset:offset + size]},
            "meta": {"has_more": True},
        })

    with CosineClient(token="test-token") as client:
        client._client.close()
        client._client = httpx.Client(
            base_url="https://cosine.club/api/v1", transport=httpx.MockTransport(respond),
        )
        result = client.similar("seed", limit=limit)
    assert [track.id for track in result.similar_tracks] == [str(i) for i in range(5)]
    assert offsets == expected_offsets


@pytest.mark.parametrize("yes", [False, True])
@pytest.mark.parametrize("stdin_tty,stdout_tty", [(False, False)])
def test_saved_clear_requires_yes_when_either_stream_is_not_tty(
    monkeypatch, tmp_path, yes, stdin_tty, stdout_tty,
):
    import sqlite3
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from cosine_digger import cli
    from cosine_digger.taste import _SESSION_SCHEMA

    db_path = tmp_path / "taste.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_SESSION_SCHEMA)
        conn.execute("INSERT INTO sessions (id, name, seeds, created_at, updated_at) "
                     "VALUES ('session', 'Test', '[]', '2026-09-29', '2026-09-29')")
        for i, event in enumerate(["save", "dig_from", "pass"], 1):
            conn.execute("INSERT INTO session_results "
                         "(id, session_id, track_id, hop, discovered_at) VALUES (?, 'session', ?, 1, '2026-09-29')",
                         (i, str(i)))
            conn.execute("INSERT INTO result_events (result_id, event_type, created_at) VALUES (?, ?, '2026-09-29')",
                         (i, event))

    monkeypatch.setenv("COSINE_DIGGER_DB", str(db_path))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(
        stdin=SimpleNamespace(isatty=lambda: stdin_tty),
        stdout=SimpleNamespace(isatty=lambda: stdout_tty),
    ))
    result = CliRunner().invoke(cli.app, ["saved", "--clear"] + (["--yes"] if yes else []))
    output = result.output
    assert result.exit_code == (0 if yes else 1), output
    assert ("Cleared 2 saved tracks" if yes else "Would clear 2 saved tracks") in output
    if not yes:
        assert "--yes" in output
    with sqlite3.connect(db_path) as conn:
        events = [row[0] for row in conn.execute("SELECT event_type FROM result_events ORDER BY result_id")]
    assert events == (["pass"] if yes else ["save", "dig_from", "pass"])


def _mock_client(respond):
    client = CosineClient(token="test-token")
    client._client.close()
    client._client = httpx.Client(base_url="https://cosine.club/api/v1", transport=httpx.MockTransport(respond))
    return client


@pytest.fixture
def rate_clock(monkeypatch):
    clock = SimpleNamespace(now=2_000_000_000.0, waits=[])

    def sleep(seconds):
        clock.waits.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(client_module, "time", SimpleNamespace(time=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(client_module, "_rate_limit_state", (None, None), raising=False)
    return clock


def test_long_rate_limit_reset_fails_after_one_request(rate_clock):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={
            "x-ratelimit-remaining": "0", "x-ratelimit-reset": "2000000047",
        }, json={"code": "RATE_LIMITED"})

    with _mock_client(respond) as client, pytest.raises(CosineHTTPError) as failure:
        client.search("Seed - Track")
    assert calls == ["/api/v1/search"]
    assert failure.value.retry_after_s == pytest.approx(47)
    assert rate_clock.waits == []


def test_short_retry_after_keeps_two_retries(rate_clock):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "2"}, json={"code": "RATE_LIMITED"})

    with _mock_client(respond) as client, pytest.raises(CosineHTTPError) as failure:
        client.search("Seed - Track")
    assert len(calls) == 3
    assert failure.value.retry_after_s == 2
    assert rate_clock.waits == [2, 2]


@pytest.mark.parametrize("first_method", ["GET"])
@pytest.mark.parametrize("remaining,delay,wait", [(1, 3, 3), (1, 50, 0), (2, 10, 10), (3, 3, 0)])
def test_low_budget_wait_is_shared_between_clients(rate_clock, first_method, remaining, delay, wait):
    calls = []

    def respond(request):
        calls.append((request.method, rate_clock.now))
        headers = {"x-ratelimit-remaining": str(remaining),
                   "x-ratelimit-reset": str(2_000_000_000 + delay)} if len(calls) == 1 else {}
        return (httpx.Response(204, headers=headers) if request.method == "POST"
                else httpx.Response(200, headers=headers, json={"data": []}))

    with _mock_client(respond) as first, _mock_client(respond) as second:
        if first_method == "GET":
            first.search("First")
        else:
            first.add_track_to_playlist("playlist", "track")
        second.search("Next")
    assert calls == [(first_method, 2_000_000_000), ("GET", 2_000_000_000 + wait)]
    assert rate_clock.waits == ([wait] if wait else [])


@pytest.mark.parametrize("delay", [10, 50])
def test_short_resets_keep_retries_and_long_resets_stop_even_with_retry_after(rate_clock, delay):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "2",
            "x-ratelimit-reset": str(2_000_000_000 + delay)}, json={})

    with _mock_client(respond) as client, pytest.raises(CosineHTTPError) as failure:
        client.search("Seed - Track")
    assert len(calls) == (3 if delay == 10 else 1)
    assert rate_clock.waits == ([2, 2] if delay == 10 else [])
    assert failure.value.retry_after_s == 2


@pytest.mark.parametrize("status,headers,expected", [
    (429, {"Retry-After": "2", "x-ratelimit-reset": "2000000047"}, 2),
    (429, {"Retry-After": format_datetime(datetime.fromtimestamp(2_000_000_047, timezone.utc), usegmt=True)}, 47),
    (429, {"x-ratelimit-reset": "2000000047.2"}, 47.2),
    (429, {"Retry-After": "bad-date", "x-ratelimit-reset": "2000000047"}, 47),
    (429, {"Retry-After": "NaN", "x-ratelimit-reset": "2000000047"}, 47),
    (429, {"Retry-After": "-2"}, 1),
    (429, {"Retry-After": "0"}, 1),
    (429, {"Retry-After": "200"}, 120),
    (429, {"x-ratelimit-reset": "1999999999"}, 1),
    (429, {"x-ratelimit-reset": "2000000200"}, 120),
    (429, {}, None),
    (429, {"Retry-After": "invalid", "x-ratelimit-reset": "inf"}, None),
    (503, {"Retry-After": "2"}, 2),
    (503, {"Retry-After": format_datetime(datetime.fromtimestamp(2_000_000_047, timezone.utc), usegmt=True)}, 47),
    (503, {"x-ratelimit-reset": "2000000047"}, 47),
    (503, {}, None),
    (502, {"Retry-After": "2"}, None),
])
def test_retry_countdown_parses_headers_and_bounds_valid_delays(rate_clock, status, headers, expected):
    with _mock_client(lambda request: httpx.Response(status, headers=headers, json={})) as client:
        with pytest.raises(CosineHTTPError) as failure:
            client.add_track_to_playlist("playlist", "track")
    assert failure.value.retry_after_s == (pytest.approx(expected) if expected is not None else None)
    assert rate_clock.waits == []


def test_a_cosine_rate_limit_is_retried_then_named_when_it_persists(monkeypatch):
    """Seen live: right after a weekly run, seeds came back "not resolved"
    because a 429 was swallowed. Now it is retried, and named if it lasts."""
    from cosine_digger import api
    import cosine_digger.client as client_module

    waits = []
    monkeypatch.setattr(client_module.time, "sleep", waits.append)
    track = {"id": "t1", "name": "Ranta", "artist": "Vladislav Delay", "track": "Ranta"}
    calls = []

    def burst(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={})
        if request.url.path.endswith("/similar"):
            return httpx.Response(200, json={"data": {"source_track": track, "similar_tracks": []},
                                             "meta": {"has_more": False}})
        return httpx.Response(200, json={"data": [track]})

    with _mock_client(burst) as client:
        out = api.quick(["Vladislav Delay - Ranta"], client=client, export_playlist=False, record=False)
    assert out.get("error") is None and out["seeds"][0]["ok"] and waits == [2.0]

    waits.clear()
    with _mock_client(lambda request: httpx.Response(429, json={})) as client:
        out = api.quick(["Vladislav Delay - Ranta"], client=client, export_playlist=False, record=False)
    assert waits == [1.0, 3.0]
    assert out["error"] == "No seeds could be resolved: cosine.club didn't answer (429); try again in a minute"


def _chain(exc):
    while exc is not None:
        yield exc
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)


def test_a_key_that_cannot_be_a_header_never_reaches_output_or_tracebacks(monkeypatch, tmp_path):
    """Seen by review: a token with a line break inside makes httpx quote the
    whole Authorization header in its error, and the CLI printed the chain."""
    from typer.testing import CliRunner

    from cosine_digger.cli import app
    from cosine_digger.client import CosineAuthError, CosineRequestError

    with pytest.raises(CosineAuthError) as refused:
        CosineClient(token="SYNTH\nKEY")
    assert all("SYNTH" not in str(e) for e in _chain(refused.value))

    def quoting(request):  # h11's words for a header it refuses
        raise httpx.LocalProtocolError("Illegal header value b'Bearer OTHER-KEY\\n'")
    with CosineClient(token="fine") as client:
        client._client = httpx.Client(base_url="https://cosine.test", transport=httpx.MockTransport(quoting))
        with pytest.raises(CosineRequestError) as failed:
            client.search("x")
    assert all("OTHER-KEY" not in str(e) for e in _chain(failed.value))

    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setenv("COSINE_DIGGER_DB", str(tmp_path / "taste.db"))
    monkeypatch.setenv("COSINE_API_TOKEN", "SYNTH\nKEY")
    result = CliRunner().invoke(app, ["quick", "Seed Artist - First", "--json", "--no-export"])
    assert result.exit_code != 0
    assert "SYNTH" not in result.output and all("SYNTH" not in str(e) for e in _chain(result.exception))


def test_an_error_answer_never_carries_its_body_or_reason_into_the_exception():
    """Seen by review (R2): an error body or reason text can quote anything, a key included;
    only the status, the wait and an error code Digger knows reach the exception."""
    def echoing(request):
        return httpx.Response(500, json={"message": "upstream failed for SECRET-KEY-ECHO"},
                              extensions={"reason_phrase": b"SECRET-KEY-ECHO"})
    with _mock_client(echoing) as client:
        with pytest.raises(CosineHTTPError) as failed:
            client.search("x", retry=False)
    assert str(failed.value) == "500 Internal Server Error" and failed.value.payload == {}
    assert all("SECRET-KEY-ECHO" not in str(e) for e in _chain(failed.value))

    def missing(request):
        return httpx.Response(404, json={"code": "TRACK_NOT_FOUND", "detail": "SECRET-KEY-ECHO"})
    with _mock_client(missing) as client:
        with pytest.raises(CosineHTTPError) as unknown:
            client.lookup("https://cosine.club/track/x", retry=False)
    assert unknown.value.payload == {"code": "TRACK_NOT_FOUND"}  # the lookup fallback still knows it
    assert str(unknown.value) == "404 Not Found: TRACK_NOT_FOUND"


@pytest.mark.parametrize("code", [{}, [], 7, None])
def test_an_error_code_that_isnt_text_still_gives_the_status_and_the_wait(rate_clock, code):
    """Seen by review (R2 re-check #9): {"code": {}} or [] in a 429 raised TypeError, so the
    answer lost its wait and skipped the provider-error handling."""
    def busy(request):
        return httpx.Response(429, headers={"retry-after": "7"}, json={"code": code})
    with _mock_client(busy) as client, pytest.raises(CosineHTTPError) as failed:
        client.search("x", retry=False)
    assert failed.value.payload == {} and failed.value.retry_after_s == 7
    assert str(failed.value) == "429 Too Many Requests"
