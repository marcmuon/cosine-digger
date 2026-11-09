"""Seed resolution: turn user input (URLs, "Artist - Track" text) into cosine.club tracks.

Console-free. Progress lines go to an optional ``notify`` callback as rich markup.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from itertools import chain, zip_longest
from typing import Callable, Iterator
from urllib.parse import urlparse

import httpx

from .client import CosineClient, CosineHTTPError, normalize_lookup_url
from .discogs import (
    BASE_URL as DISCOGS_API,
    DiscogsError,
    DiscogsAuthError,
    DiscogsHTTPError,
    DiscogsTokenMissing,
    _extract_release_id,
    _request as _discogs_request,
    _fetch_discogs_payload,
    _facts_payload,
    extract_artist_id,
    extract_label_id,
    fetch_artist_releases,
    fetch_label_releases,
    fetch_release_tracks,
    build_release_tracks,
    parse_release_facts,
)
from .fusion import COSINE_WORKERS, ordered_map
from .models import Track
from .slice_types import MoveKind, Point, Resolution, ResolvedTrack

Notify = Callable[[str], None]
ReleaseMeta = Callable[[str], dict]
FindRelease = Callable[[str, str], "str | None"]


def _quiet(_message: str) -> None:
    return None


UNAVAILABLE_NOTE = "cosine.club didn't answer"


def _unavailable(say: Notify, exc: CosineHTTPError) -> None:
    """Say when cosine.club itself failed (rate limit, server error), so an
    unresolved seed never looks like "not in the index". A 404 stays quiet."""
    if exc.status_code == 429 or exc.status_code >= 500:
        say(f"[yellow]{UNAVAILABLE_NOTE} ({exc.status_code}); try again in a minute[/]")


def _artist_matches(expected: str, actual: str) -> bool:
    """Case-insensitive check: either name contains the other."""
    e, a = expected.lower(), actual.lower()
    return bool(e.strip() and a.strip()) and (e in a or a in e)


def _artist_matches_strict(expected: str, actual: str) -> bool:
    """Typography-insensitive check: whole artist names must match.

    'klaus' matches 'Klaus' or 'KLAUS' but NOT 'Klaus Wiese'.
    'Aphex Twin' matches 'aphex twin' or 'Aphex Twin'.
    """
    e, a = _fold_artist(expected), _fold_artist(actual)
    return bool(e and a) and e == a


_ORIGINAL_SUFFIX = re.compile(r"\s*(\((original|original mix|album version)\)|-\s*original mix)$")


def recording_title(text: str) -> str:
    """A title folded to its recording: "(Original Mix)" and "(Album Version)"
    name the same recording, so they drop; any other suffix (a remix, an edit)
    stays. Every same-recording check (seeds, owned, served, known) uses this."""
    return _ORIGINAL_SUFFIX.sub("", _fold(text))


def _title_matches(expected: str, actual: str) -> bool:
    """Match a title without silently substituting a different track or remix."""
    e, a = recording_title(expected), recording_title(actual)
    return bool(e and a) and e == a


def _youtube_title(url: str, *, fetch: Callable | None = None) -> str | None:
    """Fetch video title via YouTube oEmbed (no auth needed)."""
    try:
        resp = (fetch or httpx.get)(
            "https://www.youtube.com/oembed",
            params={"url": url, "format": "json"},
            timeout=10.0,
        )
        if resp.is_success:
            return resp.json().get("title")
    except (httpx.RequestError, ValueError):
        pass
    return None


def _discogs_fallback_query(seed: str) -> str | None:
    parsed = urlparse(seed)
    match = re.search(r"/(release|master)/\d+(?:-([^/?#]+))?", parsed.path or "", re.IGNORECASE)
    if not match:
        return None
    slug = match.group(2)
    if not slug:
        return None
    return slug.replace("-", " ")


def _is_track_not_found(exc: CosineHTTPError) -> bool:
    if exc.status_code != 404:
        return False
    payload = exc.payload
    return isinstance(payload, dict) and payload.get("code") == "TRACK_NOT_FOUND"


def _parse_artist_title(seed: str) -> tuple[str, str] | None:
    """Try to split 'Artist - Title' into components."""
    parts = seed.split(" - ", 1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        return parts[0].strip(), parts[1].strip()
    return None


def _fallback_discogs_search(
    seed: str, client: CosineClient, limit: int = 5, notify: Notify | None = None
) -> Track | None:
    say = notify or _quiet
    query = _discogs_fallback_query(seed)
    if not query:
        return None
    say(f"[yellow]Discogs release not found; trying search:[/] {query}")
    results = client.search(query, limit=limit)
    if not results:
        return None
    top = results[0]
    say(
        f"[yellow]Using top search match:[/] {top.artist} - {_track_title(top)} ({top.id})"
    )
    return top


def _fold(text: str) -> str:
    return " ".join((text or "").casefold().split())


_ARTIST_PUNCTUATION = str.maketrans({
    **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
    **dict.fromkeys("\u2018\u2019\u201b\u02bc\uff07", "'"),
})


def _fold_artist(name: str) -> str:
    return _fold(unicodedata.normalize("NFKC", name or "").translate(_ARTIST_PUNCTUATION))


def _norm_artist(name: str) -> str:
    # Discogs adds numeric disambiguators to otherwise identical artist names.
    return re.sub(r"\s*\(\d+\)\s*$", "", _fold_artist(name)).strip()


def _discogs_release_meta(url: str, client=None) -> dict:
    ids = _extract_release_id(url)
    if not ids:
        return {}
    kind, resource_id = ids
    return _fetch_discogs_payload(kind, resource_id, client=client)


def discogs_find_release(artist: str, album: str, client=None, *, raise_errors: bool = False) -> str | None:
    """Find the Discogs release page for "Artist - Album" (uses DISCOGS_TOKEN).

    Only an exact (case- and space-folded) title plus a credited-artist match counts.
    """
    try:
        payload = _discogs_request(
            client, f"{DISCOGS_API}/database/search",
            {"type": "release", "artist": artist, "release_title": album, "per_page": 10},
            None, None,
        )
    except DiscogsError:
        if raise_errors:
            raise
        return None
    wanted_artist, wanted_title = _norm_artist(artist), _fold(album)
    for item in payload.get("results") or []:
        found_artist, sep, found_title = (item.get("title") or "").partition(" - ")
        if not sep:
            continue
        credited = {_norm_artist(a.rstrip("*")) for a in re.split(r",| & | / ", found_artist)}
        if wanted_artist in credited and _fold(found_title) == wanted_title:
            uri = item.get("uri") or ""
            if uri:
                return uri if uri.startswith("http") else f"https://www.discogs.com{uri}"
    return None


def resolve_album_text(
    artist: str,
    album: str,
    client: CosineClient,
    *,
    release_meta: ReleaseMeta | None = None,
    find_release: FindRelease | None = None,
    limit: int = 5,
    search_limit: int = 20,
    notify: Notify | None = None,
    discogs_client=None,
) -> list[Track]:
    """Resolve "Artist - Album" text to indexed tracks from that album.

    cosine.club search returns tracks, not albums, so a hit only counts when its
    linked Discogs release has the same title and credits the same artist.
    Without that evidence nothing is returned (no guessing).
    """
    fetch = release_meta or (lambda url: _discogs_release_meta(url, client=discogs_client))
    wanted_title, wanted_artist = _fold(album), _norm_artist(artist)
    if not wanted_title or not wanted_artist:
        return []
    try:
        hits = client.search(f"{artist} {album}", limit=search_limit)
    except CosineHTTPError as exc:
        _unavailable(notify or _quiet, exc)
        return []
    releases: dict[str, dict] = {}
    verified: list[Track] = []
    seen: set[str] = set()
    for hit in hits:
        if hit.id in seen or _norm_artist(hit.artist) != wanted_artist:
            continue
        url = hit.external_link or ""
        if urlparse(url).hostname not in {"discogs.com", "www.discogs.com"} or not _extract_release_id(url):
            continue
        if url not in releases:
            try:
                releases[url] = fetch(url) or {}
            except DiscogsError:
                releases[url] = {}
        release = releases[url]
        credited = {_norm_artist(a.get("name") or "") for a in release.get("artists") or []}
        if _fold(release.get("title") or "") == wanted_title and wanted_artist in credited:
            verified.append(hit)
            seen.add(hit.id)
            if len(verified) >= limit:
                break
    if verified:
        return verified
    # Search hits usually carry no release link; ask Discogs for the release
    # itself and resolve it like a pasted release URL.
    url = (find_release or (lambda a, b: discogs_find_release(a, b, client=discogs_client)))(artist, album)
    if not url:
        return []
    (notify or _quiet)(f"[dim]Album found on Discogs: {url}[/dim]")
    return resolve_seed(url, client, max_per_release=limit, notify=notify, release_meta=release_meta,
                        discogs_client=discogs_client)


def resolve_seed(
    seed: str,
    client: CosineClient,
    max_per_release: int = 5,
    notify: Notify | None = None,
    release_meta: ReleaseMeta | None = None,
    find_release: FindRelease | None = None,
    discogs_client=None,
) -> list[Track]:
    """Convert seed input to Track with ID.

    ``notify`` receives rich-markup progress lines; callers decide whether to
    print them. The function itself never writes to a console.
    """
    say = notify or _quiet
    # Strip shell escape backslashes that may be present from copy-paste
    seed = seed.replace("\\?", "?").replace("\\=", "=").replace("\\&", "&").strip()
    # Parse Bandcamp before generic URL normalization, which can itself reject
    # a malformed host. An invalid URL must only fail this seed.
    if "bandcamp.com" in seed.lower():
        url = seed if "://" in seed else (f"https:{seed}" if seed.startswith("//") else f"https://{seed}")
        try:
            parsed = urlparse(url)
            host = re.fullmatch(r"([a-z0-9-]+)\.bandcamp\.com", parsed.hostname or "")
            track = re.fullmatch(r"/track/([^/]+)/?", parsed.path)
            valid = parsed.scheme in {"http", "https"} and host and track
        except ValueError:
            valid = False
        if not valid:
            say(f"[yellow]Could not parse Bandcamp URL:[/] {seed}")
            return []
        artist = host.group(1)
        track_name = track.group(1).replace("-", " ")
        try:
            tracks = client.search(f"{artist} {track_name}")
        except CosineHTTPError as exc:
            _unavailable(say, exc)
            return []
        return tracks[:1]
    seed_url = normalize_lookup_url(seed)
    seed_lower = seed_url.lower()
    # Handle cosine.club track URLs directly - extract the ID from the path
    if "cosine.club/track/" in seed_lower:
        # URL format: https://cosine.club/track/{id}
        parts = seed_url.split("/track/")
        if len(parts) == 2:
            track_id = parts[1].split("?")[0].split("/")[0]  # Remove query params or trailing path
            # Ask for the track itself: the dig is then stored as "Artist - Title"
            # and teaches the taste profile, like any other seed.
            try:
                source = client.similar(track_id, limit=1).source_track
            except CosineHTTPError as exc:
                _unavailable(say, exc)
                return []
            if source is not None and source.id == track_id and source.artist:
                say(f"[dim]Resolved: {source.artist} - {_track_title(source)}[/dim]")
                return [source]
            return [Track(id=track_id, name="", artist="", track="")]
        return []
    # Handle YouTube URLs (including music.youtube.com) - extract video ID and clean up
    # normalize_lookup_url already converts youtu.be, music.youtube.com, shorts to youtube.com/watch?v=ID
    if "youtube.com" in seed_lower or "music.youtube.com" in seed_lower or "youtu.be" in seed_lower:
        if "youtube.com/watch?v=" not in seed_url:
            say(f"[yellow]Could not parse YouTube URL:[/] {seed}")
            return []
        say(f"[dim]Looking up: {seed_url}[/dim]")
        try:
            tracks = client.lookup(seed_url)
        except CosineHTTPError:
            # Fallback: get video title via oEmbed, search cosine.club
            title = getattr(client, "youtube_title", _youtube_title)(seed_url)
            if title:
                say(f"[dim]Video not in index, searching: {title}[/dim]")
                results = client.search(title, limit=3)
                if results:
                    return results[:1]
            return []
        if tracks:
            say(f"[dim]Resolved: {tracks[0].artist} - {_track_title(tracks[0])}[/dim]")
        return tracks[:1]
    # Discogs label URL -> fetch label releases -> resolve on cosine.club
    if "discogs.com/label/" in seed_lower:
        label_id = extract_label_id(seed_url)
        if not label_id:
            say(f"[yellow]Could not parse label ID from URL:[/] {seed}")
            return []
        say(f"[dim]Resolving label releases...[/dim]")
        try:
            releases = fetch_label_releases(label_id, limit=5, client=discogs_client)
        except DiscogsError as exc:
            say(f"[yellow]Discogs API error: {exc}[/]")
            return []
        if not releases:
            say(f"[yellow]No releases found for label {label_id}[/]")
            return []
        resolved: list[Track] = []
        seen_ids: set[str] = set()
        for release in releases:
            query = f"{release.artist} {release.title}".strip()
            if not query:
                continue
            try:
                results = client.search(query, limit=3)
            except CosineHTTPError:
                continue
            if results:
                for r in results:
                    if r.id not in seen_ids and _artist_matches(release.artist, r.artist):
                        resolved.append(r)
                        seen_ids.add(r.id)
                        say(f"[dim]  Found: {r.artist} - {_track_title(r)}[/dim]")
                        break
        if resolved:
            say(f"[dim]Resolved {len(resolved)} tracks from label[/dim]")
        return resolved[:max_per_release]
    # Discogs artist URL -> fetch artist releases -> resolve on cosine.club
    if "discogs.com/artist/" in seed_lower:
        artist_id = extract_artist_id(seed_url)
        if not artist_id:
            say(f"[yellow]Could not parse artist ID from URL:[/] {seed}")
            return []
        say(f"[dim]Resolving artist releases...[/dim]")
        try:
            releases = fetch_artist_releases(artist_id, limit=5, client=discogs_client)
        except DiscogsError as exc:
            say(f"[yellow]Discogs API error: {exc}[/]")
            return []
        if not releases:
            say(f"[yellow]No releases found for artist {artist_id}[/]")
            return []
        resolved = []
        seen_ids = set()
        for release in releases:
            query = f"{release.artist} {release.title}".strip()
            if not query:
                continue
            try:
                results = client.search(query, limit=3)
            except CosineHTTPError:
                continue
            if results:
                for r in results:
                    if r.id not in seen_ids and _artist_matches(release.artist, r.artist):
                        resolved.append(r)
                        seen_ids.add(r.id)
                        say(f"[dim]  Found: {r.artist} - {_track_title(r)}[/dim]")
                        break
        if resolved:
            say(f"[dim]Resolved {len(resolved)} tracks from artist[/dim]")
        return resolved[:max_per_release]
    if "discogs.com" in seed_lower:
        try:
            tracks = client.lookup(seed_url)
            # Filter to tracks whose artist matches the URL slug
            slug = _discogs_fallback_query(seed)
            if slug:
                tracks = [t for t in tracks if _artist_matches(t.artist, slug)]
            if tracks:
                say(f"[dim]Resolved {len(tracks)} tracks from release[/dim]")
                return tracks[:max_per_release]
        except CosineHTTPError:
            pass
        # Fallback: fetch tracklist from Discogs API, search each track on cosine.club
        try:
            release_tracks = fetch_release_tracks(seed, client=discogs_client)
        except DiscogsError as exc:
            say(f"[yellow]Discogs API fallback failed: {exc}[/]")
            return []
        if not release_tracks:
            return []
        say(f"[dim]Fetched {len(release_tracks)} tracks from Discogs, searching cosine.club...[/dim]")
        resolved: list[Track] = []
        seen_ids: set[str] = set()
        for rt in release_tracks:
            try:
                results = client.search(rt.query, limit=3)
            except CosineHTTPError:
                continue
            if results:
                # Validate artist matches to avoid garbage results from fuzzy search
                match = None
                for r in results:
                    if _artist_matches(rt.artist, r.artist) and _title_matches(rt.title, _track_title(r)) and r.id not in seen_ids:
                        match = r
                        break
                if match:
                    resolved.append(match)
                    seen_ids.add(match.id)
                    say(f"[dim]  Found: {match.artist} - {_track_title(match)}[/dim]")
                else:
                    say(f"[yellow]  No match for: {rt.query}[/]")
            else:
                say(f"[yellow]  Not in index: {rt.query}[/]")
        if len(resolved) > max_per_release:
            say(f"[dim]Resolved {len(resolved)} tracks from Discogs (using top {max_per_release})[/dim]")
            return resolved[:max_per_release]
        return resolved
    if "soundcloud.com" in seed_lower:
        try:
            tracks = client.lookup(seed_url)
        except CosineHTTPError as exc:
            _unavailable(say, exc)
            return []
        return tracks[:1]
    try:
        tracks = client.search(seed, limit=5)
    except CosineHTTPError as exc:
        _unavailable(say, exc)
        return []
    parsed = _parse_artist_title(seed)
    if not parsed:
        artist_tracks = [t for t in tracks if _artist_matches_strict(seed, t.artist)][:2]
        if artist_tracks:
            say(f"[dim]Digging from {artist_tracks[0].artist}'s tracks on cosine.club: "
                f"{', '.join(_track_title(t) for t in artist_tracks)}[/dim]")
            return artist_tracks
        title_tracks = [t for t in tracks if _fold(seed) == _fold(_track_title(t))]
        if title_tracks:
            top = title_tracks[0]
            say(f"[dim]Resolved: {top.artist} - {_track_title(top)}[/dim]")
            return [top]
        closest = f" (closest: {tracks[0].artist} - {_track_title(tracks[0])})" if tracks else ""
        say(f"[yellow]No exact artist or title match for '{seed}'{closest}; "
            "try 'Artist - Title' or a Discogs URL[/]")
        return []
    if not tracks:
        if parsed:
            album_tracks = resolve_album_text(
                parsed[0], parsed[1], client, release_meta=release_meta, find_release=find_release,
                discogs_client=discogs_client, limit=max_per_release, notify=notify,
            )
            if album_tracks:
                say(f"[dim]Resolved album: {seed} ({len(album_tracks)} tracks)[/dim]")
                return album_tracks
        return []
    if parsed:
        expected_artist, expected_title = parsed
        # An artist match alone cannot establish which track the user meant.
        artist_matches = [t for t in tracks if _artist_matches_strict(expected_artist, t.artist)]
        if artist_matches:
            title_matches = [
                t for t in artist_matches
                if _title_matches(expected_title, _track_title(t))
            ]
            if not title_matches:
                album_tracks = resolve_album_text(
                    expected_artist, expected_title, client,
                    release_meta=release_meta, find_release=find_release,
                    discogs_client=discogs_client, limit=max_per_release, notify=notify,
                )
                if album_tracks:
                    say(f"[dim]Resolved album: {seed} ({len(album_tracks)} tracks)[/dim]")
                    return album_tracks
                say(f"[yellow]No exact track or album match for '{seed}'. Try a release URL.[/]")
                return []
            top = title_matches[0]
        else:
            album_tracks = resolve_album_text(
                expected_artist, expected_title, client,
                release_meta=release_meta, find_release=find_release,
                discogs_client=discogs_client, limit=max_per_release, notify=notify,
            )
            if album_tracks:
                say(f"[dim]Resolved album: {seed} ({len(album_tracks)} tracks)[/dim]")
                return album_tracks
            say(
                f"[yellow]No artist match for '[/]{seed}[yellow]' "
                f"(top result was {tracks[0].artist} - {_track_title(tracks[0])}), skipping[/]"
            )
            return []
    say(f"[dim]Resolved: {top.artist} - {_track_title(top)}[/dim]")
    return [top]


def resolve_artist(artist: str, client: CosineClient, *, limit: int = 2, notify: Notify | None = None,
                   raise_errors: bool = False) -> list[Track]:
    """The tracks cosine.club has by exactly this artist. A map click digs
    from the artist, and the index often lacks the particular songs you own."""
    say = notify or _quiet
    try:
        hits = client.search(artist, limit=100)
    except CosineHTTPError as exc:
        if raise_errors:
            raise
        _unavailable(say, exc)
        return []
    wanted = _norm_artist(artist)
    tracks = [t for t in hits if _norm_artist(t.artist) == wanted][:limit]
    if tracks:
        say(f"[dim]Digging from {artist}'s tracks on cosine.club: "
            f"{', '.join(_track_title(t) for t in tracks)}[/dim]")
    return tracks


def _track_title(track: Track) -> str:
    return track.track or track.name


# GUI resolution is separate: legacy helpers deliberately turn some provider
# errors into empty results, whereas a move must fail rather than invent a miss.
def _move_note(code: str, params: dict, message: str) -> dict:
    return {"code": code, "params": params, "message": message, "phase": "resolve"}


def _move_search(query: str, client: CosineClient, *, budget: _MoveCosineBudget,
                 limit: int = 20) -> list[Track]:
    """Bound GUI queries; a rejected search is a miss, an outage still fails."""
    budget.check_cancelled()
    if budget.remaining <= 0:
        return []
    budget.spend()
    try:
        return client.search(query[:200], limit=limit)
    except CosineHTTPError as exc:
        if exc.status_code == 400:
            return []
        raise


def _move_lookup(url: str, client: CosineClient, *, budget: _MoveCosineBudget) -> list[Track]:
    budget.check_cancelled()
    if budget.remaining <= 0:
        return []
    budget.spend()
    try:
        return client.lookup(url)
    except CosineHTTPError as exc:
        if _is_track_not_found(exc):
            return []
        raise


def _move_payload(url: str, discogs_client: httpx.Client) -> dict:
    kind, resource_id = _extract_release_id(url)
    try:
        return _fetch_discogs_payload(kind, resource_id, client=discogs_client)
    except DiscogsHTTPError as exc:
        if exc.status_code == 404:
            return {}
        raise


def _move_credit_payload(url: str, discogs_client: httpx.Client) -> tuple[dict, str]:
    try:
        return _facts_payload(url, client=discogs_client, token=None, user_agent=None)
    except DiscogsHTTPError as exc:
        if exc.status_code == 404:
            return {}, url
        raise


def _move_exact(artist: str, title: str, client: CosineClient,
                budget: _MoveCosineBudget) -> list[Track]:
    return [track for track in _move_search(f"{artist} - {title}", client, budget=budget)
            if _artist_matches_strict(artist, track.artist)
            and _title_matches(title, _track_title(track))][:1]


def _unique_move_tracks(tracks: list[Track]) -> list[Track]:
    from .api import track_key

    unique = []
    seen = set()
    for track in tracks:
        identifiers = {("id", track.id), ("key", track_key(track))} - {("key", "|")}
        if track.video_id:
            identifiers.add(("video", track.video_id))
        if not seen & identifiers:
            unique.append(track)
            seen.update(identifiers)
    return unique


def _move_release(url: str, client: CosineClient, discogs_client: httpx.Client | None,
                  *, budget: _MoveCosineBudget,
                  artist: str | None = None) -> Iterator[tuple[list[Track], int]]:
    """Yield cumulative verified tracks after each recording search, including misses."""
    hits = _unique_move_tracks(_move_lookup(url, client, budget=budget))
    if artist:
        hits = [track for track in hits if _norm_artist(track.artist) == _norm_artist(artist)]
    if hits:
        yield hits, len(hits)
        return
    if discogs_client is None or budget.remaining <= 0:
        yield [], 0
        return
    release_tracks = build_release_tracks(_move_payload(url, discogs_client))
    resolved = []
    for recording in release_tracks:
        if budget.remaining <= 0:
            break
        matches = [track for track in _move_search(recording.query, client, budget=budget)
                   if _norm_artist(recording.artist) == _norm_artist(track.artist)
                   and _title_matches(recording.title, _track_title(track))]
        resolved = _unique_move_tracks([*resolved, *matches])
        yield resolved, len(release_tracks)
    if not release_tracks:
        yield [], 0


def _optional_discogs(results: Iterator[tuple[list[Track], int]]) -> Iterator[tuple[list[Track], int]]:
    """Discogs only helps a track seed here: a token Discogs refuses reads as
    not connected, so the dig still runs on cosine.club alone (P9)."""
    yielded = False
    try:
        for item in results:
            yielded = True
            yield item
    except DiscogsAuthError:
        if not yielded:
            yield [], 0


def _move_album(artist: str, album: str, client: CosineClient,
                discogs_client: httpx.Client | None, *,
                budget: _MoveCosineBudget) -> Iterator[tuple[list[Track], int]]:
    if discogs_client is None or budget.remaining <= 0:
        yield [], 0
        return
    releases = {}
    tracks = []
    total = 0
    for hit in _move_search(f"{artist} {album}", client, budget=budget, limit=100):
        if _norm_artist(hit.artist) != _norm_artist(artist):
            continue
        url = hit.external_link or ""
        if urlparse(url).hostname not in {"discogs.com", "www.discogs.com"} or not _extract_release_id(url):
            continue
        if url not in releases:
            releases[url] = _move_payload(url, discogs_client)
        payload = releases[url]
        credited = {_norm_artist(item.get("name") or "") for item in payload.get("artists") or []}
        if _fold(payload.get("title") or "") == _fold(album) and _norm_artist(artist) in credited:
            tracks.append(hit)
            total = max(total, len(build_release_tracks(payload)))
    if tracks:
        tracks = _unique_move_tracks(tracks)
        yield tracks, max(total, len(tracks))
        return
    if budget.remaining <= 0:
        yield [], 0
        return
    url = discogs_find_release(artist, album, client=discogs_client, raise_errors=True)
    if url:
        yield from _move_release(url, client, discogs_client, budget=budget, artist=artist)
    else:
        yield [], 0


class _MoveCancelled(Exception):
    """Stop origin resolution before starting another provider request."""


@dataclass
class _MoveCosineBudget:
    limit: int
    used: int = 0
    cancelled: Callable[[], bool] | None = None

    def check_cancelled(self) -> None:
        if self.cancelled is not None and self.cancelled():
            raise _MoveCancelled

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    def spend(self, calls: int = 1) -> None:
        self.used += calls


@dataclass
class _MoveDiscogsClient:
    """Check every GET, including pagination and a master's main release."""

    client: httpx.Client
    budget: _MoveCosineBudget

    def get(self, *args, **kwargs):
        self.budget.check_cancelled()
        return self.client.get(*args, **kwargs)


def _move_recordings(release, client: CosineClient, *, payload: dict,
                     budget: _MoveCosineBudget, release_url: str):
    """Yield recordings verified by release lookup or the release's tracklist.

    Label/artist callers stop at one recording. Person callers can continue
    until a recording with the verified credit is found.
    """
    tracklist = build_release_tracks(payload)
    for hit in _move_lookup(release_url, client, budget=budget):
        if _norm_artist(hit.artist) != _norm_artist(release.artist):
            continue
        if not any(_norm_artist(item.artist) == _norm_artist(hit.artist)
                   and _title_matches(item.title, _track_title(hit)) for item in tracklist):
            continue
        yield hit
    if not tracklist:
        return
    release_query = f"{release.artist} {release.title}".strip()[:200]
    for hit in _move_search(release_query, client, budget=budget):
        if any(_norm_artist(item.artist) == _norm_artist(hit.artist)
               and _title_matches(item.title, _track_title(hit)) for item in tracklist):
            yield hit

    # Keep each release sequential: a hit must prevent the next track search.
    searched_queries = {release_query}
    for recording in tracklist[:2]:
        query = recording.query[:200]
        if query in searched_queries:
            continue  # All hits from this identical query have been checked.
        searched_queries.add(query)
        for hit in _move_search(query, client, budget=budget):
            if (_norm_artist(recording.artist) == _norm_artist(hit.artist)
                    and _title_matches(recording.title, _track_title(hit))):
                yield hit


def _move_discography(point: Point, kind: str, client: CosineClient,
                      discogs_client: httpx.Client | None, notes: list[dict],
                      budget: _MoveCosineBudget) -> list[Track]:
    from .chain import BudgetExhausted

    if discogs_client is None:
        messages = {"label": "Sounds like its label needs a Discogs token. Add one in Setup.",
                    "artist": "Sounds like its artist needs a Discogs token. Add one in Setup.",
                    "person": "Sounds like a credited person needs a Discogs token. Add one in Setup."}
        raise DiscogsTokenMissing(messages[kind])
    resource_id = point.discogs_id
    if not resource_id and kind == "artist":
        matches = {}
        page = 1
        while True:
            payload = _discogs_request(discogs_client, f"{DISCOGS_API}/database/search",
                                      {"type": "artist", "q": point.name, "per_page": 100, "page": page}, None, None)
            matches.update({str(item["id"]): item for item in payload.get("results") or []
                            if item.get("id") and _norm_artist(item.get("title") or "") == _norm_artist(point.name)})
            if page >= (payload.get("pagination") or {}).get("pages", 1):
                break
            page += 1
        if len(matches) > 1:
            notes.append(_move_note("S-PICK-ARTIST", {"Artist": point.name,
                                     "choices": [{"id": key, "name": value["title"]}
                                                 for key, value in matches.items()]}, "Which artist?"))
            return []
        resource_id = next(iter(matches), None)
        if resource_id is None:
            return []
    if not resource_id or not resource_id.isascii() or not resource_id.isdigit() or int(resource_id) < 1:
        raise ValueError(f"{kind} origins require a valid Discogs id")
    if kind == "label":
        releases = fetch_label_releases(resource_id, limit=20, client=discogs_client)
    elif kind == "person":
        from .discogs import ArtistRelease

        releases = []
        # Discogs groups Main releases before credits, even when sorted by year.
        for page in range(1, 6):
            try:
                payload = _discogs_request(discogs_client, f"{DISCOGS_API}/artists/{resource_id}/releases",
                                          {"sort": "year", "sort_order": "desc", "page": page, "per_page": 100},
                                          None, None)
            except (DiscogsError, BudgetExhausted):
                if not releases:
                    raise
                break
            releases.extend(ArtistRelease(
                id=str(item.get("id", "")), title=item.get("title", ""), artist=item.get("artist", ""),
                year=item.get("year"), role=item.get("role", "Main"), type=item.get("type", "release"))
                for item in payload.get("releases", [])
                if _fold(item.get("role", "Main")) not in {"main", "main artist"})
            if len(releases) >= 20 or page >= (payload.get("pagination") or {}).get("pages", 1):
                break
    else:
        limit = 20
        while True:
            releases = fetch_artist_releases(resource_id, limit=limit, client=discogs_client)
            main = [release for release in releases if _fold(release.role) in {"main", "main artist"}]
            if len(main) >= 5 or len(releases) < limit:
                break
            limit += 20
        releases = main[:5]
    releases = sorted(releases, key=lambda release: release.year or 0, reverse=True)
    if kind == "person":
        releases = releases[:20]
    if kind == "label":
        distinct = []
        release_ids = set()
        for release in releases:
            if release.id not in release_ids:
                distinct.append(release)
                release_ids.add(release.id)
        releases = distinct[:20]
    resolved = []
    credits = []
    seen = set()

    def release_candidates(release, payload, release_budget, url):
        for hit in _move_recordings(release, client, payload=payload, budget=release_budget, release_url=url):
            key = f"{_norm_artist(hit.artist)}|{recording_title(_track_title(hit))}"
            identifiers = {("id", hit.id), ("key", key)}
            if hit.video_id:
                identifiers.add(("video", hit.video_id))
            if seen & identifiers:
                continue
            credit = None
            if kind == "person":
                facts = parse_release_facts(payload, release_url=url, track=hit)
                if any(entity.id == resource_id or _norm_artist(entity.name) == _norm_artist(point.name)
                       for entity in facts.artists):
                    continue
                matches = [credit for credit in facts.credits if credit.id == resource_id
                           and any(_fold(role) not in {"main", "main artist"} for role in credit.roles)]
                if not matches:
                    continue
                credit = {"release_url": url, "cosine_id": hit.id,
                          "credits": [item.to_dict() for item in matches]}
            yield hit, identifiers, credit

    def prepare_release(entry):
        candidates = release_candidates(*entry)
        first = next(candidates, None)
        # Keep the tail lazy: if an earlier release owns this hit, continue
        # this release's existing lookup/search order to its next recording.
        return chain((first,), candidates) if first is not None else ()

    start = 0
    payloads = {}
    discogs_exhausted = None
    while start < len(releases) and len(resolved) < 5:
        budget.check_cancelled()
        window = []
        reserved = 0
        # Reserve full tails before starting workers, then charge only actual
        # calls in release order. A cached payload can join the next window if
        # earlier releases use less than their reservations.
        stop = min(len(releases), start + min(COSINE_WORKERS, 5 - len(resolved)))
        for index in range(start, stop):
            budget.check_cancelled()
            if budget.remaining - reserved < 2:
                break
            release = releases[index]
            if index not in payloads:
                url = f"https://www.discogs.com/{getattr(release, 'type', 'release')}/{release.id}"
                try:
                    payloads[index] = (_move_credit_payload(url, discogs_client) if kind == "person" else
                                       (_move_payload(url, discogs_client), url))
                except BudgetExhausted as exc:
                    discogs_exhausted = exc
                    break
            payload, url = payloads[index]
            worst_case = 2 + min(2, len(build_release_tracks(payload)))
            if worst_case > budget.remaining - reserved:
                break
            window.append((release, payload, _MoveCosineBudget(worst_case, cancelled=budget.cancelled), url))
            reserved += worst_case
        if not window:
            break
        # Workers read seen while the main thread waits; only the ordered
        # fold below updates identities, counts and notes between windows.
        for entry, candidates in zip(window, ordered_map(prepare_release, window)):
            for hit, identifiers, credit in candidates:
                if seen & identifiers:
                    continue
                resolved.append(hit)
                seen.update(identifiers)
                if credit is not None:
                    credits.append(credit)
                break
            # Continuing a duplicate's lazy tail spends on its local counter.
            budget.spend(entry[2].used)
        start += len(window)
        if discogs_exhausted:
            break
    if discogs_exhausted is not None and not resolved:
        raise discogs_exhausted
    if kind == "label" and len(resolved) < 5:
        notes.append(_move_note("S-N-LABEL-FEW", {"m": len(resolved), "Label": point.name},
                     f"Only {len(resolved)} of the newest releases on {point.name} are on cosine.club, "
                     "so this step leans on them."))
    if kind == "person" and credits:
        notes.append(_move_note("S-HDR-PERSON", {"Person": point.name, "releases": credits},
                                f"Sounds like records {point.name} is credited on"))
    return resolved


def _move_point(point: Point, move: MoveKind, client: CosineClient,
                discogs_client: httpx.Client | None, notes: list[dict],
                budget: _MoveCosineBudget) -> Iterator[tuple[list[Track], str, int]]:
    budget.check_cancelled()
    # Saved ids and cosine track links use similar, outside the lookup budget.
    if (budget.remaining <= 0 and move != "these" and not point.cosine_id
            and not (point.url and urlparse(point.url).hostname in {"cosine.club", "www.cosine.club"})):
        notes.append(_move_note("S-N-UNCHECKED", {"seed": point.name},
                     f"'{point.name}' wasn't checked: this step used all its cosine.club lookups on your other seeds."))
        yield [], "unavailable", 0
        return
    if move == "these":
        if not point.cosine_id:
            raise ValueError("keeps require their saved cosine ids")
        artist, title = point.artist or "", point.title or point.name
        yield [Track(id=point.cosine_id, name=point.name, artist=artist, track=title,
                     video_id=point.video_id)], "exact", 1
        return
    if point.kind in {"label", "artist", "person"}:
        tracks = _move_discography(point, point.kind, client, discogs_client, notes, budget)
        yield tracks, "exact" if tracks else "missing", len(tracks)
        return
    artist, title = point.artist, point.title
    if not artist or not title:
        parsed = _parse_artist_title(point.name)
        if parsed:
            artist, title = parsed
    if point.cosine_id:
        try:
            source = client.similar(point.cosine_id, limit=1).source_track
        except CosineHTTPError as exc:
            if not _is_track_not_found(exc):
                raise
        else:
            if source.id != point.cosine_id:
                raise ValueError("cosine source id does not match the requested track")
            yield [source], "exact", 1
            return
    elif point.url:
        url = normalize_lookup_url(point.url)
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("seed URL must use HTTP or HTTPS")
        if host in {"cosine.club", "www.cosine.club"}:
            match = re.fullmatch(r"/track/([^/]+)/?", parsed.path)
            if not match:
                raise ValueError("invalid cosine track URL")
            yield from _move_point(point.model_copy(update={"cosine_id": match[1], "url": None}),
                                   move, client, discogs_client, notes, budget)
            return
        if host in {"discogs.com", "www.discogs.com"}:
            kind = "label" if extract_label_id(url) else "artist" if extract_artist_id(url) else None
            if kind:
                resource_id = extract_label_id(url) if kind == "label" else extract_artist_id(url)
                tracks = _move_discography(point.model_copy(update={"discogs_id": resource_id}),
                                           kind, client, discogs_client, notes, budget)
                yield tracks, "exact" if tracks else "missing", len(tracks)
                return
            if not _extract_release_id(url):
                raise ValueError("invalid Discogs release URL")
            for tracks, total in _optional_discogs(_move_release(url, client, discogs_client, budget=budget,
                                                                 artist=point.artist)):
                yield tracks, "exact" if tracks else "missing", total
            return
        if host not in {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}:
            raise ValueError("unsupported seed URL")
        tracks = _move_lookup(url, client, budget=budget)
        if tracks:
            yield tracks[:1], "exact", 1
            return
        if not artist or not title:
            budget.check_cancelled()
            parsed_title = _parse_artist_title(getattr(client, "youtube_title", _youtube_title)(url) or "")
            if parsed_title:
                artist, title = parsed_title
    if artist and title:
        tracks = _move_exact(artist, title, client, budget)
        if tracks:
            yield tracks, "exact", 1
            return
        # Bare text may name an album; an explicit track keeps its track meaning.
        ambiguous_text = point.kind == "track" and not any((point.library_id, point.cosine_id,
                         point.track_key, point.url, point.artist, point.title, point.video_id))
        if move == "seed" and (point.kind == "album" or ambiguous_text):
            for tracks, total in _optional_discogs(_move_album(artist, title, client, discogs_client, budget=budget)):
                yield tracks, "exact" if tracks else "missing", total if tracks else 0
            if tracks:
                return
        tracks = [hit for hit in _move_search(artist, client, budget=budget, limit=100)
                  if _norm_artist(hit.artist) == _norm_artist(artist)][:2]
        yield tracks, "substituted" if tracks else "missing", len(tracks)
        return
    if not point.url:
        hits = _move_search(point.name, client, budget=budget)
        tracks = [hit for hit in hits if _artist_matches_strict(point.name, hit.artist)][:2]
        tracks = tracks or [hit for hit in hits if _title_matches(point.name, _track_title(hit))][:1]
        yield tracks, "exact" if tracks else "missing", len(tracks)
        return
    yield [], "missing", 0


def resolve_move_origins(origins: list[Point], move: MoveKind, *, client: CosineClient,
                         discogs_client: httpx.Client | None, seed_budget: int = 10,
                         cosine_budget: int = 60,
                         cancelled: Callable[[], bool] | None = None) -> Resolution:
    """Resolve GUI chips in turns, then select recordings in round-robin order.

    Keeps arrive as the server's newest-first, validated snapshot. Requested
    metadata and provenance remain unchanged; only resolution fields are added.
    Lookups and searches share a cosine call budget; similarity calls are separate.
    """
    from .api import track_key, track_label

    if type(seed_budget) is not int or not 1 <= seed_budget <= 10:
        raise ValueError("seed budget must be an integer from one to ten")
    if type(cosine_budget) is not int or cosine_budget < 0:
        raise ValueError("cosine budget must be a nonnegative integer")
    budget = _MoveCosineBudget(cosine_budget, cancelled=cancelled)
    if discogs_client is not None and cancelled is not None:
        discogs_client = _MoveDiscogsClient(discogs_client, budget)
    if move not in {"seed", "track", "label", "artist", "person", "these"} or not origins:
        raise ValueError("invalid move or empty origins")
    if move not in {"seed", "these"} and (len(origins) != 1 or origins[0].kind != move):
        raise ValueError("branch moves require one origin of the matching kind")
    if move == "these" and any(point.kind != "track" for point in origins):
        raise ValueError("keeps must be track origins")
    if move == "seed" and len(origins) > 10:
        raise ValueError("a root has at most ten seed chips")
    notes = []
    points = []
    seen_points = set()
    for point in origins:
        if point.point_id in seen_points:
            notes.append(_move_note("S-N-DUP", {"Artist - Title": point.name},
                         f"'{point.name}' is in more than one of your seeds; it counts once."))
        else:
            seen_points.add(point.point_id)
            points.append(point.model_copy(deep=True))
    if move == "these" and len(points) > 10:
        notes.append(_move_note("S-N-KEEPS-CAP", {"k": len(points)},
                                f"Using your 10 most recent keeps of {len(points)}."))
        points = points[:10]
    groups = [[] for _ in points]
    totals = [0 for _ in points]
    owners = {}
    pending = []
    for index, point in enumerate(points):
        point.resolution = "unavailable"
        pending.append((index, _move_point(point, move, client, discogs_client, notes, budget)))
    first_turn = True
    verified = 0
    duplicates = set()
    while pending and (first_turn or verified < seed_budget):
        following = []
        for index, turns in pending:
            # Every chip takes its first turn: in the round-robin selection its
            # first track outranks any chip's second, even past ten verified.
            if verified >= seed_budget and not first_turn:
                break
            point = points[index]
            try:
                tracks, state, total = next(turns)
            except StopIteration:
                continue
            except _MoveCancelled:
                return Resolution(origins=points, resolved_tracks=[], seed_to_origin={})
            point.resolution = state
            totals[index] = total
            following.append((index, turns))
            for track in tracks:
                key = track_key(track)
                identifiers = [("id", track.id)] + ([("key", key)] if key != "|" else [])
                if track.video_id:
                    identifiers.append(("video", track.video_id))
                owner = next((owners[identifier] for identifier in identifiers if identifier in owners), None)
                if owner is not None:
                    duplicate = (point.point_id, track.id)
                    if owner != point.point_id and duplicate not in duplicates:
                        duplicates.add(duplicate)
                        notes.append(_move_note("S-N-DUP", {"Artist - Title": track_label(track)},
                                     f"'{track_label(track)}' is in more than one of your seeds; it counts once."))
                    continue
                owners.update(dict.fromkeys(identifiers, point.point_id))
                groups[index].append(track)
                verified += 1
        first_turn = False
        pending = following if budget.remaining > 0 or move == "these" else []
    selected = [[] for _ in points]
    resolved = []
    seed_to_origin = {}
    for row in zip_longest(*groups):
        for index, track in enumerate(row):
            if track is not None and len(resolved) < seed_budget:
                resolved.append(track)
                selected[index].append(track)
                seed_to_origin[track.id] = points[index].point_id
    for index, point in enumerate(points):
        tracks = selected[index]
        point.resolved_tracks = [ResolvedTrack(cosine_id=track.id, track_key=track_key(track),
                                 artist=track.artist, title=_track_title(track), video_id=track.video_id)
                                 for track in tracks]
        if point.resolution == "substituted" and tracks:
            titles = ", ".join(_track_title(track) for track in tracks)
            artist = tracks[0].artist
            params = {"seed": point.name, "n": len(tracks), "Artist": artist, "titles": titles,
                      **{f"t{i+1}": _track_title(track) for i, track in enumerate(tracks)}}
            prefix = "Your file " if point.library_id else ""
            noun = "track" if len(tracks) == 1 else "tracks"
            notes.append(_move_note("S-N-SWAP-FILE" if point.library_id else "S-N-SWAP-TEXT", params,
                         f"{prefix}'{point.name}' isn't on cosine.club. Using {len(tracks)} indexed "
                         f"{artist} {noun} instead: {titles}."))
        album = (point.kind == "album" or
                 (point.kind == "track" and point.resolution == "exact" and totals[index] > 1
                  and (point.title or _parse_artist_title(point.name))) or
                 (point.url and _extract_release_id(point.url)))
        if album and totals[index] > len(tracks):
            notes.append(_move_note("S-N-ALBUM", {"Album": point.name, "t": totals[index], "u": len(tracks)},
                         f"The album '{point.name}' has {totals[index]} tracks; {len(tracks)} of them are used "
                         "so every seed gets a turn (10 seed tracks at most)."))
        if move == "seed" and point.resolution == "missing" and not any(note["code"] == "S-PICK-ARTIST" for note in notes):
            notes.append(_move_note("S-N-DROPPED", {"seed": point.name},
                         f"'{point.name}' isn't on cosine.club and has no stand-in, so this step uses your other seeds."))
    exact = sum(point.resolution == "exact" for point in points)
    if resolved and len(points) > 1 and exact < len(points):
        notes.insert(0, _move_note("S-N-PARTIAL", {"k": exact, "n": len(points)},
                                  f"{exact} of your {len(points)} seeds are on cosine.club."))
    if not resolved and not any(note["code"] == "S-PICK-ARTIST" for note in notes):
        if move == "seed":
            notes = [note for note in notes if note["code"] != "S-N-DROPPED"]
            if all(point.resolution == "missing" for point in points):
                artists = ", ".join(dict.fromkeys(point.artist or point.name for point in points))
                notes.append(_move_note("S-N-NONE", {"Artist(s)": artists},
                             f"None of your seeds are on cosine.club, and it has no tracks by {artists} yet. "
                             "Try another track by them, or paste a cosine.club link."))
        elif move in {"track", "label", "artist", "person"}:
            name = points[0].name
            if move == "artist":
                notes.append(_move_note("S-HDR-ARTIST", {"Artist": name}, f"Sounds like the artist {name}"))
            elif move == "person":
                notes.append(_move_note("S-HDR-PERSON", {"Person": name}, f"Sounds like records {name} is credited on"))
            alternative = "sounds like this track" if move == "artist" else "sounds like its artist"
            notes.append(_move_note("S-E-EMPTY", {"alt 1": alternative, "alt 2": "go one step further"},
                         f"Nothing came back for this move. Try {alternative}, or go one step further."))
    return Resolution(origins=points, resolved_tracks=resolved, seed_to_origin=seed_to_origin, notes=notes)
