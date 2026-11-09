from cosine_digger.cli import resolve_seed
from cosine_digger.models import Track


class DummyClient:
    def __init__(
        self,
        lookup_map: dict[str, list[Track]] | None = None,
        search_map: dict[str, list[Track]] | None = None,
        lookup_errors: dict[str, Exception] | None = None,
    ) -> None:
        self.lookup_map = lookup_map or {}
        self.search_map = search_map or {}
        self.lookup_errors = lookup_errors or {}

    def lookup(self, url: str) -> list[Track]:
        error = self.lookup_errors.get(url)
        if error:
            raise error
        return list(self.lookup_map.get(url, []))

    def search(self, query: str, limit: int = 20) -> list[Track]:
        return list(self.search_map.get(query, []))[:limit]


def make_track(track_id: str, name: str, artist: str = "Tolouse Low Trax") -> Track:
    return Track(id=track_id, name=name, artist=artist, track=name)


def test_explicit_track_does_not_resolve_to_another_track_by_same_artist():
    client = DummyClient(search_map={
        "Artist - Wanted": [make_track("wrong", "Unrelated", artist="Artist")],
    })
    assert resolve_seed("Artist - Wanted", client) == []


def test_explicit_track_does_not_silently_select_a_remix():
    client = DummyClient(search_map={
        "Artist - Wanted": [make_track("remix", "Wanted (Unrelated Remix)", artist="Artist")],
    })
    assert resolve_seed("Artist - Wanted", client) == []
