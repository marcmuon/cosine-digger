"""Read-only legacy taste and explicit-judgment feed profiles."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import sqlite3
from typing import Literal

from .leads import _parse_via, detect_leads
from .taste import normalize_artist

EntityKind = Literal["artist", "label"]
LIBRARY_ANCHORS_PER_ARTIST = 2
ANCHORS_PER_ARTIST = 3
# Catalogue placeholders, not taste: never entities or anchors.
PLACEHOLDERS = frozenset({
    "various artists", "various", "va", "v.a.", "unknown artist", "unknown", "[unknown]",
    "[no label]", "no label", "not on label", "not on label (self-released)", "self-released",
    "none", "n/a",
})


@dataclass
class Entity:
    kind: EntityKind
    key: str
    name: str
    weight: float
    sources: dict[str, float]
    last_seen: str | None


@dataclass
class Anchor:
    artist: str
    title: str
    track_id: str | None
    video_id: str | None
    source: Literal["save", "dig_from", "seed", "library"]
    weight: float


@dataclass
class Edge:
    source_kind: str
    source_key: str
    target_kind: str
    target_key: str
    kind: str
    weight: float


@dataclass
class Profile:
    generated_at: str
    entities: list[Entity]
    anchors: list[Anchor]
    edges: list[Edge]
    stats: dict

    def to_dict(self) -> dict:
        return asdict(self)

    def top(self, kind: EntityKind, n: int) -> list[Entity]:
        return [entity for entity in self.entities if entity.kind == kind][:max(0, n)]


class _OptionalTables:
    """Read adapter: absent tables contribute zero rows, including in lead queries.

    Use cursor-local tuple rows without changing the caller's row factory.
    Other SQLite errors still propagate instead of hiding a broken database.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def execute(self, sql: str) -> sqlite3.Cursor:
        cursor = self.conn.cursor()
        cursor.row_factory = None
        try:
            return cursor.execute(sql)
        except sqlite3.OperationalError as exc:
            if not str(exc).startswith("no such table:"):
                raise
            return cursor.execute("SELECT NULL WHERE 0")

    def rows(self, table: str, *, order_by: str = "id") -> list[dict]:
        # Table names are internal constants, never user input.
        return self.query(f"SELECT * FROM {table} ORDER BY {order_by}")

    def query(self, sql: str) -> list[dict]:
        cursor = self.execute(sql)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor]


def _timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _feed_judgments(conn: sqlite3.Connection) -> list[dict]:
    """One effective verdict per recording, with metadata from its source.

    Read legacy winners in one pass rather than rescanning the event history
    for each recording. GUI verdicts override them even when older; controls
    without a verdict can suppress legacy history. Only the judged source
    pick supplies GUI facts and identity, never other browsed copies.
    """
    from .api import _key
    from .storage import _slice_read

    db = _OptionalTables(conn)
    # Read all precedence inputs together; end only our own read transaction
    # before interpreting metadata or building the rest of the profile.
    with _slice_read(conn):
        legacy = db.query(
            "SELECT r.artist,r.track_name AS title,r.track_id,r.video_id,r.via,"
            "e.event_type,e.created_at AS at FROM result_events e "
            "JOIN session_results r ON r.id=e.result_id "
            "WHERE e.event_type IN ('save','pass') ORDER BY e.created_at,e.id"
        )
        controls = db.rows("judgment_controls", order_by="track_key")
        gui = db.query(
            "SELECT j.track_key,j.verdict,j.at,p.artist,p.title,p.cosine_id AS track_id,"
            "p.video_id,p.facts,p.facts_state FROM judgments j LEFT JOIN step_picks p "
            "ON p.step_id=j.step_id AND p.track_key=j.track_key ORDER BY j.track_key"
        )
    winners = {}
    identities: dict[str, set[str]] = defaultdict(set)
    for row in legacy:
        key = _key(row["artist"] or "", row["title"] or "")
        identities[key].update(i for i in (row["track_id"], row["video_id"]) if i)
        via = _parse_via(row["via"])
        winners[key] = {**row, "track_key": key, "source": "legacy",
                        "verdict": "keep" if row["event_type"] == "save" else "pass",
                        "labels": [via[1]] if via and via[0] == "label" else []}
    for control in controls:
        if control["suppress_legacy"]:
            winners.pop(control["track_key"], None)
    for row in gui:
        facts = json.loads(row["facts"]) if row["facts_state"] == "ready" and row["facts"] else {}
        labels = {}
        if isinstance(facts, dict) and facts.get("source") == "discogs":
            for label in facts.get("labels", []):
                name = label["name"].strip()
                key = normalize_artist(name)
                if key and key not in PLACEHOLDERS:
                    labels.setdefault(key, name)
        key = row["track_key"]
        identities[key].update(i for i in (row["track_id"], row["video_id"]) if i)
        winners[key] = {**row, "source": "gui", "labels": list(labels.values())}
    return [{**winners[key], "ids": identities[key]} for key in sorted(winners)]


def build_profile(
    conn: sqlite3.Connection, *, now: datetime | None = None, half_life_days: float = 45.0,
) -> Profile:
    """Combine evidence without writes or I/O beyond reading ``conn``.

    Unknown timestamps receive no decay; future timestamps have age zero.
    Library scores already encode recency, so do not decay them again.
    Each linked library track gets its artist's library weight divided by that
    artist's signal track count. All anchors share a single 200-entry limit.
    Source counts count evidence rows (seeds individually), before filtering.
    Relationship edges use unit weight unless discovery_edges supplies one.
    """
    return _build_profile(conn, now=now, half_life_days=half_life_days, feed_only=False)


def build_feed_profile(
    conn: sqlite3.Connection, *, now: datetime | None = None, half_life_days: float = 45.0,
) -> Profile:
    """Weight only effective keeps/passes and the source-agnostic library.

    Keeps use the existing ``save`` source/count fields in Profile. Browsing
    contributes neither weights, anchors, display metadata nor relationships.
    Library math, normalization and anchor limits are shared with the CLI.
    """
    return _build_profile(conn, now=now, half_life_days=half_life_days, feed_only=True)


def _build_profile(
    conn: sqlite3.Connection, *, now: datetime | None, half_life_days: float, feed_only: bool,
) -> Profile:
    if not math.isfinite(half_life_days) or half_life_days <= 0:
        raise ValueError("half_life_days must be positive and finite")
    now = now or datetime.now(timezone.utc)
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    generated_at = now.isoformat()
    db = _OptionalTables(conn)
    sessions = [] if feed_only else db.rows("sessions")
    results = [] if feed_only else db.rows("session_results")
    tracks = db.rows("tracks")
    events = [] if feed_only else db.rows("result_events")
    muted = {(row["lead_type"].lower(), normalize_artist(row["lead_value"]))
             for row in db.rows("muted_leads")}
    entities: dict[tuple[str, str], Entity] = {}
    displays: dict[tuple[str, str], tuple[datetime, str, str | None]] = {}
    anchors: list[Anchor] = []
    edge_weights: dict[tuple[str, str, str, str, str], float] = defaultdict(float)
    counts = dict.fromkeys(("dig_seed", "save", "dig_from", "pass", "lead", "library"), 0)

    def decay(timestamp: str | None) -> float:
        seen = _timestamp(timestamp)
        age = max(0.0, (now - seen).total_seconds() / 86400) if seen else 0.0
        return 0.5 ** (age / half_life_days)

    def observe(kind: str, name: str | None, timestamp: str | None) -> None:
        if not name or not normalize_artist(name):
            return
        key = (kind, normalize_artist(name))
        seen = _timestamp(timestamp) or datetime.min.replace(tzinfo=timezone.utc)
        previous = displays.get(key)
        # A lexical tie-break makes equal-time display forms deterministic.
        if previous is None or (seen, name.strip()) > previous[:2]:
            displays[key] = (seen, name.strip(), timestamp)

    def add(kind: EntityKind, name: str | None, weight: float, source: str, timestamp: str | None) -> None:
        if not name or not normalize_artist(name) or normalize_artist(name) in PLACEHOLDERS:
            return
        key = (kind, normalize_artist(name))
        # detect_leads aggregates timestamps but can retain an older spelling.
        # Prefer the display observed directly in the underlying evidence.
        if source != "lead" or key not in displays:
            observe(kind, name, timestamp)
        entity = entities.setdefault(key, Entity(kind, key[1], name.strip(), 0.0, {}, None))
        entity.sources[source] = entity.sources.get(source, 0.0) + weight
        entity.weight += weight

    def edge(source_kind: str, source: str | None, target_kind: str, target: str | None,
             kind: str = "released_on", weight: float = 1.0) -> None:
        if not source or not target:
            return
        source_key, target_key = normalize_artist(source), normalize_artist(target)
        if not source_key or not target_key or {source_key, target_key} & PLACEHOLDERS:
            return
        if (source_kind, source_key) in muted or (target_kind, target_key) in muted:
            return
        edge_weights[(source_kind, source_key, target_kind, target_key, kind)] += weight

    result_by_id = {row["id"]: row for row in results}
    seed_results = {}
    for row in results:
        seed_results.setdefault((row["session_id"], normalize_artist(row["artist"] or ""),
                                 (row["track_name"] or "").strip().casefold()), row)
        observe("artist", row["artist"], row["discovered_at"])
        via = _parse_via(row["via"])
        if via and via[0] == "label":
            observe("label", via[1], row["discovered_at"])
            edge("artist", row["artist"], "label", via[1])

    for session in sessions:
        try:
            seeds = json.loads(session["seeds"])
        except (ValueError, TypeError):
            seeds = []
        if not isinstance(seeds, list):
            continue
        for seed in seeds:
            if not isinstance(seed, str) or "://" in seed or seed.strip().lower().startswith("www."):
                continue
            artist, separator, title = seed.partition(" - ")
            artist, title = artist.strip(), title.strip()
            if not separator or not artist or not title:
                continue
            weight = 3.0 * decay(session["created_at"])
            counts["dig_seed"] += 1
            add("artist", artist, weight, "dig_seed", session["created_at"])
            result = seed_results.get((session["id"], normalize_artist(artist), title.casefold()), {})
            anchors.append(Anchor(artist, title, result.get("track_id"), result.get("video_id"), "seed", weight))

    for event in events:
        source = event["event_type"]
        if source not in ("save", "dig_from", "pass"):
            continue
        counts[source] += 1
        result = result_by_id.get(event["result_id"])
        if result is None:
            continue
        factor = decay(event["created_at"])
        weight = {"save": 4.0, "dig_from": 5.0, "pass": -2.0}[source] * factor
        add("artist", result["artist"], weight, source, event["created_at"])
        if source == "pass":
            continue
        via = _parse_via(result["via"])
        if via and via[0] == "label":
            add("label", via[1], (2.0 if source == "save" else 2.5) * factor, source, event["created_at"])
        if result["artist"] and result["track_name"]:
            anchors.append(Anchor(result["artist"], result["track_name"], result["track_id"],
                                  result.get("video_id"), source, weight))

    if feed_only:
        for judgment in _feed_judgments(conn):
            keep = judgment["verdict"] == "keep"
            source = "save" if keep else "pass"
            counts[source] += 1
            factor = decay(judgment["at"])
            weight = (4.0 if keep else -2.0) * factor
            add("artist", judgment["artist"], weight, source, judgment["at"])
            labels = judgment["labels"]
            for label in labels:
                add("label", label, (2.0 if keep else -1.0) * factor / len(labels), source, judgment["at"])
                edge("artist", judgment["artist"], "label", label)
            if keep and judgment["artist"] and judgment["title"]:
                anchors.append(Anchor(judgment["artist"], judgment["title"], judgment["track_id"],
                                      judgment["video_id"], "save", weight))
    else:
        session_dates = {session["id"]: session["created_at"] for session in sessions}
        for branch in db.rows("session_branches"):
            observe(branch["branch_type"].lower(), branch["branch_value"],
                    branch["explored_at"] or session_dates.get(branch["session_id"]))
        for lead in detect_leads(db):
            if lead["type"] in ("artist", "label"):
                counts["lead"] += 1
                add(lead["type"], lead["value"], 0.75 * lead["appearances"] * decay(lead["last_seen"]),
                    "lead", lead["last_seen"])

    for signal in db.rows("taste_signals"):
        kind = signal["signal_type"]
        if kind not in ("artist", "label"):
            continue
        counts["library"] += 1
        track_count = max(0, signal["track_count"] or 0)
        weight = math.log1p(track_count) * (0.5 + (signal["recency_score_365d"] or 0.0))
        add(kind, signal["signal_value"], weight, "library", signal["latest_mtime"])

    owned_by_artist: dict[str, list[dict]] = defaultdict(list)
    for track in tracks:
        observe("artist", track["artist"], track["mtime"])
        observe("label", track["label"], track["mtime"])
        edge("artist", track["artist"], "label", track["label"])
        key = normalize_artist(track["artist"] or "")
        if key and track["title"]:
            owned_by_artist[key].append(track)
    # A few owned tracks per library artist become anchors (playlists dig from
    # them; ids are resolved later when the scan did not verify them), sharing
    # the artist's library weight so big and small artists keep their ratio.
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    for key, owned in owned_by_artist.items():
        library_weight = entities.get(("artist", key), Entity("artist", key, "", 0.0, {}, None)).sources.get("library")
        if not library_weight:
            continue
        owned.sort(key=lambda track: (not track.get("cosine_track_id"),
                                      -(_timestamp(track["mtime"]) or oldest).timestamp(), track["title"]))
        chosen = owned[:LIBRARY_ANCHORS_PER_ARTIST]
        for track in chosen:
            anchors.append(Anchor(track["artist"], track["title"], track.get("cosine_track_id") or None, None,
                                  "library", library_weight / len(chosen)))

    if not feed_only:
        for row in db.rows("discovery_edges"):
            observe(row["source_type"], row["source_value"], row["discovered_at"])
            observe(row["target_type"], row["target_value"], row["discovered_at"])
            edge(row["source_type"], row["source_value"], row["target_type"], row["target_value"],
                 row["edge_type"], row["weight"] if row["weight"] is not None else 1.0)

    retained = []
    for key, entity in entities.items():
        if key in muted or entity.weight <= 0:
            continue
        _, entity.name, entity.last_seen = displays[key]
        entity.sources = dict(sorted(entity.sources.items()))
        retained.append(entity)
    retained.sort(key=lambda entity: (-entity.weight, entity.key, entity.kind))
    # One anchor per recording (repeats add up), only for artists still in the
    # profile (net-negative, muted and placeholder artists drop out), and at
    # most ANCHORS_PER_ARTIST each so one artist can't fill the anchor list.
    kept_artists = {entity.key for entity in retained if entity.kind == "artist"}
    merged: dict[tuple[str, str], Anchor] = {}
    for anchor in anchors:
        artist_key = normalize_artist(anchor.artist)
        if anchor.weight <= 0 or artist_key not in kept_artists:
            continue
        key = (artist_key, " ".join(anchor.title.casefold().split()))
        if key in merged:
            first = merged[key]
            first.weight += anchor.weight
            first.track_id = first.track_id or anchor.track_id
            first.video_id = first.video_id or anchor.video_id
        else:
            merged[key] = Anchor(anchor.artist, anchor.title, anchor.track_id, anchor.video_id,
                                 anchor.source, anchor.weight)
    anchors = sorted(merged.values(), key=lambda anchor: (
        -anchor.weight, normalize_artist(anchor.artist), anchor.title.casefold(), anchor.track_id or "",
        anchor.video_id or "", anchor.source, anchor.artist, anchor.title))
    per_artist: Counter = Counter()
    capped = []
    for anchor in anchors:
        artist_key = normalize_artist(anchor.artist)
        if per_artist[artist_key] < ANCHORS_PER_ARTIST:
            per_artist[artist_key] += 1
            capped.append(anchor)
    anchors = capped
    edges = [Edge(*key, weight) for key, weight in edge_weights.items()]
    edges.sort(key=lambda item: (-item.weight, item.source_key, item.target_key,
                                item.source_kind, item.target_kind, item.kind))
    stats = {"counts_per_source": counts, "sessions": len(sessions), "saves": counts["save"],
             "passes": counts["pass"], "library_tracks": len(tracks), "generated_at": generated_at}
    return Profile(generated_at, retained, anchors[:200], edges, stats)
