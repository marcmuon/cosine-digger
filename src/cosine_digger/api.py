"""Application API: console-free use cases shared by the CLI, the web app,
the /dig skill and other programs that import this module.

Every function takes its collaborators (cosine client, playlist sink, DB
connection) as arguments or builds the default ones from ``load_config()``.
Nothing here prints; progress goes to an optional ``notify`` callback.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Iterator, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from . import seeds as _seeds
from .client import CosineClient
from .config import scrub  # noqa: F401  (api.scrub is the public name)
from .fusion import RankedTrack, fuse_similar_tracks
from .models import Track
from .slice_types import MoveEvent, MoveKind, Point, StepIdentity

if TYPE_CHECKING:
    from .playlists import Exclusions

Notify = Callable[[str], None]

_STYLE = r"(?:dim|bold|italic|underline|strike|reverse|blink|black|red|green|yellow|blue|magenta|cyan|white|default)"
_MARKUP = re.compile(rf"\[(?:/|/?{_STYLE}(?: +{_STYLE})*)\]")


def plain(message: str) -> str:
    """Strip rich markup tags like [dim]...[/dim] from a progress line."""
    return _MARKUP.sub("", message)


def make_client(cfg=None) -> CosineClient:
    """A cosine.club client with the key in these settings and only that key: settings
    without one give a client without one (CosineKeyMissing), never the environment's,
    where a key saved meanwhile is also written."""
    from .config import load_config

    cfg = cfg or load_config()
    return CosineClient(token=cfg.cosine_token or "")


ANCHOR_TTL_S = 90 * 86400      # resolved "Artist - Title" -> cosine track ids
ANCHOR_MISS_TTL_S = 7 * 86400


def anchor_cache_key(artist: str, title: str) -> str:
    return "anchor:" + _key(artist, title)


def track_label(track: Track) -> str:
    title = track.track or track.name
    return f"{track.artist} - {title}" if track.artist else title


def _key(artist: str, title: str) -> str:
    return f"{_seeds._norm_artist(artist)}|{_seeds.recording_title(title)}"


def track_key(track: Track) -> str:
    """Identity for "same recording" checks across sources (owned, served)."""
    return _key(track.artist, track.track or track.name)


# ---------------------------------------------------------------------------
# Seeds
# ---------------------------------------------------------------------------


@dataclass
class ResolvedSeed:
    raw: str
    tracks: list[Track] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.tracks)

    @property
    def unavailable(self) -> bool:
        """cosine.club failed (429/5xx) while resolving: a miss that proves nothing."""
        return any(_seeds.UNAVAILABLE_NOTE in note for note in self.notes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "ok": self.ok,
            "tracks": [{"id": t.id, "label": track_label(t), "video_id": t.video_id} for t in self.tracks],
            "notes": self.notes,
        }


def _is_multi_track(raw: str) -> bool:
    low = raw.lower()
    return "discogs.com" in low or ("http" not in low and " - " in raw)


def resolve_seeds(
    raw_seeds: list[str],
    client: CosineClient,
    *,
    seed_budget: int = 10,
    notify: Notify | None = None,
    workers: int = 4,
    release_meta: Callable[[str], dict] | None = None,
    discogs_client=None,
) -> list[ResolvedSeed]:
    """Resolve every seed, in parallel, keeping input order.

    Seeds that can expand to several tracks (releases, labels, albums) share a
    budget so one long album cannot crowd out the others.
    """
    raw_seeds = [s for s in dict.fromkeys(s.strip() for s in raw_seeds) if s]
    multi = sum(1 for s in raw_seeds if _is_multi_track(s))
    per_seed = max(1, seed_budget // multi) if multi > 1 else 5

    def one(raw: str) -> ResolvedSeed:
        notes: list[str] = []

        def say(message: str) -> None:
            notes.append(plain(message))
            if notify:
                notify(message)

        tracks = _seeds.resolve_seed(
            raw, client, max_per_release=per_seed, notify=say, release_meta=release_meta,
            discogs_client=discogs_client,
        )
        return ResolvedSeed(raw=raw, tracks=tracks, notes=notes)

    if len(raw_seeds) <= 1 or workers <= 1:
        return [one(s) for s in raw_seeds]
    with ThreadPoolExecutor(max_workers=min(workers, len(raw_seeds))) as pool:
        return list(pool.map(one, raw_seeds))


# ---------------------------------------------------------------------------
# Digging
# ---------------------------------------------------------------------------


@dataclass
class Pick:
    track: Track
    score: float
    why: str
    matched_seeds: list[str] = field(default_factory=list)
    via: str = ""
    hop: int = 0

    def to_dict(self) -> dict[str, Any]:
        t = self.track
        return {
            "id": t.id,
            "artist": t.artist,
            "title": t.track or t.name,
            "video_id": t.video_id,
            "youtube_url": f"https://music.youtube.com/watch?v={t.video_id}" if t.video_id else None,
            "discogs_url": t.external_link,
            "score": round(self.score, 4),
            "why": self.why,
            "matched_seeds": self.matched_seeds,
            "via": self.via,
            "hop": self.hop,
        }


def explain(item: RankedTrack, seed_labels: dict[str, str]) -> tuple[str, list[str]]:
    """One plain sentence on why a track surfaced, from its real route."""
    matched = list(dict.fromkeys(
        seed_labels[sid]
        for sid, _score in sorted(item.seed_scores.items(), key=lambda kv: -kv[1])
        if sid in seed_labels
    ))
    parts = []
    if matched:
        head = " and ".join(matched[:2])
        more = f" (+{len(matched) - 2} more)" if len(matched) > 2 else ""
        parts.append(f"Close to {head}{more}")
    if item.via and item.via != "seed":
        parts.append(f"found via {item.via}")
    why = "; ".join(parts) if parts else "Similar to your seeds"
    return why[0].upper() + why[1:], matched


def to_picks(ranked: list[RankedTrack], seeds: list[ResolvedSeed]) -> list[Pick]:
    # Name seeds the way the user typed them ("Artist - Album"), not per track;
    # fall back to the track label for pasted URLs.
    seed_labels = {
        t.id: (s.raw if "://" not in s.raw else track_label(t))
        for s in seeds for t in s.tracks if t.artist or "://" not in s.raw
    }
    picks = []
    for item in ranked:
        why, matched = explain(item, seed_labels)
        picks.append(Pick(
            track=item.track, score=item.final_score, why=why,
            matched_seeds=matched, via=item.via, hop=item.hop,
        ))
    return picks


def _filter_excluded(
    ranked: list[RankedTrack],
    exclusions: Exclusions | None,
    excluded: dict[str, int] | None,
) -> list[RankedTrack]:
    """Apply the shared rules to all candidates before limiting the picks."""
    if exclusions is None:
        return ranked
    from .leads import _parse_via

    kept = []
    for item in ranked:
        route = _parse_via(item.via)
        label = route[1] if route and route[0] == "label" else None
        reason = exclusions.reason(item.track, label)
        if reason is None:
            kept.append(item)
        elif excluded is not None:
            excluded[reason] = excluded.get(reason, 0) + 1
    return kept


def dig(
    seeds: list[ResolvedSeed],
    client: CosineClient,
    *,
    limit: int = 25,
    scorer: str = "rrf",
    mode: str = "centroid",
    limit_per_seed: int = 100,
    exclude_ids: set[str] | None = None,
    exclude_keys: set[str] | None = None,
    exclusions: Exclusions | None = None,
    excluded: dict[str, int] | None = None,
) -> list[Pick]:
    """Fuse similar tracks across all resolved seeds and explain each pick.

    ``exclude_ids`` are cosine track ids or YouTube video ids; ``exclude_keys``
    are ``track_key`` identities (owned or already-served recordings).
    ``exclusions`` applies playlist rules; ``excluded`` collects their counts.
    """
    seed_ids = list(dict.fromkeys(t.id for s in seeds for t in s.tracks))
    if not seed_ids:
        return []
    exclude_ids = set(exclude_ids or ())
    exclude_keys = set(exclude_keys or ())
    seed_keys = {track_key(t) for s in seeds for t in s.tracks if t.artist}
    ranked = fuse_similar_tracks(
        seed_ids, client, mode=mode, limit_per_seed=limit_per_seed, scorer=scorer,
        exclude_track_ids=exclude_ids or None,
    )
    kept = [
        r for r in _filter_excluded(ranked, exclusions, excluded)
        if r.track.id not in exclude_ids
        and (r.track.video_id or "") not in exclude_ids
        and track_key(r.track) not in exclude_keys
        and track_key(r.track) not in seed_keys
    ]
    return to_picks(kept[:limit], seeds)


def chain(
    seeds: list[ResolvedSeed],
    client: CosineClient,
    *,
    limit: int = 25,
    hops: int = 2,
    branches: int = 3,
    max_discogs_calls: int = 40,
    scorer: str = "rrf",
    on_progress: Callable[[str, dict], None] | None = None,
    exclude_ids: set[str] | None = None,
    exclude_keys: set[str] | None = None,
    exclusions: Exclusions | None = None,
    excluded: dict[str, int] | None = None,
    http_cache=None,
    max_per_route: int | None = None,
    discogs_token: str | None = None,
) -> list[Pick]:
    """Go past the seeds' neighbours by following the labels and artists of
    the first results (Discogs), then explain each pick by its route.

    ``max_per_route`` caps how many picks one label or artist branch may
    supply (default: a fifth of ``limit``), so one prolific label cannot fill
    the list. ``discogs_token`` defaults to the current saved config."""
    from .chain import chain_dig

    seed_ids = list(dict.fromkeys(t.id for s in seeds for t in s.tracks))
    if not seed_ids:
        return []
    exclude_ids = set(exclude_ids or ())
    exclude_keys = set(exclude_keys or ())
    seed_keys = {track_key(t) for s in seeds for t in s.tracks if t.artist}
    seed_artists = {_seeds._norm_artist(t.artist) for s in seeds for t in s.tracks if t.artist}
    if discogs_token is None:
        from .config import load_config

        discogs_token = load_config().discogs_token
    ranked = chain_dig(
        seed_ids, client, hops=hops, branches=branches, scorer=scorer,
        max_discogs_calls=max_discogs_calls, on_progress=on_progress, seed_artists=seed_artists,
        http_cache=http_cache, discogs_token=discogs_token,
    )
    cap = max_per_route if max_per_route is not None else max(2, limit // 5)
    per_route: dict[str, int] = {}
    kept = []
    for r in _filter_excluded(ranked, exclusions, excluded):
        if (r.track.id in exclude_ids
                or (r.track.video_id or "") in exclude_ids
                or track_key(r.track) in exclude_keys
                or track_key(r.track) in seed_keys):
            continue
        route = r.via or "seed"
        if route != "seed" and per_route.get(route, 0) >= cap:
            continue
        per_route[route] = per_route.get(route, 0) + 1
        kept.append(r)
        if len(kept) >= limit:
            break
    return to_picks(kept, seeds)


# ---------------------------------------------------------------------------
# GUI moves (independent of the CLI's sessions and serializers)
# ---------------------------------------------------------------------------


def run_move(
    origins: list[Point], move: MoveKind, further: bool, *,
    client: CosineClient, discogs_client: httpx.Client | None,
    exclusions: Exclusions, seed_budget: int = 10,
    near_limit: int = 25, further_limit: int = 25, branch_limit: int = 3,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[MoveEvent]:
    """Resolve and stream admitted GUI picks, without database or export writes.

    The first progress note carries resolved origins for the job writer to
    persist before any pick. Clients belong to the caller. Normal exhaustion
    follows the last batch/pick/note; the writer then commits ``picks_done``
    and settles metadata before its terminal event. Provider failures propagate
    so that an already persisted near prefix survives a failed further batch.
    """
    from .chain import iter_move_batches

    if type(further) is not bool or any(type(limit) is not int or limit < 0
                                      for limit in (near_limit, further_limit, branch_limit)):
        raise ValueError("invalid further flag or move limits")
    is_cancelled = cancelled if cancelled is not None else lambda: False
    if is_cancelled():
        return
    resolution = _seeds.resolve_move_origins(
        origins, move, client=client, discogs_client=discogs_client, seed_budget=seed_budget,
        cancelled=is_cancelled,
    )
    if is_cancelled():
        return
    yield MoveEvent(event="note", data={
        "code": "S-HDR-PROGRESS", "params": {"i": 0, "n": near_limit},
        "message": f"listening for neighbours · 0 of {near_limit}", "phase": "resolve",
        "progress": {"origins": [point.to_dict() for point in resolution.origins]},
    })
    for note in resolution.notes:
        if is_cancelled():
            return
        yield MoveEvent(event="note", data=note)
    rank = 0
    hidden: dict[str, int] = {}
    for batch in iter_move_batches(
        resolution, client, discogs_client=discogs_client, exclusions=exclusions,
        further=further, near_limit=near_limit, further_limit=further_limit,
        branch_limit=branch_limit, cancelled=is_cancelled,
    ):
        if is_cancelled():
            return
        yield MoveEvent(event="batch", data={
            "hop": batch.hop, "start_rank": rank + 1,
            "target_count": further_limit if batch.hop else near_limit,
            "label": "further" if batch.hop else "near", "count": len(batch.candidates),
            "score_floor": min((candidate.ranked.final_score for candidate in batch.candidates), default=None),
        })
        for reason, count in batch.hidden.items():
            hidden[reason] = hidden.get(reason, 0) + count
        for candidate in batch.candidates:
            if is_cancelled():
                return
            rank += 1
            item = candidate.ranked.track
            yield MoveEvent(event="pick", data={
                "rank": rank, "track_key": track_key(item), "cosine_id": item.id,
                "artist": item.artist, "title": item.track or item.name,
                "video_id": item.video_id, "score": candidate.ranked.final_score,
                "batch": batch.hop, "route": {**candidate.route, "move": move},
                "facts": candidate.facts.to_dict() if candidate.facts is not None else None,
                "facts_state": candidate.facts_state,
            })
        for note in batch.notes:
            if is_cancelled():
                return
            yield MoveEvent(event="note", data=note)
        # The running count is saved after each batch, so a later failure keeps it
        # (the step shows the last hidden note).
        if batch.hidden and not is_cancelled():
            yield MoveEvent(event="note", data=hidden_note(hidden, "further" if batch.hop else "near"))
    if rank == 0 and resolution.resolved_tracks and not is_cancelled():
        alternative = "sounds like this track" if move == "artist" else "sounds like its artist"
        second = "sounds like its label" if move == "artist" and further else "go one step further"
        yield MoveEvent(event="note", data={
            "code": "S-E-EMPTY",
            "params": {"alt 1": alternative, "alt 2": second},
            "message": f"Nothing came back for this move. Try {alternative}, or {second}.",
            "phase": "further" if further else "near",
        })


# Exclusion reasons as the step header words them, in display order.
HIDDEN_REASONS = {
    "owned": "you own",
    "served": "already in your Daily or Weekly",
    "kept": "you kept",
    "passed": "you passed",
    "muted": "by artists or labels you muted",
    "known": "you've dug from before",
}


def hidden_note(counts: dict[str, int], phase: str) -> dict:
    """The step's "hidden from this step" note: tracks that would have made
    the list but were left out, by exclusion reason."""
    params = {reason: counts[reason] for reason in HIDDEN_REASONS if counts.get(reason)}
    parts = ", ".join(f"{count} {HIDDEN_REASONS[reason]}" for reason, count in params.items())
    return {"code": "S-HDR-HIDDEN", "params": params, "message": f"Hidden from this step: {parts}.", "phase": phase}


def record_step(
    conn: sqlite3.Connection, *, dig_id: str, parent_id: str | None,
    origins: list[Point], move: MoveKind, further: bool,
    rerun_of: str | None = None, reuse_empty_step_id: str | None = None,
    context: dict | None = None,
) -> StepIdentity:
    """Allocate a GUI step/attempt in the caller's active write transaction.

    Dig creation, admission and commit remain the caller's responsibility.
    Duplicate moves return their existing identity; Re-run allocates a sibling,
    and Retry of an empty failed/stopped step allocates a fresh job only.
    """
    from .storage import create_step

    return create_step(
        conn, dig_id=dig_id, parent_id=parent_id, origins=origins, move=move,
        further=further, rerun_of=rerun_of, reuse_empty_step_id=reuse_empty_step_id,
        context=context,
    )


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


class DbIdStore(MutableMapping[str, str]):
    """Playlist-name → playlist-id mapping kept in the sink_playlists table."""

    def __init__(self, conn: sqlite3.Connection, sink: str = "ytmusic"):
        self.conn, self.sink = conn, sink

    def __getitem__(self, name: str) -> str:
        row = self.conn.execute(
            "SELECT playlist_id FROM sink_playlists WHERE sink = ? AND name = ?", (self.sink, name)
        ).fetchone()
        if row is None:
            raise KeyError(name)
        return row[0]

    def __setitem__(self, name: str, playlist_id: str) -> None:
        from .storage import playlist_id_put

        playlist_id_put(self.conn, self.sink, name, playlist_id)

    def __delitem__(self, name: str) -> None:
        cur = self.conn.execute("DELETE FROM sink_playlists WHERE sink = ? AND name = ?", (self.sink, name))
        self.conn.commit()
        if cur.rowcount == 0:
            raise KeyError(name)

    def __iter__(self) -> Iterator[str]:
        rows = self.conn.execute("SELECT name FROM sink_playlists WHERE sink = ?", (self.sink,)).fetchall()
        return iter([r[0] for r in rows])

    def __len__(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM sink_playlists WHERE sink = ?", (self.sink,)
        ).fetchone()[0]


def open_db(db_path=None) -> sqlite3.Connection:
    """Open (and migrate) the taste DB at ``db_path`` or the configured path."""
    from .config import load_config
    from .storage import connect, migrate

    conn = connect(db_path or load_config().db_path)
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def import_library(conn: sqlite3.Connection, tracks, *, source: str) -> int:
    """Add owned tracks from another catalogue (a media server, a CSV export, ...).

    Each track is a dict with ``id`` (stable within ``source``), ``artist`` and
    ``title``; ``album``, ``label``, ``year``, ``genre`` and ``added_at`` (ISO
    8601, used for recency) are optional. Owned tracks never appear in
    generated playlists, and their artists and labels feed the taste profile.
    Re-importing the same ids updates them. Returns the number stored.
    """
    from .taste import _compute_signals

    rows = [
        (f"{source}:{t['id']}", t.get("album"), t.get("added_at"), t["artist"], t.get("album"),
         t["title"], t.get("label") or None, t.get("year"), t.get("genre"), source)
        for t in tracks
        if t.get("id") and (t.get("artist") or "").strip() and (t.get("title") or "").strip()
    ]
    with conn:
        conn.executemany(
            """INSERT INTO tracks (path, folder, mtime, artist, album, title, label, year, genre, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(path) DO UPDATE SET folder = excluded.folder, mtime = excluded.mtime,
                 artist = excluded.artist, album = excluded.album, title = excluded.title,
                 label = excluded.label, year = excluded.year, genre = excluded.genre""",
            rows,
        )
        _compute_signals(conn)
    return len(rows)


def record_dig(conn: sqlite3.Connection, seeds: list, picks: list[Pick], name: str | None = None) -> str:
    """Store a dig as a session so the taste profile learns from it.

    ``seeds`` are ResolvedSeed objects (or plain strings). A pasted URL is
    stored as the "Artist - Title" tracks it resolved to, so URL digs teach
    the profile too, and every seed's cosine ids are cached for playlists."""
    import json

    from . import storage
    from .session import auto_session_name, create_session, record_results

    stored: list[str] = []
    for seed in seeds:
        if isinstance(seed, str):
            stored.append(seed)
            continue
        if "://" in seed.raw or " - " not in seed.raw:
            labels = [track_label(t) for t in seed.tracks if t.artist][:3]
            stored.extend(labels or [seed.raw])
            for t in seed.tracks[:3]:
                if t.artist:
                    storage.cache_put(conn, anchor_cache_key(t.artist, t.track or t.name),
                                      json.dumps([t.id]), ANCHOR_TTL_S)
        else:
            stored.append(seed.raw)
            artist, _, title = seed.raw.partition(" - ")
            if seed.tracks:
                storage.cache_put(conn, anchor_cache_key(artist.strip(), title.strip()),
                                  json.dumps([t.id for t in seed.tracks][:2]), ANCHOR_TTL_S)
    stored = list(dict.fromkeys(stored))
    # The recordings behind your seeds are known to you: never serve them.
    storage.record_known(conn, [
        {"track_id": t.id, "video_id": t.video_id, "artist": t.artist, "title": t.track or t.name}
        for seed in seeds if not isinstance(seed, str) for t in seed.tracks
    ], source="seed")
    session_id = create_session(conn, name or auto_session_name(stored), stored)
    by_hop: dict[int, list[dict[str, Any]]] = {}
    for p in picks:
        by_hop.setdefault(p.hop, []).append({
            "track_id": p.track.id, "artist": p.track.artist, "track_name": p.track.track or p.track.name,
            "video_id": p.track.video_id, "score": p.score, "via": p.via,
        })
    for hop, results in by_hop.items():
        record_results(conn, session_id, results, hop=hop)
    conn.execute(
        "UPDATE sessions SET total_results = ?, updated_at = ? WHERE id = ?",
        (len(picks), datetime.now(tz=timezone.utc).isoformat(), session_id),
    )
    conn.commit()
    return session_id


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export(picks: list[Pick], name: str, *, sink=None, conn: sqlite3.Connection | None = None,
           description: str = "Made by cosine-digger", drop_served: bool = False,
           recheck: Callable[[list[Pick]], list[Pick]] | None = None,
           key: str | None = None, record_served: bool = True):
    """Replace the named private YouTube Music playlist with these picks.

    With ``conn``, the picks go into the served ledger before the write, so no
    generated playlist serves them again even if the re-read fails. An export
    that certainly changed nothing takes its ledger entries back out.
    ``drop_served`` (generated playlists) rechecks the ledger under its lock
    and leaves out anything another export served meanwhile; ``recheck``
    filters the picks again there too (e.g. the full playlist rules).
    ``record_served=False`` leaves the ledger alone (a dig's playlist), and
    ``key`` finds the playlist when ``name`` is only its title. Every failure
    is our own YouTubeExportError wording, never provider text."""
    from contextlib import nullcontext

    from . import storage
    from .locks import LockTimeout, file_lock
    from .youtube import YouTubeAuthMissing, YouTubeExportError, YTMusicSink

    if not record_served and (drop_served or recheck):
        raise ValueError("drop_served and recheck run under the ledger, which record_served=False leaves alone")
    if sink is None and conn is None:
        # A live export always keeps its playlist mapping and ledger in the
        # configured DB, so the same name is replaced, never duplicated.
        own = open_db()
        try:
            return export(picks, name, conn=own, description=description,
                          drop_served=drop_served, recheck=recheck, key=key, record_served=record_served)
        finally:
            own.close()
    playable = [p for p in picks if p.track.video_id]
    omitted = [p.track.id for p in picks if not p.track.video_id]
    if not playable:
        raise YouTubeExportError("None of the picks has a YouTube Music video; the playlist was left unchanged.",
                                 changed=False)
    sent = False
    try:
        if sink is None:
            sink = YTMusicSink(id_store=DbIdStore(conn) if conn is not None else None)
        db_file = _db_file(conn)
        home = db_file.parent if db_file else None
        # One writer per playlist name across threads and processes sharing
        # the DB; the ledger lock (always innermost, held briefly) makes
        # "check served, then record" atomic across every export.
        guard = file_lock(f"export:{key or name}", home=home) if home else nullcontext()
        with guard:
            served_at = None
            if conn is not None and record_served:
                with file_lock("ledger", home=home) if home else nullcontext():
                    # One write transaction: the final rule check and the claim
                    # see the same database, and no library import, mute or
                    # other export can commit between them.
                    if conn.in_transaction:
                        conn.commit()
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        if drop_served or recheck:
                            kept = playable
                            if drop_served:
                                served = storage.served_keys(conn)
                                kept = [p for p in kept if p.track.id not in served and p.track.video_id not in served]
                            if recheck:
                                allowed = {id(p) for p in recheck(kept)}
                                kept = [p for p in kept if id(p) in allowed]
                            omitted += [p.track.id for p in playable if p not in kept]
                            playable = kept
                            if not playable:
                                raise YouTubeExportError(
                                    "Every pick was served by another playlist meanwhile; the playlist was left unchanged.",
                                    changed=False)
                        served_at = storage.record_served(conn, name, [  # commits
                            {"track_id": p.track.id, "video_id": p.track.video_id,
                             "artist": p.track.artist, "title": p.track.track or p.track.name}
                            for p in playable
                        ])
                    except BaseException:
                        if conn.in_transaction:
                            conn.rollback()
                        raise
            sent = True
            try:
                report = sink.replace_playlist(name, [p.track.video_id for p in playable], description=description,
                                               titles={p.track.video_id: p.track.track or p.track.name for p in playable},
                                               key=key)
            except YouTubeExportError as exc:
                if served_at and not exc.changed:
                    storage.unrecord_served(conn, name, served_at)
                raise
            report.omitted = omitted
            if conn is not None and record_served and (report.substituted or report.inserted):
                # YouTube stored some songs under another id, or put another
                # recording in a declined song's place: never serve those either.
                by_video = {p.track.video_id: p for p in playable}
                storage.record_served(conn, name, [
                    {"track_id": by_video[old].track.id, "video_id": new,
                     "artist": by_video[old].track.artist, "title": by_video[old].track.track or by_video[old].track.name}
                    for old, new in report.substituted.items() if old in by_video
                ] + [
                    {"track_id": f"yt:{item['video_id']}", "video_id": item["video_id"],
                     "artist": item["artist"] or None, "title": item["title"] or None}
                    for item in report.inserted
                ])
            return report
    except (YouTubeExportError, YouTubeAuthMissing, LockTimeout):
        raise
    except sqlite3.Error as exc:
        if sent:
            raise YouTubeExportError(_export_error(exc)) from None
        # Nothing reached YouTube: this is Digger's own database, not the login.
        raise YouTubeExportError(
            "Digger's database is busy (a scan or another run is writing to it); "
            "the playlist was left unchanged. Try again in a minute.", changed=False) from None
    except Exception as exc:
        raise YouTubeExportError(_export_error(exc)) from None


def dig_playlist_key(dig_id: str) -> str:
    """A dig's playlist in the id store: found by the dig, never by its title."""
    return f"dig:{dig_id}"


def export_dig(conn: sqlite3.Connection, dig_id: str, *, sink=None) -> dict[str, Any]:
    """Replace the dig's own private YouTube Music playlist with the dig as it
    stands, to listen outside Digger (its player pauses in a hidden tab).

    Every step in order, each track once, passes left out as ▶ Play step skips
    them; a track Digger's player can't embed still goes, since YouTube Music
    plays it. The served ledger is left alone: digging never changes the feeds
    or hides a later step's picks."""
    from . import storage
    from .youtube import YouTubeExportError

    queue = storage.dig_queue(conn, dig_id)
    if not queue["tracks"]:
        raise YouTubeExportError("Every track in this dig is passed; there's nothing to send." if queue["passed"]
                                 else "This dig has no tracks yet; nothing was sent.", changed=False)
    dig = queue["dig"]
    started = datetime.fromisoformat(dig["created_at"]).astimezone()
    day = f"{started:%b} {started.day}"
    name = dig["name"] if len(dig["name"]) <= 100 else dig["name"][:99] + "…"
    picks = [Pick(Track(id=t["cosine_id"], name=t["title"], artist=t["artist"], track=t["title"],
                        video_id=t["video_id"]), t["score"], "From your dig") for t in queue["tracks"]]
    key = dig_playlist_key(dig_id)
    report = export(picks, f"Digger: {name} · {day}", sink=sink, conn=conn, key=key, record_served=False,
                    description=f"Your Digger dig “{name}”, started {day}: every step's picks in order, each "
                                "track once, passes left out. Send it again from Digger to bring it up to date.")
    # The mapping's updated_at says when the sent dig was read, so Digger can tell which steps
    # finished later and aren't in the playlist (during a first send it briefly holds the creation time).
    sent_at = storage.playlist_id_put(conn, DbIdStore(conn).sink, key, report.playlist_id, updated_at=queue["at"])
    return {"dig_id": dig_id, "playlist_url": report.playlist_url, "sent_at": sent_at, "created": report.created,
            "verified": report.verified, "sent": len(report.requested), "added": len(report.added),
            "missing": len(report.missing), "passed": queue["passed"], "no_video": len(report.omitted)}


def _export_error(exc: Exception) -> str:
    """A message that is safe to show: our own wording, never provider text
    (YouTube errors can contain the login cookies)."""
    from .locks import LockTimeout
    from .youtube import SIGNED_OUT, YouTubeAuthMissing, YouTubeExportError, signed_out

    if isinstance(exc, YouTubeAuthMissing):
        return "YouTube Music login missing: run `cosine-digger youtube login`"
    if isinstance(exc, (YouTubeExportError, LockTimeout)):
        return str(exc)
    if signed_out(exc):
        return SIGNED_OUT
    return (f"YouTube Music export failed ({type(exc).__name__}). Details are hidden because they can "
            "contain your login; run `cosine-digger youtube status` to check it, then retry.")


def _db_file(conn: sqlite3.Connection | None):
    """The file behind ``conn`` (None for in-memory DBs)."""
    from pathlib import Path

    if conn is None:
        return None
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]) if row and row[2] else None


def _db_cache(conn: sqlite3.Connection | None):
    """A Discogs response cache in the same DB file as ``conn`` (None in memory)."""
    db_file = _db_file(conn)
    if db_file is None:
        return None
    from .httpcache import DbHttpCache

    return DbHttpCache(db_file)


def _artist_fallbacks(seeds: list[ResolvedSeed], artists: list[str], client: CosineClient,
                      notify: Notify | None) -> list[ResolvedSeed]:
    """A seed per artist none of whose own tracks resolved, made of the
    tracks cosine.club has by them (stored under the artist's name, so the
    profile learns those recordings, never a wrong id for the owned song)."""
    have = {_seeds._norm_artist(t.artist) for s in seeds for t in s.tracks}
    extra = []
    for artist in dict.fromkeys(a.strip() for a in artists if a and a.strip()):
        if _seeds._norm_artist(artist) in have:
            continue
        notes: list[str] = []

        def say(message: str, notes=notes) -> None:
            notes.append(plain(message))
            if notify:
                notify(message)
        tracks = _seeds.resolve_artist(artist, client, notify=say)
        have.add(_seeds._norm_artist(artist))
        extra.append(ResolvedSeed(raw=artist, tracks=tracks, notes=notes))
    return extra


def default_playlist_name(seeds: list[ResolvedSeed]) -> str:
    artists = list(dict.fromkeys(t.artist for s in seeds for t in s.tracks if t.artist))
    if not artists:
        return "Digger: new finds"
    head = ", ".join(artists[:3])
    return f"Digger: {head}" + (f" + {len(artists) - 3} more" if len(artists) > 3 else "")


def quick(
    raw_seeds: list[str],
    *,
    limit: int = 25,
    playlist: str | None = None,
    export_playlist: bool = True,
    scorer: str = "rrf",
    client: CosineClient | None = None,
    sink=None,
    conn: sqlite3.Connection | None = None,
    notify: Notify | None = None,
    record: bool = True,
    deep: bool = False,
    on_progress: Callable[[str, dict], None] | None = None,
    fallback_artists: list[str] | None = None,
    profile=None,
) -> dict[str, Any]:
    """Seeds in, explained picks (and optionally a YouTube Music playlist) out.

    This is the fast path behind ``cosine-digger quick`` and the /dig skill;
    ``deep`` follows labels and artists one hop further (slower, Discogs);
    ``on_progress(event, data)`` then receives chain events as they happen
    (``branches_discovered``, ``branch_start``, ``hop_done`` ...).
    ``profile`` is a taste profile the caller already built from ``conn``'s DB
    (a map click), so the exclusions don't build it again.
    ``fallback_artists`` (a map click) are the artists the dig is about: one
    whose own seed tracks aren't on cosine.club digs from the tracks cosine.club
    has by them instead.
    ``excluded`` reports candidate counts by playlist-rule reason, with their
    sum in ``excluded_count`` (before the result limit and route caps).
    """
    timings: dict[str, float] = {}
    t0 = time.perf_counter()
    from .chain import make_rate_limited_client
    from .config import load_config

    with ExitStack() as resources:
        if client is None:
            client = make_client()
            resources.callback(client.close)
        if conn is None and (record or export_playlist):
            conn = open_db()
            resources.callback(conn.close)
        # One paced, cached Discogs client for seed lookups (album text, release,
        # label and artist URLs); a deep dig shares the same response cache.
        cache = _db_cache(conn)
        if cache is not None:
            resources.callback(cache.close)
        discogs_client, _ = make_rate_limited_client(budget=10_000, http_cache=cache,
                                                    token=load_config().discogs_token)
        resources.callback(discogs_client.close)
        seeds = resolve_seeds(raw_seeds, client, notify=notify, discogs_client=discogs_client)
        if fallback_artists:
            seeds += _artist_fallbacks(seeds, fallback_artists, client, notify)
        timings["resolve_s"] = round(time.perf_counter() - t0, 2)
        resolved = [s for s in seeds if s.ok]
        excluded: dict[str, int] = {}
        result: dict[str, Any] = {
            "seeds": [s.to_dict() for s in seeds],
            "picks": [],
            "playlist_name": None,
            "playlist_url": None,
            "export": None,
            "session_id": None,
            "timings": timings,
            "excluded": excluded,
            "excluded_count": 0,
        }
        if not resolved:
            down = next((n for s in seeds for n in s.notes if _seeds.UNAVAILABLE_NOTE in n), None)
            if down:
                result["error"] = f"No seeds could be resolved: {down}"
            elif fallback_artists:
                result["error"] = f"cosine.club has no tracks by {', '.join(fallback_artists[:3])} yet"
            else:
                result["error"] = "No seeds could be resolved"
            return result
        t1 = time.perf_counter()
        exclusions = None
        if conn is not None:
            from .playlists import load_exclusions
            from .profile import build_profile

            exclusions = load_exclusions(conn, profile or build_profile(conn))
        if deep:
            picks = chain(resolved, client, limit=limit, scorer=scorer, http_cache=cache,
                          on_progress=on_progress, exclusions=exclusions, excluded=excluded)
        else:
            picks = dig(resolved, client, limit=limit, scorer=scorer,
                        exclusions=exclusions, excluded=excluded)
        timings["dig_s"] = round(time.perf_counter() - t1, 2)
        result["excluded_count"] = sum(excluded.values())
        result["picks"] = [p.to_dict() for p in picks]
        if record and conn is not None and picks:
            result["session_id"] = record_dig(conn, resolved, picks)
        if export_playlist and picks:
            t2 = time.perf_counter()
            name = playlist or default_playlist_name(resolved)
            result["playlist_name"] = name
            try:
                report = export(picks, name, sink=sink, conn=conn)
            except Exception as exc:  # reported to the caller, never swallowed
                result["export_error"] = _export_error(exc)
            else:
                result["playlist_url"] = report.playlist_url
                result["export"] = report.to_dict()
            timings["export_s"] = round(time.perf_counter() - t2, 2)
        timings["total_s"] = round(time.perf_counter() - t0, 2)
        return result
