from http import HTTPStatus
import math
import os
import re
import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse, urlunparse

import httpx
from pydantic import ValidationError

from .config import load_env, scrub, usable_key

from .models import (
    LookupResponse,
    Playlist,
    PlaylistDetail,
    PlaylistDetailResponse,
    PlaylistListResponse,
    PlaylistResponse,
    SearchResponse,
    SimilarTracksData,
    SimilarTracksResponse,
    Track,
)

BASE_URL = "https://cosine.club/api/v1"


class CosineClientError(Exception):
    pass


class CosineAuthError(CosineClientError):
    pass


class CosineKeyMissing(CosineAuthError):
    """No cosine.club key at all, as opposed to one cosine.club refused."""


class CosineRequestError(CosineClientError):
    pass


API_BASE_ENV = "COSINE_DIGGER_API_BASE"


def api_base() -> str:
    """cosine.club's API, or a stand-in on this computer for checks.

    COSINE_DIGGER_API_BASE is refused unless it is a plain http:// address on
    127.0.0.1, ::1 or localhost, so a key can never be sent anywhere else."""
    override = os.environ.get(API_BASE_ENV, "").strip()
    if not override:
        return BASE_URL
    try:
        parts = urlparse(override)
        loopback = (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1", "localhost"}
                    and not (parts.username or parts.password or parts.query or parts.fragment)
                    and parts.port is not None)
    except ValueError:
        loopback = False
    if not loopback:
        raise CosineRequestError(f"{API_BASE_ENV} must be an http:// address on this computer")
    return override.rstrip("/")


def proxies_allowed(base: str) -> bool:
    """Only cosine.club itself may be reached through a proxy from the environment: a
    stand-in on this computer is plain http, so a proxy would read its Authorization header."""
    return base == BASE_URL


class CosineResponseError(CosineClientError):
    pass


KNOWN_CODES = frozenset({"TRACK_NOT_FOUND"})


class CosineHTTPError(CosineClientError):
    """cosine.club answered with an error. Only its status, the wait it asked for and an error
    code Digger knows are kept: the body and the reason text can quote anything, a key included."""

    def __init__(self, status_code: int, reason: str, payload: Any, retry_after_s: float | None = None):
        code = payload.get("code") if isinstance(payload, dict) else None
        self.status_code = status_code
        # Only a text code can be one of the known codes ({} or [] can't even be looked up).
        self.payload = {"code": code} if isinstance(code, str) and code in KNOWN_CODES else {}
        self.retry_after_s = retry_after_s
        try:
            phrase = HTTPStatus(status_code).phrase
        except ValueError:
            phrase = "error"
        super().__init__(f"{status_code} {phrase}" + (f": {code}" if self.payload else ""))


DEFAULT_WAIT_S = 30


def _parse(model, payload):
    """An answer that doesn't fit the expected shape is cosine.club's problem, not Digger's."""
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise CosineResponseError("Unexpected response shape") from None


def failure(exc: Exception) -> tuple[str, int | None]:
    """How a failed cosine.club call reads to people, and how long to wait.

    missing (no key), rejected (401/403, or a key that can't be sent), busy (429),
    server_error (5xx or an answer Digger can't read) or unreachable (no answer)."""
    if isinstance(exc, CosineKeyMissing):
        return "missing", None
    if isinstance(exc, CosineAuthError):
        return "rejected", None
    if isinstance(exc, CosineRequestError):
        return "unreachable", None
    wait = getattr(exc, "retry_after_s", None)
    wait = max(1, min(120, math.ceil(wait))) if wait is not None and math.isfinite(wait) else DEFAULT_WAIT_S
    if isinstance(exc, CosineHTTPError) and exc.status_code == 429:
        return "busy", wait
    return "server_error", wait


def _normalize_discogs_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "discogs.com" and not host.endswith(".discogs.com"):
        return url
    match = re.search(
        r"/(release|master)/(\d+)(?:-([^/?#]+))?",
        parsed.path or "",
        re.IGNORECASE,
    )
    if not match:
        return url
    kind, discogs_id = match.group(1).lower(), match.group(2)
    slug = match.group(3)
    path = f"/{kind}/{discogs_id}"
    if slug:
        path = f"{path}-{slug}"
    normalized = parsed._replace(
        scheme="https",
        netloc="www.discogs.com",
        path=path,
        query="",
        fragment="",
    )
    return urlunparse(normalized)


def _normalize_youtube_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "youtu.be" and host != "youtube.com" and not host.endswith(".youtube.com"):
        return url

    video_id = None
    if host == "youtu.be":
        path_id = (parsed.path or "").lstrip("/").split("/")[0]
        if path_id:
            video_id = path_id
    else:
        params = parse_qs(parsed.query)
        if params.get("v"):
            video_id = params["v"][0]
        else:
            match = re.match(r"^/(shorts|embed)/([^/?#]+)", parsed.path or "")
            if match:
                video_id = match.group(2)

    if not video_id:
        return url

    return f"https://www.youtube.com/watch?v={video_id}"


def _normalize_soundcloud_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host != "soundcloud.com" and not host.endswith(".soundcloud.com"):
        return url
    path = (parsed.path or "/").rstrip("/") or "/"
    normalized = parsed._replace(
        scheme="https",
        netloc="soundcloud.com",
        path=path,
        query="",
        fragment="",
    )
    return urlunparse(normalized)


def normalize_lookup_url(url: str) -> str:
    normalized = url.strip()
    normalized = _normalize_discogs_url(normalized)
    normalized = _normalize_youtube_url(normalized)
    normalized = _normalize_soundcloud_url(normalized)
    return normalized


# GETs retry twice unless a 429's reset is beyond the short wait window.
RETRY_STATUS = frozenset({429, 502, 503, 504})
RETRY_WAITS = (1.0, 3.0)
TIMEOUT_S = 30.0
INTERACTIVE_TIMEOUT_S = 10.0  # the seed box says "Couldn't reach cosine.club" sooner
_rate_limit_lock = Lock()
_rate_limit_state: tuple[float | None, float | None] = (None, None)


def rate_limit_snapshot() -> tuple[float | None, float | None]:
    """Return the last response's remaining requests and reset epoch."""
    with _rate_limit_lock:
        return _rate_limit_state


def _header_number(value: str | None) -> float | None:
    try:
        number = float(value) if value is not None else None
    except ValueError:
        return None
    return number if number is not None and math.isfinite(number) else None


def _record_rate_limit(response: httpx.Response) -> float | None:
    global _rate_limit_state
    remaining = _header_number(response.headers.get("x-ratelimit-remaining"))
    reset = _header_number(response.headers.get("x-ratelimit-reset"))
    with _rate_limit_lock:
        _rate_limit_state = (remaining, reset)
    return reset


def _wait_for_rate_limit() -> None:
    with _rate_limit_lock:
        remaining, reset = _rate_limit_state
    if remaining is not None and remaining <= 2 and reset is not None:
        wait = reset - time.time()
        if 0 < wait <= 10:
            time.sleep(wait)


def _retry_after_s(response: httpx.Response) -> float | None:
    if response.status_code not in (429, 503):
        return None
    now = time.time()
    header = response.headers.get("Retry-After")
    delay = _header_number(header)
    if delay is None and header is not None:
        try:
            date = parsedate_to_datetime(header)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            delay = date.timestamp() - now
        except (ValueError, TypeError, OverflowError):
            pass
    if delay is None:
        reset = _header_number(response.headers.get("x-ratelimit-reset"))
        if reset is not None:
            delay = reset - now
    return min(max(delay, 1.0), 120.0) if delay is not None else None


def _retry_wait(response: httpx.Response, default: float) -> float:
    delay = _header_number(response.headers.get("Retry-After"))
    return min(max(delay, 0.0), 10.0) if delay is not None else default


class CosineClient:
    def __init__(self, token: str | None = None):
        """Load token from COSINE_API_TOKEN env if not provided."""
        load_env()
        if token is None:
            token = os.getenv("COSINE_API_TOKEN")
        try:
            token = usable_key(token, "COSINE_API_TOKEN")
        except ValueError as exc:
            raise CosineAuthError(str(exc)) from None
        if not token:
            raise CosineKeyMissing("COSINE_API_TOKEN is not set")
        base = api_base()
        self._client = httpx.Client(
            base_url=base,
            trust_env=proxies_allowed(base),
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=TIMEOUT_S,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CosineClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            # The raw error (and its chain) can quote request headers.
            raise CosineRequestError(f"Request failed: {type(exc).__name__}: {scrub(str(exc))}") from None

        _record_rate_limit(response)
        if response.status_code in (401, 403):
            raise CosineAuthError("Unauthorized: check COSINE_API_TOKEN")

        if response.is_error:
            try:
                payload: Any = response.json()
            except ValueError:
                payload = response.text
            raise CosineHTTPError(response.status_code, response.reason_phrase, payload, _retry_after_s(response))

        if response.status_code == 204:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise CosineResponseError("Invalid JSON response") from exc

    def _get(self, path: str, params: dict[str, Any] | None = None, *, retry: bool = True) -> dict[str, Any]:
        for wait in (*(RETRY_WAITS if retry else ()), None):
            if retry:  # an interactive call answers at once; a 429 then says how long to wait
                _wait_for_rate_limit()
            try:
                response = (self._client.get(path, params=params) if retry else
                            self._client.get(path, params=params, timeout=INTERACTIVE_TIMEOUT_S))
            except httpx.RequestError as exc:
                # The raw error (and its chain) can quote request headers.
                raise CosineRequestError(f"Request failed: {type(exc).__name__}: {scrub(str(exc))}") from None
            reset = _record_rate_limit(response)
            if response.status_code == 429 and reset is not None and reset - time.time() > 10:
                break
            if wait is None or response.status_code not in RETRY_STATUS:
                break
            time.sleep(_retry_wait(response, wait))

        if response.status_code in (401, 403):
            raise CosineAuthError("Unauthorized: check COSINE_API_TOKEN")

        if response.is_error:
            try:
                payload: Any = response.json()
            except ValueError:
                payload = response.text
            raise CosineHTTPError(response.status_code, response.reason_phrase, payload, _retry_after_s(response))

        try:
            return response.json()
        except ValueError as exc:
            raise CosineResponseError("Invalid JSON response") from exc

    def search(self, query: str, limit: int = 20, *, retry: bool = True) -> list[Track]:
        """GET /search?q={query}&limit={limit}. retry=False answers at once (the seed box)."""
        payload = self._get("/search", params={"q": query, "limit": limit}, retry=retry)
        response = _parse(SearchResponse, payload)
        return response.data

    def lookup(self, url: str, *, retry: bool = True) -> list[Track]:
        """GET /tracks/lookup?url={url}."""
        normalized_url = normalize_lookup_url(url)
        payload = self._get("/tracks/lookup", params={"url": normalized_url}, retry=retry)
        response = _parse(LookupResponse, payload)
        return response.data

    def similar(
        self,
        track_id: str,
        limit: int = 50,
        start_year: int | None = None,
        end_year: int | None = None,
        min_have: int | None = None,
        max_have: int | None = None,
        min_want: int | None = None,
        max_want: int | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
    ) -> SimilarTracksData:
        """GET /tracks/{id}/similar with optional filters."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        params: dict[str, Any] = {}
        if start_year is not None:
            params["start_year"] = start_year
        if end_year is not None:
            params["end_year"] = end_year
        if min_have is not None:
            params["min_have"] = min_have
        if max_have is not None:
            params["max_have"] = max_have
        if min_want is not None:
            params["min_want"] = min_want
        if max_want is not None:
            params["max_want"] = max_want
        if min_price is not None:
            params["min_price"] = min_price
        if max_price is not None:
            params["max_price"] = max_price

        max_api_limit = 100
        collected: list[Any] = []
        seed_track = None
        offset = 0

        while len(collected) < limit:
            page_limit = min(max_api_limit, limit - len(collected))
            page_params = {**params, "limit": page_limit, "offset": offset}
            payload = self._get(f"/tracks/{track_id}/similar", params=page_params)
            response = _parse(SimilarTracksResponse, payload)
            if seed_track is None:
                seed_track = response.data.source_track
            collected.extend(response.data.similar_tracks)
            if not response.data.similar_tracks or response.meta is None or not response.meta.has_more:
                break
            offset += len(response.data.similar_tracks)

        return SimilarTracksData(source_track=seed_track, similar_tracks=collected[:limit])

    def create_playlist(self, name: str, is_public: bool = False) -> Playlist:
        payload = self._request("POST", "/playlists", json={"name": name, "is_public": is_public})
        return _parse(PlaylistResponse, payload).data

    def add_track_to_playlist(self, playlist_id: str, track_id: str) -> None:
        self._request("POST", f"/playlists/{playlist_id}/tracks", json={"track_id": track_id})

    def list_playlists(self) -> list[Playlist]:
        payload = self._get("/playlists")
        return _parse(PlaylistListResponse, payload).data

    def get_playlist(self, playlist_id: str) -> PlaylistDetail:
        payload = self._get(f"/playlists/{playlist_id}")
        return _parse(PlaylistDetailResponse, payload).data

    def delete_playlist(self, playlist_id: str) -> None:
        self._request("DELETE", f"/playlists/{playlist_id}")

    def remove_track_from_playlist(self, playlist_id: str, track_id: str) -> None:
        self._request("DELETE", f"/playlists/{playlist_id}/tracks/{track_id}")
