"""Read the YouTube Music login from Chrome on this Mac, instead of developer tools.

Chrome keeps each profile's cookies in an SQLite file, their values encrypted
with a key derived from the "Chrome Safe Storage" password in the login
Keychain. macOS asks before handing that password out: Allow (once) is enough;
Always Allow would let any program that runs `security` read it silently.

Only youtube.com cookies are read. Their values go straight into the private
login file through ``youtube.login``; nothing here prints, logs or returns them
in an error. The cookie file is read through SQLite's backup into memory: a
consistent snapshot under SQLite's own lock, with nothing copied to disk, so
Chrome can keep running.
"""
from __future__ import annotations

from contextlib import closing
import ctypes
import ctypes.util
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

CHROME_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
# Least specific first: a cookie set for music.youtube.com wins over the same name on .youtube.com.
HOSTS = (".youtube.com", "youtube.com", ".music.youtube.com", "music.youtube.com")
# ytmusicapi signs every request with this cookie; without it the profile isn't signed in.
SIGNED_IN = "__Secure-3PAPISID"
ORIGIN = "https://music.youtube.com"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
KEYCHAIN_TIMEOUT_S = 120  # long enough to read and answer the macOS prompt
SNAPSHOT_TIMEOUT_S = 10  # Chrome holds its cookie lock for milliseconds; past this it's stuck
_CHROME_EPOCH_OFFSET_S = 11644473600  # 1601-01-01 to 1970-01-01


class ChromeLoginError(Exception):
    """A plain message that is safe to show: never cookie values."""


def available(base: Path | None = None) -> bool:
    return sys.platform == "darwin" and bool(profiles(base))


def profiles(base: Path | None = None) -> list[Path]:
    """Chrome profile folders holding a cookie file, Default first."""
    base = base or CHROME_DIR
    if not base.is_dir():
        return []
    found = [d for d in base.iterdir() if d.is_dir() and (d.name == "Default" or d.name.startswith("Profile "))
             and _cookie_file(d) is not None]
    return sorted(found, key=lambda d: (d.name != "Default", d.name))


def _cookie_file(profile: Path) -> Path | None:
    for candidate in (profile / "Network" / "Cookies", profile / "Cookies"):
        if candidate.is_file():
            return candidate
    return None


def keychain_password() -> bytes:
    """The "Chrome Safe Storage" password; macOS asks the user first."""
    try:
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-w",
                                 "-s", "Chrome Safe Storage", "-a", "Chrome"],
                                capture_output=True, timeout=KEYCHAIN_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        raise ChromeLoginError("macOS didn't answer in time. Try again and click Allow.") from None
    if result.returncode != 0 or not result.stdout.strip():
        raise ChromeLoginError("macOS didn't share Chrome's cookie key. Try again and click Allow "
                               "when it asks to use “Chrome Safe Storage”.")
    return result.stdout.strip()


def derive_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, 16)


def _cccrypt(operation: int, key: bytes, data: bytes) -> bytes:
    """AES-128-CBC with PKCS7 padding and Chrome's fixed IV, via macOS CommonCrypto."""
    library = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib")
    crypt = library.CCCrypt
    crypt.restype = ctypes.c_int32
    crypt.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t,
                      ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t,
                      ctypes.POINTER(ctypes.c_size_t)]
    out = ctypes.create_string_buffer(len(data) + 16)
    moved = ctypes.c_size_t(0)
    # kCCAlgorithmAES = 0, kCCOptionPKCS7Padding = 1
    status = crypt(operation, 0, 1, key, len(key), b" " * 16, data, len(data), out, len(out), ctypes.byref(moved))
    if status != 0:
        raise ChromeLoginError("Couldn't read Chrome's cookies with the key macOS gave.")
    return out.raw[:moved.value]


def decrypt(key: bytes, encrypted: bytes, host: str, version: int) -> str:
    """One cookie value. Version 24+ files prefix the plaintext with SHA-256 of the host."""
    if encrypted[:3] != b"v10":
        raise ChromeLoginError("This Chrome stores cookies in a format Digger can't read yet.")
    plain = _cccrypt(1, key, encrypted[3:])  # kCCDecrypt
    if version >= 24:
        if plain[:32] != hashlib.sha256(host.encode()).digest():
            raise ChromeLoginError("Couldn't read Chrome's cookies with the key macOS gave.")
        plain = plain[32:]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        raise ChromeLoginError("Couldn't read Chrome's cookies with the key macOS gave.") from None


def read_cookies(cookie_file: Path, key: bytes, *, now: datetime | None = None) -> dict[str, str]:
    """youtube.com cookies that haven't expired, from an in-memory snapshot of ``cookie_file``."""
    now = now or datetime.now(timezone.utc)
    now_chrome = int((now.timestamp() + _CHROME_EPOCH_OFFSET_S) * 1_000_000)
    deadline = time.monotonic() + SNAPSHOT_TIMEOUT_S

    def progress(status, remaining, total):
        # backup() retries a busy lock forever; this bounds it.
        if time.monotonic() > deadline:
            raise ChromeLoginError("Chrome was busy saving cookies. Try again in a moment.")

    try:
        with closing(sqlite3.connect(f"{cookie_file.as_uri()}?mode=ro", uri=True, timeout=1)) as source, \
                closing(sqlite3.connect(":memory:")) as conn:
            # Consistent under SQLite's lock, even while Chrome writes; never on disk.
            source.backup(conn, progress=progress, sleep=0.1)
            row = conn.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
            version = int(row[0]) if row else 0
            rows = conn.execute(
                f"SELECT host_key, name, value, encrypted_value, has_expires, expires_utc FROM cookies "
                f"WHERE host_key IN ({','.join('?' * len(HOSTS))})", HOSTS).fetchall()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc) or "busy" in str(exc):
            raise ChromeLoginError("Chrome was busy saving cookies. Try again in a moment.") from None
        raise ChromeLoginError("Couldn't read Chrome's cookie file.") from None
    except (OSError, sqlite3.Error):
        raise ChromeLoginError("Couldn't read Chrome's cookie file.") from None
    cookies: dict[str, str] = {}
    for host, name, value, encrypted, has_expires, expires in sorted(rows, key=lambda r: HOSTS.index(r[0])):
        if has_expires and expires and expires < now_chrome:
            continue
        # Chrome encrypts on macOS; an unencrypted row keeps its plain value, even an empty one.
        cookies[name] = decrypt(key, encrypted, host, version) if encrypted else (value or "")
    return cookies


def youtube_headers(profile: str | None = None, *, base: Path | None = None, authuser: int = 0,
                    password=keychain_password) -> str:
    """Request headers for ``youtube.login``, from the first signed-in Chrome profile
    (or ``profile``, a folder name such as "Default" or "Profile 1")."""
    if sys.platform != "darwin":
        raise ChromeLoginError("Log in from Chrome works on a Mac only. Paste request headers instead.")
    folders = [d for d in profiles(base) if profile in (None, d.name)]
    if not folders:
        raise ChromeLoginError(f"Chrome profile {profile!r} wasn't found." if profile else
                               "Chrome isn't set up on this Mac. Paste request headers instead.")
    key = derive_key(password())
    for folder in folders:
        cookies = read_cookies(_cookie_file(folder), key)
        if cookies.get(SIGNED_IN):
            from ytmusicapi.helpers import get_authorization

            cookie = "; ".join(f"{name}={value}" for name, value in cookies.items())
            # ytmusicapi reads headers as a browser login only with a SAPISIDHASH authorization
            # (it signs each request afresh); the one a browser would send now is built here.
            return "\n".join([f"cookie: {cookie}", f"x-goog-authuser: {authuser}", f"origin: {ORIGIN}",
                              f"authorization: {get_authorization(cookies[SIGNED_IN] + ' ' + ORIGIN)}",
                              f"user-agent: {USER_AGENT}"])
    raise ChromeLoginError("Chrome isn't signed in to YouTube Music. Sign in at music.youtube.com "
                           "in Chrome, then try again.")
