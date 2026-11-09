"""Daily and weekly playlists built from the taste profile.

A run samples anchors from the feed profile (the tracks your keeps and
library say you like), gathers their neighbours on cosine.club, optionally
follows labels one hop (weekly) and adds fresh finds from Exa, then filters
out anything owned, already served, muted or already known, and spreads the
picks across artists and labels. Every track keeps a plain-words why.

Non-dry runs replace the named private YouTube Music playlist and put every
track the re-read confirms into the served ledger.
"""
from __future__ import annotations

import json
import random
import threading
from contextlib import closing, nullcontext
from pathlib import Path
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from . import api, storage
from . import seeds as _seeds
from .api import Pick, ResolvedSeed, _key, track_key
from .client import CosineClient
from .config import AppConfig, load_config
from .fusion import RankedTrack, fuse_similar_tracks, ordered_map
from .leads import _parse_via
from .locks import LockTimeout, file_lock
from .models import Track
from .profile import PLACEHOLDERS, Anchor, Profile, _feed_judgments, build_feed_profile
from .taste import normalize_artist

KINDS = ("daily", "weekly")
_WRITER = threading.Lock()  # real runs in this process, one at a time (file lock covers other processes)
WRITER_TIMEOUT_S = 900
DEFAULT_NAMES = {"daily": "Digger Daily", "weekly": "Digger Weekly"}
EXPLORE_SHARE = 0.2          # share of anchors drawn from the lower-weighted half


@dataclass(frozen=True)
class Recipe:
    anchors: int            # profile anchors sampled per run
    per_anchor: int         # similar tracks fetched per anchor
    max_per_artist: int
    max_per_label: int
    fresh_share: float      # at most this share of the playlist from fresh finds
    chain_share: float      # share of the stream from the label/artist hop
    exa_queries: int
    deep: bool              # follow labels and artists one hop (Discogs)


RECIPES = {
    "daily": Recipe(anchors=10, per_anchor=60, max_per_artist=1, max_per_label=3,
                    fresh_share=0.2, chain_share=0.0, exa_queries=3, deep=False),
    "weekly": Recipe(anchors=24, per_anchor=80, max_per_artist=2, max_per_label=5,
                     fresh_share=0.15, chain_share=0.3, exa_queries=6, deep=True),
}


class PlaylistError(Exception):
    """The run cannot produce a playlist (empty profile, login missing)."""


@dataclass
class PlanTrack(Pick):
    source: str = "similar"         # similar | chain | fresh
    label: str | None = None
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = super().to_dict()
        out.update(source=self.source, label=self.label, source_url=self.source_url)
        return out


# ---------------------------------------------------------------------------
# What never goes in a playlist
# ---------------------------------------------------------------------------


@dataclass
class Exclusions:
    owned_ids: set[str] = field(default_factory=set)
    owned_keys: set[str] = field(default_factory=set)
    served_ids: set[str] = field(default_factory=set)
    served_keys: set[str] = field(default_factory=set)
    muted_artists: set[str] = field(default_factory=set)
    muted_labels: set[str] = field(default_factory=set)
    known_ids: set[str] = field(default_factory=set)    # saved / dug-from / anchor ids
    known_keys: set[str] = field(default_factory=set)   # seeds, saves, anchors
    artist_labels: dict[str, str] = field(default_factory=dict)       # artist key -> usual label
    muted_label_artists: set[str] = field(default_factory=set)        # artists seen on a muted label
    passed_ids: set[str] = field(default_factory=set)
    passed_keys: set[str] = field(default_factory=set)
    kept_ids: set[str] = field(default_factory=set)     # effective keeps, also in known_*; for counts only
    kept_keys: set[str] = field(default_factory=set)

    def label_for(self, track: Track, label: str | None = None) -> str | None:
        return label or self.artist_labels.get(normalize_artist(track.artist or ""))

    def reason(self, track: Track, label: str | None = None) -> str | None:
        key = track_key(track)
        ids = {track.id, track.video_id or ""} - {""}
        if track.id in self.owned_ids or key in self.owned_keys:
            return "owned"
        if ids & self.served_ids or key in self.served_keys:
            return "served"
        artist = normalize_artist(track.artist or "")
        label = self.label_for(track, label)
        if (artist in self.muted_artists or artist in self.muted_label_artists
                or (label and normalize_artist(label) in self.muted_labels)):
            return "muted"
        if ids & self.passed_ids or key in self.passed_keys:
            return "passed"
        if ids & self.known_ids or key in self.known_keys:
            return "known"
        return None

    def hidden_reason(self, track: Track, label: str | None = None) -> str | None:
        """``reason`` with keeps told apart from other known tracks, for the
        GUI's "hidden from this step" counts; exclusion itself is unchanged."""
        reason = self.reason(track, label)
        if reason == "known" and (({track.id, track.video_id or ""} - {""}) & self.kept_ids
                                  or track_key(track) in self.kept_keys):
            return "kept"
        return reason


def load_exclusions(conn: sqlite3.Connection, profile: Profile, *, scope: str = "legacy") -> Exclusions:
    """Keep recording ledgers independent of taste weights.

    The default retains CLI behavior, including historical passes. Slice and
    feed callers use effective verdicts; GUI suppression never erases owned,
    served, known or legacy save/dig-from history.
    """
    if scope not in ("legacy", "slice", "feed"):
        raise ValueError(f"Unknown exclusion scope: {scope!r}")
    judgments = [] if scope == "legacy" else _feed_judgments(conn)
    ex = Exclusions()
    for track_id, artist, title in conn.execute(
            "SELECT cosine_track_id, artist, title FROM tracks"):
        if track_id:
            ex.owned_ids.add(track_id)
        if artist and title:
            ex.owned_keys.add(_key(artist, title))
    ex.served_ids = storage.served_keys(conn)
    ex.served_keys = {_key(a, t) for a, t in conn.execute(
        "SELECT artist, title FROM served_ledger WHERE artist IS NOT NULL AND title IS NOT NULL")}
    for kind, value in conn.execute("SELECT lead_type, lead_value FROM muted_leads"):
        target = ex.muted_labels if kind.lower() == "label" else ex.muted_artists
        target.add(normalize_artist(value))
    for (raw,) in conn.execute("SELECT seeds FROM sessions"):
        try:
            seeds = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for seed in seeds if isinstance(seeds, list) else []:
            artist, sep, title = str(seed).partition(" - ")
            if sep and "://" not in seed:
                ex.known_keys.add(_key(artist.strip(), title.strip()))
    if scope != "legacy":
        # Pre-ledger sessions can still identify a seed by its result row.
        # Feed anchors deliberately omit seeds, so keep these ids separately.
        for track_id, video_id, artist, title in conn.execute(
                "SELECT track_id,video_id,artist,track_name FROM session_results"):
            if artist and title and _key(artist, title) in ex.known_keys:
                ex.known_ids.update(i for i in (track_id, video_id) if i)
    for anchor in profile.anchors:
        ex.known_keys.add(_key(anchor.artist, anchor.title))
        ex.known_ids.update(i for i in (anchor.track_id, anchor.video_id) if i)
    # The recordings behind every seed you typed, kept apart from the profile's limits.
    for track_id, video_id, artist, title in conn.execute(
            "SELECT track_id, video_id, artist, title FROM known_tracks"):
        ex.known_ids.update(i for i in (track_id, video_id) if i)
        if artist and title:
            ex.known_keys.add(_key(artist, title))
    if scope == "legacy":
        for track_id, video_id, artist, title in conn.execute(
                "SELECT r.track_id, r.video_id, r.artist, r.track_name FROM session_results r "
                "JOIN result_events e ON e.result_id = r.id WHERE e.event_type = 'pass'"):
            ex.passed_ids.update(i for i in (track_id, video_id) if i)
            if artist and title:
                ex.passed_keys.add(_key(artist, title))
    else:
        for judgment in judgments:
            ids = ex.known_ids if judgment["verdict"] == "keep" else ex.passed_ids
            keys = ex.known_keys if judgment["verdict"] == "keep" else ex.passed_keys
            ids.update(judgment["ids"])
            keys.add(judgment["track_key"])
            if judgment["verdict"] == "keep":
                ex.kept_ids.update(judgment["ids"])
                ex.kept_keys.add(judgment["track_key"])
    # Everything you saved or dug from, not just the anchors that made the cut.
    for track_id, video_id, artist, title in conn.execute(
            "SELECT r.track_id, r.video_id, r.artist, r.track_name FROM session_results r "
            "JOIN result_events e ON e.result_id = r.id WHERE e.event_type IN ('save', 'dig_from')"):
        ex.known_ids.update(i for i in (track_id, video_id) if i)
        if artist and title:
            ex.known_keys.add(_key(artist, title))
    # Label estimates affect both similarity mutes and label spread. Feed and
    # slice metadata follows the same explicit-judgment boundary as weights.
    seen_on: dict[str, Counter] = {}
    for artist, label in _artist_label_rows(conn, judgments, scope=scope):
        a, l = normalize_artist(artist or ""), (label or "").strip()
        if a and l and normalize_artist(l) not in PLACEHOLDERS:
            seen_on.setdefault(a, Counter())[l] += 1
    for artist, labels in seen_on.items():
        ex.artist_labels[artist] = labels.most_common(1)[0][0]
        if {normalize_artist(l) for l in labels} & ex.muted_labels:
            ex.muted_label_artists.add(artist)
    return ex


def _artist_label_rows(conn: sqlite3.Connection, judgments: list[dict], *, scope: str):
    yield from conn.execute("SELECT artist, label FROM tracks WHERE label IS NOT NULL")
    if scope == "legacy":
        for artist, via in conn.execute("SELECT artist, via FROM session_results WHERE via LIKE 'label:%'"):
            yield artist, _label_of(via)
        yield from conn.execute("SELECT source_value, target_value FROM discovery_edges "
                                "WHERE source_type = 'artist' AND target_type = 'label'")
    else:
        # Effective legacy verdicts retain via compatibility; GUI verdicts use
        # their judged source's verified facts. Unjudged copies supply neither.
        for judgment in judgments:
            for label in judgment["labels"]:
                yield judgment["artist"], label


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------


def sample_anchors(anchors: list[Anchor], n: int, rng: random.Random) -> list[Anchor]:
    """Pick up to ``n`` anchors, one per artist, favouring heavy ones.

    Most are drawn by weight (so the heaviest anchors come up most days but not
    every day); ``EXPLORE_SHARE`` come uniformly from the lighter half, so the
    playlist also reaches into corners of the profile.
    """
    pool: dict[str, Anchor] = {}
    for anchor in sorted(anchors, key=lambda a: -a.weight):
        pool.setdefault(normalize_artist(anchor.artist), anchor)
    ranked = list(pool.values())
    # Weighted sampling without replacement (Efraimidis-Spirakis keys). A
    # small pool is still returned in the day's weighted order, since callers
    # keep a prefix of it: a fixed order would give the same anchors daily.
    keyed = sorted(ranked, key=lambda a: rng.random() ** (1.0 / max(a.weight, 1e-6)), reverse=True)
    if len(ranked) <= n:
        return keyed
    n_explore = round(n * EXPLORE_SHARE)
    main = keyed[:n - n_explore]
    rest = [a for a in ranked if a not in main]
    lighter = rest[len(rest) // 2:] or rest
    return main + rng.sample(lighter, min(n_explore, len(lighter)))


def resolve_anchors(
    anchors: list[Anchor], client: CosineClient, conn: sqlite3.Connection,
    *, discogs_client=None,
) -> list[list[str]]:
    """Cosine track ids for each anchor (empty when it isn't on cosine.club).

    Resolutions, misses included, are cached in ``http_cache`` so each anchor
    costs network once. A seed cosine.club failed to answer (429/5xx) is not
    a miss and is never cached.
    """
    found: list[list[str] | None] = []
    todo: list[int] = []
    for i, anchor in enumerate(anchors):
        if anchor.track_id:
            found.append([anchor.track_id])
            continue
        cached = storage.cache_get(conn, api.anchor_cache_key(anchor.artist, anchor.title))
        found.append(json.loads(cached) if cached is not None else None)
        if cached is None:
            todo.append(i)
    down = False
    if todo:
        raws = [f"{anchors[i].artist} - {anchors[i].title}" for i in todo]
        for i, seed in zip(todo, api.resolve_seeds(raws, client, seed_budget=2 * len(todo),
                                                  discogs_client=discogs_client)):
            ids = [t.id for t in seed.tracks][:2]
            if not ids and seed.unavailable:
                down = True
                continue  # cosine.club didn't answer: ask again next run, never cache it as a miss
            storage.cache_put(conn, api.anchor_cache_key(anchors[i].artist, anchors[i].title), json.dumps(ids),
                              api.ANCHOR_TTL_S if ids else api.ANCHOR_MISS_TTL_S)
            found[i] = ids
    if down and not any(found):
        raise PlaylistError(f"{_seeds.UNAVAILABLE_NOTE} while resolving your anchors; try again in a minute.")
    return [ids or [] for ids in found]


def artist_fallbacks(
    anchors: list[Anchor], client: CosineClient, conn: sqlite3.Connection,
) -> tuple[list[list[str]], bool]:
    """For anchors whose own track isn't on cosine.club: up to two tracks it
    has by exactly that artist (what a map click digs from). Most library
    anchors aren't indexed, so without this the playlists dig from a few
    dozen of your artists. Cached per artist, misses included; an outage is
    never cached and leaves that anchor out for this run (the second value
    says whether that happened); a rejected API key is raised, never
    mistaken for an artist that isn't there."""
    from .client import CosineRequestError, CosineResponseError

    keys = ["anchor-artist:" + _seeds._norm_artist(a.artist) for a in anchors]
    found: list[list[str] | None] = [None if (c := storage.cache_get(conn, k)) is None else json.loads(c)
                                     for k in keys]
    todo = list(dict.fromkeys(i for i, ids in enumerate(found) if ids is None))

    def lookup(i: int) -> list[str] | None:
        notes: list[str] = []
        try:
            tracks = _seeds.resolve_artist(anchors[i].artist, client, notify=notes.append)
        except (CosineRequestError, CosineResponseError):  # network trouble: ask again next run
            return None
        if not tracks and any(_seeds.UNAVAILABLE_NOTE in n for n in notes):
            return None
        return [t.id for t in tracks]
    down = False
    for i, ids in zip(todo, ordered_map(lookup, todo)):
        if ids is None:
            down = True
            continue
        storage.cache_put(conn, keys[i], json.dumps(ids), api.ANCHOR_TTL_S if ids else api.ANCHOR_MISS_TTL_S)
        for j, key in enumerate(keys):  # another anchor by the same artist
            if key == keys[i]:
                found[j] = ids
    return [ids or [] for ids in found], down


def choose_anchors(
    profile: Profile, n: int, rng: random.Random, client: CosineClient, conn: sqlite3.Connection,
    *, discogs_client=None,
) -> tuple[list[Anchor], dict[str, str]]:
    """Sample three times the anchors needed, then keep the first ``n`` that
    are on cosine.club, holding the exploration share. Returns the anchors
    and a map of their cosine track ids to "Artist - Title" labels (just
    "Artist" when the ids are that artist's other tracks); an anchor's ids
    share its label, which fusion uses to give each anchor one vote."""
    pool = sample_anchors([a for a in profile.anchors if a.weight > 0], 3 * n, rng)
    n_explore = round(len(pool) * EXPLORE_SHARE) if len(pool) >= 3 * n else 0
    main, explore = pool[:len(pool) - n_explore], pool[len(pool) - n_explore:]
    resolved = resolve_anchors(pool, client, conn, discogs_client=discogs_client)
    missing = [i for i, ids in enumerate(resolved) if not ids]
    by_artist = set()
    fallbacks, down = artist_fallbacks([pool[i] for i in missing], client, conn)
    for i, ids in zip(missing, fallbacks):
        if ids:
            resolved[i] = ids
            by_artist.add(id(pool[i]))
    ok = [(anchor, ids) for anchor, ids in zip(pool, resolved) if ids]
    if down and not ok:
        raise PlaylistError(f"{_seeds.UNAVAILABLE_NOTE} while resolving your anchors; try again in a minute.")
    want_explore = round(n * EXPLORE_SHARE) if explore else 0
    picked = [x for x in ok if x[0] in main][:n - want_explore]
    picked += [x for x in ok if x[0] in explore][:want_explore]
    picked += [x for x in ok if x not in picked][:n - len(picked)]
    seed_labels = {tid: a.artist if id(a) in by_artist else f"{a.artist} - {a.title}"
                   for a, ids in picked for tid in ids}
    return [a for a, _ in picked], seed_labels


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _label_of(via: str) -> str | None:
    parsed = _parse_via(via or "")
    return parsed[1] if parsed and parsed[0] == "label" else None


def _as_plan_tracks(ranked: list[RankedTrack], seed_labels: dict[str, str], source: str) -> list[PlanTrack]:
    out = []
    for item in ranked:
        why, matched = api.explain(item, seed_labels)
        out.append(PlanTrack(track=item.track, score=item.final_score, why=why, matched_seeds=matched,
                             via=item.via, hop=item.hop, source=source, label=_label_of(item.via)))
    return out


def _fresh_why(source_url: str, source_title: str | None = None) -> str:
    domain = urlparse(source_url or "").netloc.removeprefix("www.")
    where = f" on {domain}" if domain else ""
    title = f": {source_title[:80]}" if source_title else ""
    return f"New release spotted{where}{title}"


def fresh_candidates(
    conn: sqlite3.Connection, profile: Profile, client: CosineClient, *,
    api_key: str | None, max_queries: int, max_finds: int, kind: str,
) -> tuple[list[PlanTrack], dict]:
    """New Exa finds (bounded per run) plus earlier finds not yet served."""
    diagnostics: dict[str, Any] = {"skipped": "no EXA_API_KEY"} if not api_key else {}
    if api_key:
        from .fresh import FreshAuthError, find_fresh, save_finds

        try:
            finds, diagnostics = find_fresh(
                [e.name for e in profile.top("label", 6)], [e.name for e in profile.top("artist", 6)],
                api_key=api_key, client=client, max_queries=max_queries, max_finds=max_finds,
            )
        except FreshAuthError as exc:
            diagnostics = {"errors": [{"stage": "auth", "error": str(exc) or "Exa rejected the key"}]}
        else:
            save_finds(conn, f"{kind} playlist", finds)
    out, seen = [], set()
    for artist, title, track_id, video_id, source_url in conn.execute(
            "SELECT artist, title, track_id, video_id, source_url FROM fresh_finds "
            "WHERE video_id IS NOT NULL ORDER BY found_at DESC, id DESC LIMIT 200"):
        key = _key(artist or "", title or "")
        if key in seen:
            continue
        seen.add(key)
        track = Track(id=track_id or f"yt:{video_id}", name=title, artist=artist, track=title,
                      video_id=video_id)
        out.append(PlanTrack(track=track, score=0.0, why=_fresh_why(source_url), source="fresh",
                             via="fresh", source_url=source_url))
    return out, diagnostics


def interleave(streams: list[tuple[list[PlanTrack], float]]) -> list[PlanTrack]:
    """Merge ranked streams so each gets its share of every stretch of the list
    (smooth weighted round-robin); an empty stream hands its turns to the rest."""
    queues = [(list(items), weight) for items, weight in streams if items and weight > 0]
    credit = [0.0] * len(queues)
    out: list[PlanTrack] = []
    while any(q for q, _ in queues):
        live = [i for i, (q, _) in enumerate(queues) if q]
        total = sum(queues[i][1] for i in live)
        for i in live:
            credit[i] += queues[i][1]
        best = max(live, key=lambda i: credit[i])
        credit[best] -= total
        out.append(queues[best][0].pop(0))
    return out


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select(
    stream: list[PlanTrack], size: int, recipe: Recipe, ex: Exclusions, *, fresh_cap: int,
) -> tuple[list[PlanTrack], Counter]:
    """Walk the merged stream, drop what must never be served, and keep artist
    and label spread. If the caps leave the list short, relax them in stages
    (never the exclusions)."""
    def ident(item: PlanTrack) -> set[str]:
        return {item.track.id, item.track.video_id or "", track_key(item.track)} - {""}

    # One recording can arrive from several sources with different labels:
    # every copy knows every label, and a muted one on any copy mutes them all.
    labels_of: dict[str, set[str]] = {}
    for item in stream:
        if item.label:
            for i in ident(item):
                labels_of.setdefault(i, set()).add(item.label)
    dropped: Counter = Counter()
    eligible: list[PlanTrack] = []
    for item in stream:
        labels = set().union(*(labels_of.get(i, set()) for i in ident(item)))
        item.label = item.label or (min(labels) if labels else None)
        item.label = ex.label_for(item.track, item.label)
        reason = ex.reason(item.track, item.label)
        if reason is None and any(ex.reason(item.track, label) == "muted" for label in labels):
            reason = "muted"
        if reason is None and not item.track.video_id:
            reason = "no_video"
        if reason is None and not item.why:
            reason = "missing_why"
        if reason:
            dropped[reason] += 1
            continue
        eligible.append(item)

    chosen: list[PlanTrack] = []
    chosen_ids: set[str] = set()
    duplicates: set[int] = set()
    taken: set[int] = set()
    stages = [(recipe.max_per_artist, recipe.max_per_label),
              (recipe.max_per_artist + 1, recipe.max_per_label * 2),
              (size, size)]
    for max_artist, max_label in stages:
        artists: Counter = Counter(normalize_artist(p.track.artist or "") for p in chosen)
        labels: Counter = Counter(normalize_artist(p.label) for p in chosen if p.label)
        fresh = sum(1 for p in chosen if p.source == "fresh")
        for i, item in enumerate(eligible):
            if len(chosen) >= size:
                break
            if i in taken:
                continue
            if ident(item) & chosen_ids:  # another copy of this recording is already in
                duplicates.add(i)
                continue
            artist = normalize_artist(item.track.artist or "")
            label = normalize_artist(item.label) if item.label else None
            if artists[artist] >= max_artist or (label and labels[label] >= max_label):
                continue
            if item.source == "fresh" and fresh >= fresh_cap:
                continue
            taken.add(i)
            chosen.append(item)
            chosen_ids |= ident(item)
            artists[artist] += 1
            if label:
                labels[label] += 1
            fresh += item.source == "fresh"
        if len(chosen) >= size:
            break
    if duplicates:
        dropped["duplicate"] += len(duplicates)
    return chosen, dropped


def verify(tracks: list[PlanTrack], ex: Exclusions) -> dict[str, int]:
    """Recount the finished plan against every rule, independent of selection."""
    reasons = Counter(ex.reason(p.track, p.label) for p in tracks)
    idents = Counter(track_key(p.track) for p in tracks)
    return {
        "owned": reasons["owned"],
        "served": reasons["served"],
        "muted": reasons["muted"],
        "passed": reasons["passed"],
        "known": reasons["known"],
        "missing_why": sum(1 for p in tracks if not p.why),
        "no_video": sum(1 for p in tracks if not p.track.video_id),
        "duplicates": sum(n - 1 for n in idents.values() if n > 1),
    }


# ---------------------------------------------------------------------------
# Plan and run
# ---------------------------------------------------------------------------


def plan_playlist(
    kind: str,
    conn: sqlite3.Connection,
    client: CosineClient,
    *,
    cfg: AppConfig | None = None,
    name: str | None = None,
    now: datetime | None = None,
    http_cache=None,
    notify: Callable[[str], None] | None = None,
    discogs_client=None,
) -> dict[str, Any]:
    if kind not in RECIPES:
        raise ValueError(f"Unknown playlist kind: {kind!r} (daily or weekly)")
    cfg = cfg or load_config()
    now = now or datetime.now().astimezone()
    recipe = RECIPES[kind]
    size = cfg.daily_size if kind == "daily" else cfg.weekly_size
    say = notify or (lambda _msg: None)
    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    profile = build_feed_profile(conn, now=now)
    ex = load_exclusions(conn, profile, scope="feed")
    rng = random.Random(f"{kind}:{now.date().isoformat()}")
    if not any(a.weight > 0 for a in profile.anchors):
        raise PlaylistError("Your feed profile is empty: keep a track or scan a music folder.")
    anchors, seed_labels = choose_anchors(profile, recipe.anchors, rng, client, conn,
                                         discogs_client=discogs_client)
    timings["anchors_s"] = round(time.perf_counter() - t0, 2)
    if not seed_labels:
        raise PlaylistError("None of your profile's anchor tracks are on cosine.club yet; "
                            "keep a track or scan a music folder.")
    say(f"{len(anchors)} anchors from your profile")

    t1 = time.perf_counter()
    exclude = ex.owned_ids | ex.served_ids
    similar = _as_plan_tracks(fuse_similar_tracks(
        list(seed_labels), client, mode="centroid", limit_per_seed=recipe.per_anchor,
        scorer="rrf", exclude_track_ids=exclude or None, seed_groups=seed_labels,
    ), seed_labels, "similar")
    timings["similar_s"] = round(time.perf_counter() - t1, 2)
    say(f"{len(similar)} neighbours on cosine.club")

    chained: list[PlanTrack] = []
    if recipe.deep:
        from .chain import chain_dig

        t2 = time.perf_counter()
        seed_artists = {normalize_artist(a.artist) for a in anchors}
        ranked = chain_dig(list(seed_labels)[:8], client, hops=2, branches=4, limit_per_seed=40,
                           max_discogs_calls=60, seed_artists=seed_artists, http_cache=http_cache,
                           discogs_token=cfg.discogs_token, seed_groups=seed_labels,
                           discogs_client=discogs_client)
        chained = _as_plan_tracks([r for r in ranked if r.hop >= 1], seed_labels, "chain")
        timings["chain_s"] = round(time.perf_counter() - t2, 2)
        say(f"{len(chained)} tracks one hop out through labels and artists")

    t3 = time.perf_counter()
    fresh_cap = int(size * recipe.fresh_share)
    fresh, fresh_diag = fresh_candidates(conn, profile, client, api_key=cfg.exa_api_key,
                                         max_queries=recipe.exa_queries, max_finds=2 * fresh_cap,
                                         kind=kind)
    timings["fresh_s"] = round(time.perf_counter() - t3, 2)

    stream = interleave([
        (fresh, recipe.fresh_share),
        (chained, recipe.chain_share),
        (similar, 1.0 - recipe.fresh_share - recipe.chain_share),
    ])
    tracks, dropped = select(stream, size, recipe, ex, fresh_cap=fresh_cap)
    warnings = []
    if len(tracks) < size:
        warnings.append(f"Only {len(tracks)} of {size} tracks after filtering; "
                        "keep a track or scan a music folder to widen the profile.")
    timings["total_s"] = round(time.perf_counter() - t0, 2)
    return {
        "kind": kind,
        "name": name or DEFAULT_NAMES[kind],
        "date": now.date().isoformat(),
        "tracks": [p.to_dict() for p in tracks],
        "checks": verify(tracks, ex),
        "sources": {"anchors": len(anchors), "anchor_tracks": len(seed_labels),
                    "similar": len(similar), "chain": len(chained), "fresh": len(fresh),
                    **{f"picked_{s}": n for s, n in Counter(p.source for p in tracks).items()}},
        "dropped": dict(dropped),
        "anchors": [f"{a.artist} - {a.title}" for a in anchors],
        "fresh": fresh_diag,
        "warnings": warnings,
        "timings": timings,
        "export": None,
        "_picks": tracks,
    }


def run_playlist(
    kind: str,
    *,
    cfg: AppConfig | None = None,
    conn: sqlite3.Connection | None = None,
    client: CosineClient | None = None,
    sink=None,
    name: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    notify: Callable[[str], None] | None = None,
    log_run: bool = False,
    discogs_client=None,
) -> dict[str, Any]:
    """Plan a daily or weekly playlist and (unless ``dry_run``) replace the
    named private YouTube Music playlist with it. Raises PlaylistError when
    nothing can be built; export problems are reported in the result.

    ``log_run`` is for manual runs (app, CLI): a real run of the default-named
    playlist is logged in ``job_runs`` like a scheduled one, so its card shows
    the outcome, and a success counts as the current scheduled occurrence."""
    cfg = cfg or load_config()
    run = lambda: _run_playlist(kind, cfg=cfg, conn=conn, client=client, sink=sink, name=name,  # noqa: E731
                                dry_run=dry_run, now=now, notify=notify, discogs_client=discogs_client)
    if dry_run:
        return run()
    if not log_run or (name or DEFAULT_NAMES[kind]) != DEFAULT_NAMES[kind]:
        return _locked(run, cfg, conn)
    from .schedule import STALE_AFTER_S, _safe_detail

    with (closing(api.open_db(cfg.db_path)) if conn is None else nullcontext(conn)) as log:
        if log.in_transaction:
            log.commit()
        log.execute("BEGIN IMMEDIATE")
        try:
            running = log.execute(
                "SELECT started_at FROM job_runs WHERE job = ? AND status = 'running' "
                "ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
            if running and (datetime.now(timezone.utc).timestamp()
                            - datetime.fromisoformat(running[0]).timestamp()) < STALE_AFTER_S:
                raise PlaylistError(f"A {kind} run is already going; it will finish on its own.")
            run_id = storage.job_start(log, kind, None)  # commits the check and insert together
        except BaseException:
            log.rollback()
            raise
        try:
            result = _locked(run, cfg, conn)
        except BaseException as exc:
            storage.job_finish(log, run_id, "error", _safe_detail(exc) if isinstance(exc, Exception) else "interrupted")
            raise
        try:
            status, detail = "ok", run_outcome(result)
        except PlaylistError as exc:
            status, detail = "error", _safe_detail(exc)
        storage.job_finish(log, run_id, status, detail)
        return result


def _locked(run: Callable[[], dict[str, Any]], cfg: AppConfig, conn: sqlite3.Connection | None) -> dict[str, Any]:
    # Real runs (daily, weekly; app, scheduler, OS schedule, CLI) take turns, and
    # each plans inside the lock so it sees what the previous run just served.
    if not _WRITER.acquire(timeout=WRITER_TIMEOUT_S):
        raise PlaylistError("Another playlist run is still going; try again when it finishes.")
    try:
        db_file = api._db_file(conn) if conn is not None else Path(cfg.db_path)
        guard = file_lock("playlists", home=db_file.parent, timeout=WRITER_TIMEOUT_S) if db_file else nullcontext()
        try:
            with guard:
                return run()
        except LockTimeout as exc:
            raise PlaylistError(str(exc)) from None
    finally:
        _WRITER.release()


def _run_playlist(kind, *, cfg, conn, client, sink, name, dry_run, now, notify,
                  discogs_client=None) -> dict[str, Any]:
    own_conn, own_client = conn is None, client is None
    conn = conn if conn is not None else api.open_db(cfg.db_path)
    client = client or api.make_client(cfg)
    cache = api._db_cache(conn) if RECIPES.get(kind, RECIPES["daily"]).deep else None
    try:
        if not dry_run and sink is None:
            from .youtube import YTMusicSink

            try:
                sink = YTMusicSink(id_store=api.DbIdStore(conn))
            except Exception as exc:  # fail before spending any lookups
                raise PlaylistError(api._export_error(exc)) from exc
        result = plan_playlist(kind, conn, client, cfg=cfg, name=name, now=now,
                               http_cache=cache, notify=notify, discogs_client=discogs_client)
        picks = result.pop("_picks")
        if dry_run or not picks:
            return result
        t0 = time.perf_counter()
        description = (f"{DEFAULT_NAMES[kind]} from cosine-digger, refreshed {result['date']}. "
                       "Picked from your taste profile; nothing you own or were already served.")
        try:
            def still_allowed(candidates):
                # Rechecked under the ledger lock: a manual export, a library
                # scan or a mute may have landed while this plan was being made.
                fresh_ex = load_exclusions(conn, build_feed_profile(conn, now=now), scope="feed")
                return [p for p in candidates if fresh_ex.reason(p.track, getattr(p, "label", None)) is None]

            report = api.export(picks, result["name"], sink=sink, conn=conn, description=description,
                                drop_served=True, recheck=still_allowed)
        except Exception as exc:
            result["export_error"] = api._export_error(exc)
        else:
            result["export"] = report.to_dict()
            result["playlist_url"] = report.playlist_url
            if report.omitted:
                result["warnings"].append(f"{len(report.omitted)} picks left out at export "
                                          "(served by another playlist meanwhile, or no video)")
        result["timings"]["export_s"] = round(time.perf_counter() - t0, 2)
        return result
    finally:
        if cache is not None:
            cache.close()
        if own_client:
            client.close()
        if own_conn:
            conn.close()


def make_runner(cfg: AppConfig, **deps) -> Callable[[str, datetime], str]:
    """The scheduler's job runner: one playlist run per call, its own DB
    connection, a one-line summary for ``job_runs.detail``. Raises when the
    playlist was not written, so the job stays due."""

    def run(job: str, scheduled_for: datetime) -> str:
        return run_outcome(run_playlist(job, cfg=cfg, **deps))

    return run


def run_outcome(result: dict[str, Any]) -> str:
    """A real run's one-line summary for ``job_runs.detail``; raises
    PlaylistError unless the playlist was written and verified."""
    if result.get("export_error"):
        raise PlaylistError(result["export_error"])
    report = result.get("export") or {}
    if not result["tracks"] or not report:
        raise PlaylistError("No playlist was written: " + ("; ".join(result.get("warnings") or []) or "no tracks"))
    if not report.get("verified"):
        # The tracks are already in the served ledger; the retry builds a new plan.
        raise PlaylistError(f"Playlist not verified: {len(report.get('missing', []))} missing, "
                            f"{len(report.get('failed', []))} failed ({report.get('playlist_url')})")
    return f"{len(result['tracks'])} tracks, verified: {report.get('playlist_url')}"
