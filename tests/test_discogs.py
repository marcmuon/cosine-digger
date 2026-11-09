import httpx
import pytest

from cosine_digger import discogs
from cosine_digger.discogs import build_release_tracks


def test_build_release_tracks_uses_release_artist() -> None:
    payload = {
        "artists_sort": "Tolouse Low Trax",
        "tracklist": [
            {"type_": "track", "title": "Jeidem Fall"},
            {"type_": "track", "title": "Delta"},
        ],
    }

    queries = [t.query for t in build_release_tracks(payload)]

    assert queries == ["Tolouse Low Trax Jeidem Fall", "Tolouse Low Trax Delta"]


def test_build_release_tracks_prefers_track_artist_and_subtracks() -> None:
    payload = {
        "artists_sort": "Various",
        "tracklist": [
            {
                "type_": "track",
                "title": "Main",
                "artists": [{"name": "Artist (2)"}],
                "sub_tracks": [
                    {"type_": "track", "title": "Sub A", "artists": [{"name": "Sub"}]},
                    {"type_": "heading", "title": "Heading"},
                ],
            }
        ],
    }

    queries = [t.query for t in build_release_tracks(payload)]

    assert queries == ["Artist Main", "Sub Sub A"]


@pytest.mark.parametrize(
    "status,body,error",
    [
        (401, b'{}', discogs.DiscogsAuthError),
        (403, b'{}', discogs.DiscogsAuthError),
        (500, b'{}', discogs.DiscogsHTTPError),
        (200, b'not json', discogs.DiscogsResponseError),
    ],
)
def test_request_maps_errors(status, body, error):
    transport = httpx.MockTransport(lambda request: httpx.Response(status, content=body))
    with httpx.Client(transport=transport) as client:
        with pytest.raises(error):
            discogs._request(client, f"{discogs.BASE_URL}/artists/1", None, "token", None)


def test_an_error_answer_never_carries_its_reason_text_into_the_exception():
    """Seen by review (deep A1 #2): the reason text is the provider's own and can quote anything,
    a token included; the exception names the status and its standard phrase only."""
    def echoing(request):
        return httpx.Response(500, json={"message": "SECRET-TOKEN-ECHO"},
                              extensions={"reason_phrase": b"SECRET-TOKEN-ECHO"})

    with httpx.Client(transport=httpx.MockTransport(echoing)) as client:
        with pytest.raises(discogs.DiscogsHTTPError) as failed:
            discogs._request(client, f"{discogs.BASE_URL}/artists/1", None, "token", None)
    assert str(failed.value) == "500 Internal Server Error"
    assert "SECRET-TOKEN-ECHO" not in repr(failed.value.args)


def test_request_maps_transport_error():
    def fail(request):
        raise httpx.ConnectError("offline", request=request)

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(discogs.DiscogsRequestError, match="offline"):
            discogs._request(client, f"{discogs.BASE_URL}/artists/1", None, "token", None)


@pytest.mark.parametrize("limit,expected_pages", [(120, [1, 2]), (250, [1, 2]), (50, [1])])
def test_label_releases_follows_pagination_and_respects_limit(limit, expected_pages):
    pages = []

    def respond(request):
        assert request.url.path == "/labels/1/releases"
        assert request.url.params["sort"] == "year"
        assert request.url.params["sort_order"] == "desc"
        page = int(request.url.params.get("page", "1"))
        pages.append(page)
        assert page <= 2
        size = int(request.url.params["per_page"])
        start = (page - 1) * size
        return httpx.Response(200, json={
            "releases": [{"id": i, "title": f"Release {i}", "artist": "Artist", "year": 2000,
                          "catno": f"CAT{i}"} for i in range(start, start + size)],
            "pagination": {"pages": 2},
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        releases = discogs.fetch_label_releases("1", limit=limit, token="token", client=client)
    assert [release.id for release in releases] == [str(i) for i in range(min(limit, 200))]
    assert pages == expected_pages
