
import pytest

from cosine_digger.fusion import (
    SCORERS,
    fuse_similar_tracks,
    score_adventurous_batch,
    score_consensus_batch,
    )
from cosine_digger.models import SimilarTracksData, Track


class DummyClient:
    def __init__(self, responses: dict[str, list[Track]]):
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def similar(self, track_id: str, limit: int = 50, **filters) -> SimilarTracksData:
        self.calls.append({"track_id": track_id, "limit": limit, "filters": filters})
        return SimilarTracksData(
            source_track=Track(
                id=track_id,
                name=f"seed-{track_id}",
                artist="seed-artist",
                track="seed-track",
            ),
            similar_tracks=self.responses.get(track_id, []),
        )


def make_track(track_id: str, score: float) -> Track:
    return Track(
        id=track_id,
        name=f"name-{track_id}",
        artist=f"artist-{track_id}",
        track="track",
        score=score,
    )


def build_sample_client() -> DummyClient:
    responses = {
        "S1": [
            make_track("A", 0.87),
            make_track("B", 0.82),
            make_track("C", 0.79),
            make_track("D", 0.75),
        ],
        "S2": [
            make_track("B", 0.91),
            make_track("E", 0.85),
            make_track("A", 0.80),
            make_track("F", 0.77),
        ],
        "S3": [
            make_track("A", 0.89),
            make_track("B", 0.84),
            make_track("G", 0.81),
            make_track("H", 0.78),
        ],
    }
    return DummyClient(responses)


def test_centroid_scoring_and_order() -> None:
    client = build_sample_client()
    results = fuse_similar_tracks(["S1", "S2", "S3"], client, scorer="consensus")

    assert len(results) == 8
    assert [item.track.id for item in results[:2]] == ["B", "A"]

    top = results[0]
    assert top.match_count == 3
    assert top.avg_score == pytest.approx((0.82 + 0.91 + 0.84) / 3)

    # Seed weighting: all 3 seeds have Jaccard overlap = 2/6 = 1/3
    # weight = 1/(1 + 2*(1/3)) = 3/5 = 0.6
    # effective_matches = 3 * 0.6 = 1.8
    expected_weight = 1.0 / (1.0 + 2.0 * (1.0 / 3.0))
    expected_effective = 3 * expected_weight
    assert top.effective_matches == pytest.approx(expected_effective, rel=1e-3)
    # Consensus scorer normalizes to [0, 1] — top item is always 1.0
    assert top.final_score == pytest.approx(1.0)
    # Verify relative scoring: second item's ratio to top preserves the formula
    second = results[1]
    raw_top = top.avg_score * (1 + 0.3 * (expected_effective - 1))
    eff_second = max(1.0, sum(expected_weight for _ in second.seed_scores))
    raw_second = second.avg_score * (1 + 0.3 * (eff_second - 1))
    assert second.final_score == pytest.approx(raw_second / raw_top, rel=1e-3)
    assert set(top.seed_scores.keys()) == {"S1", "S2", "S3"}


def test_intersection_filters_and_ranks_by_avg() -> None:
    client = build_sample_client()
    results = fuse_similar_tracks(["S1", "S2", "S3"], client, mode="intersection", scorer="consensus")

    assert [item.track.id for item in results] == ["B", "A"]
    assert results[0].avg_score > results[1].avg_score


def test_redundant_seeds_get_downweighted() -> None:
    """Seeds with near-identical neighborhoods (same album) get reduced effective_matches."""
    # 8 shared tracks + 1 unique each = Jaccard 8/10 = 0.8
    shared = [make_track(f"T{i}", 0.9 - i * 0.01) for i in range(8)]
    client = DummyClient({
        "S1": shared + [make_track("U1", 0.5)],
        "S2": shared + [make_track("U2", 0.5)],
    })
    results = fuse_similar_tracks(["S1", "S2"], client, scorer="consensus")

    t0 = next(r for r in results if r.track.id == "T0")
    assert t0.match_count == 2

    # weight = 1/(1 + 2*0.8) = 1/2.6 ≈ 0.385
    # effective = max(1.0, 2 * 0.385) = 1.0 (clamped)
    assert t0.effective_matches == pytest.approx(1.0)
    # Clamped to 1.0 means no match bonus — redundant seeds don't inflate scores
    # After normalization, T0 has the highest raw score, so final_score == 1.0
    assert t0.final_score == pytest.approx(1.0)


def test_independent_seeds_get_full_bonus() -> None:
    """Seeds with low overlap (different artists/labels) keep their match bonus."""
    # 1 shared track, 4 unique each = Jaccard 1/9 ≈ 0.111
    client = DummyClient({
        "S1": [
            make_track("SHARED", 0.85),
            make_track("A1", 0.80),
            make_track("A2", 0.75),
            make_track("A3", 0.70),
            make_track("A4", 0.65),
        ],
        "S2": [
            make_track("SHARED", 0.90),
            make_track("B1", 0.80),
            make_track("B2", 0.75),
            make_track("B3", 0.70),
            make_track("B4", 0.65),
        ],
    })
    results = fuse_similar_tracks(["S1", "S2"], client, scorer="consensus")

    shared = next(r for r in results if r.track.id == "SHARED")
    assert shared.match_count == 2

    # Jaccard = 1/9 ≈ 0.111, weight = 1/(1 + 2*0.111) ≈ 0.818
    # effective = 2 * 0.818 ≈ 1.636
    expected_weight = 1.0 / (1.0 + 2.0 * (1.0 / 9.0))
    expected_effective = 2 * expected_weight
    assert shared.effective_matches == pytest.approx(expected_effective, rel=1e-2)
    # Should get a meaningful match bonus (not clamped)
    assert shared.effective_matches > 1.5
    assert shared.final_score > shared.avg_score


# ---------------------------------------------------------------------------
# Adventurous scorer tests
# ---------------------------------------------------------------------------


def test_adventurous_ranking_inversion() -> None:
    """High-spread track (0.96/0.85) ranks above consensus track (0.93/0.92)."""
    candidates = {
        "A": {"seed_scores": {"S1": 0.96, "S2": 0.85}, "track": None},
        "B": {"seed_scores": {"S1": 0.93, "S2": 0.92}, "track": None},
        "C": {"seed_scores": {"S1": 0.94}, "track": None},
    }
    scores = score_adventurous_batch(candidates, {"S1": 1.0, "S2": 1.0})
    # A (high spread) > C (single match) > B (consensus)
    assert scores["A"] > scores["C"]
    assert scores["C"] > scores["B"]


# ---------------------------------------------------------------------------
# RRF scorer tests
# ---------------------------------------------------------------------------


def test_an_anchor_with_two_recordings_brings_one_list_of_the_usual_depth() -> None:
    """Its recordings' neighbours are merged in turn and cut to the depth every
    anchor gets, so it can't bring twice the candidates or vote twice."""
    client = DummyClient({
        "A1": [make_track("x", 0.9), make_track("p", 0.9), make_track("q", 0.9)],
        "A2": [make_track("x", 0.9), make_track("r", 0.9), make_track("s", 0.9)],
        "B": [make_track("p", 0.9), make_track("t", 0.9), make_track("u", 0.9)],
    })
    out = fuse_similar_tracks(["A1", "A2", "B"], client, limit_per_seed=3,
                              seed_groups={"A1": "Artist A", "A2": "Artist A", "B": "Artist B"})
    ids = [r.track.id for r in out]
    assert set(ids) == {"x", "p", "r", "t", "u"}      # A's list is x, p, r; q and s fall past its depth
    assert ids[:2] == ["p", "x"]                       # both anchors agree on p; x is one vote, not two
    assert set(out[1].seed_scores) == {"A1"}


def test_rrf_different_ordering_than_consensus() -> None:
    """RRF produces different ordering than consensus on same pool."""
    client = build_sample_client()
    consensus_results = fuse_similar_tracks(["S1", "S2", "S3"], client, scorer="consensus")
    rrf_results = fuse_similar_tracks(["S1", "S2", "S3"], client, scorer="rrf")

    consensus_order = [r.track.id for r in consensus_results]
    rrf_order = [r.track.id for r in rrf_results]
    # Rankings should differ (RRF ignores score magnitude)
    assert consensus_order != rrf_order


# ---------------------------------------------------------------------------
# Serendipity tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scorer", SCORERS)
@pytest.mark.parametrize("mode", ["centroid"])
def test_repeated_seed_is_one_vote_and_one_request(scorer, mode) -> None:
    expected = fuse_similar_tracks(["S1", "S2"], build_sample_client(), scorer=scorer, mode=mode)
    client = build_sample_client()
    actual = fuse_similar_tracks(["S1", "S2", "S1"], client, scorer=scorer, mode=mode)
    assert actual == expected
    assert [call["track_id"] for call in client.calls] == ["S1", "S2"]


@pytest.mark.parametrize("scorer", SCORERS)
def test_seed_tracks_are_not_recommended(scorer) -> None:
    client = DummyClient({
        "S1": [make_track("S1", 1.0), make_track("S2", 0.99), make_track("new", 0.8)],
        "S2": [make_track("S1", 0.99), make_track("new", 0.85)],
    })
    results = fuse_similar_tracks(["S1", "S2"], client, scorer=scorer)
    assert [result.track.id for result in results] == ["new"]
    assert results[0].final_score == pytest.approx(1.0)


def test_unknown_artists_do_not_collapse_distinct_tracks() -> None:
    tracks = [make_track("a", 0.9), make_track("b", 0.8)]
    for track in tracks:
        track.artist = ""
    results = fuse_similar_tracks(["seed"], DummyClient({"seed": tracks}))
    assert [result.track.id for result in results] == ["a", "b"]
