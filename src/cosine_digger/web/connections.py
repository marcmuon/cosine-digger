"""Check a key with its provider before Digger says it works (first-run setup).

One short request, no retries. The state is what the browser words:
connected, rejected (the provider refused the key), busy (429, with the wait it
asked for), server_error (5xx), unreachable (no answer). A key with spaces or
line breaks is malformed and never sent. Nothing here logs or returns a key.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import time

import httpx

from .. import discogs
from ..client import CosineRequestError, api_base, proxies_allowed
from ..config import ENV_VARS, usable_key

DEFAULT_WAIT_S = 30
TIMEOUT_S = 5.0


@dataclass(frozen=True)
class Check:
    state: str  # connected | rejected | busy | server_error | unreachable | malformed | missing
    retry_after_s: int | None = None


def _wait(response: httpx.Response) -> int:
    """Retry-After as seconds or an HTTP date, kept to 1-120 s; anything else waits 30 s."""
    header = response.headers.get("Retry-After", "").strip()
    try:
        seconds = float(header)
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = when.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return DEFAULT_WAIT_S
    if not math.isfinite(seconds):
        return DEFAULT_WAIT_S
    return max(1, min(120, math.ceil(seconds)))


def classify(response: httpx.Response) -> Check:
    if response.is_success:
        return Check("connected")
    if response.status_code in (401, 403):
        return Check("rejected")
    if response.status_code == 429:
        return Check("busy", _wait(response))
    return Check("server_error", _wait(response))


def _send(method: str, url: str, *, timeout: float, **kwargs) -> Check:
    try:
        response = httpx.request(method, url, timeout=timeout, follow_redirects=False, **kwargs)
    except httpx.HTTPError:  # its text can quote request headers: never shown
        return Check("unreachable")
    return classify(response)


def check_cosine(key: str) -> Check:
    try:
        base = api_base()
    except CosineRequestError:
        return Check("unreachable")
    return _send("GET", f"{base}/search", timeout=TIMEOUT_S, params={"q": "test", "limit": 1},
                 headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
                 trust_env=proxies_allowed(base))


def check_discogs(token: str) -> Check:
    return _send("GET", f"{discogs.BASE_URL}/oauth/identity", timeout=TIMEOUT_S,
                 headers={"Authorization": f"Discogs token={token}",
                          "User-Agent": discogs.DEFAULT_USER_AGENT, "Accept": "application/json"})


def check_exa(key: str) -> Check:
    # Exa has no free "who am I" call: the smallest search, sent only when someone connects.
    return _send("POST", "https://api.exa.ai/search", timeout=2 * TIMEOUT_S, headers={"x-api-key": key},
                 json={"query": "new music", "numResults": 1})


CHECKS = {"cosine_token": check_cosine, "discogs_token": check_discogs, "exa_api_key": check_exa}


def check(name: str, value: str | None, *, offline: bool = False) -> Check:
    """Check one key as typed or as configured; malformed and missing send nothing.
    offline (the app's fixture test mode) accepts any well-formed key unsent."""
    try:
        key = usable_key(value, ENV_VARS[name])
    except ValueError:
        return Check("malformed")
    if not key:
        return Check("missing")
    return Check("connected") if offline else CHECKS[name](key)
