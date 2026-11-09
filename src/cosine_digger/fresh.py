"""Optional web discovery, with exact identity checks before accepting a find."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from typing import Callable, Literal

import httpx

from .client import CosineClient
from .config import scrub
from .seeds import _fold, _norm_artist, resolve_album_text


class FreshAuthError(Exception):
    """Exa rejected the supplied API key."""


@dataclass
class FreshFind:
    artist: str
    title: str
    kind: Literal["track", "album"] = "track"
    source_url: str = ""
    source_title: str = ""
    published: str | None = None
    track_id: str | None = None
    video_id: str | None = None
    resolved_via: Literal["cosine", "ytmusic"] | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class ExaResults(list[dict]):
    """Search results with a returned ``diagnostics`` list, including on failure."""

    def __init__(self, results=(), *, diagnostics=()):
        super().__init__(results)
        self.diagnostics: list[dict] = list(diagnostics)


def build_queries(
    labels: list[str], artists: list[str], *, max_queries: int = 6,
) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()
    if max_queries <= 0:
        return queries
    for label, artist in zip_longest(labels, artists):
        for value, template in (
            (label, "new release on {} records"),
            (artist, "{} new album OR new EP"),
        ):
            if not value or not value.strip():
                continue
            query = template.format(" ".join(value.split()))
            if _fold(query) in seen:
                continue
            seen.add(_fold(query))
            queries.append(query)
            if len(queries) >= max_queries:
                return queries
    return queries


def exa_search(
    query: str, *, api_key: str, days: int = 45, num_results: int = 5, http=None,
) -> ExaResults:
    """Return a list of hits; non-auth errors live in ``results.diagnostics``.

    ``http`` may be an httpx client (or a fake exposing ``post``). The caller
    owns an injected client's lifetime. No key or response body is logged.
    """
    try:
        response = (http if http is not None else httpx).post(
            "https://api.exa.ai/search",
            headers={"x-api-key": api_key},
            json={
                "query": query,
                "numResults": num_results,
                "type": "auto",
                "startPublishedDate": (
                    datetime.now(timezone.utc) - timedelta(days=days)
                ).isoformat(),
                "contents": {"text": {"maxCharacters": 500}},
            },
            timeout=15.0,
        )
        if response.status_code in (401, 403):
            raise FreshAuthError(f"Exa HTTP {response.status_code}: check EXA_API_KEY")
        if not 200 <= response.status_code < 300:
            return ExaResults(diagnostics=[{
                "query": query, "stage": "exa", "status_code": response.status_code,
                "error": f"Exa HTTP {response.status_code}",
            }])
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise ValueError("Exa response must contain a results list")
        if any(not isinstance(hit, dict) for hit in payload["results"]):
            raise ValueError("Exa results must contain objects")
        return ExaResults(payload["results"])
    except FreshAuthError:
        raise
    except Exception as exc:
        return ExaResults(diagnostics=[{
            # Our words only: transport errors can quote the request, key included.
            "query": query, "stage": "exa", "error": f"Exa request failed ({type(exc).__name__})",
        }])


_SUFFIX = re.compile(
    r"\s*(?:\((?:LP|EP|album|single)\)|\[(?:LP|EP|album|single)\]"
    r"|\s[|–—-]\s*(?:Boomkat|Resident Advisor|Bandcamp))\s*$",
    re.IGNORECASE,
)


def _clean_title(text: str) -> str:
    text = text.strip()
    while (cleaned := _SUFFIX.sub("", text).strip()) != text:
        text = cleaned
    return text


def extract_candidates(result: dict) -> list[tuple[str, str]]:
    """Parse a page heading and first text line, retaining distinct identities."""
    heading = result.get("title") or ""
    text = result.get("text") or ""
    lines = [heading, text.splitlines()[0] if text else ""]
    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in lines:
        line = _clean_title(re.sub(r"^\s*RA Review:\s*", "", line, flags=re.I))
        if re.search(r",\s+by\s+", line, flags=re.I):
            title, artist = re.split(r",\s+by\s+", line, maxsplit=1, flags=re.I)
        elif " | " in line:
            title, artist = line.rsplit(" | ", 1)
        else:
            parts = re.split(r"\s+[-–—]\s+", line, maxsplit=1)
            if len(parts) != 2:
                continue
            artist, title = parts
        artist, title = artist.strip(), _clean_title(title)
        key = (_fold(artist), _fold(title))
        if not (0 < len(artist) <= 80 and 0 < len(title) <= 80) or key in seen:
            continue
        seen.add(key)
        candidates.append((artist, title))
    return candidates


def resolve_candidate(
    artist: str, title: str, client: CosineClient, *,
    ytmusic_search: Callable[[str], list[dict]] | None = None,
) -> FreshFind | None:
    """Prefer an exact indexed track, then a verified album, then exact YTM."""
    if not _norm_artist(artist) or not _fold(title):
        return None
    query = f"{artist} {title}"
    for track in client.search(query, limit=10):
        if (
            track.id
            and _norm_artist(track.artist) == _norm_artist(artist)
            and _fold(track.track or track.name) == _fold(title)
        ):
            return FreshFind(
                artist, title, track_id=track.id, video_id=track.video_id,
                resolved_via="cosine",
            )
    album_tracks = resolve_album_text(artist, title, client)
    if album_tracks:
        # Keep the recording's own artist and title (not the album's), so
        # owned and served checks match the track that will actually play.
        track = album_tracks[0]
        return FreshFind(
            track.artist or artist, track.track or track.name or title, kind="album",
            track_id=track.id, video_id=track.video_id, resolved_via="cosine",
        )
    if ytmusic_search is not None:
        for track in ytmusic_search(query):
            if (
                track.get("videoId")
                and _fold(track.get("title")) == _fold(title)
                and any(
                    _fold(credit.get("name")) == _fold(artist)
                    for credit in track.get("artists") or []
                )
            ):
                return FreshFind(
                    artist, title, video_id=track["videoId"], resolved_via="ytmusic",
                )
    return None


def find_fresh(
    profile_labels: list[str], profile_artists: list[str], *, api_key: str | None,
    client: CosineClient, ytmusic_search: Callable[[str], list[dict]] | None = None,
    max_queries: int = 6, max_finds: int = 10, http=None,
) -> tuple[list[FreshFind], dict]:
    """Discover verified finds; query and candidate failures remain inspectable."""
    if not api_key:
        return [], {"skipped": "no EXA_API_KEY"}
    diagnostics: dict = {"queries": [], "errors": [], "unresolved": []}
    finds: list[FreshFind] = []
    seen: set[tuple[str, str]] = set()
    if max_finds <= 0:
        return finds, diagnostics
    for query in build_queries(profile_labels, profile_artists, max_queries=max_queries):
        diagnostics["queries"].append(query)
        results = exa_search(query, api_key=api_key, http=http)
        diagnostics["errors"].extend(results.diagnostics)
        for result in results:
            try:
                candidates = extract_candidates(result)
            except Exception as exc:
                diagnostics["errors"].append({
                    "query": query, "stage": "extract", "error": f"{type(exc).__name__}: {scrub(str(exc))[:300]}",
                })
                continue
            for artist, title in candidates:
                key = (_fold(artist), _fold(title))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    find = resolve_candidate(artist, title, client, ytmusic_search=ytmusic_search)
                except Exception as exc:
                    diagnostics["errors"].append({
                        "query": query, "stage": "resolve", "artist": artist, "title": title,
                        "error": f"{type(exc).__name__}: {scrub(str(exc))[:300]}",
                    })
                    continue
                if find is None:
                    diagnostics["unresolved"].append({"artist": artist, "title": title})
                    continue
                find.source_url = result.get("url") or ""
                find.source_title = result.get("title") or ""
                find.published = result.get("publishedDate")
                finds.append(find)
                if len(finds) >= max_finds:
                    return finds, diagnostics
    return finds, diagnostics


def save_finds(conn: sqlite3.Connection, query: str, finds: list[FreshFind]) -> None:
    """Persist finds using the existing source/artist/title uniqueness constraint."""
    found_at = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.executemany(
            """INSERT INTO fresh_finds
               (query, source_url, artist, title, track_id, video_id, found_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_url, artist, title) DO NOTHING""",
            [(query, find.source_url, find.artist, find.title, find.track_id,
              find.video_id, found_at) for find in finds],
        )
