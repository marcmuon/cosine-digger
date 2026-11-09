"""Unit tests for the chain-dig discovery engine (chain.py)."""

from __future__ import annotations

import pytest
import httpx
import cosine_digger.chain as chain_module

from cosine_digger.chain import BudgetExhausted, BudgetTracker, _merge_results
from cosine_digger.fusion import RankedTrack
from cosine_digger.models import Track


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_rt(
    track_id: str,
    artist: str,
    title: str,
    score: float,
    hop: int = 0,
    via: str = "",
) -> RankedTrack:
    """Build a minimal RankedTrack for testing."""
    return RankedTrack(
        track=Track(id=track_id, name=f"{title} - {artist}", artist=artist, track=title),
        final_score=score,
        match_count=1,
        avg_score=score,
        seed_scores={},
        hop=hop,
        via=via,
    )


# ---------------------------------------------------------------------------
# Branch score formula
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Merge results: multi-hop
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Merge results: gap bonus
# ---------------------------------------------------------------------------


class TestMergeResultsGapBonus:
    """gap_artists get a 1.2x boost."""

    def test_merge_results_gap_bonus(self):
        results = [
            [
                _make_rt("t1", "Known Artist", "Known Track", 0.80, hop=0),
                _make_rt("t2", "Gap Artist", "Gap Track", 0.80, hop=0),
            ]
        ]

        merged = _merge_results(results, gap_artists={"Gap Artist"})

        by_id = {rt.track.id: rt for rt in merged}
        # Raw: t2=0.80*1.2=0.96, t1=0.80; normalized: t2=1.0, t1=0.80/0.96
        assert by_id["t2"].final_score == pytest.approx(1.0, abs=1e-5)
        assert by_id["t1"].final_score == pytest.approx(0.80 / 0.96, abs=1e-4)


# ---------------------------------------------------------------------------
# Merge results: cross-branch bonus
# ---------------------------------------------------------------------------


class TestMergeResultsCrossBranch:
    """Tracks appearing in 2 branches get cross_branch_bonus."""

    def test_merge_results_cross_branch(self):
        branch_a = [
            _make_rt("t1", "Artist X", "Shared Track", 0.85, hop=1, via="label: L1"),
        ]
        branch_b = [
            _make_rt("t1", "Artist X", "Shared Track", 0.80, hop=1, via="artist: A1"),
        ]

        merged = _merge_results(
            [branch_a, branch_b],
            cross_branch_bonus=0.3,
            hop_decay=0.85,
        )

        assert len(merged) == 1
        rt = merged[0]
        # Single item normalizes to 1.0
        assert rt.final_score == pytest.approx(1.0, abs=1e-5)
        assert rt.match_count == 2

    def test_artist_dedup_keeps_highest(self):
        """Same artist appearing with different tracks: keep highest scored."""
        results = [
            [
                _make_rt("t1", "Artist X", "Track 1", 0.90, hop=0),
                _make_rt("t2", "Artist X", "Track 2", 0.70, hop=0),
            ]
        ]
        merged = _merge_results(results)
        # Only one entry per artist
        assert len(merged) == 1
        assert merged[0].track.id == "t1"


# ---------------------------------------------------------------------------
# BudgetTracker
# ---------------------------------------------------------------------------


def test_same_branch_does_not_manufacture_convergence():
    first = _make_rt("repeat", "A", "Repeated", 0.8, hop=1, via="label: Example")
    again = first.model_copy(update={"hop": 2})
    reference = _make_rt("other", "B", "Other", 0.9, hop=1, via="label: Other")
    merged = _merge_results([[first, reference], [again, again]])
    by_id = {result.track.id: result for result in merged}
    assert by_id["repeat"].match_count == 1
    assert by_id["repeat"].final_score < by_id["other"].final_score
    assert len(by_id["repeat"].provenance_chain) == 2


def test_merge_preserves_tracks_with_unknown_artists():
    merged = _merge_results([[
        _make_rt("a", "", "A", 0.9),
        _make_rt("b", "", "B", 0.8),
    ]])
    assert [result.track.id for result in merged] == ["a", "b"]


@pytest.mark.parametrize("budget,status,expected_calls", [(0, 200, 0)])
def test_discogs_budget_counts_each_request_including_retries(monkeypatch, budget, status, expected_calls):
    calls = []
    responses = []

    def request(req):
        calls.append(req)
        response = httpx.Response(status, json={"message": "fixture"})
        responses.append(response)
        return response

    monkeypatch.setattr(chain_module.httpx, "HTTPTransport", lambda: httpx.MockTransport(request))
    monkeypatch.setattr(chain_module.discogs.time, "sleep", lambda _: None)
    client, tracker = chain_module.make_rate_limited_client(budget)
    with client:
        if budget < 2:
            with pytest.raises(BudgetExhausted):
                client.get("https://api.discogs.com/releases/1")
        else:
            client.get("https://api.discogs.com/releases/1")
    assert len(calls) == expected_calls
    assert tracker.remaining == 0
    assert all(response.is_closed for response in responses)


def test_chain_excludes_original_seeds_and_does_not_repeat_branches(monkeypatch):
    monkeypatch.setattr(chain_module, "make_rate_limited_client", lambda **kw: (httpx.Client(), BudgetTracker(10)))
    monkeypatch.setattr(chain_module, "fuse_similar_tracks", lambda **kw: [_make_rt("new", "New Artist", "New", 0.9)])
    followed = []
    artist_sets = []

    def extract(**kwargs):
        artist_sets.append(kwargs["seed_artists"])
        return [chain_module.Branch(type="label", value="Example", discogs_id="1")]

    def follow(**kwargs):
        followed.append(kwargs["branch"].value)
        return [
            _make_rt("seed", "Seed Artist", "Seed", 1.0, hop=1, via="label: Example"),
            _make_rt("discovery", "Different", "Discovery", 0.8, hop=1, via="label: Example"),
        ]

    monkeypatch.setattr(chain_module, "extract_branches", extract)
    monkeypatch.setattr(chain_module, "follow_branch", follow)
    results = chain_module.chain_dig(["seed"], object(), hops=3, seed_artists={"Seed Artist"})
    assert "seed" not in {result.track.id for result in results}
    assert followed == ["Example"]
    assert all(artists == {"Seed Artist"} for artists in artist_sets)


def test_chain_does_not_hide_initial_provider_failure(monkeypatch):
    from cosine_digger.client import CosineRequestError

    monkeypatch.setattr(chain_module, "make_rate_limited_client", lambda **kw: (httpx.Client(), BudgetTracker(10)))

    def failed(**kwargs):
        raise CosineRequestError("offline fixture")

    monkeypatch.setattr(chain_module, "fuse_similar_tracks", failed)
    with pytest.raises(CosineRequestError, match="offline fixture"):
        chain_module.chain_dig(["seed"], object())


def test_chain_does_not_fall_back_to_unbudgeted_client(monkeypatch):
    def failed(**kwargs):
        raise RuntimeError("client setup failed")

    monkeypatch.setattr(chain_module, "make_rate_limited_client", failed)
    monkeypatch.setattr(chain_module, "fuse_similar_tracks", lambda **kw: [])
    with pytest.raises(RuntimeError, match="client setup failed"):
        chain_module.chain_dig(["seed"], object())


def test_artist_branch_does_not_follow_an_unrelated_first_hit(monkeypatch):
    class FakeCosine:
        def search(self, query, limit):
            return [
                Track(id="wrong", name="Song", artist="Unrelated", track="Song"),
                Track(id="correct", name="Song", artist="Wanted Artist", track="Song"),
            ]

    captured = {}

    def fuse(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(chain_module, "fuse_similar_tracks", fuse)
    chain_module.follow_branch(chain_module.Branch(type="artist", value="Wanted Artist"), FakeCosine(), None)
    assert captured["seed_ids"] == ["correct"]


def test_original_seed_cannot_suppress_unheard_track_by_same_artist(monkeypatch):
    from cosine_digger.models import SimilarTracksData

    class FakeCosine:
        def search(self, query, limit):
            return [Track(id="branch-seed", artist="Branch Artist", name="Branch", track="Branch")]

        def similar(self, track_id, **kwargs):
            source = Track(id=track_id, artist="Seed Artist", name="Seed", track="Seed")
            tracks = [Track(id="hop0", artist="Other", name="First", track="First", score=0.8)]
            if track_id == "branch-seed":
                tracks = [
                    source.model_copy(update={"id": "original", "score": 0.99}),
                    source.model_copy(update={"id": "unheard", "track": "Unheard", "score": 0.9}),
                ]
            return SimilarTracksData(source_track=source, similar_tracks=tracks)

    monkeypatch.setattr(chain_module, "extract_branches", lambda **kwargs: [chain_module.Branch(type="artist", value="Branch Artist")])
    results = chain_module.chain_dig(["original"], FakeCosine(), hops=2, max_discogs_calls=0)
    assert {track.track.id for track in results} == {"hop0", "unheard"}


def test_chain_gives_an_anchor_with_two_recordings_one_vote(monkeypatch):
    """Weekly chains start from anchors that may carry two recordings; at hop 0
    they vote once, as in the similarity stream."""
    from cosine_digger.models import SimilarTracksData

    monkeypatch.setattr(chain_module, "make_rate_limited_client", lambda **kw: (httpx.Client(), BudgetTracker(10)))
    x, y = Track(id="X", name="X", artist="Ex", track="X"), Track(id="Y", name="Y", artist="Why", track="Y")
    lists = {"A1": [x, y], "A2": [x], "B": [y]}

    class Fake:
        def similar(self, track_id, limit=50, **_):
            return SimilarTracksData(source_track=Track(id=track_id, name="", artist="", track=""),
                                     similar_tracks=[t.model_copy(update={"score": 0.9}) for t in lists[track_id]])
    grouped = chain_module.chain_dig(["A1", "A2", "B"], Fake(), hops=1,
                                     seed_groups={"A1": "Anchor A", "A2": "Anchor A", "B": "Anchor B"})
    assert [r.track.id for r in grouped] == ["Y", "X"]
    ungrouped = chain_module.chain_dig(["A1", "A2", "B"], Fake(), hops=1)
    assert [r.track.id for r in ungrouped] == ["X", "Y"]
