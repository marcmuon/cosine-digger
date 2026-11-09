import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from cosine_digger import seeds
from cosine_digger.fresh import (
    FreshAuthError,
    exa_search,
    extract_candidates,
    find_fresh,
    resolve_candidate,
    save_finds,
)
from cosine_digger.models import Track
from cosine_digger.storage import migrate


class FakeClient:
    def __init__(self, tracks=()):
        self.tracks = list(tracks)
        self.calls = []

    def search(self, query, limit=20):
        self.calls.append((query, limit))
        return self.tracks[:limit]


def track(title="Title", artist="Artist", tid="cosine-1", **kwargs):
    return Track(id=tid, name=title, track=title, artist=artist, video_id="video-1", **kwargs)


def http_results(results):
    return httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"results": results}),
    ))


def test_no_key_skips_without_touching_services():
    assert find_fresh(["Label"], ["Artist"], api_key=None, client=None, http=None) == (
        [], {"skipped": "no EXA_API_KEY"},
    )


@pytest.mark.parametrize("heading", [
    "Artist - Title", "Artist – Title", "Artist — Title", "Title | Artist",
    "Title, by Artist", "RA Review: Artist - Title", "Artist - Title (LP)",
    "Artist - Title [EP] | Boomkat", "Artist - Title - Resident Advisor",
    "Title (LP), by Artist", "Title [EP] | Artist | Bandcamp",
])
def test_title_patterns(heading):
    assert extract_candidates({"title": heading}) == [("Artist", "Title")]


def test_exact_cosine_match_wins_over_remix_and_ytmusic():
    client = FakeClient([
        track("Title (Remix)"), track(artist="Artist Else"),
        track("  TITLE  ", artist="Artist (2)", tid="exact"),
    ])

    def unexpected_youtube(query):
        pytest.fail("Exact cosine match should win")

    found = resolve_candidate("artist", "title", client, ytmusic_search=unexpected_youtube)
    assert found.to_dict() == {
        "artist": "artist", "title": "title", "kind": "track", "source_url": "",
        "source_title": "", "published": None, "track_id": "exact",
        "video_id": "video-1", "resolved_via": "cosine",
    }
    assert client.calls == [("artist title", 10)]


def test_album_fallback_uses_real_release_verification(monkeypatch):
    release = "https://www.discogs.com/release/123-Artist-Album"
    other = "https://www.discogs.com/release/456-Artist-Other"
    metadata = {
        release: {"title": "Album", "artists": [{"name": "Artist (2)"}]},
        other: {"title": "Other", "artists": [{"name": "Artist"}]},
    }
    monkeypatch.setattr(seeds, "_discogs_release_meta", lambda url, client=None: metadata[url])
    client = FakeClient([
        track("Wrong", tid="wrong", external_link=other),
        track("Opening", tid="opening", external_link=release),
        track("Closing", tid="closing", external_link=release),
    ])
    found = resolve_candidate("Artist", "Album", client)
    # The find names the recording that will play, so owned/served checks match it.
    assert (found.kind, found.title, found.track_id, found.resolved_via) == (
        "album", "Opening", "opening", "cosine",
    )


@pytest.mark.parametrize("title,artist,video,accepted", [
    (" TITLE ", " ARTIST ", "video", True),
    ("Title (Remix)", "Artist", "video", False),
    ("Title", "Artist Else", "video", False),
    ("Title", "Artist (2)", "video", False),
    ("Title", "Artist", None, False),
])
def test_youtube_fallback_requires_exact_identity_and_video(title, artist, video, accepted):
    def youtube(query):
        assert query == "Artist Title"
        return [{"videoId": video, "title": title, "artists": [{"name": artist}]}]

    found = resolve_candidate("Artist", "Title", FakeClient([track("Unrelated")]), ytmusic_search=youtube)
    assert (found is not None) == accepted
    if accepted:
        assert (found.track_id, found.video_id, found.resolved_via) == (None, "video", "ytmusic")


@pytest.mark.parametrize("status", [401, 403])
def test_exa_auth_failure_propagates(status):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as http:
        with pytest.raises(FreshAuthError):
            find_fresh(["Label"], [], api_key="key", client=FakeClient(), http=http)


def test_query_failure_is_reported_and_pipeline_continues_then_saves():
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url == "https://api.exa.ai/search"
        assert request.headers["x-api-key"] == "key"
        assert request.extensions["timeout"]["read"] == 15.0
        assert payload["numResults"] == 5
        assert payload["type"] == "auto"
        assert payload["contents"] == {"text": {"maxCharacters": 500}}
        cutoff = datetime.fromisoformat(payload["startPublishedDate"])
        assert abs(datetime.now(timezone.utc) - cutoff - timedelta(days=45)) < timedelta(seconds=5)
        if len(requests) == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"results": [
            {"title": "Artist - Title [EP]", "url": "https://example.com/release", "publishedDate": "2026-09-28"},
            {"title": "ARTIST - TITLE", "url": "https://example.com/duplicate"},
            {"title": "Artist - Unknown", "url": "https://example.com/unknown"},
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        finds, diagnostics = find_fresh(["Label"], ["Artist"], api_key="key", client=FakeClient([track()]), http=http)
    assert len(finds) == 1
    assert finds[0].source_url == "https://example.com/release"
    assert finds[0].source_title == "Artist - Title [EP]"
    assert finds[0].published == "2026-09-28"
    assert diagnostics["errors"] == [{
        "query": "new release on Label records", "stage": "exa", "status_code": 500, "error": "Exa HTTP 500",
    }]
    assert diagnostics["unresolved"] == [{"artist": "Artist", "title": "Unknown"}]
    with sqlite3.connect(":memory:") as conn:
        migrate(conn)
        save_finds(conn, "query", finds)
        save_finds(conn, "query", finds)
        rows = conn.execute("SELECT query, source_url, artist, title, track_id, video_id, found_at FROM fresh_finds").fetchall()
        assert len(rows) == 1
        assert rows[0][:6] == ("query", "https://example.com/release", "Artist", "Title", "cosine-1", "video-1")
        assert datetime.fromisoformat(rows[0][6]).tzinfo is not None


@pytest.mark.parametrize("failure", ["timeout", "json", "shape"])
def test_search_failures_return_empty_list_and_diagnostics(failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "json":
            return httpx.Response(200, text="not json")
        return httpx.Response(200, json={"results": "bad shape"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        results = exa_search("query", api_key="key", http=http)
    assert results == []
    assert results.diagnostics[0]["query"] == "query"
    assert results.diagnostics[0]["error"]


def test_find_cap_and_candidate_failure_isolation():
    class FlakyClient(FakeClient):
        def search(self, query, limit=20):
            if query == "Bad Song":
                raise RuntimeError("search unavailable")
            return super().search(query, limit)

    with http_results([
        {"title": "Bad - Song"}, {"title": "Artist - Title"}, {"title": "Other - Song"},
    ]) as http:
        finds, diagnostics = find_fresh(
            ["Label", "Second"], [], api_key="key", client=FlakyClient([track()]),
            max_finds=1, http=http,
        )
    assert len(finds) == 1
    assert len(diagnostics["queries"]) == 1
    assert diagnostics["errors"][0]["stage"] == "resolve"
    assert "search unavailable" in diagnostics["errors"][0]["error"]


def test_resolve_failures_never_quote_a_key():
    class Leaky(FakeClient):
        def search(self, query, limit=20):
            raise RuntimeError("Illegal header value b'Bearer SYNTH-KEY\\n'")

    with http_results([{"title": "Artist - Title"}]) as http:
        _, diagnostics = find_fresh(["Label"], [], api_key="key", client=Leaky(), http=http)
    assert diagnostics["errors"] and "SYNTH-KEY" not in json.dumps(diagnostics)
