"""Chrome's cookie format: decryption, host precedence, expiry and the signed-in check,
on a synthetic cookie file encrypted the way Chrome 130+ does (meta version 24)."""
import hashlib
import sqlite3
import sys
import time

import pytest

from cosine_digger import chrome_login

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Chrome's macOS cookie format")
PASSWORD = b"synthetic-safe-storage"
YEAR_US = 365 * 86400 * 1_000_000
NOW_CHROME = int((time.time() + 11644473600) * 1_000_000)
FUTURE, PAST = NOW_CHROME + YEAR_US, NOW_CHROME - YEAR_US


def encrypted(host, value):
    key = chrome_login.derive_key(PASSWORD)
    return b"v10" + chrome_login._cccrypt(0, key, hashlib.sha256(host.encode()).digest() + value.encode())


def chrome(tmp_path, rows, profile="Default"):
    folder = tmp_path / "Chrome" / profile
    folder.mkdir(parents=True)
    with sqlite3.connect(folder / "Cookies") as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('version', '24')")
        conn.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, encrypted_value BLOB, "
                     "has_expires INTEGER, expires_utc INTEGER)")
        conn.executemany("INSERT INTO cookies VALUES (?, ?, '', ?, 1, ?)",
                         [(host, name, encrypted(host, value), expires) for host, name, value, expires in rows])
    return tmp_path / "Chrome"


def headers(base, password=PASSWORD, **kwargs):
    return chrome_login.youtube_headers(base=base, password=lambda: password, **kwargs)


def test_signed_in_profile_gives_ytmusicapi_headers(tmp_path):
    base = chrome(tmp_path, [
        (".youtube.com", "SAPISID", "sapisid-value", FUTURE),
        (".youtube.com", "__Secure-3PAPISID", "papisid-value", FUTURE),
        (".youtube.com", "PREF", "from-youtube", FUTURE),
        ("music.youtube.com", "PREF", "from-music", FUTURE),
        (".youtube.com", "OLD", "expired", PAST),
        (".google.com", "SID", "google-only", FUTURE),
    ])
    with sqlite3.connect(base / "Default" / "Cookies") as conn:  # an unencrypted, empty cookie
        conn.execute("INSERT INTO cookies VALUES ('.youtube.com', 'EMPTY', '', x'', 1, ?)", (FUTURE,))
    lines = headers(base).splitlines()
    cookie = dict(part.split("=", 1) for part in lines[0].removeprefix("cookie: ").split("; "))
    assert cookie == {"SAPISID": "sapisid-value", "__Secure-3PAPISID": "papisid-value", "PREF": "from-music", "EMPTY": ""}
    assert lines[1:3] == ["x-goog-authuser: 0", "origin: https://music.youtube.com"]


def test_the_headers_make_a_browser_login_ytmusicapi_accepts(tmp_path):
    # ytmusicapi treats headers as a browser login only with an "authorization: SAPISIDHASH"
    # header; without one every call failed before reaching YouTube (seen on the real login).
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.types import AuthType
    from cosine_digger import youtube

    base = chrome(tmp_path, [(".youtube.com", "__Secure-3PAPISID", "papisid-value", FUTURE)])
    path = youtube.login(headers(base), tmp_path / "auth.json")
    assert YTMusic(str(path)).auth_type == AuthType.BROWSER


def test_signed_out_profile_and_a_wrong_key_say_so_without_values(tmp_path):
    base = chrome(tmp_path, [(".youtube.com", "PREF", "not-a-login", FUTURE)])
    with pytest.raises(chrome_login.ChromeLoginError, match="isn't signed in to YouTube Music"):
        headers(base)
    with pytest.raises(chrome_login.ChromeLoginError) as wrong:
        headers(base, password=b"some other password")
    assert "not-a-login" not in str(wrong.value)


def test_a_named_profile_is_used_and_a_missing_one_is_named(tmp_path):
    base = chrome(tmp_path, [(".youtube.com", "__Secure-3PAPISID", "second", FUTURE)], profile="Profile 2")
    assert "__Secure-3PAPISID=second" in headers(base, profile="Profile 2")
    with pytest.raises(chrome_login.ChromeLoginError, match="'Profile 9' wasn't found"):
        headers(base, profile="Profile 9")


def test_a_cookie_file_locked_too_long_gives_up_in_plain_words(tmp_path, monkeypatch):
    base = chrome(tmp_path, [(".youtube.com", "__Secure-3PAPISID", "held", FUTURE)])
    monkeypatch.setattr(chrome_login, "SNAPSHOT_TIMEOUT_S", 0.5)
    holder = sqlite3.connect(base / "Default" / "Cookies", isolation_level=None)
    holder.execute("BEGIN EXCLUSIVE")  # a writer that never lets go
    started = time.monotonic()
    try:
        with pytest.raises(chrome_login.ChromeLoginError, match="busy saving cookies") as busy:
            headers(base)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert time.monotonic() - started < 5 and "held" not in str(busy.value)
