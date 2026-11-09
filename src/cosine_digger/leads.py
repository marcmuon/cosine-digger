"""Query-time lead detection and event tracking for cosine-digger.

A "lead" is a pattern detected across multiple digs — a label, artist,
or producer that keeps appearing.  Leads are computed on the fly via SQL
GROUP BY queries, not persisted to a separate table.

Pure data layer — no console output, no side-effects beyond SQL writes.
All functions take an open ``sqlite3.Connection`` as first argument.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_VIA_PATTERN = re.compile(
    r"^(label|artist|producer):\s*(.+)$", re.IGNORECASE
)


def _parse_via(via: str) -> tuple[str, str] | None:
    """Extract (type, value) from a via string like 'label: Hessle Audio'."""
    if not via:
        return None
    m = _VIA_PATTERN.match(via.strip())
    if m:
        return m.group(1).lower(), m.group(2).strip()
    return None


# ---------------------------------------------------------------------------
# 1. detect_leads
# ---------------------------------------------------------------------------

def detect_leads(conn: sqlite3.Connection) -> list[dict]:
    """Find entities appearing across 2+ distinct seed sets.

    Parses ``session_results.via`` for typed patterns (``label: X``,
    ``artist: X``, ``producer: X``) and counts distinct seed fingerprints
    (so running the same seeds twice doesn't inflate appearance counts).

    Also uses ``session_branches`` as supplementary source — it stores
    ALL discovered branches (not just followed ones).

    Muted leads (via ``muted_leads`` table) are excluded entirely.
    Remaining leads are sorted by appearances DESC, last_seen DESC.

    Returns list of dicts::

        {type, value, appearances, last_seen, total_tracks}
    """
    # --- Load muted leads ---
    muted: set[tuple[str, str]] = set()
    try:
        muted = {
            (r[0].lower(), r[1].lower())
            for r in conn.execute("SELECT lead_type, LOWER(lead_value) FROM muted_leads").fetchall()
        }
    except sqlite3.OperationalError:
        pass  # table doesn't exist yet — no muted leads

    # --- Seed fingerprint lookup (de-dupe identical-seed sessions) ---
    # Map session_id → normalised seeds JSON for fingerprinting.  Seeds are
    # sorted so that ["A","B"] and ["B","A"] produce the same fingerprint.
    # Sessions with empty seeds (e.g. tests) fall back to session_id so
    # they aren't collapsed.
    seed_map: dict[str, str] = {}  # session_id → fingerprint
    try:
        for sid, seeds_json in conn.execute("SELECT id, seeds FROM sessions").fetchall():
            if seeds_json and seeds_json != "[]":
                seed_map[sid] = json.dumps(sorted(json.loads(seeds_json)))
    except sqlite3.OperationalError:
        pass  # sessions table may not exist yet

    # --- Source 1: parse via column from session_results ---
    rows = conn.execute(
        """
        SELECT sr.via, sr.session_id, MAX(sr.discovered_at) AS last_seen
        FROM session_results sr
        WHERE sr.via IS NOT NULL AND sr.via != '' AND sr.via != 'seed'
        GROUP BY sr.via, sr.session_id
        """
    ).fetchall()

    # Aggregate by (type, LOWER(value))
    lead_map: dict[tuple[str, str], dict] = {}
    for via_text, session_id, last_seen in rows:
        parsed = _parse_via(via_text)
        if not parsed:
            continue
        lead_type, lead_value = parsed
        key = (lead_type, lead_value.lower())
        if key in muted:
            continue
        if key not in lead_map:
            lead_map[key] = {
                "type": lead_type,
                "value": lead_value,
                "seed_fingerprints": set(),
                "last_seen": last_seen,
            }
        lead_map[key]["seed_fingerprints"].add(seed_map.get(session_id, session_id))
        if last_seen and last_seen > lead_map[key]["last_seen"]:
            lead_map[key]["last_seen"] = last_seen

    # --- Source 2: session_branches (supplementary) ---
    branch_rows = conn.execute(
        """
        SELECT sb.branch_type, sb.branch_value, sb.session_id, MAX(
            COALESCE(sb.explored_at, s.created_at)
        ) AS last_seen
        FROM session_branches sb
        JOIN sessions s ON s.id = sb.session_id
        GROUP BY LOWER(sb.branch_type), LOWER(sb.branch_value), sb.session_id
        """
    ).fetchall()

    for branch_type, branch_value, session_id, last_seen in branch_rows:
        key = (branch_type.lower(), branch_value.lower())
        if key in muted:
            continue
        if key not in lead_map:
            lead_map[key] = {
                "type": branch_type.lower(),
                "value": branch_value,
                "seed_fingerprints": set(),
                "last_seen": last_seen or "",
            }
        lead_map[key]["seed_fingerprints"].add(seed_map.get(session_id, session_id))
        if last_seen and last_seen > (lead_map[key]["last_seen"] or ""):
            lead_map[key]["last_seen"] = last_seen

    # --- Batch track counts via single query ---
    track_counts: dict[tuple[str, str], int] = {}
    count_rows = conn.execute(
        """
        SELECT LOWER(via), COUNT(*) FROM session_results
        WHERE via IS NOT NULL AND via != '' AND via != 'seed'
        GROUP BY LOWER(via)
        """
    ).fetchall()
    for via_lower, cnt in count_rows:
        parsed = _parse_via(via_lower)
        if parsed:
            key = (parsed[0], parsed[1].lower())
            track_counts[key] = track_counts.get(key, 0) + cnt

    # --- Filter: 2+ unique seed sets, build result ---
    leads = []
    for key, info in lead_map.items():
        appearances = len(info["seed_fingerprints"])
        if appearances < 2:
            continue

        leads.append({
            "type": info["type"],
            "value": info["value"],
            "appearances": appearances,
            "last_seen": info["last_seen"],
            "total_tracks": track_counts.get(key, 0),
        })

    leads.sort(key=lambda x: (x["appearances"], x["last_seen"] or ""), reverse=True)
    return leads


# ---------------------------------------------------------------------------
# 2. get_lead_tracks
# ---------------------------------------------------------------------------

def get_lead_tracks(
    conn: sqlite3.Connection,
    lead_type: str,
    lead_value: str,
    limit: int = 10,
) -> list[dict]:
    """Get unactioned tracks for a lead.

    Matches via exact ``'{type}: {value}'`` (case-insensitive).
    Excludes results with entries in ``result_events``.
    Deduplicates by ``track_id`` (keeps highest score).
    Orders by score DESC, discovered_at ASC.
    """
    exact_pattern = f"{lead_type.lower()}: {lead_value.lower()}"

    # Collect track_ids that have been actioned under ANY lead/result row,
    # so a track saved under one lead doesn't resurface under another.
    actioned_track_ids = {
        r[0] for r in conn.execute(
            """
            SELECT DISTINCT sr.track_id
            FROM session_results sr
            JOIN result_events re ON re.result_id = sr.id
            """
        ).fetchall()
    }

    rows = conn.execute(
        """
        SELECT sr.id, sr.track_id, sr.artist, sr.track_name, sr.score,
               sr.hop, sr.via, sr.session_id, sr.discovered_at
        FROM session_results sr
        LEFT JOIN result_events re ON re.result_id = sr.id
        WHERE LOWER(sr.via) = ?
          AND re.id IS NULL
        ORDER BY sr.score DESC, sr.discovered_at ASC
        """,
        (exact_pattern,),
    ).fetchall()

    # Deduplicate by track_id and skip tracks actioned under any lead
    seen_tracks: set[str] = set()
    results: list[dict] = []
    for r in rows:
        track_id = r[1]
        if track_id in seen_tracks or track_id in actioned_track_ids:
            continue
        seen_tracks.add(track_id)
        results.append({
            "id": r[0],
            "track_id": track_id,
            "artist": r[2],
            "track_name": r[3],
            "score": r[4],
            "hop": r[5],
            "via": r[6],
            "session_id": r[7],
            "discovered_at": r[8],
        })
        if len(results) >= limit:
            break

    return results


# ---------------------------------------------------------------------------
# 3. record_event
# ---------------------------------------------------------------------------

_VALID_EVENT_TYPES = frozenset({"save", "pass", "dig_from"})


def record_event(
    conn: sqlite3.Connection,
    result_id: int,
    event_type: str,
) -> None:
    """Log a user action on a result.

    Valid event types: ``'save'``, ``'pass'``, ``'dig_from'``.
    Uses ``INSERT OR REPLACE`` on the UNIQUE(result_id) constraint so the
    latest action always wins.
    """
    if event_type not in _VALID_EVENT_TYPES:
        raise ValueError(
            f"Invalid event_type {event_type!r}; expected one of {sorted(_VALID_EVENT_TYPES)}"
        )
    conn.execute(
        """
        INSERT OR REPLACE INTO result_events (result_id, event_type, created_at)
        VALUES (?, ?, ?)
        """,
        (result_id, event_type, _now_iso()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 4. mute / unmute leads
# ---------------------------------------------------------------------------


def mute_lead(conn: sqlite3.Connection, lead_type: str, lead_value: str) -> None:
    """Mute a lead so it no longer appears in detect_leads() results.

    Stores the normalized (lowercased) type for matching, but preserves
    the original value casing for display.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO muted_leads (lead_type, lead_value, muted_at)
        VALUES (LOWER(?), ?, ?)
        """,
        (lead_type, lead_value, _now_iso()),
    )
    conn.commit()


def unmute_lead(conn: sqlite3.Connection, lead_type: str, lead_value: str) -> None:
    """Unmute a previously muted lead."""
    conn.execute(
        "DELETE FROM muted_leads WHERE lead_type = LOWER(?) AND LOWER(lead_value) = LOWER(?)",
        (lead_type, lead_value),
    )
    conn.commit()


def get_muted_leads(conn: sqlite3.Connection) -> list[dict]:
    """Return all muted leads, sorted by muted_at DESC."""
    try:
        rows = conn.execute(
            "SELECT lead_type, lead_value, muted_at FROM muted_leads ORDER BY muted_at DESC"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"type": r[0], "value": r[1], "muted_at": r[2]} for r in rows]


# ---------------------------------------------------------------------------
# 5. get_saved_tracks
# ---------------------------------------------------------------------------


def get_saved_tracks(
    conn: sqlite3.Connection,
    limit: int = 50,
) -> list[dict]:
    """Return tracks the user explicitly saved or dug from, most recent first.

    Includes both ``'save'`` and ``'dig_from'`` events — both are strong
    positive signals. Deduplicates by ``track_id`` (keeps most recent action).
    """
    rows = conn.execute(
        """
        SELECT sr.track_id, sr.artist, sr.track_name, sr.score,
               sr.via, sr.session_id, re.created_at, re.event_type, sr.video_id
        FROM result_events re
        JOIN session_results sr ON sr.id = re.result_id
        WHERE re.event_type IN ('save', 'dig_from')
        ORDER BY re.created_at DESC
        """,
    ).fetchall()

    seen_tracks: set[str] = set()
    results: list[dict] = []
    for r in rows:
        track_id = r[0]
        if track_id in seen_tracks:
            continue
        seen_tracks.add(track_id)
        results.append({
            "track_id": track_id,
            "artist": r[1],
            "track_name": r[2],
            "score": r[3],
            "via": r[4],
            "session_id": r[5],
            "saved_at": r[6],
            "event_type": r[7],
            "video_id": r[8],
        })
        if len(results) >= limit:
            break

    return results


# ---------------------------------------------------------------------------
# 6. get_unactioned_results
# ---------------------------------------------------------------------------

def get_unactioned_results(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> list[dict]:
    """Top unactioned results across all digs (fallback when no leads).

    Excludes tracks that have been actioned under any result row.
    Deduplicates by track_id (keeps highest score).
    Orders by score DESC, discovered_at ASC.
    """
    actioned_track_ids = {
        r[0] for r in conn.execute(
            """
            SELECT DISTINCT sr.track_id
            FROM session_results sr
            JOIN result_events re ON re.result_id = sr.id
            """
        ).fetchall()
    }

    rows = conn.execute(
        """
        SELECT sr.id, sr.track_id, sr.artist, sr.track_name, sr.score,
               sr.hop, sr.via, sr.session_id, sr.discovered_at
        FROM session_results sr
        LEFT JOIN result_events re ON re.result_id = sr.id
        WHERE re.id IS NULL
        ORDER BY sr.score DESC, sr.discovered_at ASC
        """,
    ).fetchall()

    seen_tracks: set[str] = set()
    results: list[dict] = []
    for r in rows:
        track_id = r[1]
        if track_id in seen_tracks or track_id in actioned_track_ids:
            continue
        seen_tracks.add(track_id)
        results.append({
            "id": r[0],
            "track_id": track_id,
            "artist": r[2],
            "track_name": r[3],
            "score": r[4],
            "hop": r[5],
            "via": r[6],
            "session_id": r[7],
            "discovered_at": r[8],
        })
        if len(results) >= limit:
            break

    return results


# ---------------------------------------------------------------------------
# 7. get_discovery_context
# ---------------------------------------------------------------------------


def get_discovery_context(
    conn: sqlite3.Connection,
    taste_profile: object | None = None,
) -> dict:
    """Unified view of the user's discovery state.

    Composes existing query functions into a single snapshot that
    shows leads, saves, muted leads, session stats, and optionally
    taste seeds. Used by the ``context`` CLI command and the
    deep-seed skill.
    """
    leads = detect_leads(conn)
    saves = get_saved_tracks(conn, limit=20)
    muted = get_muted_leads(conn)

    session_count = conn.execute(
        "SELECT COUNT(*) FROM sessions"
    ).fetchone()[0]

    total_discoveries = conn.execute(
        "SELECT COUNT(*) FROM session_results"
    ).fetchone()[0]

    taste_seeds: list[str] = []
    taste_stats: dict | None = None
    if taste_profile is not None:
        try:
            taste_seeds = taste_profile.generate_dig_seeds(
                window="all", max_seeds=6, verified_only=False,
            )
            track_count = taste_profile.conn.execute(
                "SELECT COUNT(*) FROM tracks"
            ).fetchone()[0]
            artist_count = taste_profile.conn.execute(
                "SELECT COUNT(DISTINCT artist) FROM tracks WHERE artist IS NOT NULL"
            ).fetchone()[0]
            label_count = taste_profile.conn.execute(
                "SELECT COUNT(DISTINCT label) FROM tracks WHERE label IS NOT NULL"
            ).fetchone()[0]
            taste_stats = {
                "track_count": track_count,
                "artist_count": artist_count,
                "label_count": label_count,
            }
        except Exception:
            pass

    return {
        "leads": leads,
        "saves": saves,
        "muted": muted,
        "session_count": session_count,
        "total_discoveries": total_discoveries,
        "taste_seeds": taste_seeds,
        "taste_stats": taste_stats,
    }


# ---------------------------------------------------------------------------
# 8. get_recap
# ---------------------------------------------------------------------------

def get_recap(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Aggregate stats for recent discovery activity.

    Returns::

        {
            "dig_count": int,
            "tracks_found": int,
            "saves": int,
            "passes": int,
            "hot_leads": list[dict],  # top 5 leads
            "convergence_count": int,  # tracks in 2+ sessions
            "new_artist_count": int,   # unique artists found
        }
    """
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    dig_count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE created_at >= ?", (cutoff,)
    ).fetchone()[0]

    tracks_found = conn.execute(
        "SELECT COUNT(*) FROM session_results WHERE discovered_at >= ?", (cutoff,)
    ).fetchone()[0]

    saves = conn.execute(
        """
        SELECT COUNT(*) FROM result_events re
        JOIN session_results sr ON sr.id = re.result_id
        WHERE re.created_at >= ? AND re.event_type = 'save'
        """,
        (cutoff,),
    ).fetchone()[0]

    passes = conn.execute(
        """
        SELECT COUNT(*) FROM result_events re
        JOIN session_results sr ON sr.id = re.result_id
        WHERE re.created_at >= ?
          AND re.event_type IN ('pass', 'skip', 'dead_end')
        """,
        (cutoff,),
    ).fetchone()[0]

    convergence_count = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT track_id FROM session_results
            WHERE discovered_at >= ?
            GROUP BY track_id
            HAVING COUNT(DISTINCT session_id) >= 2
        )
        """,
        (cutoff,),
    ).fetchone()[0]

    new_artist_count = conn.execute(
        """
        SELECT COUNT(DISTINCT artist) FROM session_results
        WHERE discovered_at >= ?
        """,
        (cutoff,),
    ).fetchone()[0]

    hot_leads = detect_leads(conn)[:5]

    return {
        "dig_count": dig_count,
        "tracks_found": tracks_found,
        "saves": saves,
        "passes": passes,
        "hot_leads": hot_leads,
        "convergence_count": convergence_count,
        "new_artist_count": new_artist_count,
    }
