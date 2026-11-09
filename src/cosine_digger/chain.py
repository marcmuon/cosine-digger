"""Chain-dig orchestration: multi-hop discovery via Discogs graph traversal.

Resolves seeds on cosine.club, extracts label/artist/producer branches from
Discogs metadata, follows them through additional hops, and merges everything
into a single ranked list.  Pure functions, data in/data out -- the CLI layer
handles all display.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Any, Callable, ContextManager, Iterator

import httpx

from . import discogs
from .client import CosineClient, CosineClientError, rate_limit_snapshot
from .config import usable_key
from .discogs import (
    DiscogsError,
    _extract_release_id,
    _fetch_discogs_payload,
    fetch_artist_releases,
    fetch_label_releases,
)
from .fusion import RankedTrack, fuse_move_tracks, fuse_similar_tracks, ordered_map
from .httpcache import HttpCache
from .seeds import resolve_move_origins
from .slice_types import MoveBatch, MoveCandidate, Point, Resolution
from .taste import normalize_artist

if TYPE_CHECKING:
    from .models import Track
    from .playlists import Exclusions

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Budget tracking
# ---------------------------------------------------------------------------


class BudgetExhausted(Exception):
    """Raised when the Discogs request budget has been spent."""


class BudgetTracker:
    """Simple request counter that raises BudgetExhausted at zero."""

    def __init__(self, budget: int) -> None:
        self._remaining = budget
        self._lock = Lock()

    @property
    def remaining(self) -> int:
        with self._lock:
            return self._remaining

    def spend(self) -> None:
        with self._lock:
            if self._remaining <= 0:
                raise BudgetExhausted("Discogs request budget exhausted (started at 0 remaining)")
            self._remaining -= 1


# ---------------------------------------------------------------------------
# Rate-limited Discogs client
# ---------------------------------------------------------------------------


def make_rate_limited_client(
    budget: int = 100, http_cache: HttpCache | None = None, token: str | None = None,
    *, budget_tracker: BudgetTracker | None = None,
    request_slot: Callable[[], ContextManager[None]] | None = None,
) -> tuple[httpx.Client, BudgetTracker]:
    """Create an httpx.Client with rate limiting and a budget tracker.

    The client spaces real requests by 1.1s (Discogs allows 60/min) and
    retries once on 429 after a 5s backoff.  The BudgetTracker raises
    BudgetExhausted when ``budget`` requests have been made. Cache hits
    consume neither budget nor pacing time; the caller owns the cache.
    ``token`` (the current config's Discogs key) wins over any key the
    request carried from the environment.
    GUI workers may share a tracker and inject a request-priority slot; the
    slot wraps real requests and retries only, leaving the shared pace intact.

    Returns:
        (client, tracker) -- caller must close the client when done.
    """
    tracker = budget_tracker if budget_tracker is not None else BudgetTracker(budget)

    transport = _RetryOn429Transport(
        wrapped=httpx.HTTPTransport(), budget_tracker=tracker, http_cache=http_cache, token=token,
        request_slot=request_slot,
    )
    client = httpx.Client(transport=transport)
    return client, tracker


class _RetryOn429Transport(httpx.BaseTransport):
    """Cache GETs, pace real requests, and back off 5s once on 429."""

    def __init__(
        self, wrapped: httpx.BaseTransport,
        budget_tracker: BudgetTracker | None = None,
        http_cache: HttpCache | None = None,
        token: str | None = None,
        request_slot: Callable[[], ContextManager[None]] | None = None,
    ) -> None:
        self._wrapped = wrapped
        try:
            self._token = usable_key(token, "DISCOGS_TOKEN")
        except ValueError as exc:
            raise discogs.DiscogsAuthError(str(exc)) from None
        self._budget_tracker = budget_tracker
        self._http_cache = http_cache
        self._request_slot = request_slot

    def _send(self, request: httpx.Request, backoff: float = 0.0) -> httpx.Response:
        with self._request_slot() if self._request_slot is not None else nullcontext():
            # Another client may have filled the cache while this one queued.
            if self._request_slot is not None:
                cached = self._cached_response(request)
                if cached is not None:
                    return cached
            if self._budget_tracker is not None:
                self._budget_tracker.spend()
            discogs.pace(backoff)  # shared by every thread and client in the process
            response = self._wrapped.handle_request(request)
            if self._request_slot is not None:
                # Publish the complete body before releasing the slot.
                self._store_response(request, response)
            return response

    def _store_response(self, request: httpx.Request, response: httpx.Response) -> None:
        if self._http_cache is not None and request.method == "GET" and response.status_code == 200:
            response.read()
            try:
                self._http_cache.put("discogs:" + str(request.url), response.text, 604800)
            except Exception:
                logger.warning("HTTP cache write failed", exc_info=True)

    def _cached_response(self, request: httpx.Request) -> httpx.Response | None:
        cache = self._http_cache if request.method == "GET" else None
        if cache is not None:
            try:
                body = cache.get("discogs:" + str(request.url))
            except Exception:
                logger.warning("HTTP cache read failed", exc_info=True)
            else:
                if body is not None:
                    return httpx.Response(
                        200, content=body, headers={"content-type": "application/json"},
                        request=request,
                    )
        return None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self._token:
            request.headers["Authorization"] = f"Discogs token={self._token}"
        cached = self._cached_response(request)
        if cached is not None:
            return cached
        response = self._send(request)
        if response.status_code == 429:
            response.close()
            response = self._send(request, backoff=5.0)
        if self._request_slot is None:
            # Library enrichment overrides _send; retain its legacy cache path.
            self._store_response(request, response)
        return response

    def close(self) -> None:
        self._wrapped.close()


# ---------------------------------------------------------------------------
# Branch dataclass
# ---------------------------------------------------------------------------


@dataclass
class Branch:
    """A discovery branch to follow on the next hop."""

    type: str  # "label", "artist", or "producer"
    value: str  # human-readable name
    discogs_id: str | None = None
    score: float = 0.0
    parent_via: str = ""  # e.g. "Artist X on Label Y"


# ---------------------------------------------------------------------------
# Branch extraction
# ---------------------------------------------------------------------------


def extract_branches(
    results: list[RankedTrack],
    client: CosineClient,
    discogs_client: httpx.Client | None,
    taste_profile: Any = None,
    seed_artists: set[str] | None = None,
) -> list[Branch]:
    """Score and rank discovery branches from the top results.

    Examines up to the top 10 results, pulls label/artist/producer info from
    Discogs (or falls back to text search / raw artist name), then scores each
    candidate branch.

    Returns branches sorted by ``branch_score`` descending.
    """
    seed_artists_lower = {a.lower() for a in (seed_artists or set())}

    gap_artists: set[str] = set()
    if taste_profile is not None:
        # Collect all artists from results so we can ask taste for gaps
        result_artist_names = {rt.track.artist for rt in results}
        try:
            gap_artists = taste_profile.gap_artists(result_artist_names)
        except Exception:
            pass
    gap_artists_lower = {a.lower() for a in gap_artists}

    # Counters for cluster_strength
    label_counts: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "discogs_id": None, "parent_via": ""}
    )
    artist_counts: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "discogs_id": None, "parent_via": ""}
    )
    producer_counts: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "discogs_id": None, "parent_via": ""}
    )

    top_n = min(10, len(results))
    for rt in results[:top_n]:
        track = rt.track
        payload: dict[str, Any] | None = None

        # Try to get Discogs metadata from external_link
        if track.external_link and "discogs.com" in track.external_link:
            try:
                extracted = _extract_release_id(track.external_link)
                if extracted is not None:
                    kind, resource_id = extracted
                    payload = _fetch_discogs_payload(kind, resource_id, client=discogs_client)
            except DiscogsError:
                pass

        if payload is not None:
            # Labels
            for label in payload.get("labels") or []:
                label_name = (label.get("name") or "").strip()
                label_id = str(label.get("id", ""))
                if label_name:
                    entry = label_counts[label_name.lower()]
                    entry["count"] += 1
                    if label_id:
                        entry["discogs_id"] = label_id
                    entry["display_name"] = label_name
                    entry["parent_via"] = f"{track.artist} - {track.track}"

            # Artists
            for artist in payload.get("artists") or []:
                artist_name = (artist.get("name") or "").strip()
                artist_id = str(artist.get("id", ""))
                if artist_name:
                    entry = artist_counts[artist_name.lower()]
                    entry["count"] += 1
                    if artist_id:
                        entry["discogs_id"] = artist_id
                    entry["display_name"] = artist_name
                    entry["parent_via"] = f"{track.artist} - {track.track}"

            # Producers / extraartists
            for extra in payload.get("extraartists") or []:
                role = (extra.get("role") or "").lower()
                if any(r in role for r in ("producer", "mixed by", "remix")):
                    prod_name = (extra.get("name") or "").strip()
                    prod_id = str(extra.get("id", ""))
                    if prod_name:
                        entry = producer_counts[prod_name.lower()]
                        entry["count"] += 1
                        if prod_id:
                            entry["discogs_id"] = prod_id
                        entry["display_name"] = prod_name
                        entry["parent_via"] = f"{track.artist} - {track.track}"
        else:
            # FALLBACK 2: extract artist name directly from the Track
            artist_name = track.artist.strip()
            if artist_name:
                entry = artist_counts[artist_name.lower()]
                entry["count"] += 1
                entry["display_name"] = artist_name
                entry["parent_via"] = f"{artist_name} - {track.track}"

    # Build candidate branches and score them
    max_count = max(
        max((e["count"] for e in label_counts.values()), default=0),
        max((e["count"] for e in artist_counts.values()), default=0),
        max((e["count"] for e in producer_counts.values()), default=0),
        1,  # prevent division by zero
    )

    branches: list[Branch] = []

    for key, entry in label_counts.items():
        cluster_strength = entry["count"] / max_count
        novelty = 1.0  # labels are always "novel" in the artist sense
        gap_bonus = 1.0 if taste_profile is not None else 0.0
        branch_score = 0.44 * cluster_strength + 0.33 * novelty + 0.22 * gap_bonus
        branches.append(
            Branch(
                type="label",
                value=entry.get("display_name", key),
                discogs_id=entry["discogs_id"],
                score=round(branch_score, 4),
                parent_via=entry["parent_via"],
            )
        )

    for key, entry in artist_counts.items():
        cluster_strength = entry["count"] / max_count
        novelty = 0.0 if key in seed_artists_lower else 1.0
        gap_bonus = 1.0 if key in gap_artists_lower else 0.0
        branch_score = 0.44 * cluster_strength + 0.33 * novelty + 0.22 * gap_bonus
        branches.append(
            Branch(
                type="artist",
                value=entry.get("display_name", key),
                discogs_id=entry["discogs_id"],
                score=round(branch_score, 4),
                parent_via=entry["parent_via"],
            )
        )

    for key, entry in producer_counts.items():
        cluster_strength = entry["count"] / max_count
        novelty = 0.0 if key in seed_artists_lower else 1.0
        gap_bonus = 1.0 if key in gap_artists_lower else 0.0
        branch_score = 0.44 * cluster_strength + 0.33 * novelty + 0.22 * gap_bonus
        branches.append(
            Branch(
                type="producer",
                value=entry.get("display_name", key),
                discogs_id=entry["discogs_id"],
                score=round(branch_score, 4),
                parent_via=entry["parent_via"],
            )
        )

    branches.sort(key=lambda b: b.score, reverse=True)
    return branches


# ---------------------------------------------------------------------------
# Follow a single branch
# ---------------------------------------------------------------------------


def _resolve_releases(releases: list, cosine_client: CosineClient) -> list[str]:
    """cosine.club ids for Discogs releases, by artist-matched search (a few at once)."""
    releases = [r for r in releases if f"{r.artist} {r.title}".strip()]

    def find(release) -> str | None:
        try:
            tracks = cosine_client.search(f"{release.artist} {release.title}".strip(), limit=5)
        except CosineClientError:
            return None
        return next((track.id for track in tracks if normalize_artist(track.artist) == normalize_artist(release.artist) and track.artist.strip()), None)

    return [tid for tid in ordered_map(find, releases) if tid]


def follow_branch(
    branch: Branch,
    cosine_client: CosineClient,
    discogs_client: httpx.Client | None,
    budget_tracker: BudgetTracker | None = None,
    hop: int = 1,
    limit_per_seed: int = 15,
    scorer: str = "rrf",
    exclude_track_ids: set[str] | None = None,
) -> list[RankedTrack]:
    """Resolve a branch into ranked tracks.

    - label branch with discogs_id  -> fetch label releases, resolve on cosine, fuse
    - artist branch with discogs_id -> fetch artist releases, resolve on cosine, fuse
    - artist branch text-only       -> cosine search by name, fuse similar
    - producer branches treated the same as artist branches

    All returned RankedTracks are tagged with ``hop`` and ``via``.
    """
    via = f"{branch.type}: {branch.value}"
    seed_ids: list[str] = []

    if branch.type == "label" and branch.discogs_id:
        seed_ids = _resolve_releases(
            fetch_label_releases(branch.discogs_id, limit=limit_per_seed, client=discogs_client),
            cosine_client,
        )

    elif branch.type in ("artist", "producer") and branch.discogs_id:
        seed_ids = _resolve_releases(
            fetch_artist_releases(branch.discogs_id, limit=limit_per_seed, client=discogs_client),
            cosine_client,
        )

    else:
        # Text-only fallback: search cosine.club for the artist name
        try:
            search_results = cosine_client.search(branch.value, limit=5)
            match = next((track for track in search_results if normalize_artist(track.artist) == normalize_artist(branch.value) and track.artist.strip()), None)
            if match:
                seed_ids = [match.id]
        except CosineClientError:
            return []

    if not seed_ids:
        return []

    ranked = fuse_similar_tracks(
        seed_ids=seed_ids,
        client=cosine_client,
        limit_per_seed=limit_per_seed,
        scorer=scorer,
        normalize=False,
        exclude_track_ids=exclude_track_ids,
    )

    # Tag every result with hop number and via
    tagged: list[RankedTrack] = []
    for rt in ranked:
        tagged.append(
            rt.model_copy(update={"hop": hop, "via": via})
        )

    return tagged


# ---------------------------------------------------------------------------
# Merge results across hops
# ---------------------------------------------------------------------------


def _merge_results(
    all_results: list[list[RankedTrack]],
    cross_branch_bonus: float = 0.3,
    hop_decay: float = 0.85,
    gap_artists: set[str] | None = None,
) -> list[RankedTrack]:
    """Merge and re-score tracks from multiple hops/branches.

    Scoring per unique track:
        score = max_similarity * (1 + cross_branch_bonus * (appearances - 1))
                * hop_decay ^ min_hop
        if artist in gap_artists: score *= 1.2

    Deduplicates by artist (keeps highest scored per artist).
    """
    gap_lower = {a.lower() for a in (gap_artists or set())}

    # Group by track_id
    grouped: dict[str, list[RankedTrack]] = defaultdict(list)
    for hop_results in all_results:
        for rt in hop_results:
            grouped[rt.track.id].append(rt)

    merged: list[RankedTrack] = []
    for track_id, group in grouped.items():
        max_similarity = max(rt.final_score for rt in group)
        # Revisiting one branch is not independent confirmation.
        appearances = len({rt.via.strip().casefold() or f"hop{rt.hop}" for rt in group})
        min_hop = min(rt.hop for rt in group)

        score = max_similarity * (1 + cross_branch_bonus * (appearances - 1)) * (hop_decay ** min_hop)

        if group[0].track.artist.lower() in gap_lower:
            score *= 1.2

        # Build provenance chain
        provenance: list[str] = []
        for rt in group:
            entry = f"hop{rt.hop}"
            if rt.via:
                entry += f":{rt.via}"
            if entry not in provenance:
                provenance.append(entry)

        # Use the instance with the highest original score as the base
        best = max(group, key=lambda rt: rt.final_score)
        merged.append(
            best.model_copy(
                update={
                    "final_score": round(score, 6),
                    "match_count": appearances,
                    "provenance_chain": provenance,
                    "hop": min_hop,
                }
            )
        )

    # Deduplicate by artist (keep highest scored per artist)
    merged.sort(key=lambda rt: rt.final_score, reverse=True)
    seen_artists: set[str] = set()
    deduped: list[RankedTrack] = []
    for rt in merged:
        key = rt.track.artist.strip().lower()
        if not key or key not in seen_artists:
            seen_artists.add(key)
            deduped.append(rt)

    # Normalize final scores to [0, 1] for display
    if deduped:
        max_score = max(rt.final_score for rt in deduped)
        if max_score > 0:
            deduped = [
                rt.model_copy(update={"final_score": round(rt.final_score / max_score, 6)})
                for rt in deduped
            ]

    return deduped


# ---------------------------------------------------------------------------
# Main orchestration
# ---------------------------------------------------------------------------


ProgressCallback = Callable[[str, dict[str, Any]], None]


def chain_dig(
    seeds: list[str],
    cosine_client: CosineClient,
    hops: int = 2,
    branches: int = 3,
    limit_per_seed: int = 50,
    taste_profile: Any = None,
    on_progress: ProgressCallback | None = None,
    hop_decay: float = 0.85,
    max_discogs_calls: int = 100,
    cross_branch_bonus: float = 0.3,
    scorer: str = "rrf",
    seed_artists: set[str] | None = None,
    http_cache: HttpCache | None = None,
    discogs_token: str | None = None,
    seed_groups: dict[str, str] | None = None,
    discogs_client: httpx.Client | None = None,
) -> list[RankedTrack]:
    """Run a multi-hop chain dig.

    Args:
        seeds: cosine.club track IDs to start from.
        cosine_client: Authenticated CosineClient.
        hops: Number of hops (1 = seeds only, 2 = seeds + one expansion, ...).
        branches: How many branches to follow per hop.
        limit_per_seed: Results to fetch per seed / branch.
        taste_profile: Optional TasteProfile for gap-artist scoring.
        on_progress: Optional callback ``(event, data)`` for live UI updates.
        seed_artists: Resolved seed artist names, if known. Unknown names are
            never inferred from recommended tracks.
        http_cache: Optional caller-owned cache for Discogs GET responses.
        discogs_token: Discogs key to send (else whatever the environment has).
        seed_groups: Seed ID -> anchor; at hop 0 each anchor gets one vote.
        discogs_client: Optional caller-owned client, already paced and budgeted.

    Returns:
        Merged, de-duplicated list of RankedTrack sorted by final_score desc.
    """
    if not seeds:
        return []
    seeds = list(dict.fromkeys(seeds))
    original_seed_ids = set(seeds)

    def _progress(event: str, **data: Any) -> None:
        if on_progress is not None:
            on_progress(event, data)

    own_discogs_client = discogs_client is None
    budget_tracker = None
    if own_discogs_client:
        discogs_client, budget_tracker = make_rate_limited_client(
            budget=max_discogs_calls, http_cache=http_cache, token=discogs_token,
        )

    all_hop_results: list[list[RankedTrack]] = []
    seed_artists = seed_artists or set()
    followed_branches: set[tuple[str, str]] = set()

    try:
        # ---- Hop 0: resolve seeds via cosine.club fusion ----
        _progress("hop_start", hop=0, seed_count=len(seeds))

        try:
            hop0_results = fuse_similar_tracks(
                seed_ids=seeds,
                client=cosine_client,
                limit_per_seed=limit_per_seed,
                scorer=scorer,
                normalize=False,
                seed_groups=seed_groups,
            )
            # Tag hop 0 results
            hop0_tagged = [
                rt.model_copy(update={"hop": 0, "via": "seed"})
                for rt in hop0_results
            ]
            all_hop_results.append(hop0_tagged)
            _progress("hop_done", hop=0, result_count=len(hop0_tagged))
        except CosineClientError as exc:
            _progress("hop_error", hop=0, error=str(exc))
            raise

        # ---- Additional hops ----
        current_results = hop0_tagged

        for hop_num in range(1, hops):
            _progress("hop_start", hop=hop_num, result_count=len(current_results))

            # Extract branches from current results
            try:
                candidate_branches = extract_branches(
                    results=current_results,
                    client=cosine_client,
                    discogs_client=discogs_client,
                    taste_profile=taste_profile,
                    seed_artists=seed_artists,
                )
            except BudgetExhausted:
                _progress("budget_exhausted", hop=hop_num)
                break
            except Exception as exc:
                _progress("branch_error", hop=hop_num, error=str(exc))
                break

            if not candidate_branches:
                _progress("no_branches", hop=hop_num)
                break

            # Emit all discovered branches for trail persistence
            _progress("branches_discovered", hop=hop_num, branches=candidate_branches)

            # Follow top N branches
            top_branches = [
                branch for branch in candidate_branches
                if (branch.type, branch.discogs_id or branch.value.casefold()) not in followed_branches
            ][:branches]
            if not top_branches:
                _progress("no_branches", hop=hop_num)
                break
            hop_results: list[RankedTrack] = []

            for branch in top_branches:
                followed_branches.add((branch.type, branch.discogs_id or branch.value.casefold()))
                _progress("branch_start", hop=hop_num, branch=branch.value, branch_type=branch.type)

                try:
                    branch_results = follow_branch(
                        branch=branch,
                        cosine_client=cosine_client,
                        discogs_client=discogs_client,
                        budget_tracker=budget_tracker,
                        hop=hop_num,
                        limit_per_seed=limit_per_seed,
                        scorer=scorer,
                        exclude_track_ids=original_seed_ids,
                    )
                    hop_results.extend(rt for rt in branch_results if rt.track.id not in original_seed_ids)
                    _progress(
                        "branch_done",
                        hop=hop_num,
                        branch=branch.value,
                        branch_type=branch.type,
                        result_count=len(branch_results),
                    )
                except BudgetExhausted:
                    _progress("budget_exhausted", hop=hop_num, branch=branch.value)
                    break
                except DiscogsError as exc:
                    _progress("branch_error", hop=hop_num, branch=branch.value, error=str(exc))
                    continue
                except CosineClientError as exc:
                    _progress("branch_error", hop=hop_num, branch=branch.value, error=str(exc))
                    continue

            if hop_results:
                all_hop_results.append(hop_results)
                current_results = hop_results

            _progress("hop_done", hop=hop_num, result_count=len(hop_results))

    finally:
        if own_discogs_client and discogs_client is not None:
            discogs_client.close()

    # ---- Merge everything ----
    _progress("merging", hop_count=len(all_hop_results))

    gap_artists: set[str] | None = None
    if taste_profile is not None:
        all_artists = {
            rt.track.artist
            for hop_results in all_hop_results
            for rt in hop_results
        }
        try:
            gap_artists = taste_profile.gap_artists(all_artists)
        except Exception:
            pass

    merged = _merge_results(
        all_results=[[rt for rt in batch if rt.track.id not in original_seed_ids] for batch in all_hop_results],
        cross_branch_bonus=cross_branch_bonus,
        hop_decay=hop_decay,
        gap_artists=gap_artists,
    )

    _progress("done", total_results=len(merged))
    return merged


# GUI batches deliberately do not use chain_dig's joint merge/normalization.
class _MoveAdmission:
    def __init__(self, resolution: Resolution, exclusions: Exclusions, cap: int) -> None:
        self.exclusions, self.cap = exclusions, cap
        self.keys: set[str] = set()
        self.ids: set[str] = set()
        self.videos: set[str] = set()
        self.artists: set[str] = set()
        self.routes: dict[tuple[str, str], int] = defaultdict(int)
        # Why tracks that would have made the step were left out, per batch,
        # each recording counted once however many batches or routes offer it.
        self.hidden: dict[str, int] = defaultdict(int)
        self.hidden_keys: set[str] = set()
        self.hidden_ids: set[str] = set()
        self.hidden_videos: set[str] = set()
        self.exclude_sources(resolution)

    def exclude_sources(self, resolution: Resolution) -> None:
        from .api import track_key

        for track in resolution.resolved_tracks:
            self.ids.add(track.id)
            self.keys.add(track_key(track))
            if track.video_id:
                self.videos.add(track.video_id)
        for point in resolution.origins:
            if point.cosine_id:
                self.ids.add(point.cosine_id)
            if point.track_key:
                self.keys.add(point.track_key)
            elif point.artist and point.title:
                from .api import _key

                self.keys.add(_key(point.artist, point.title))
            if point.video_id:
                self.videos.add(point.video_id)
            for track in point.resolved_tracks or []:
                self.ids.add(track.cosine_id)
                self.keys.add(track.track_key)
                if track.video_id:
                    self.videos.add(track.video_id)
        self.keys.discard("|")

    def admit(self, candidates: list[MoveCandidate], limit: int) -> list[MoveCandidate]:
        from .api import track_key

        accepted = []
        for candidate in candidates:
            if len(accepted) >= limit:
                break
            track = candidate.ranked.track
            key = track_key(track)
            aliases = candidate.route.get("aliases", [{"cosine_id": track.id, "video_id": track.video_id}])
            ids = {alias["cosine_id"] for alias in aliases}
            videos = {alias["video_id"] for alias in aliases if alias["video_id"]}
            artist = normalize_artist(track.artist) or f"track:{key}"
            if key in self.keys or ids & self.ids or videos & self.videos or artist in self.artists:
                continue
            branch = candidate.route.get("branch")
            route_key = _move_branch_key(branch) if branch else None
            labels = [label.name for label in candidate.facts.labels] if candidate.facts else []
            if branch and branch["kind"] == "label":
                labels.append(branch["name"])  # Route mutes do not establish release facts.
            alias_tracks = [track.model_copy(update={"id": alias["cosine_id"], "video_id": alias["video_id"]})
                            for alias in aliases]
            if route_key is not None and self.routes[route_key] >= self.cap:
                continue
            # Checked last, so the count holds only tracks that would otherwise have made the step.
            reason = self._hidden_reason(alias_tracks, labels)
            if reason:
                if not (key in self.hidden_keys or ids & self.hidden_ids or videos & self.hidden_videos):
                    self.hidden[reason] += 1
                    self.hidden_keys.add(key)
                    self.hidden_ids.update(ids)
                    self.hidden_videos.update(videos)
                continue
            self.keys.add(key)
            self.ids.update(ids)
            self.videos.update(videos)
            self.artists.add(artist)
            if route_key is not None:
                self.routes[route_key] += 1
            accepted.append(candidate)
        return accepted

    def _hidden_reason(self, aliases: list[Track], labels: list[str]) -> str | None:
        for alias in aliases:
            reason = self.exclusions.hidden_reason(alias) or next(
                (found for label in labels if (found := self.exclusions.hidden_reason(alias, label))), None)
            if reason:
                return reason
        return None

    def take_hidden(self) -> dict[str, int]:
        counts, self.hidden = dict(self.hidden), defaultdict(int)
        return counts


def _move_branch_key(branch: dict) -> tuple[str, str]:
    return branch["kind"], branch.get("discogs_id") or normalize_artist(branch["name"])


@dataclass
class _MoveBranch:
    branch: Branch
    parents: list[tuple[int, MoveCandidate]]
    roles: list[str]

    def route_branch(self) -> dict:
        result = {"kind": self.branch.type, "name": self.branch.value,
                  "discogs_id": self.branch.discogs_id}
        if self.roles:
            result["roles"] = self.roles.copy()
        return result


def _further_note(reason: str) -> dict:
    code, message = {
        "no_token": ("S-INS-FACTS-NOTOKEN", "Facts need a Discogs token. Add one in Setup."),
        "unavailable": ("S-MAP-DISCOGS-DOWN", "! Discogs isn't answering, so shared labels and people are missing."),
        "budget": ("discogs_budget_exhausted", "Discogs request budget exhausted; remaining facts are unavailable."),
        "missing": ("no_further_branches", "No labels, artists or credited people were found to go one step further. The near tracks are kept."),
        "rate_reserve": ("cosine_rate_reserve", "cosine.club is near its rate limit, so one step further stopped early. The near tracks are kept."),
    }[reason]
    return {"code": code, "params": {}, "message": message, "phase": "further"}


def _extract_move_branches(near: list[MoveCandidate], resolution: Resolution,
                          discogs_client: httpx.Client, notes: list[dict],
                          cancelled: Callable[[], bool]) -> list[_MoveBranch]:
    from .api import track_label

    branches: dict[tuple[str, str], _MoveBranch] = {}
    for rank, candidate in enumerate(near[:10], 1):
        if cancelled():
            break
        facts = candidate.facts
        if facts is None and candidate.facts_state == "loading":
            try:
                facts = discogs.fetch_track_facts(candidate.ranked.track, client=discogs_client)
            except BudgetExhausted:
                notes.append(_further_note("budget"))
                break
            except DiscogsError:
                notes.append(_further_note("unavailable"))
                continue
        if facts is None:
            continue
        # Work on a copy: metadata reads for expansion cannot mutate emitted picks.
        parent = candidate.model_copy(deep=True, update={"facts": facts, "facts_state": "ready"})
        main_ids = {artist.id for artist in facts.artists if artist.id}
        main_names = {normalize_artist(artist.name) for artist in facts.artists}
        main_names.add(normalize_artist(candidate.ranked.track.artist))
        entities = [("label", label, []) for label in facts.labels if label.id]
        entities += [("artist", artist, []) for artist in facts.artists]
        entities += [("person", credit, credit.roles) for credit in facts.credits
                     if credit.id and credit.id not in main_ids
                     and normalize_artist(credit.name) not in main_names
                     and any(normalize_artist(role) not in {"main", "main artist"} for role in credit.roles)]
        for kind, entity, roles in entities:
            key = kind, entity.id or normalize_artist(entity.name)
            entry = branches.setdefault(key, _MoveBranch(
                Branch(type=kind, value=entity.name, discogs_id=entity.id,
                       parent_via=track_label(candidate.ranked.track)), [], []))
            if not any(parent_rank == rank for parent_rank, _ in entry.parents):
                entry.parents.append((rank, parent))
            entry.roles.extend(role for role in roles if role not in entry.roles)
    max_count = max((len(entry.parents) for entry in branches.values()), default=1)
    source_artists = {normalize_artist(track.artist) for track in resolution.resolved_tracks}
    for entry in branches.values():
        novelty = 1.0 if entry.branch.type == "label" else float(normalize_artist(entry.branch.value) not in source_artists)
        entry.branch.score = round(0.44 * len(entry.parents) / max_count + 0.33 * novelty, 4)
    return sorted(branches.values(), key=lambda entry: (
        -entry.branch.score, entry.branch.type, normalize_artist(entry.branch.value), entry.branch.discogs_id or ""))


def _merge_move_branches(branch_results: list[list[MoveCandidate]],
                         resolution: Resolution) -> list[MoveCandidate]:
    from .api import track_key

    grouped: dict[tuple[str, str], list[MoveCandidate]] = defaultdict(list)
    for candidates in branch_results:
        for candidate in candidates:
            track = candidate.ranked.track
            key = track_key(track)
            identity = ("key", key) if key != "|" else ("id", track.id)
            grouped[identity].append(candidate)
    merged = []
    order = [point.point_id for point in resolution.origins]
    first_resolved = next((origin for origin in order if origin in resolution.seed_to_origin.values()), order[0])
    for group in grouped.values():
        best = max(group, key=lambda candidate: candidate.route["raw_score"])
        route = best.route.copy()
        aliases = {(alias["cosine_id"], alias["video_id"]): alias
                   for candidate in group for alias in candidate.route["aliases"]}
        route["aliases"] = list(aliases.values())
        appearances = len({_move_branch_key(candidate.route["branch"]) for candidate in group})
        raw_score = route["raw_score"] * (1 + 0.3 * (appearances - 1)) * 0.85
        scores: dict[str, float | None] = dict.fromkeys(order)
        direct: dict[str, float | None] = {}
        parents = {}
        for candidate in group:
            for origin, score in candidate.route["seed_scores"].items():
                if score is not None and (scores[origin] is None or score > scores[origin]):
                    scores[origin] = score
            for seed, score in candidate.route["branch_seed_scores"].items():
                if seed not in direct or (score is not None and (direct[seed] is None or score > direct[seed])):
                    direct[seed] = score
            parents.update(zip(candidate.route["parent_pick_ranks"], candidate.route["parent_pick_keys"]))
        measured = [origin for origin in order if scores[origin] is not None]
        nearest = max(measured, key=lambda origin: scores[origin]) if measured else None
        close = [origin for origin in order if scores[origin] is not None and scores[origin] >= 0.70]
        route.update(raw_score=raw_score, seed_scores=scores, branch_seed_scores=direct,
                     nearest_seed=nearest, layout_seed=nearest if nearest is not None else first_resolved,
                     close_seed_ids=close, close_count=len(close), seed_count=len(order),
                     parent_pick_ranks=sorted(parents), parent_pick_keys=[parents[rank] for rank in sorted(parents)])
        provenance = list(dict.fromkeys(f"hop1:{candidate.route['via']}" for candidate in group))
        release_link = next((candidate.ranked.track.external_link for candidate in group
                             if discogs._release_reference(candidate.ranked.track.external_link or "")),
                            best.ranked.track.external_link)
        ranked = best.ranked.model_copy(update={"final_score": raw_score, "match_count": appearances,
            "provenance_chain": provenance,
            "track": best.ranked.track.model_copy(update={"external_link": release_link})})
        facts_candidate = next((candidate for candidate in group if candidate.facts is not None), best)
        merged.append(best.model_copy(deep=True, update={"ranked": ranked, "route": route,
                       "facts": facts_candidate.facts, "facts_state": facts_candidate.facts_state}))
    basis = max((candidate.route["raw_score"] for candidate in merged), default=0.0)
    for candidate in merged:
        candidate.route["normalization_basis"] = basis
        candidate.ranked = candidate.ranked.model_copy(update={
            "final_score": candidate.route["raw_score"] / basis if basis else 0.0})
    return sorted(merged, key=lambda candidate: (
        -candidate.ranked.final_score, track_key(candidate.ranked.track), candidate.ranked.track.id))


def iter_move_batches(resolution: Resolution, client: CosineClient, *,
                      discogs_client: httpx.Client | None, exclusions: Exclusions,
                      further: bool, near_limit: int = 25, further_limit: int = 25,
                      branch_limit: int = 3,
                      cancelled: Callable[[], bool] | None = None) -> Iterator[MoveBatch]:
    """Yield a frozen, admitted near prefix before doing any further work.

    The caller owns both clients and completion/facts persistence. Cosine
    failures propagate; Discogs expansion failures only warn and keep near.
    """
    from .api import track_key
    from .fusion import move_why

    if any(type(limit) is not int or limit < 0 for limit in (near_limit, further_limit, branch_limit)):
        raise ValueError("move limits must be nonnegative integers")
    is_cancelled = cancelled if cancelled is not None else lambda: False
    if is_cancelled():
        return
    cap = max(2, (near_limit + (further_limit if further else 0)) // 5)
    admission = _MoveAdmission(resolution, exclusions, cap)
    order = [point.point_id for point in resolution.origins]
    near_candidates = fuse_move_tracks([track.id for track in resolution.resolved_tracks], client,
        seed_to_origin=resolution.seed_to_origin, origin_order=order) if resolution.resolved_tracks else []
    names = {point.point_id: point.name for point in resolution.origins}
    near_candidates = [candidate.model_copy(deep=True) for candidate in near_candidates]
    for candidate in near_candidates:
        candidate.route["further"] = further
        candidate.route["why"] = move_why(candidate.route, names)
        candidate.ranked = candidate.ranked.model_copy(update={"hop": 0, "via": "seed"})
        if discogs_client is None and candidate.facts is None:
            candidate.facts_state = "no_token"
    if is_cancelled():
        return
    near = admission.admit(near_candidates, near_limit)
    yield MoveBatch(hop=0, candidates=near, hidden=admission.take_hidden())
    if not further or is_cancelled():
        return

    notes: list[dict] = []
    branch_results: list[list[MoveCandidate]] = []

    def rate_reserve_reached():
        remaining, reset = rate_limit_snapshot()
        return remaining is not None and remaining < 30 and reset is not None and reset > time.time()

    if discogs_client is None:
        notes.append(_further_note("no_token"))
    elif near and branch_limit and further_limit:
        branches = _extract_move_branches(near, resolution, discogs_client, notes, is_cancelled)[:branch_limit]
        resolved = []
        for entry in branches:
            if is_cancelled():
                return
            if rate_reserve_reached():
                notes.append(_further_note("rate_reserve"))
                break
            branch = entry.branch
            point = Point(point_id=f"{branch.type}:{branch.discogs_id or normalize_artist(branch.value)}",
                          kind=branch.type, name=branch.value, discogs_id=branch.discogs_id)
            try:
                source = resolve_move_origins([point], branch.type, client=client,
                                              discogs_client=discogs_client, cosine_budget=15,
                                              cancelled=is_cancelled)
            except BudgetExhausted:
                notes.append(_further_note("budget"))
                break
            except DiscogsError:
                notes.append(_further_note("unavailable"))
                continue
            # Resolve all branches first so another branch's source is excluded too.
            admission.exclude_sources(source)
            # Branch misses use the further warning, not root-seed resolution copy.
            if source.resolved_tracks:
                resolved.append((entry, source))
        for entry, source in resolved:
            if is_cancelled():
                return
            if rate_reserve_reached():
                notes.append(_further_note("rate_reserve"))
                break
            ids = [track.id for track in source.resolved_tracks]
            candidates = fuse_move_tracks(ids, client, seed_to_origin={seed: seed for seed in ids}, origin_order=ids)
            branch = entry.branch
            via = f"{branch.type}: {branch.value}"
            subject = f"records {branch.value} is credited on" if branch.type == "person" else f"the {branch.type} {branch.value}"
            parent_scores = {origin: max((parent.route["seed_scores"].get(origin) for _, parent in entry.parents
                                         if parent.route["seed_scores"].get(origin) is not None), default=None)
                             for origin in order}
            tagged = []
            for candidate in candidates:
                route = {**candidate.route, "further": True, "hop": 1, "via": via,
                         "branch": entry.route_branch(), "parent_via": branch.parent_via,
                         "parent_pick_ranks": [rank for rank, _ in entry.parents],
                         "parent_pick_keys": [track_key(parent.ranked.track) for _, parent in entry.parents],
                         "seed_scores": parent_scores.copy(), "attribution": "via_parent",
                         "branch_seed_scores": candidate.route["seed_scores"].copy(),
                         "why": f"one step further: sounds like {subject}"}
                tagged.append(candidate.model_copy(deep=True, update={"route": route,
                    "ranked": candidate.ranked.model_copy(update={"hop": 1, "via": via})}))
            branch_results.append(tagged)
    if is_cancelled():
        return
    candidates = _merge_move_branches(branch_results, resolution) if branch_results else []
    if not candidates and not notes and branch_limit and further_limit:
        notes.append(_further_note("missing"))
    notes = list({(note["code"], note["message"]): note for note in notes}.values())
    further_picks = admission.admit(candidates, further_limit)
    yield MoveBatch(hop=1, candidates=further_picks, notes=notes, hidden=admission.take_hidden())
