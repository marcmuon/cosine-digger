from cosine_digger.models import Track
from cosine_digger.seeds import resolve_album_text, resolve_seed

RELEASE = "https://www.discogs.com/release/111-Artist-Album"
OTHER = "https://www.discogs.com/release/222-Artist-Other"


class Client:
    def __init__(self, search_map):
        self.search_map = search_map

    def search(self, query, limit=20):
        return list(self.search_map.get(query, []))[:limit]

    def lookup(self, url):
        return []


def track(tid, name, artist="Artist", link=RELEASE):
    return Track(id=tid, name=name, artist=artist, track=name, external_link=link)


META = {
    RELEASE: {"title": "The Album", "artists": [{"name": "Artist (2)"}]},
    OTHER: {"title": "Other Record", "artists": [{"name": "Artist"}]},
}


def test_album_text_keeps_only_tracks_from_the_named_release():
    client = Client({"Artist The Album": [
        track("1", "Opening"), track("2", "Closer"), track("3", "Elsewhere", link=OTHER),
        track("4", "Opening", artist="Someone Else"),
    ]})
    got = resolve_album_text("Artist", "The Album", client, release_meta=META.get)
    assert [t.id for t in got] == ["1", "2"]


def test_album_text_without_discogs_evidence_returns_nothing():
    client = Client({"Artist The Album": [track("1", "Opening", link="")]})
    assert resolve_album_text("Artist", "The Album", client, release_meta=META.get) == []


def test_resolve_seed_falls_back_to_album_when_no_track_title_matches():
    client = Client({
        "Artist - The Album": [track("9", "Unrelated")],
        "Artist The Album": [track("1", "Opening"), track("2", "Closer")],
    })
    notes = []
    got = resolve_seed("Artist - The Album", client, release_meta=META.get, notify=notes.append)
    assert [t.id for t in got] == ["1", "2"]
    assert any("Resolved album" in n for n in notes)


def test_explicit_track_still_wins_over_album():
    client = Client({"Artist - Opening": [track("1", "Opening")]})
    assert [t.id for t in resolve_seed("Artist - Opening", client, release_meta=META.get)] == ["1"]


def test_album_falls_back_to_the_discogs_release_when_search_has_no_links():
    release = "https://www.discogs.com/release/333-Artist-The-Album"

    class C(Client):
        def lookup(self, url):
            return [track("7", "Opening", link=""), track("8", "Closer", link="")] if url.startswith("https://www.discogs.com/release/333") else []

    client = C({"Artist The Album": [track("1", "Opening", link="")]})
    got = resolve_album_text("Artist", "The Album", client, release_meta=META.get,
                             find_release=lambda a, b: release)
    assert [t.id for t in got] == ["7", "8"]


def test_original_mix_is_the_same_recording_but_a_remix_is_not():
    from cosine_digger.seeds import _title_matches
    assert _title_matches("NX1_05_013 (Original Mix)", "NX1_05_013")
    assert _title_matches("Bell", "bell (album version)")
    assert not _title_matches("Bell (Surgeon Remix)", "Bell")


def test_album_seed_metadata_comes_from_the_discogs_cache_the_second_time(monkeypatch, tmp_path):
    """Seed lookups go through the same paced, cached Discogs client as deep digs."""
    import httpx

    from cosine_digger import api, chain
    from cosine_digger.models import SimilarTracksData
    from cosine_digger.storage import connect, migrate

    class Cosine(Client):
        def similar(self, track_id, limit=50, **filters):
            return SimilarTracksData(source_track=Track(id=track_id, name="", artist="", track=""),
                                     similar_tracks=[])

        def close(self):
            pass

    requests = []

    def discogs(request):
        requests.append(str(request.url))
        return httpx.Response(200, json=META[RELEASE])

    monkeypatch.setattr(chain.httpx, "HTTPTransport", lambda: httpx.MockTransport(discogs))
    conn = connect(tmp_path / "taste.db")
    migrate(conn)
    client = Cosine({"Artist - The Album": [track("9", "Unrelated")],
                     "Artist The Album": [track("1", "Opening"), track("2", "Closer")]})
    for _ in range(2):
        out = api.quick(["Artist - The Album"], client=client, conn=conn, export_playlist=False, record=False)
        assert [t["id"] for s in out["seeds"] for t in s["tracks"]] == ["1", "2"]
    assert requests == ["https://api.discogs.com/releases/111"]
    conn.close()
