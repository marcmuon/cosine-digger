from __future__ import annotations

import heapq
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, Callable, ContextManager, Iterator, Literal
from urllib.parse import urlsplit

import httpx

from .config import load_env, scrub, usable_key
from .models import Track
from .slice_types import Credit, FactEntity, Facts

BASE_URL = "https://api.discogs.com"

# Discogs allows 60 requests a minute. Every real request in this process
# (seed lookups, chain hops, parallel workers, concurrent app digs) takes the
# next slot from this one gate.
PACE_S = 1.1
_pace_lock = threading.Lock()
_next_slot = 0.0


def pace(backoff: float = 0.0) -> None:
    """Wait for this process's next Discogs request slot (``backoff`` pushes
    it at least that far out, e.g. after a 429)."""
    global _next_slot
    with _pace_lock:
        now = time.monotonic()
        slot = max(now + backoff, _next_slot)
        _next_slot = slot + PACE_S
    wait = slot - now
    if wait > 0:
        time.sleep(wait)
DEFAULT_USER_AGENT = "cosine-digger/0.1.0 (+https://cosine.club)"


class DiscogsError(Exception):
    pass


class DiscogsAuthError(DiscogsError):
    pass


class DiscogsTokenMissing(DiscogsAuthError):
    """A move that needs Discogs ran with no usable token (as opposed to one Discogs refused)."""


class DiscogsRequestError(DiscogsError):
    pass


class DiscogsHTTPError(DiscogsError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class DiscogsResponseError(DiscogsError):
    pass


def _request(
    client: httpx.Client | None,
    url: str,
    params: dict[str, Any] | None,
    token: str | None,
    user_agent: str | None,
) -> dict[str, Any]:
    """Fetch Discogs JSON through the supplied client, with optional authentication."""
    load_env()
    try:
        token = usable_key(token or os.getenv("DISCOGS_TOKEN"), "DISCOGS_TOKEN")
    except ValueError as exc:
        raise DiscogsAuthError(str(exc)) from None
    user_agent = user_agent or os.getenv("DISCOGS_USER_AGENT") or DEFAULT_USER_AGENT
    headers = {"User-Agent": user_agent}
    if token:
        headers["Authorization"] = f"Discogs token={token}"

    try:
        if client is None:
            pace()  # a supplied client (chain's) paces in its transport, after its cache
        get = client.get if client is not None else httpx.get
        response = get(url, headers=headers, params=params or {}, timeout=30.0)
    except httpx.RequestError as exc:
        # The raw error (and its chain) can quote request headers.
        raise DiscogsRequestError(f"Discogs request failed: {type(exc).__name__}: {scrub(str(exc))}") from None

    if response.status_code in (401, 403):
        raise DiscogsAuthError("Unauthorized: check DISCOGS_TOKEN")
    if response.is_error:
        # The status's own phrase, never the provider's reason text: it could quote anything.
        try:
            phrase = HTTPStatus(response.status_code).phrase
        except ValueError:
            phrase = "error"
        raise DiscogsHTTPError(f"{response.status_code} {phrase}", status_code=response.status_code)

    try:
        return response.json()
    except ValueError as exc:
        raise DiscogsResponseError("Invalid JSON from Discogs") from exc


@dataclass
class ReleaseTrack:
    artist: str
    title: str
    query: str


@dataclass
class CreditedArtist:
    """An artist credited on a release (extraartists)."""
    id: str
    name: str
    role: str
    role_normalized: str  # Role with annotations stripped


@dataclass
class ArtistRelease:
    """A release from an artist's discography."""
    id: str
    title: str
    artist: str  # Primary artist on the release
    year: int | None
    role: str  # The role this artist played on this release
    type: str  # "release" or "master"


DEFAULT_PRODUCER_ROLES = [
    "Producer",
    "Mixed By",
    "Engineered By",
    "Mastered By",
    "Recorded By",
    "Co-producer",
    "Executive-Producer",
    "Remix",
]


def _extract_release_id(url: str) -> tuple[str, str] | None:
    """Return (kind, id) where kind is 'release' or 'master'."""
    match = re.search(r"/(release|master)/(\d+)", url, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


def extract_label_id(url: str) -> str | None:
    """Extract label ID from a Discogs label URL.

    Handles formats like:
    - https://www.discogs.com/label/27576-Sound-Signature
    - https://discogs.com/label/27576
    """
    match = re.search(r"/label/(\d+)", url, re.IGNORECASE)
    if not match:
        return None
    return match.group(1)


def extract_artist_id(url: str) -> str | None:
    """Extract artist ID from a Discogs artist URL.

    Handles formats like:
    - https://www.discogs.com/artist/123-Artist-Name
    - https://discogs.com/artist/123
    """
    match = re.search(r"/artist/(\d+)", url, re.IGNORECASE)
    if not match:
        return None
    return match.group(1)


@dataclass
class ArtistInfo:
    """Basic info about a Discogs artist."""
    id: str
    name: str


def fetch_artist_info(
    artist_id: str,
    token: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> ArtistInfo:
    """Fetch basic info about a Discogs artist."""
    payload = _request(client, f"{BASE_URL}/artists/{artist_id}", None, token, user_agent)

    return ArtistInfo(
        id=artist_id,
        name=payload.get("name", "Unknown Artist"),
    )


def fetch_release_artists(
    release_url: str,
    token: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> list[ArtistInfo]:
    """Fetch main artists from a Discogs release (the artists[] field, not credits)."""
    result = _extract_release_id(release_url)
    if not result:
        raise DiscogsResponseError("Discogs release/master ID not found in URL")

    kind, resource_id = result
    payload = _fetch_discogs_payload(kind, resource_id, token, user_agent, client=client)

    artists: list[ArtistInfo] = []
    for artist in payload.get("artists") or []:
        artist_id = str(artist.get("id", ""))
        name = _artist_name(artist)
        if artist_id and name:
            artists.append(ArtistInfo(id=artist_id, name=name))

    return artists


def _artist_name(artist: dict[str, Any]) -> str:
    name = (artist.get("name") or "").strip()
    return re.sub(r"\s*\(\d+\)$", "", name).strip()


def build_release_tracks(payload: dict[str, Any]) -> list[ReleaseTrack]:
    release_artists = payload.get("artists") or []
    release_artist_names = " & ".join(
        filter(None, (_artist_name(artist) for artist in release_artists))
    )
    release_artist_names = release_artist_names or (payload.get("artists_sort") or "").strip()

    tracks: list[ReleaseTrack] = []

    def add_track(item: dict[str, Any]) -> None:
        if item.get("type_") not in (None, "track"):
            return
        title = (item.get("title") or "").strip()
        if not title:
            return
        artists = item.get("artists") or []
        artist_names = " & ".join(filter(None, (_artist_name(artist) for artist in artists)))
        artist = artist_names or release_artist_names
        query = f"{artist} {title}".strip() if artist else title
        if query:
            tracks.append(ReleaseTrack(artist=artist, title=title, query=query))

    for item in payload.get("tracklist") or []:
        add_track(item)
        for sub_track in item.get("sub_tracks") or []:
            add_track(sub_track)

    return tracks


def _fetch_discogs_payload(
    kind: str,
    resource_id: str,
    token: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    endpoint = "masters" if kind == "master" else "releases"
    return _request(client, f"{BASE_URL}/{endpoint}/{resource_id}", None, token, user_agent)


def fetch_release_tracks(
    release_url: str,
    token: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> list[ReleaseTrack]:
    result = _extract_release_id(release_url)
    if not result:
        raise DiscogsResponseError("Discogs release/master ID not found in URL")

    kind, resource_id = result
    payload = _fetch_discogs_payload(kind, resource_id, token, user_agent, client=client)
    return build_release_tracks(payload)


@dataclass
class LabelInfo:
    id: str
    name: str
    release_count: int


@dataclass
class LabelRelease:
    id: str
    title: str
    artist: str
    year: int | None
    catno: str


def fetch_label_info(
    label_id: str,
    token: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> LabelInfo:
    """Fetch basic info about a Discogs label."""
    payload = _request(client, f"{BASE_URL}/labels/{label_id}", None, token, user_agent)

    return LabelInfo(
        id=label_id,
        name=payload.get("name", "Unknown Label"),
        release_count=payload.get("releases_count", 0),
    )


def fetch_label_releases(
    label_id: str,
    limit: int = 50,
    token: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> list[LabelRelease]:
    """Fetch releases from a Discogs label, sorted by year (newest first)."""
    releases: list[LabelRelease] = []
    page = 1
    per_page = min(limit, 100)

    while len(releases) < limit:
        payload = _request(
            client,
            f"{BASE_URL}/labels/{label_id}/releases",
            {"sort": "year", "sort_order": "desc", "page": page, "per_page": per_page},
            token,
            user_agent,
        )
        items = payload.get("releases", [])
        for item in items:
            releases.append(
                LabelRelease(
                    id=str(item.get("id", "")),
                    title=item.get("title", ""),
                    artist=item.get("artist", ""),
                    year=item.get("year"),
                    catno=item.get("catno", ""),
                )
            )

        if not items or page >= payload.get("pagination", {}).get("pages", 1):
            break
        page += 1

    return releases[:limit]


def _normalize_role(role: str) -> str:
    """Normalize role string by removing bracketed annotations.

    "Producer [Tracks A1-A3]" -> "Producer"
    "Mixed By, Mastered By" -> "Mixed By, Mastered By"
    """
    normalized = re.sub(r'\s*\[[^\]]*\]', '', role).strip()
    return normalized


def _role_matches(role: str, filter_roles: list[str]) -> bool:
    """Check if role matches any of the filter roles (case-insensitive substring)."""
    role_lower = _normalize_role(role).lower()
    return any(fr.lower() in role_lower for fr in filter_roles)


def fetch_release_credits(
    release_url: str,
    roles: list[str] | None = None,
    token: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> list[CreditedArtist]:
    """
    Fetch credited artists (extraartists) from a Discogs release.

    Args:
        release_url: Discogs release URL (must be /release/, not /master/)
        roles: List of roles to filter by (case-insensitive substring match).
               If None, uses DEFAULT_PRODUCER_ROLES.
        token: Discogs API token (falls back to DISCOGS_TOKEN env)
        user_agent: User agent string

    Returns:
        List of CreditedArtist with id, name, role, and normalized role.

    Raises:
        DiscogsResponseError: If URL is a master (credits only on releases)
        DiscogsAuthError: If token is invalid
        DiscogsHTTPError: On API errors
    """
    result = _extract_release_id(release_url)
    if not result:
        raise DiscogsResponseError("Discogs release/master ID not found in URL")

    kind, resource_id = result
    if kind == "master":
        raise DiscogsResponseError(
            "Credits are only available on release URLs, not masters. "
            "Please use a specific release URL instead."
        )

    if roles is None:
        roles = DEFAULT_PRODUCER_ROLES

    payload = _fetch_discogs_payload(kind, resource_id, token, user_agent, client=client)

    extraartists = payload.get("extraartists") or []
    credits: list[CreditedArtist] = []
    seen_ids: set[str] = set()

    for artist in extraartists:
        artist_id = str(artist.get("id", ""))
        if not artist_id or artist_id in seen_ids:
            continue

        role = artist.get("role", "")
        if not _role_matches(role, roles):
            continue

        seen_ids.add(artist_id)
        credits.append(
            CreditedArtist(
                id=artist_id,
                name=artist.get("name", "").strip(),
                role=role,
                role_normalized=_normalize_role(role),
            )
        )

    return credits


def fetch_artist_releases(
    artist_id: str,
    limit: int = 50,
    sort: str = "year",
    sort_order: str = "desc",
    token: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> list[ArtistRelease]:
    """
    Fetch releases from an artist's discography.

    Args:
        artist_id: Discogs artist ID
        limit: Maximum releases to return (handles pagination internally)
        sort: Sort field ("year", "title", "format")
        sort_order: "asc" or "desc"
        token: Discogs API token
        user_agent: User agent string

    Returns:
        List of ArtistRelease with id, title, artist, year, role, type.
    """
    releases: list[ArtistRelease] = []
    page = 1
    per_page = min(limit, 100)  # Discogs max is 100

    while len(releases) < limit:
        payload = _request(
            client,
            f"{BASE_URL}/artists/{artist_id}/releases",
            {"sort": sort, "sort_order": sort_order, "page": page, "per_page": per_page},
            token,
            user_agent,
        )

        for item in payload.get("releases", []):
            releases.append(
                ArtistRelease(
                    id=str(item.get("id", "")),
                    title=item.get("title", ""),
                    artist=item.get("artist", ""),
                    year=item.get("year"),
                    role=item.get("role", "Main"),
                    type=item.get("type", "release"),
                )
            )

        # Check pagination
        pagination = payload.get("pagination", {})
        if page >= pagination.get("pages", 1):
            break
        page += 1

    return releases[:limit]


# GUI facts deliberately have no route/via input and do not use the CLI's
# producer-role filter. The transport remains responsible for cache and pace.
def _safe_discogs_url(value: Any, *, image: bool = False) -> str | None:
    if not isinstance(value, str) or re.search(r"[\s\x00-\x1f\x7f]", value):
        return None
    hosts = {"i.discogs.com", "img.discogs.com"} if image else {
        "discogs.com", "www.discogs.com", "api.discogs.com",
    }
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or url.hostname not in hosts
                or url.username is not None or url.password is not None
                or url.port not in (None, 80, 443)):
            return None
    except ValueError:
        return None
    return value


def _release_reference(url: str) -> tuple[str, str] | None:
    if not _safe_discogs_url(url):
        return None
    match = re.fullmatch(r"/(release|master|releases|masters)/(\d+)(?:-[^/]*)?/?", urlsplit(url).path)
    if not match or int(match[2]) <= 0:
        return None
    return match[1].rstrip("s"), str(int(match[2]))


def _fact_id(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    value = str(value).strip()
    return str(int(value)) if value.isascii() and value.isdigit() and int(value) > 0 else None


def _fold_fact(value: str) -> str:
    return " ".join(value.split()).casefold()


def _placeholder_fact_name(name: str) -> bool:
    from .profile import PLACEHOLDERS, normalize_artist

    return normalize_artist(name) in PLACEHOLDERS


def _entities(items: list[dict], *, artists: bool = False) -> list[FactEntity]:
    result: dict[str, FactEntity] = {}
    for item in items:
        name = _artist_name(item) if artists else (item.get("name") or "").strip()
        if name and (artists or not _placeholder_fact_name(name)):
            entity = FactEntity(id=_fact_id(item.get("id")), name=name)
            result.setdefault(entity.id or _fold_fact(name), entity)
    return list(result.values())


def _validate_facts_payload(payload: Any) -> None:
    """Reject malformed facts before traversing release or nested track data."""
    if not isinstance(payload, dict):
        raise DiscogsResponseError("Invalid Discogs facts payload")
    for field in ("name", "artists_sort", "title", "position", "type_", "role", "tracks",
                  "type", "uri", "uri150", "thumb"):
        value = payload.get(field)
        if value is not None and not isinstance(value, str):
            raise DiscogsResponseError("Invalid Discogs facts text field")
    styles = payload.get("styles")
    if styles is not None and (not isinstance(styles, list) or any(not isinstance(style, str) for style in styles)):
        raise DiscogsResponseError("Invalid Discogs styles")
    for field in ("artists", "labels", "extraartists", "images", "tracklist", "sub_tracks"):
        items = payload.get(field)
        if items is None:
            continue
        if not isinstance(items, list):
            raise DiscogsResponseError("Invalid Discogs facts list")
        for item in items:
            _validate_facts_payload(item)


def _matching_tracks(payload: dict, track: Track) -> list[dict]:
    """Exact recording/artist matches, including nested tracklists on compilations."""
    from .seeds import recording_title

    _validate_facts_payload(payload)
    wanted_title = recording_title(track.track or track.name)
    wanted_artist = _fold_fact(re.sub(r"\s*\(\d+\)$", "", track.artist.strip()))
    result = []

    def visit(items: list[dict], inherited: list[dict]) -> None:
        for item in items:
            artists = item.get("artists") or inherited
            names = [_artist_name(artist) for artist in artists]
            artist_matches = wanted_artist and wanted_artist in {
                *map(_fold_fact, names), _fold_fact(" & ".join(names)),
            }
            if (item.get("type_") in (None, "track") and artist_matches
                    and wanted_title and recording_title(item.get("title") or "") == wanted_title):
                result.append({**item, "artists": artists})
            visit(item.get("sub_tracks") or [], artists)

    artists = payload.get("artists") or []
    if not artists and payload.get("artists_sort"):
        artists = [{"name": payload["artists_sort"]}]
    visit(payload.get("tracklist") or [], artists)
    return result


def _credit_roles(role: str) -> list[str]:
    # Commas inside annotations belong to the role, not to the separator.
    return list(dict.fromkeys(part.strip() for part in re.split(r",(?![^\[]*\])", role) if part.strip()))


def _credit_position_matches(selector: str, positions: set[str], ordered_positions: list[str]) -> bool:
    for part in selector.split(","):
        part = _fold_fact(part)
        if part in positions:
            return True
        if part in ordered_positions:
            continue  # An existing position names only that track, never a range.
        endpoints = (re.fullmatch(r"(.+?)\s+to\s+(.+)", part)
                     or re.fullmatch(r"([a-z]*\d+)\s*[-–]\s*([a-z]*\d+)", part))
        if endpoints and all(endpoint in ordered_positions for endpoint in endpoints.groups()):
            start, end = (ordered_positions.index(endpoint) for endpoint in endpoints.groups())
            if positions.intersection(ordered_positions[start:end + 1]):
                return True
            continue
        # Older payloads can omit unselected tracks. Numeric, side-number and
        # same-disc compound ranges are unambiguous without their endpoints.
        match = re.fullmatch(r"([a-z]*)(\d+)\s*(?:to|[-–])\s*([a-z]*)(\d+)", part)
        if match and match[1] == match[3]:
            for position in positions:
                number = re.fullmatch(r"([a-z]*)(\d+)", position)
                if (number and number[1] == match[1]
                        and int(match[2]) <= int(number[2]) <= int(match[4])):
                    return True
        compound = re.fullmatch(r"(\d+)([-.])(\d+)\s+to\s+(\d+)([-.])(\d+)", part)
        if compound and compound[1] == compound[4] and compound[2] == compound[5]:
            for position in positions:
                number = re.fullmatch(r"(\d+)([-.])(\d+)", position)
                if (number and number[1] == compound[1] and number[2] == compound[2]
                        and int(compound[3]) <= int(number[3]) <= int(compound[6])):
                    return True
    return False


def parse_release_facts(payload: dict, *, release_url: str, track: Track) -> Facts:
    """Parse verified payload data only, merging credits by (person id, scope).

    A person credited at both levels has two entries, preserving both scopes.
    Only an exact matching track contributes track credits/main artists; on a
    compilation those artists replace the release's Various Artists credit.
    Catalogue placeholders are not label/person identities. This pure parser
    never fetches a master's main release.
    """
    reference = _release_reference(release_url)
    if reference is None or not isinstance(payload, dict):
        raise DiscogsResponseError("Invalid Discogs release payload or URL")
    kind, resource_id = reference
    payload_id = _fact_id(payload.get("id"))
    if "id" in payload and payload_id != resource_id:
        raise DiscogsResponseError("Discogs payload does not match the requested release")
    matches = _matching_tracks(payload, track)
    positions = {_fold_fact(item.get("position") or "") for item in matches} - {""}
    ordered_positions = []

    def collect_positions(items: list[dict]) -> None:
        for item in items:
            position = _fold_fact(item.get("position") or "")
            if position:
                ordered_positions.append(position)
            collect_positions(item.get("sub_tracks") or [])

    collect_positions(payload.get("tracklist") or [])
    credits: dict[tuple[str, str], Credit] = {}

    def add_credits(items: list[dict], scope: Literal["release", "track"]) -> None:
        for item in items:
            name = _artist_name(item)
            if not name or _placeholder_fact_name(name):
                continue
            selector = (item.get("tracks") or "").strip()
            if selector and not _credit_position_matches(selector, positions, ordered_positions):
                continue
            credit_scope = "track" if selector else scope
            person_id = _fact_id(item.get("id"))
            key = (person_id or _fold_fact(name), credit_scope)
            roles = _credit_roles(item.get("role") or "")
            if key in credits:
                credits[key].roles = list(dict.fromkeys([*credits[key].roles, *roles]))
            else:
                credits[key] = Credit(id=person_id, name=name, roles=roles, scope=credit_scope)

    add_credits(payload.get("extraartists") or [], "release")
    for item in matches:
        add_credits(item.get("extraartists") or [], "track")
    artists = ([artist for item in matches for artist in item["artists"]] if matches else
               payload.get("artists") or ([{"name": payload["artists_sort"]}] if payload.get("artists_sort") else []))
    year = payload.get("year")
    if isinstance(year, str) and year.isascii() and year.isdigit():
        year = int(year)
    if type(year) is not int or not 1 <= year <= 9999:
        year = None
    styles = list(dict.fromkeys(style.strip() for style in payload.get("styles") or []
                               if isinstance(style, str) and style.strip()))
    thumb = _safe_discogs_url(payload.get("thumb"), image=True)
    if thumb is None:
        images = sorted(payload.get("images") or [], key=lambda item: item.get("type") != "primary")
        thumb = next((url for item in images for key in ("uri150", "uri")
                      if (url := _safe_discogs_url(item.get(key), image=True))), None)
    return Facts(release_id=resource_id, release_url=f"https://www.discogs.com/{kind}/{resource_id}",
                 labels=_entities(payload.get("labels") or []), year=year, styles=styles,
                 artists=_entities(artists, artists=True), credits=list(credits.values()), thumb=thumb)


def _facts_payload(release_url: str, *, client: httpx.Client, token: str | None,
                   user_agent: str | None) -> tuple[dict, str]:
    reference = _release_reference(release_url)
    if reference is None:
        raise DiscogsResponseError("Discogs release/master ID not found in a safe URL")
    kind, resource_id = reference
    payload = _fetch_discogs_payload(kind, resource_id, token, user_agent, client=client)
    if not isinstance(payload, dict) or ("id" in payload and _fact_id(payload["id"]) != resource_id):
        raise DiscogsResponseError("Discogs payload does not match the requested release")
    _validate_facts_payload(payload)

    def has_credits(item: dict) -> bool:
        return bool(item.get("extraartists")) or any(
            has_credits(child) for child in [*(item.get("tracklist") or []), *(item.get("sub_tracks") or [])]
        )

    if kind == "master" and not has_credits(payload):
        main_release = _fact_id(payload.get("main_release"))
        if main_release:
            try:
                release = _fetch_discogs_payload("release", main_release, token, user_agent, client=client)
            except DiscogsHTTPError as exc:
                if exc.status_code != 404:
                    raise
            else:
                _validate_facts_payload(release)
                return release, f"https://www.discogs.com/release/{main_release}"
    return payload, release_url


def fetch_track_facts(track: Track, *, client: httpx.Client, token: str | None = None,
                      user_agent: str | None = None) -> Facts | None:
    """Fetch a linked release, or verify an exact recording in search payloads.

    The GUI factory must return None instead of a client when no token is
    configured. The configured-token transport wins over legacy env headers.
    Search-result summaries alone never establish a recording's facts.
    """
    if track.external_link and _release_reference(track.external_link):
        try:
            payload, url = _facts_payload(track.external_link, client=client, token=token, user_agent=user_agent)
        except DiscogsHTTPError as exc:
            if exc.status_code == 404:
                return None
            raise
        return parse_release_facts(payload, release_url=url, track=track)
    if not track.artist.strip() or not (track.track or track.name).strip():
        return None
    search = _request(client, f"{BASE_URL}/database/search",
                      {"type": "release", "artist": track.artist, "track": track.track or track.name,
                       "per_page": 10}, token, user_agent)
    if (not isinstance(search, dict) or not isinstance(search.get("results"), list)
            or any(not isinstance(item, dict) for item in search["results"])):
        raise DiscogsResponseError("Invalid Discogs search results")
    seen = set()
    for item in (search.get("results") or [])[:10]:
        resource_id = _fact_id(item.get("id"))
        if item.get("type") != "release" or not resource_id or resource_id in seen:
            continue
        seen.add(resource_id)
        url = f"https://www.discogs.com/release/{resource_id}"
        try:
            payload, url = _facts_payload(url, client=client, token=token, user_agent=user_agent)
        except DiscogsHTTPError as exc:
            if exc.status_code == 404:
                continue
            raise
        if _matching_tracks(payload, track):
            return parse_release_facts(payload, release_url=url, track=track)
    return None


def _creation_order(value: int | float | str) -> float:
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            value = (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            raise ValueError("step_created_at must be a creation number or ISO timestamp") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("step_created_at must be finite")
    return float(value)


def _facts_unavailable(rank: int, reason: str = "provider") -> dict:
    code, message = {
        "budget": ("discogs_budget_exhausted", "Discogs request budget exhausted; remaining facts are unavailable."),
        "interrupted": ("facts_interrupted", "Facts unavailable: this step stopped before facts settled."),
    }.get(reason, ("discogs_unavailable", "! Facts unavailable: Discogs isn't answering."))
    return {"rank": rank, "facts_state": "unavailable", "facts": None,
            "note": {"code": code, "params": {}, "message": message, "phase": "facts"}}


@dataclass(eq=False)
class _FactsTask:
    job_id: str
    rank: int
    key: tuple[int, int, float, int]
    track: Track
    client_factory: Callable[[], ContextManager[httpx.Client | None] | None]
    on_result: Callable[[dict], None]
    result: dict | None = None


@dataclass
class _FactsJob:
    step_id: str | None = None
    created_at: float | None = None
    ranks: set[int] = field(default_factory=set)
    pending: int = 0
    sealed: bool = False
    unavailable_reason: str | None = None
    callback_failed: bool = False


class _FactsCancelled(Exception):
    pass


class FactsQueue:
    """Process-wide ranked facts and request-boundary dispatcher.

    submit's zero-argument factory runs in a worker, returning an owned client
    or owning context manager (None means no token). It must close over the
    job's shared 40-request tracker/cache and a request_slot hook for this pick. S4 wires that hook
    into the existing transport *after* cache lookup, around each real request
    and retry. This dispatcher does not pace, retry, spend budget, or commit.

    Four reusable workers consume the priority heap. At a request boundary a
    worker can serve a newly queued lower rank before resuming its own pick,
    preserving preemption even when the pool is occupied. Result callbacks
    run on one dispatcher thread, outside locks, in pending rank/creation/
    serial order (including cache hits), once per submitted rank. Callbacks
    for a failed job drain independently, still in that job's rank order.
    Callbacks finish before settle returns. Each worker closes its own client; any cache
    resource owned by that factory must be closed with the client.
    """

    _shared: FactsQueue | None = None
    _shared_lock = threading.Lock()

    @classmethod
    def shared(cls) -> FactsQueue:
        with cls._shared_lock:
            if cls._shared is None or cls._shared._closed:
                cls._shared = cls()
            return cls._shared

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._tasks: list[tuple[tuple[int, int, float, int], _FactsTask]] = []
        self._pending_tasks: list[tuple[tuple[int, int, float, int], _FactsTask]] = []
        self._jobs: dict[str, _FactsJob] = {}
        self._waiters: list[tuple[tuple[int, int, float, int], object]] = []
        self._serial = 0
        self._active_slot = False
        self._workers: set[threading.Thread] = set()
        self._busy_workers: set[threading.Thread] = set()
        self._local = threading.local()
        self._closing = self._closed = False
        self._publisher = threading.Thread(target=self._publish, name="discogs-facts-results")
        self._publisher.start()

    def _next_serial(self) -> int:
        self._serial += 1
        return self._serial

    def submit(self, *, job_id: str, step_id: str, rank: int,
               step_created_at: int | float | str, track: Track,
               client_factory: Callable[[], ContextManager[httpx.Client | None] | None],
               on_result: Callable[[dict], None]) -> None:
        if type(rank) is not int or rank < 1:
            raise ValueError("facts rank must be a positive integer")
        created_at = _creation_order(step_created_at)
        with self._condition:
            if self._closing:
                raise RuntimeError("facts queue is closed")
            job = self._jobs.setdefault(job_id, _FactsJob(step_id=step_id, created_at=created_at))
            if job.sealed or rank in job.ranks:
                raise ValueError("facts job is sealed or rank was already submitted")
            if (job.step_id, job.created_at) != (step_id, created_at):
                raise ValueError("facts job must belong to one step and creation time")
            task = _FactsTask(job_id, rank, (1, rank, created_at, self._next_serial()), track, client_factory, on_result)
            job.ranks.add(rank)
            job.pending += 1
            heapq.heappush(self._tasks, (task.key, task))
            heapq.heappush(self._pending_tasks, (task.key, task))
            self._start_workers()
            self._condition.notify_all()

    def _start_workers(self) -> None:
        while len(self._workers) < 4:
            worker = threading.Thread(target=self._work, name=f"discogs-facts-worker-{len(self._workers) + 1}")
            self._workers.add(worker)
            try:
                worker.start()
            except RuntimeError:
                self._workers.remove(worker)
                if not self._workers:
                    for _, task in self._pending_tasks:
                        task.result = _facts_unavailable(task.rank)
                    self._pending_tasks.clear()
                break

    def _lower_pending(self, key: tuple[int, int, float, int]) -> bool:
        return any(other_key < key and task.result is None for other_key, task in self._tasks)

    def _work(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._pending_tasks or self._lower_pending(self._pending_tasks[0][0]):
                        if self._closing and not self._pending_tasks:
                            return
                        self._condition.wait()
                    _, task = heapq.heappop(self._pending_tasks)
                    self._busy_workers.add(threading.current_thread())
                try:
                    self._run(task)
                finally:
                    with self._condition:
                        self._busy_workers.remove(threading.current_thread())
                        self._condition.notify_all()
        finally:
            with self._condition:
                self._workers.remove(threading.current_thread())
                self._condition.notify_all()

    def _run(self, task: _FactsTask) -> None:
        from .chain import BudgetExhausted

        previous_task = getattr(self._local, "task", None)
        self._local.task = task
        result = _facts_unavailable(task.rank)
        try:
            with self._condition:
                if self._jobs[task.job_id].unavailable_reason:
                    raise _FactsCancelled
            resource = task.client_factory()
            if resource is None:
                result = {"rank": task.rank, "facts_state": "no_token", "facts": None, "note": None}
            else:
                with resource as client:
                    facts = fetch_track_facts(task.track, client=client) if client is not None else None
                result = {"rank": task.rank,
                          "facts_state": "no_token" if client is None else "ready" if facts is not None else "missing",
                          "facts": facts.to_dict() if facts is not None else None, "note": None}
        except BudgetExhausted:
            result = _facts_unavailable(task.rank, "budget")
        except _FactsCancelled:
            result = _facts_unavailable(task.rank, "interrupted")
        except DiscogsAuthError:
            # Discogs answered and refused the token: Facts read as not connected (P9), not as Discogs down.
            result = {"rank": task.rank, "facts_state": "no_token", "facts": None, "note": None}
        except Exception:
            # Provider/factory errors may carry headers or keys. Only a fixed,
            # safe note crosses the callback/persistence boundary.
            result = _facts_unavailable(task.rank)
        finally:
            with self._condition:
                if self._jobs[task.job_id].unavailable_reason:
                    result = _facts_unavailable(task.rank, "interrupted")
                if task.result is None:
                    task.result = result
                self._condition.notify_all()
            self._local.task = previous_task

    def _publishable_task(self) -> _FactsTask | None:
        if self._tasks and self._tasks[0][1].result is not None:
            return self._tasks[0][1]
        failed_heads: dict[str, _FactsTask] = {}
        for key, task in self._tasks:
            if self._jobs[task.job_id].unavailable_reason:
                head = failed_heads.get(task.job_id)
                if head is None or key < head.key:
                    failed_heads[task.job_id] = task
        return min((task for task in failed_heads.values() if task.result is not None),
                   key=lambda task: task.key, default=None)

    def _publish(self) -> None:
        while True:
            with self._condition:
                while (task := self._publishable_task()) is None:
                    if (self._closing and not self._tasks and not self._workers
                            and not self._active_slot and not self._waiters):
                        self._closed = True
                        self._condition.notify_all()
                        return
                    self._condition.wait()
            failed = False
            try:
                task.on_result(task.result)
            except Exception:
                failed = True
            finally:
                with self._condition:
                    self._tasks.remove((task.key, task))
                    heapq.heapify(self._tasks)
                    job = self._jobs[task.job_id]
                    job.pending -= 1
                    job.callback_failed |= failed
                    self._condition.notify_all()

    def seal(self, job_id: str) -> None:
        with self._condition:
            self._jobs.setdefault(job_id, _FactsJob()).sealed = True
            self._condition.notify_all()

    def settle(self, job_id: str, unavailable_reason: str | None = None) -> None:
        if threading.current_thread() is self._publisher:
            raise RuntimeError("cannot settle inside a facts callback")
        with self._condition:
            job = self._jobs.setdefault(job_id, _FactsJob())
            job.sealed = True
            if unavailable_reason is not None:
                job.unavailable_reason = unavailable_reason or "interrupted"
                for _, task in self._pending_tasks:
                    if task.job_id == job_id:
                        task.result = _facts_unavailable(task.rank, "interrupted")
                self._pending_tasks = [(key, task) for key, task in self._pending_tasks if task.job_id != job_id]
                heapq.heapify(self._pending_tasks)
                # Waiting picks have no in-flight response of their own. A
                # worker may be serving another job before resuming this wait;
                # that unrelated request must not delay this job's terminal.
                waiting_keys = {key for key, _ in self._waiters}
                for key, task in self._tasks:
                    if task.job_id == job_id and task.result is None and key in waiting_keys:
                        task.result = _facts_unavailable(task.rank, "interrupted")
                self._condition.notify_all()
            while job.pending:
                self._condition.wait()
            if job.callback_failed:
                raise DiscogsError("Facts result callback failed") from None

    @contextmanager
    def request_slot(self, *, priority: Literal["foreground", "facts", "library"],
                     rank: int | None = None,
                     step_created_at: int | float | str | None = None,
                     cancelled: Callable[[], bool] | None = None) -> Iterator[None]:
        if priority not in ("foreground", "facts", "library"):
            raise ValueError("invalid Discogs request priority")
        if priority == "facts" and (type(rank) is not int or rank < 1 or step_created_at is None):
            raise ValueError("facts requests require a rank and step creation time")
        created_at = _creation_order(step_created_at) if priority == "facts" else 0.0
        task = getattr(self._local, "task", None)
        ticket = object()
        with self._condition:
            if self._closing and task is None:
                raise RuntimeError("facts queue is closed")
            serial = task.key[3] if task is not None else self._next_serial()
            key = ({"foreground": 0, "facts": 1, "library": 2}[priority],
                   rank if priority == "facts" else 0, created_at, serial)
            waiter = (key, ticket)
            heapq.heappush(self._waiters, waiter)
            self._condition.notify_all()
        acquired = False
        try:
            while not acquired:
                with self._condition:
                    if cancelled is not None and cancelled():
                        raise _FactsCancelled
                    if task is not None and self._jobs[task.job_id].unavailable_reason:
                        raise _FactsCancelled
                    if (not self._active_slot and self._waiters[0][1] is ticket
                            and not self._lower_pending(key)):
                        heapq.heappop(self._waiters)
                        self._active_slot = True
                        acquired = True
                        break
                    if (task is not None and len(self._busy_workers) == len(self._workers)
                            and self._pending_tasks and self._pending_tasks[0][0] < key):
                        _, ahead = heapq.heappop(self._pending_tasks)
                    else:
                        self._condition.wait(timeout=0.1 if cancelled is not None else None)
                        continue
                # All pool workers may already be waiting on higher ranks.
                # Serve the higher-priority pick outside locks on this worker;
                # _run restores this pick's request identity when it returns.
                self._run(ahead)
            yield
        finally:
            with self._condition:
                if acquired:
                    self._active_slot = False
                else:
                    self._waiters.remove(waiter)
                    heapq.heapify(self._waiters)
                self._condition.notify_all()

    def close(self) -> None:
        """Drain registered facts/requests, then allow a fresh shared singleton."""
        if threading.current_thread() is self._publisher:
            raise RuntimeError("cannot close inside a facts callback")
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._publisher.join()
