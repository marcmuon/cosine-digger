"""Browser-authenticated YouTube Music exports with explicit read-back verification."""
from __future__ import annotations

import functools
import json
import math
import os
import re
import tempfile
import time
from collections.abc import MutableMapping
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass, field
from io import StringIO
from pathlib import Path

import requests
import ytmusicapi
from ytmusicapi import YTMusic


class YouTubeAuthMissing(FileNotFoundError):
    """Browser credentials have not been configured."""


class YouTubeExportError(RuntimeError):
    """An export was refused or could not be confirmed. Messages are written
    here, never copied from the provider (its errors can carry cookies).
    ``changed`` is False when the playlist was certainly left untouched."""

    def __init__(self, message: str, *, changed: bool = True):
        super().__init__(message)
        self.changed = changed


def _not_found(exc: Exception) -> bool:
    return isinstance(exc, ytmusicapi.exceptions.YTMusicServerError) and "HTTP 404" in str(exc)


SIGNED_OUT = ("YouTube Music says you're signed out: the saved login has expired. Run "
              "`cosine-digger youtube login` with fresh browser headers, then retry.")


def signed_out(exc: Exception) -> bool:
    """YouTube refused the saved login (HTTP 401/403): it expired or was revoked."""
    return (isinstance(exc, ytmusicapi.exceptions.YTMusicServerError)
            and re.match(r"Server returned HTTP 40[13]\b", str(exc)) is not None)


def signed_in(library) -> bool:
    """Signed out, YouTube still answers a library listing, just empty; a
    signed-in library always holds at least Liked Music."""
    return isinstance(library, list) and any(isinstance(p, dict) and p.get("playlistId") for p in library)


def has_login(path: str | Path) -> bool:
    """A saved browser login keeps the request headers, cookie included; an
    empty {} file (or anything else) isn't a login."""
    try:
        auth = json.loads(Path(path).expanduser().read_text())
    except (OSError, ValueError):
        return False
    return isinstance(auth, dict) and any(str(key).lower() == "cookie" and value for key, value in auth.items())


def login_account(path: str | Path) -> int:
    """Which of the browser's signed-in Google accounts the saved login acts as
    (its x-goog-authuser); 0, the first, when it doesn't say."""
    try:
        auth = json.loads(Path(path).expanduser().read_text())
        return max(0, int(next((v for k, v in auth.items() if str(k).lower() == "x-goog-authuser"), 0)))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def account_link(url: str | None, account: int) -> str | None:
    """A private playlist opens only in the account that owns it, so its link names that account."""
    return f"{url}&authuser={account}" if url and account else url


def check_login(path: str | Path, *, timeout: float = 10.0) -> str:
    """connected, rejected, unreachable or missing, from the read-only library
    listing `cosine-digger youtube status` uses. Nothing is written."""
    path = Path(path).expanduser()
    if not has_login(path):
        return "missing"
    session = requests.Session()
    session.request = functools.partial(session.request, timeout=timeout)
    answers = []  # the HTTP status of each answer YouTube gave
    session.hooks["response"].append(lambda response, *args, **kwargs: answers.append(response.status_code))
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            library = YTMusic(str(path), requests_session=session).get_library_playlists(limit=1)
    except requests.RequestException:
        return "unreachable"
    except ytmusicapi.exceptions.YTMusicUserError:
        return "rejected"  # the saved headers can't make a login (a cookie value missing and the like)
    except Exception as exc:  # its message can carry cookies: only the cause is kept
        # A verdict on the login only from YouTube's refusal (HTTP 401/403). An error page, or an
        # answer ytmusicapi couldn't read (it parses the body before the status), is YouTube not
        # answering properly.
        refused = signed_out(exc) or (answers and answers[-1] in (401, 403))
        return "rejected" if refused else "unreachable"
    finally:
        session.close()
    # Signed out, YouTube still answers the listing, just empty.
    return "connected" if signed_in(library) else "rejected"


def auth_path() -> Path:
    """Where the ytmusicapi browser headers live (follows COSINE_DIGGER_HOME)."""
    from .config import load_config

    return load_config().ytmusic_auth_path


_HEADER_NAME = re.compile(r"^:?[A-Za-z][A-Za-z0-9-]*:?$")


def normalize_headers(text: str) -> str:
    """Browser request headers as ``name: value`` lines, from any of the
    shapes a browser copies: raw headers, a "Copy as cURL" command, or the
    formatted DevTools view (name and value on separate lines or tab-split)."""
    text = text.strip()
    if text.startswith("curl "):
        import shlex

        tokens = shlex.split(text.replace("\\\r\n", " ").replace("\\\n", " "))
        lines = []
        for flag, value in zip(tokens, tokens[1:] + [""]):
            if flag in ("-H", "--header"):
                lines.append(value)
            elif flag in ("-b", "--cookie"):
                lines.append(f"cookie: {value}")
        return "\n".join(lines)
    rows = [line.strip() for line in text.splitlines() if line.strip()]
    if sum(": " in row for row in rows) * 2 >= len(rows):
        return "\n".join(rows)
    lines, i = [], 0
    while i < len(rows):
        row = rows[i]
        if "\t" in row:
            name, value = row.split("\t", 1)
            lines.append(f"{name.strip().rstrip(':')}: {value.strip()}")
            i += 1
        elif _HEADER_NAME.match(row) and i + 1 < len(rows):
            lines.append(f"{row.rstrip(':')}: {rows[i + 1]}")
            i += 2
        else:
            i += 1
    return "\n".join(lines)


def login(headers_raw: str, path: str | Path) -> Path:
    """Save browser headers privately, without emitting credentials or library errors."""
    headers_raw = normalize_headers(headers_raw)
    if not headers_raw.strip():
        raise ValueError("No browser headers supplied.")
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, filename = tempfile.mkstemp(prefix=".ytmusic-", dir=path.parent)
    os.close(fd)
    temporary = Path(filename)
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            ytmusicapi.setup(filepath=str(temporary), headers_raw=headers_raw)
        temporary.chmod(0o600)
        temporary.replace(path)
    except Exception:
        raise ValueError("Could not save YouTube Music login; check the browser headers.") from None
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _authenticated_ytm():
    path = auth_path()
    if not path.is_file():
        raise YouTubeAuthMissing(
            f"YouTube Music auth file missing: {path}. Run cosine-digger youtube login."
        )
    return YTMusic(str(path))


def _dedupe(video_ids):
    requested, skipped = [], []
    seen = set()
    for video_id in video_ids:
        if video_id in seen:
            skipped.append(video_id)
        else:
            seen.add(video_id)
            requested.append(video_id)
    return requested, skipped


def _succeeded(response) -> bool:
    status = response.get("status") if isinstance(response, dict) else response
    return status == "STATUS_SUCCEEDED"


def _create(ytm, name, description) -> str:
    # Every playlist this app makes is private.
    playlist_id = ytm.create_playlist(name, description=description, privacy_status="PRIVATE")
    if not isinstance(playlist_id, str) or not playlist_id:
        raise RuntimeError("YouTube Music did not return a playlist ID; creation was not retried.")
    return playlist_id


@dataclass
class ExportReport:
    """Ordered IDs, with duplicates counted in skipped.

    Added means observed in the final read, not necessarily newly inserted.
    Failed includes the uncertain batch and unattempted remainder; an uncertain
    ID may also be observed in added. When the final read fails, all requested
    IDs are missing (unconfirmed), and verified is always False.
    """

    playlist_id: str
    playlist_url: str
    created: bool
    requested: list[str]
    added: list[str]
    missing: list[str]
    skipped: list[str]
    failed: list[str]
    verified: bool
    # Picks the caller left out before the write (no video, already served);
    # ``verified`` speaks only for what was requested.
    omitted: list[str] = field(default_factory=list)
    # Requested id -> the id YouTube Music stored instead, same song title.
    substituted: dict[str, str] = field(default_factory=dict)
    # New items YouTube put in that match no request by title (another
    # recording in a declined song's place): {video_id, artist, title}.
    # They are not counted as added; the served ledger still learns them.
    inserted: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# Below this share of the request present, an export is not verified.
MIN_PRESENT_SHARE = 0.8


# Bracket words that don't change which recording it is ("Official Audio",
# "Original Mix", "Remastered 2011"); a remix or edit name is never in here.
_TITLE_NOISE = frozenset(
    "official audio video music lyric lyrics visualizer visualiser hd hq remaster remastered "
    "original mix version album explicit clean".split())


def _noise(words: str) -> bool:
    found = re.findall(r"[^\W_]+", words)
    return bool(found) and all(w in _TITLE_NOISE or w.isdigit() for w in found)


def _title_key(title: str | None) -> str:
    """A song title for same-song checks: case, punctuation, featured artists
    and noise brackets dropped; a remix or edit name ("(Someone Remix)") and a
    title that is all brackets ("(slow)") keep their words."""
    text = (title or "").casefold()
    text = re.sub(r"\s[-–]\s([^-–]+)$", lambda m: " " if _noise(m.group(1)) else m.group(0), text)
    text = re.sub(r"[\(\[]\s*(feat\.|ft\.|featuring\s)[^\)\]]*[\)\]]", " ", text)
    # An unbracketed "feat. X" ends where a bracket or " - " starts the version name.
    text = re.sub(r"\b(feat\.|ft\.|featuring\s)(?:(?!\s[-–]\s)[^\(\[])*", " ", text)
    text = re.sub(r"[\(\[]([^\)\]]*)[\)\]]", lambda m: " " if _noise(m.group(1)) else f" {m.group(1)} ", text)
    return re.sub(r"[\W_]+", "", text)


def _match(requested: list[str], present: list[dict], writes_ok: bool, titles: dict[str, str] | None = None,
           before: frozenset[str] = frozenset()):
    """Compare what we asked for with what the playlist holds.

    YouTube Music sometimes stores a song under another video id (usually its
    official audio, sometimes under another artist spelling), declines some
    videos, and occasionally puts a different recording in a declined song's
    place. ``before`` holds the playlist's entry ids (setVideoId) from before
    this replace; every add makes a fresh one, even for the same video, so an
    entry still carrying an old one is stray (a stale read or a failed
    removal) and blocks verification. After clean writes, a new unrequested
    item is a substitution when its title is the same song title as a missing
    request (``_title_key``); otherwise it is ``inserted``, reported but never
    counted as added. Returns (added, missing, substituted, stray, inserted),
    added in request order."""
    asked = set(requested)
    # An old entry blocks verification even when its video has gone unavailable (no videoId).
    stray = [item.get("videoId") or item.get("setVideoId") for item in present if item.get("setVideoId") in before]
    present = [item for item in present if item.get("videoId") and item.get("setVideoId") not in before]
    have = {item["videoId"] for item in present}
    lost = [v for v in requested if v not in have]
    extra = [item for item in present if item["videoId"] not in asked]
    substituted: dict[str, str] = {}
    if writes_ok and titles:
        for video_id in lost:
            want = _title_key(titles.get(video_id))
            for item in extra:
                if (want and item["videoId"] not in substituted.values()
                        and _title_key(item.get("title")) == want):
                    substituted[video_id] = item["videoId"]
                    break
    lost = [v for v in lost if v not in substituted]
    inserted = [{"video_id": item["videoId"], "title": item.get("title") or "",
                 "artist": ((item.get("artists") or [{}])[0] or {}).get("name") or ""}
                for item in extra if item["videoId"] not in substituted.values()]
    added = [v for v in requested if v in have or v in substituted]
    return added, lost, substituted, stray, inserted


class YTMusicSink:
    # YouTube Music can take a few seconds to show writes: re-read with these
    # waits (about 20 s in all) before calling a playlist unverified.
    READ_WAITS = (0, 2, 3, 4, 5, 6)

    def __init__(self, ytm_factory=None, id_store: MutableMapping[str, str] | None = None, sleep=time.sleep):
        self.ytm = (ytm_factory or _authenticated_ytm)()
        # Callers may supply a persistent mapping; the default lives with this sink.
        self.id_store = id_store if id_store is not None else {}
        self._sleep = sleep

    def _in_library(self, playlist_id: str) -> bool:
        """False only when a well-formed library listing lacks the playlist.
        An empty or malformed listing proves nothing (the library always holds
        at least Liked Music), so it counts as present."""
        try:
            playlists = self.ytm.get_library_playlists(limit=None)
        except Exception:
            return True  # can't confirm it's gone: assume it still exists
        if (not isinstance(playlists, list) or not playlists
                or not all(isinstance(p, dict) and p.get("playlistId") for p in playlists)):
            return True
        return any(p["playlistId"] == playlist_id for p in playlists)

    def _signed_out(self, exc: Exception) -> bool:
        """YouTube refuses an expired login with HTTP 401/403, or often answers
        it with an ordinary page ytmusicapi can't read. The library listing
        tells that case apart: signed in, it always holds Liked Music."""
        if signed_out(exc):
            return True
        try:
            return not signed_in(self.ytm.get_library_playlists(limit=1))
        except Exception as listing_error:
            return signed_out(listing_error)

    def replace_playlist(self, name, video_ids, description="", titles: dict[str, str] | None = None,
                         key: str | None = None) -> ExportReport:
        """Make the named private playlist hold exactly these videos.

        ``verified`` means every write succeeded, nothing from the old
        playlist is left, it is private, and at least MIN_PRESENT_SHARE of the
        request is present (YouTube declines some videos; those stay in
        ``missing``, and a different recording it puts in their place is
        listed in ``inserted``). ``titles`` (video id -> song title) lets
        same-song substitutions be recognised. ``key`` finds the playlist in
        the id store when its name is only its title (a dig's playlist is
        found by the dig, so two digs with one name never share it)."""
        requested, skipped = _dedupe(video_ids)
        if not requested:
            raise YouTubeExportError(
                "None of the picks has a YouTube Music video; the playlist was left unchanged.",
                changed=False)
        key = key or name
        playlist_id = self.id_store.get(key)
        existing = None
        if playlist_id:
            try:
                existing = self.ytm.get_playlist(playlist_id, limit=None)
                # Validate before any writes; error payloads are not playlists.
                if not isinstance(existing["tracks"], list):
                    raise ValueError("Invalid playlist response")
            except Exception as exc:
                if self._signed_out(exc):
                    raise YouTubeExportError(SIGNED_OUT, changed=False) from None
                # Recreate only when YouTube says the playlist is gone and the
                # library agrees; any other failure keeps the mapping and stops,
                # so a timeout can never leave a duplicate playlist behind.
                if not (_not_found(exc) and not self._in_library(playlist_id)):
                    raise YouTubeExportError(
                        f"Could not read the existing playlist {name!r} ({type(exc).__name__}); "
                        "nothing was changed. Try again, or run `cosine-digger youtube status`.",
                        changed=False,
                    ) from None
                del self.id_store[key]
                playlist_id = None
                existing = None
        created = not playlist_id
        if created:
            try:
                playlist_id = _create(self.ytm, name, description)
            except Exception as exc:
                if self._signed_out(exc):
                    raise YouTubeExportError(SIGNED_OUT, changed=False) from None
                raise
            self.id_store[key] = playlist_id

        failed = []
        write_ok = True
        removing = False
        try:
            if existing is not None:
                if existing.get("privacy", "PRIVATE") != "PRIVATE":
                    if not _succeeded(self.ytm.edit_playlist(playlist_id, privacyStatus="PRIVATE")):
                        raise RuntimeError("Playlist privacy update failed")
                items = [
                    {"videoId": item["videoId"], "setVideoId": item["setVideoId"]}
                    for item in existing["tracks"]
                ]
                if any(not item["setVideoId"] for item in items):
                    raise ValueError("Playlist item lacks its removal ID")
                removing = True
                for start in range(0, len(items), 50):
                    if not _succeeded(self.ytm.remove_playlist_items(playlist_id, items[start:start + 50])):
                        raise RuntimeError("Playlist removal failed")
        except Exception:
            if not removing:  # failed before any track was touched
                raise YouTubeExportError(
                    f"Could not prepare the playlist {name!r} for replacement; its tracks were left unchanged. "
                    "Try again, or run `cosine-digger youtube status`.",
                    changed=False,
                ) from None
            failed = requested.copy()
            write_ok = False

        if write_ok:
            for start in range(0, len(requested), 50):
                try:
                    response = self.ytm.add_playlist_items(
                        playlist_id, requested[start:start + 50], duplicates=False
                    )
                    if not _succeeded(response):
                        raise RuntimeError("Playlist addition failed")
                except Exception:
                    failed = requested[start:]
                    write_ok = False
                    break

        added, missing, verified, substituted, inserted = [], requested.copy(), False, {}, []
        before = frozenset(item.get("setVideoId") for item in (existing or {}).get("tracks", []) if item.get("setVideoId"))
        for attempt, wait in enumerate(self.READ_WAITS):
            if verified or (attempt and not write_ok):  # a failed write gets one read
                break
            self._sleep(wait)
            try:
                final = self.ytm.get_playlist(playlist_id, limit=None)
                present = final["tracks"]
                private = final.get("privacy", "PRIVATE") == "PRIVATE"
            except Exception:
                continue
            added, missing, substituted, stray, inserted = _match(
                requested, present, write_ok and not failed, titles, before)
            verified = (write_ok and not failed and not stray and private
                        and len(added) >= math.ceil(MIN_PRESENT_SHARE * len(requested)))
        return ExportReport(
            playlist_id, f"https://music.youtube.com/playlist?list={playlist_id}",
            created, requested, added, missing, skipped, failed, verified, substituted=substituted,
            inserted=inserted,  # new entries are on the playlist even when a later write failed
        )
