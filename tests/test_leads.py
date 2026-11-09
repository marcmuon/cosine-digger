"""Tests for the leads engine (leads.py) and CLI commands."""

from __future__ import annotations

import sqlite3

import pytest

from cosine_digger.leads import (
    detect_leads,
    mute_lead,
    )
from cosine_digger.taste import _SESSION_SCHEMA


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn():
    """In-memory SQLite with full session + result_events schema."""
    c = sqlite3.connect(":memory:")
    c.executescript(_SESSION_SCHEMA)
    yield c
    c.close()


def _create_session(conn, name, seeds=None):
    """Helper: insert a session row and return its id."""
    import json
    from uuid import uuid4

    sid = str(uuid4())
    conn.execute(
        "INSERT INTO sessions (id, name, seeds, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (sid, name, json.dumps(seeds or []), "2026-03-25T00:00:00Z", "2026-03-25T00:00:00Z"),
    )
    conn.commit()
    return sid


def _add_result(conn, session_id, track_id, artist, track_name, score, hop, via):
    """Helper: insert a session_result row and return its rowid."""
    cur = conn.execute(
        """
        INSERT INTO session_results
            (session_id, track_id, artist, track_name, score, hop, via, discovered_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, '2026-03-25T00:00:00Z')
        """,
        (session_id, track_id, artist, track_name, score, hop, via),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# _parse_via
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# detect_leads
# ---------------------------------------------------------------------------


class TestDetectLeads:
    def test_label_lead_two_sessions(self, conn):
        """Label appearing in 2+ sessions is detected as a lead."""
        s1 = _create_session(conn, "dig-1")
        s2 = _create_session(conn, "dig-2")
        _add_result(conn, s1, "t1", "A", "Track1", 0.9, 1, "label: Hessle Audio")
        _add_result(conn, s2, "t2", "B", "Track2", 0.8, 1, "label: Hessle Audio")

        leads = detect_leads(conn)
        assert len(leads) >= 1
        assert leads[0]["value"] == "Hessle Audio"
        assert leads[0]["type"] == "label"
        assert leads[0]["appearances"] == 2

    def test_duplicate_seeds_not_double_counted(self, conn):
        """Same seeds run twice should count as 1 appearance, not 2."""
        seeds = ["Artist X - Track Y"]
        s1 = _create_session(conn, "dig-1", seeds=seeds)
        s2 = _create_session(conn, "dig-2", seeds=seeds)
        _add_result(conn, s1, "t1", "A", "Track1", 0.9, 1, "label: Seil Records")
        _add_result(conn, s2, "t2", "A", "Track1", 0.9, 1, "label: Seil Records")

        leads = detect_leads(conn)
        # Same seeds → 1 unique angle → below the 2-appearance threshold
        assert not any(l["value"] == "Seil Records" for l in leads)

    def test_muted_lead_excluded(self, conn):
        """Muted leads do not appear in detect_leads() results."""
        s1 = _create_session(conn, "dig-1")
        s2 = _create_session(conn, "dig-2")
        _add_result(conn, s1, "t1", "A", "Track1", 0.9, 1, "label: Muted Label")
        _add_result(conn, s2, "t2", "B", "Track2", 0.8, 1, "label: Muted Label")

        mute_lead(conn, "label", "Muted Label")

        leads = detect_leads(conn)
        assert not any(l["value"] == "Muted Label" for l in leads)

    def test_exact_match_no_prefix_bleed(self, conn):
        """'label: Warp' should not bleed into 'label: Warp Records'."""
        s1 = _create_session(conn, "dig-1")
        s2 = _create_session(conn, "dig-2")
        _add_result(conn, s1, "t1", "A", "T1", 0.9, 1, "label: Warp")
        _add_result(conn, s2, "t2", "B", "T2", 0.8, 1, "label: Warp")
        _add_result(conn, s1, "t3", "C", "T3", 0.7, 1, "label: Warp Records")
        _add_result(conn, s2, "t4", "D", "T4", 0.6, 1, "label: Warp Records")

        leads = detect_leads(conn)
        warp = [l for l in leads if l["value"] == "Warp"]
        warp_records = [l for l in leads if l["value"] == "Warp Records"]
        assert len(warp) == 1
        assert len(warp_records) == 1
        assert warp[0]["total_tracks"] == 2
        assert warp_records[0]["total_tracks"] == 2


# ---------------------------------------------------------------------------
# get_lead_tracks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# record_event
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# mute / unmute
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_saved_tracks
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_unactioned_results
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_discovery_context
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_recap
# ---------------------------------------------------------------------------


class TestGetRecap:
    @pytest.fixture(autouse=True)
    def fixed_clock(self, monkeypatch):
        from datetime import datetime, timezone
        import cosine_digger.leads as leads_module

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 4, 1, tzinfo=timezone.utc).astimezone(tz)

        monkeypatch.setattr(leads_module, "datetime", FixedDatetime)


# ---------------------------------------------------------------------------
# CLI command registration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------
