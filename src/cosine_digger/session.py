"""Session persistence and discovery-graph storage for cosine-digger.

Pure data layer — no console output, no side-effects beyond SQL writes.
All functions take an open ``sqlite3.Connection`` as first argument and
use parameterized queries throughout.

Tables are defined in ``taste._SESSION_SCHEMA`` and created via
``taste._migrate_schema()``.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Current UTC timestamp in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def _slugify(text: str, max_len: int = 50) -> str:
    """Lowercase, hyphens-only slug truncated to *max_len* chars."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text


# ---------------------------------------------------------------------------
# 1. auto_session_name
# ---------------------------------------------------------------------------

def auto_session_name(seeds: list[str]) -> str:
    """Generate a readable session name from the first seed + today's date.

    * ``"Artist - Track"`` seeds become ``"artist-track-2026-03-22"``
    * URL seeds use the domain/path as a slug.
    * Result is lowercase, hyphens only, max 50 chars.
    """
    date_suffix = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not seeds:
        return _slugify(f"session-{date_suffix}")

    first = seeds[0]

    # URL seed → extract domain + last path segment
    if first.startswith(("http://", "https://")):
        parsed = urlparse(first)
        host = parsed.hostname or ""
        # Strip common prefixes
        host = re.sub(r"^www\.", "", host)
        # Use last non-empty path segment if available
        path_parts = [p for p in parsed.path.split("/") if p]
        slug_parts = [host]
        if path_parts:
            slug_parts.append(path_parts[-1])
        raw = "-".join(slug_parts)
    else:
        raw = first

    # Reserve room for date suffix: "-YYYY-MM-DD" = 11 chars
    prefix_max = 50 - len(date_suffix) - 1
    prefix = _slugify(raw, max_len=prefix_max)
    return _slugify(f"{prefix}-{date_suffix}")


# ---------------------------------------------------------------------------
# 2. create_session
# ---------------------------------------------------------------------------

def create_session(conn: sqlite3.Connection, name: str, seeds: list[str]) -> str:
    """Create a new dig session and return its UUID-based session_id."""
    session_id = str(uuid4())
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO sessions (id, name, seeds, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session_id, name, json.dumps(seeds), now, now),
    )
    conn.commit()
    return session_id


# ---------------------------------------------------------------------------
# 3. resume_session
# ---------------------------------------------------------------------------

def resume_session(conn: sqlite3.Connection, session_id: str) -> dict:
    """Load session metadata and unexplored branches.

    Returns::

        {
            "session": {id, name, seeds, created_at, updated_at, ...},
            "unexplored_branches": [rows from session_branches where explored=0],
        }

    Raises ``ValueError`` if the session does not exist.
    """
    row = conn.execute(
        "SELECT id, name, seeds, created_at, updated_at, hops_completed, total_results "
        "FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Session not found: {session_id}")

    session_info = {
        "id": row[0],
        "name": row[1],
        "seeds": json.loads(row[2]),
        "created_at": row[3],
        "updated_at": row[4],
        "hops_completed": row[5],
        "total_results": row[6],
    }

    branches = conn.execute(
        """
        SELECT id, hop, branch_type, branch_value, branch_id_ext,
               branch_score, parent_branch_id
        FROM session_branches
        WHERE session_id = ? AND explored = 0
        ORDER BY hop, id
        """,
        (session_id,),
    ).fetchall()

    unexplored = [
        {
            "id": b[0],
            "hop": b[1],
            "branch_type": b[2],
            "branch_value": b[3],
            "branch_id_ext": b[4],
            "branch_score": b[5],
            "parent_branch_id": b[6],
        }
        for b in branches
    ]

    return {
        "session": session_info,
        "unexplored_branches": unexplored,
    }


# ---------------------------------------------------------------------------
# 4. record_results
# ---------------------------------------------------------------------------

def record_results(
    conn: sqlite3.Connection,
    session_id: str,
    results: list,
    hop: int,
    branch_id: int | None = None,
) -> list[int]:
    """Insert discovered tracks into session_results (deduped per session).

    Each item in *results* must have attributes or keys: ``track_id`` (or
    ``track.id``), ``artist`` (or ``track.artist``), ``track_name`` (or
    ``track.track``), ``score`` (or ``final_score``), ``via``.

    Returns list of inserted row IDs (skips duplicates silently).
    """
    now = _now_iso()
    inserted_ids: list[int] = []

    for r in results:
        # Support both RankedTrack objects and plain dicts
        if hasattr(r, "track"):
            track_id = r.track.id
            artist = r.track.artist
            track_name = r.track.track
            video_id = r.track.video_id
            score = r.final_score
            via = getattr(r, "via", "")
        elif isinstance(r, dict):
            track_id = r.get("track_id", "")
            artist = r.get("artist", "")
            track_name = r.get("track_name", "")
            video_id = r.get("video_id")
            score = r.get("score")
            via = r.get("via", "")
        else:
            continue

        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO session_results
                (session_id, track_id, artist, track_name, video_id, score, hop, via, branch_id, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, track_id, artist, track_name, video_id, score, hop, via, branch_id, now),
        )
        if cursor.rowcount > 0:
            inserted_ids.append(cursor.lastrowid)
        elif video_id:
            conn.execute(
                "UPDATE session_results SET video_id = ? WHERE session_id = ? AND track_id = ? AND video_id IS NULL",
                (video_id, session_id, track_id),
            )

    # Update session aggregate
    if inserted_ids:
        conn.execute(
            """
            UPDATE sessions
            SET total_results = (
                    SELECT COUNT(*) FROM session_results WHERE session_id = ?
                ),
                hops_completed = MAX(hops_completed, ?),
                updated_at = ?
            WHERE id = ?
            """,
            (session_id, hop, _now_iso(), session_id),
        )

    conn.commit()
    return inserted_ids


# ---------------------------------------------------------------------------
# 5. record_branches
# ---------------------------------------------------------------------------

def record_branches(
    conn: sqlite3.Connection,
    session_id: str,
    branches: list,
    hop: int,
    parent_branch_id: int | None = None,
) -> list[int]:
    """Insert branch points into session_branches.

    Each item in *branches* must be a dict with keys:
    ``branch_type``, ``branch_value``, and optionally ``branch_id_ext``,
    ``branch_score``.

    Returns list of newly created branch row IDs.
    """
    ids: list[int] = []
    for b in branches:
        cursor = conn.execute(
            """
            INSERT INTO session_branches
                (session_id, hop, branch_type, branch_value, branch_id_ext,
                 branch_score, parent_branch_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                hop,
                b["branch_type"],
                b["branch_value"],
                b.get("branch_id_ext"),
                b.get("branch_score"),
                parent_branch_id,
            ),
        )
        ids.append(cursor.lastrowid)

    conn.commit()
    return ids


# ---------------------------------------------------------------------------
# 6. mark_branch_explored
# ---------------------------------------------------------------------------

def mark_branch_explored(conn: sqlite3.Connection, branch_id: int) -> None:
    """Mark a branch as explored with a timestamp."""
    conn.execute(
        "UPDATE session_branches SET explored = 1, explored_at = ? WHERE id = ?",
        (_now_iso(), branch_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 7. store_edges
# ---------------------------------------------------------------------------

def store_edges(
    conn: sqlite3.Connection,
    session_id: str,
    edges: list[dict],
) -> None:
    """Upsert discovery graph edges.

    Each edge dict: ``{source_type, source_value, target_type, target_value,
    edge_type, weight}``.
    """
    now = _now_iso()
    conn.executemany(
        """
        INSERT OR REPLACE INTO discovery_edges
            (source_type, source_value, target_type, target_value,
             edge_type, weight, session_id, discovered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                e["source_type"],
                e["source_value"],
                e["target_type"],
                e["target_value"],
                e["edge_type"],
                e.get("weight"),
                session_id,
                now,
            )
            for e in edges
        ],
    )
    conn.commit()


# ---------------------------------------------------------------------------
# 9. get_convergence
# ---------------------------------------------------------------------------

def get_convergence(
    conn: sqlite3.Connection,
    track_ids: list[str],
) -> dict[str, int]:
    """Count how many distinct sessions each track appears in.

    Returns a dict mapping track_id to session count, filtered to tracks
    appearing in 2 or more sessions.
    """
    if not track_ids:
        return {}

    placeholders = ",".join("?" for _ in track_ids)
    rows = conn.execute(
        f"""
        SELECT track_id, COUNT(DISTINCT session_id) AS session_count
        FROM session_results
        WHERE track_id IN ({placeholders})
        GROUP BY track_id
        HAVING session_count >= 2
        """,
        track_ids,
    ).fetchall()

    return {row[0]: row[1] for row in rows}


# ---------------------------------------------------------------------------
# 10. get_all_branches
# ---------------------------------------------------------------------------

def get_all_branches(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Return ALL branches (explored + unexplored) for a session.

    Unlike ``resume_session()`` which only returns unexplored branches,
    this returns everything -- needed by the trail map renderer to show
    the full exploration tree with checkmarks and numbered unexplored.
    """
    rows = conn.execute(
        """
        SELECT id, hop, branch_type, branch_value, branch_id_ext,
               branch_score, parent_branch_id, explored, explored_at
        FROM session_branches
        WHERE session_id = ?
        ORDER BY hop, id
        """,
        (session_id,),
    ).fetchall()

    return [
        {
            "id": r[0],
            "hop": r[1],
            "branch_type": r[2],
            "branch_value": r[3],
            "branch_id_ext": r[4],
            "branch_score": r[5],
            "parent_branch_id": r[6],
            "explored": bool(r[7]),
            "explored_at": r[8],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 11. list_sessions
# ---------------------------------------------------------------------------

def list_sessions(conn: sqlite3.Connection) -> list[dict]:
    """List all sessions with result counts, newest first."""
    rows = conn.execute(
        """
        SELECT s.id, s.name, s.seeds, s.created_at, s.updated_at,
               s.hops_completed, s.total_results,
               COALESCE(cnt.n, 0) AS result_count
        FROM sessions s
        LEFT JOIN (
            SELECT session_id, COUNT(*) AS n
            FROM session_results
            GROUP BY session_id
        ) cnt ON cnt.session_id = s.id
        ORDER BY s.updated_at DESC
        """,
    ).fetchall()

    return [
        {
            "id": r[0],
            "name": r[1],
            "seeds": json.loads(r[2]),
            "created_at": r[3],
            "updated_at": r[4],
            "hops_completed": r[5],
            "total_results": r[7],  # live count from session_results
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 11. get_session_results
# ---------------------------------------------------------------------------

def get_session_results(conn: sqlite3.Connection, session_id: str) -> list[dict]:
    """Fetch all results for a session with their feedback reaction (if any)."""
    rows = conn.execute(
        """
        SELECT sr.id, sr.track_id, sr.artist, sr.track_name, sr.score,
               sr.hop, sr.via
        FROM session_results sr
        WHERE sr.session_id = ?
        ORDER BY sr.hop, sr.score DESC
        """,
        (session_id,),
    ).fetchall()

    return [
        {
            "id": r[0],
            "track_id": r[1],
            "artist": r[2],
            "track_name": r[3],
            "score": r[4],
            "hop": r[5],
            "via": r[6],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 12. get_session_by_name
# ---------------------------------------------------------------------------

def get_session_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    """Look up a session by name (case-insensitive LIKE match).

    Returns the first matching session dict, or ``None``.
    """
    row = conn.execute(
        """
        SELECT id, name, seeds, created_at, updated_at, hops_completed, total_results
        FROM sessions
        WHERE name LIKE ?
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (name,),
    ).fetchone()

    if row is None:
        return None

    return {
        "id": row[0],
        "name": row[1],
        "seeds": json.loads(row[2]),
        "created_at": row[3],
        "updated_at": row[4],
        "hops_completed": row[5],
        "total_results": row[6],
    }


# ---------------------------------------------------------------------------
# 13. delete_session
# ---------------------------------------------------------------------------

def delete_session(conn: sqlite3.Connection, session_id: str) -> None:
    """Delete a session and all its related data (results, events, branches, feedback, edges)."""
    # Delete result_events for results belonging to this session
    conn.execute(
        """
        DELETE FROM result_events WHERE result_id IN (
            SELECT id FROM session_results WHERE session_id = ?
        )
        """,
        (session_id,),
    )
    conn.execute("DELETE FROM session_feedback WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM session_results WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM session_branches WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM discovery_edges WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
