"""Typed seeds resolve through the public API without trusting fuzzy ranking."""
import pytest

from cosine_digger import api
from cosine_digger.models import SimilarTracksData, Track


def track(tid, artist, title):
    return Track(id=tid, name=title, artist=artist, track=title)


class Cosine:
    def __init__(self, hits):
        self.hits = hits
        self.dug = []

    def search(self, query, limit=20):
        return self.hits[:limit]

    def similar(self, track_id, limit=50, **filters):
        self.dug.append(track_id)
        return SimilarTracksData(
            source_track=next(t for t in self.hits if t.id == track_id),
            similar_tracks=[],
        )


@pytest.mark.parametrize("seed,artist", [("klaus", "Klaus Wiese"), ("Ulla", "Ulla Straus")])
def test_bare_artist_does_not_dig_from_a_longer_name(seed, artist):
    client = Cosine([track("wrong", artist, "Bell")])
    out = api.quick([seed], client=client, export_playlist=False, record=False)
    assert out["seeds"][0]["tracks"] == []
    assert not out["seeds"][0]["ok"]
    assert client.dug == []
    assert out["seeds"][0]["notes"] == [
        f"No exact artist or title match for '{seed}' (closest: {artist} - Bell); "
        "try 'Artist - Title' or a Discogs URL"
    ]


def test_bare_artist_uses_up_to_two_exact_artist_hits_before_title_hits():
    client = Cosine([
        track("title", "Someone Else", "klaus"),
        track("wrong", "Klaus Wiese", "Bell"),
        track("first", "Klaus", "First"),
        track("second", "KLAUS", "Second"),
        track("third", "Klaus", "Third"),
    ])
    out = api.quick(["klaus"], client=client, export_playlist=False, record=False)
    assert [t["id"] for t in out["seeds"][0]["tracks"]] == ["first", "second"]
    assert set(client.dug) == {"first", "second"}
    assert any("Digging from Klaus's tracks" in note for note in out["seeds"][0]["notes"])


def test_bare_title_uses_the_exact_title_instead_of_the_top_hit():
    client = Cosine([
        track("wrong", "Another Artist", "Bell (Remix)"),
        track("right", "Klaus", "Bell"),
    ])
    seed = api.resolve_seeds(["bell"], client)[0]
    assert [t.id for t in seed.tracks] == ["right"]


@pytest.mark.parametrize("artist,canonical", [
    (f"R{dash}Zone", "R-Zone") for dash in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
] + [
    (f"O{apostrophe}Zone", "O'Zone") for apostrophe in "\u2018\u2019\u02bc\uff07"
] + [("Ｒ－Ｚｏｎｅ", "R-Zone"), ("  R\t Zone  ", "R Zone")])
def test_artist_title_seed_folds_unicode_artist_typography(artist, canonical):
    client = Cosine([track("right", canonical, "303909")])
    seed = api.resolve_seeds([f"{artist} - 303909"], client)[0]
    assert [t.id for t in seed.tracks] == ["right"]


def test_artist_fallback_uses_the_same_unicode_normalization():
    client = Cosine([track("right", "R-Zone", "303909")])
    out = api.quick([], fallback_artists=["Ｒ‐Ｚｏｎｅ"], client=client,
                    export_playlist=False, record=False)
    assert [t["id"] for t in out["seeds"][0]["tracks"]] == ["right"]
    assert client.dug == ["right"]
