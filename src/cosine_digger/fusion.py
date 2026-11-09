from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from itertools import zip_longest
from statistics import mean, pstdev
from typing import Any, Callable, Iterable, Literal, TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from .client import CosineClient
from .models import Track

if TYPE_CHECKING:
    from .slice_types import MoveCandidate

T = TypeVar("T")
R = TypeVar("R")

# cosine.club calls in flight at once; results keep their input order.
COSINE_WORKERS = 4


def ordered_map(fn: Callable[[T], R], items: Iterable[T], workers: int = COSINE_WORKERS) -> list[R]:
    """``[fn(x) for x in items]``, run a few at a time. The first failing
    item (in input order) raises, as it would in the plain loop."""
    items = list(items)
    if len(items) < 2 or workers < 2:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        return list(pool.map(fn, items))


class RankedTrack(BaseModel):
    """Track with fusion scoring."""

    track: Track
    final_score: float
    match_count: int
    avg_score: float
    seed_scores: dict[str, float]
    effective_matches: float = 0.0
    serendipity: float = 0.0
    # Chain-dig metadata (backward compatible defaults)
    hop: int = 0
    via: str = ""
    provenance_chain: list[str] = []


# ---------------------------------------------------------------------------
# Seed weighting (Jaccard overlap)
# ---------------------------------------------------------------------------


def _compute_seed_weights(
    seed_neighborhoods: dict[str, set[str]],
    redundancy_penalty: float,
) -> dict[str, float]:
    """Compute per-seed weights via Jaccard neighborhood overlap.

    Seeds with highly overlapping similarity neighborhoods are redundant
    (e.g. tracks from the same album). They get downweighted so correlated
    seeds don't inflate match-count bonuses in the fusion formula.

    Returns a weight in (0, 1] for each seed. Independent seeds get ~1.0,
    redundant seeds get << 1.0.
    """
    seed_ids = list(seed_neighborhoods)
    n = len(seed_ids)

    if n <= 1:
        return {sid: 1.0 for sid in seed_ids}

    overlaps: dict[str, list[float]] = {sid: [] for sid in seed_ids}
    for i in range(n):
        for j in range(i + 1, n):
            a = seed_neighborhoods[seed_ids[i]]
            b = seed_neighborhoods[seed_ids[j]]
            union_size = len(a | b)
            jaccard = len(a & b) / union_size if union_size else 0.0
            overlaps[seed_ids[i]].append(jaccard)
            overlaps[seed_ids[j]].append(jaccard)

    weights: dict[str, float] = {}
    for sid in seed_ids:
        mean_overlap = mean(overlaps[sid]) if overlaps[sid] else 0.0
        weights[sid] = 1.0 / (1.0 + redundancy_penalty * mean_overlap)

    return weights


# ---------------------------------------------------------------------------
# Scorer functions
# ---------------------------------------------------------------------------


def score_consensus_batch(
    candidates: dict[str, dict[str, Any]],
    seed_weights: dict[str, float],
    *,
    match_bonus: float = 0.3,
    **_context: Any,
) -> dict[str, float]:
    """Consensus scorer: avg_score * (1 + match_bonus * (effective_matches - 1)).

    Returns raw scores (caller normalizes).
    """
    raw_scores: dict[str, float] = {}
    for cid, entry in candidates.items():
        seed_scores: dict[str, float] = entry["seed_scores"]
        if not seed_scores:
            continue
        avg_score = mean(seed_scores.values())
        effective_matches = max(1.0, sum(seed_weights[sid] for sid in seed_scores))
        raw_scores[cid] = avg_score * (1 + match_bonus * (effective_matches - 1))

    return raw_scores


def score_adventurous_batch(
    candidates: dict[str, dict[str, Any]],
    seed_weights: dict[str, float],
    *,
    adventure_bonus: float = 2.0,
    **_context: Any,
) -> dict[str, float]:
    """Adventurous scorer: max_score * (1 + adventure_bonus * pstdev(scores)).

    Uses the best single-seed score as the base. Rewards score spread
    across seeds. Intentionally skips Jaccard seed weights.
    Returns raw scores (caller normalizes).
    """
    raw_scores: dict[str, float] = {}
    for cid, entry in candidates.items():
        seed_scores: dict[str, float] = entry["seed_scores"]
        if not seed_scores:
            continue
        max_score = max(seed_scores.values())
        score_spread = pstdev(seed_scores.values()) if len(seed_scores) > 1 else 0.0
        raw_scores[cid] = max_score * (1 + adventure_bonus * score_spread)

    return raw_scores


def score_rrf_batch(
    candidates: dict[str, dict[str, Any]],
    seed_weights: dict[str, float],
    *,
    k: int = 60,
    seed_rank_lists: dict[str, list[str]] | None = None,
    **_context: Any,
) -> dict[str, float]:
    """RRF scorer: sum(1 / (k + rank)) for each seed that found the track.

    Pure rank-based fusion. Ignores raw similarity scores.
    Rank is the post-filter position in the API results (after dropping
    score=None entries). The API returns results sorted by similarity,
    so position in the returned list IS the rank.
    Returns raw scores (caller normalizes).
    """
    if seed_rank_lists is None:
        seed_rank_lists = {}

    # Build rank lookup: seed_id -> {track_id: rank}
    rank_lookup: dict[str, dict[str, int]] = {}
    for seed_id, ordered_ids in seed_rank_lists.items():
        rank_lookup[seed_id] = {tid: rank for rank, tid in enumerate(ordered_ids)}

    raw_scores: dict[str, float] = {}
    for cid, entry in candidates.items():
        seed_scores: dict[str, float] = entry["seed_scores"]
        if not seed_scores:
            continue
        rrf_sum = 0.0
        for seed_id in seed_scores:
            if seed_id in rank_lookup and cid in rank_lookup[seed_id]:
                rank = rank_lookup[seed_id][cid]
            else:
                # Fallback: one past the last known rank for this seed
                rank = len(rank_lookup.get(seed_id, {}))
            rrf_sum += 1.0 / (k + rank)
        raw_scores[cid] = rrf_sum

    return raw_scores


# ---------------------------------------------------------------------------
# Scorer registry
# ---------------------------------------------------------------------------

SCORERS: dict[str, Any] = {
    "rrf": score_rrf_batch,
    "consensus": score_consensus_batch,
    "adventurous": score_adventurous_batch,
}


# ---------------------------------------------------------------------------
# Serendipity computation
# ---------------------------------------------------------------------------


def _compute_serendipity(
    candidates: dict[str, dict[str, Any]],
) -> dict[str, float]:
    """Compute serendipity for each candidate.

    Multi-match (>1 seed): max_seed_score - avg_seed_score.
    Single-match (1 seed): track_score - avg_score_of_all_candidates_from_same_seed.
    """
    # Pre-compute per-seed average scores across all candidates
    seed_score_sums: dict[str, float] = {}
    seed_score_counts: dict[str, int] = {}
    for entry in candidates.values():
        seed_scores: dict[str, float] = entry["seed_scores"]
        for sid, score in seed_scores.items():
            seed_score_sums[sid] = seed_score_sums.get(sid, 0.0) + score
            seed_score_counts[sid] = seed_score_counts.get(sid, 0) + 1

    seed_averages: dict[str, float] = {}
    for sid in seed_score_sums:
        seed_averages[sid] = seed_score_sums[sid] / seed_score_counts[sid]

    result: dict[str, float] = {}
    for cid, entry in candidates.items():
        seed_scores: dict[str, float] = entry["seed_scores"]
        if not seed_scores:
            result[cid] = 0.0
            continue

        if len(seed_scores) > 1:
            # Multi-match: surprise across seeds
            max_s = max(seed_scores.values())
            avg_s = mean(seed_scores.values())
            result[cid] = max_s - avg_s
        else:
            # Single-match: surprise within seed's results
            sid, score = next(iter(seed_scores.items()))
            seed_avg = seed_averages.get(sid, score)
            result[cid] = score - seed_avg

    return result


# ---------------------------------------------------------------------------
# Main fusion function
# ---------------------------------------------------------------------------


def merge_anchor_lists(
    lists: dict[str, list[Track]], seed_groups: dict[str, str], limit: int,
) -> dict[str, list[Track]]:
    """One neighbour list per anchor. An anchor that resolved to several
    recordings takes their neighbours in turn (each one's best, then each
    one's second, ...) cut to ``limit``, so it brings as many candidates, at
    the same ranks, as an anchor with one recording. Keyed by its first id."""
    groups: dict[str, list[str]] = {}
    for sid in lists:
        groups.setdefault(seed_groups.get(sid, sid), []).append(sid)
    merged: dict[str, list[Track]] = {}
    for ids in groups.values():
        out: list[Track] = []
        seen: set[str] = set()
        for row in zip_longest(*(lists[sid] for sid in ids)):
            for track in row:
                if track is not None and track.id not in seen:
                    seen.add(track.id)
                    out.append(track)
        merged[ids[0]] = out[:limit] if len(ids) > 1 else out
    return merged


def fuse_similar_tracks(
    seed_ids: list[str],
    client: CosineClient,
    mode: Literal["centroid", "intersection"] = "centroid",
    limit_per_seed: int = 100,
    match_bonus: float = 0.3,
    redundancy_penalty: float = 2.0,
    scorer: str = "rrf",
    normalize: bool = True,
    exclude_track_ids: set[str] | None = None,
    seed_groups: dict[str, str] | None = None,
    **api_filters,
) -> list[RankedTrack]:
    """
    Fetch similar tracks for each seed and fuse results.

    Args:
        seed_ids: List of cosine.club track IDs
        client: CosineClient instance
        mode: "centroid" (all matches, weighted) or "intersection" (only tracks matching all seeds)
        limit_per_seed: Results to fetch per seed
        match_bonus: Score multiplier bonus per additional seed match
        redundancy_penalty: How aggressively to downweight redundant seeds (0=off, higher=stronger)
        scorer: Scoring strategy — "rrf", "consensus", or "adventurous"
        normalize: Whether to normalize scores to [0, 1]. Set False when
            scores will be compared across independently-fused batches (e.g. chain-dig).
        exclude_track_ids: Additional known seed IDs to remove before ranking
            and artist deduplication, including seeds of earlier chain hops.
        seed_groups: Seed ID -> the anchor it came from; an anchor's
            recordings count as one seed (see ``merge_anchor_lists``).
        **api_filters: Passed to client.similar() (start_year, max_price, etc.)

    Returns:
        List of RankedTrack sorted by final_score descending
    """
    if not seed_ids:
        return []
    if mode not in ("centroid", "intersection"):
        raise ValueError(f"Unsupported fusion mode: {mode}")
    if scorer not in SCORERS:
        raise ValueError(f"Unknown scorer: {scorer!r}. Choose from: {', '.join(SCORERS)}")

    seed_ids = list(dict.fromkeys(seed_ids))
    seed_id_set = set(seed_ids) | (exclude_track_ids or set())

    # ---- Collect candidates ----
    responses = ordered_map(lambda sid: client.similar(sid, limit=limit_per_seed, **api_filters), seed_ids)
    lists = {sid: [t for t in response.similar_tracks if t.score is not None and t.id not in seed_id_set]
             for sid, response in zip(seed_ids, responses)}
    if seed_groups:
        lists = merge_anchor_lists(lists, seed_groups, limit_per_seed)
    candidates: dict[str, dict[str, Any]] = {}
    seed_neighborhoods: dict[str, set[str]] = {sid: set() for sid in lists}
    seed_rank_lists: dict[str, list[str]] = {sid: [] for sid in lists}
    for seed_id, similars in lists.items():
        seen_this_seed: set[str] = set()
        for similar in similars:
            seed_neighborhoods[seed_id].add(similar.id)
            if similar.id not in seen_this_seed:
                seed_rank_lists[seed_id].append(similar.id)
                seen_this_seed.add(similar.id)
            entry = candidates.setdefault(similar.id, {"track": similar, "seed_scores": {}})
            seed_scores = entry["seed_scores"]
            existing = seed_scores.get(seed_id)
            if existing is None or similar.score > existing:
                seed_scores[seed_id] = similar.score

    # ---- Compute seed weights (used by consensus scorer) ----
    seed_weights = _compute_seed_weights(seed_neighborhoods, redundancy_penalty)

    # ---- Apply scorer ----
    scorer_fn = SCORERS[scorer]
    scores = scorer_fn(
        candidates,
        seed_weights,
        match_bonus=match_bonus,
        seed_rank_lists=seed_rank_lists,
    )

    # ---- Normalize scores to [0, 1] ----
    if normalize and scores:
        max_raw = max(scores.values())
        if max_raw > 0:
            scores = {cid: s / max_raw for cid, s in scores.items()}
        else:
            scores = {cid: 0.0 for cid in scores}

    # ---- Compute serendipity ----
    serendipity_scores = _compute_serendipity(candidates)

    # ---- Build RankedTrack list ----
    ranked: list[RankedTrack] = []
    for cid, entry in candidates.items():
        seed_scores: dict[str, float] = entry["seed_scores"]
        if not seed_scores:
            continue
        match_count = len(seed_scores)
        avg_score = mean(seed_scores.values())
        effective_matches = max(1.0, sum(seed_weights[sid] for sid in seed_scores))
        final_score = scores.get(cid, 0.0)
        ranked.append(
            RankedTrack(
                track=entry["track"],
                final_score=final_score,
                match_count=match_count,
                avg_score=avg_score,
                seed_scores=dict(seed_scores),
                effective_matches=round(effective_matches, 3),
                serendipity=round(serendipity_scores.get(cid, 0.0), 4),
            )
        )

    # ---- Filter & sort ----
    if mode == "intersection":
        required = len(seed_rank_lists)
        ranked = [item for item in ranked if item.match_count == required]

    ranked.sort(key=lambda item: item.final_score, reverse=True)

    # ---- Artist deduplication ----
    seen_artists: set[str] = set()
    deduped: list[RankedTrack] = []
    for item in ranked:
        key = item.track.artist.strip().lower()
        if not key or key not in seen_artists:
            seen_artists.add(key)
            deduped.append(item)
    return deduped


def move_why(route: dict, origin_names: dict[str, str] | None = None) -> str:
    """Render the approved direct-attribution wording with requested names.

    Fusion knows origin ids; run_move supplies Resolution.origins' names before
    emitting picks. Nullable similarities never become a claim of closeness.
    """
    names = origin_names or {}
    close = route["close_seed_ids"]
    if len(close) > 1:
        if len(close) == route["seed_count"]:
            return f"close to all {len(close)} seeds"
        return f"close to {len(close)} of your {route['seed_count']} seeds"
    if close:
        return f"close to {names.get(close[0], close[0])}"
    nearest = route["nearest_seed"]
    if nearest is not None:
        return f"nearest to {names.get(nearest, nearest)}"
    seed = route["layout_seed"]
    return f"suggested from {names.get(seed, seed)}; similarity unavailable"


def fuse_move_tracks(seed_ids: list[str], client: CosineClient, *,
                     seed_to_origin: dict[str, str], origin_order: list[str],
                     limit_per_seed: int = 100) -> list[MoveCandidate]:
    """One RRF vote per requested origin, with all raw grouped maxima intact.

    Combine recording aliases before collecting attribution and origin votes. Unmeasured entries
    remain rankable. Exclusions and artist/route admission belong to the shared
    move accumulator, so this function does not discard an artist's alternatives.
    """
    from .api import track_key
    from .discogs import _release_reference
    from .slice_types import MoveCandidate

    if len(set(origin_order)) != len(origin_order) or not origin_order:
        raise ValueError("origin order must contain unique requested origins")
    if type(limit_per_seed) is not int or limit_per_seed < 1:
        raise ValueError("limit per seed must be a positive integer")
    seed_ids = list(dict.fromkeys(seed_ids))
    if any(seed_to_origin.get(seed) not in origin_order for seed in seed_ids):
        raise ValueError("every recording must belong to a requested origin")
    if not seed_ids:
        return []
    responses = ordered_map(lambda seed: client.similar(seed, limit=limit_per_seed), seed_ids)
    sources = [response.source_track for response in responses]
    source_ids = set(seed_ids)
    source_keys = {track_key(track) for track in sources} - {"|"}
    source_videos = {track.video_id for track in sources if track.video_id}
    lists = {seed: [track for track in response.similar_tracks[:limit_per_seed]
                    if track.id not in source_ids and track_key(track) not in source_keys
                    and (not track.video_id or track.video_id not in source_videos)]
             for seed, response in zip(seed_ids, responses)}
    raw: dict[str, dict[str, float | None]] = {}
    recordings: dict[tuple[str, str], Track] = {}
    aliases: dict[str, dict[tuple[str, str | None], None]] = {}
    for seed, tracks in lists.items():
        origin = seed_to_origin[seed]
        for index, track in enumerate(tracks):
            key = track_key(track)
            identity = ("key", key) if key != "|" else ("id", track.id)
            recording = recordings.setdefault(identity, track.model_copy())
            if (not _release_reference(recording.external_link or "")
                    and _release_reference(track.external_link or "")):
                recording.external_link = track.external_link
            aliases.setdefault(recording.id, {})[(track.id, track.video_id)] = None
            scores = raw.setdefault(recording.id, dict.fromkeys(origin_order))
            if track.score is not None and (scores[origin] is None or track.score > scores[origin]):
                scores[origin] = track.score
            tracks[index] = recording
    grouped = merge_anchor_lists(lists, seed_to_origin, limit_per_seed)
    candidates: dict[str, dict[str, Any]] = {}
    ranks = {}
    for seed, tracks in grouped.items():
        ranks[seed] = list(dict.fromkeys(track.id for track in tracks))
        for track in tracks:
            entry = candidates.setdefault(track.id, {"track": track, "seed_scores": {}})
            # RankedTrack's legacy fields require numbers. Only the GUI route
            # carries measured nullable attribution; RRF ignores these values.
            measured = raw[track.id][seed_to_origin[seed]]
            entry["seed_scores"][seed] = measured if measured is not None else 0.0
    weights = _compute_seed_weights({seed: set(ids) for seed, ids in ranks.items()}, 2.0)
    rrf = score_rrf_batch(candidates, weights, seed_rank_lists=ranks)
    basis = max(rrf.values(), default=0.0)
    resolved_origins = set(seed_to_origin[seed] for seed in seed_ids)
    first_resolved = next(origin for origin in origin_order if origin in resolved_origins)
    result = []
    for candidate_id, entry in candidates.items():
        scores = raw[candidate_id]
        measured = [origin for origin in origin_order if scores[origin] is not None]
        nearest = max(measured, key=lambda origin: scores[origin]) if measured else None
        close = [origin for origin in origin_order if scores[origin] is not None and scores[origin] >= 0.70]
        route = {"move": "seed", "further": False, "hop": 0, "via": "seed", "branch": None,
                 "parent_via": None, "parent_pick_ranks": [], "seed_scores": scores,
                 "nearest_seed": nearest, "layout_seed": nearest if nearest is not None else first_resolved,
                 "close_seed_ids": close, "close_count": len(close), "seed_count": len(origin_order),
                 "attribution": "direct", "branch_seed_scores": {},
                 "aliases": [{"cosine_id": cid, "video_id": video} for cid, video in aliases[candidate_id]],
                 "raw_score": rrf[candidate_id], "normalization_basis": basis}
        route["why"] = move_why(route)
        numeric = entry["seed_scores"]
        ranked = RankedTrack(track=entry["track"], final_score=rrf[candidate_id] / basis if basis else 0.0,
                             match_count=len(numeric), avg_score=mean(numeric.values()), seed_scores=numeric,
                             effective_matches=round(max(1.0, sum(weights[seed] for seed in numeric)), 3))
        result.append(MoveCandidate(ranked=ranked, route=route))
    result.sort(key=lambda candidate: (-candidate.ranked.final_score,
                                       track_key(candidate.ranked.track), candidate.ranked.track.id))
    return result
