import json
from copy import deepcopy

import pytest
from typer.testing import CliRunner

from cosine_digger import youtube
from cosine_digger.cli import app


class FakeYTMusic:
    """Stateful provider boundary: writes affect subsequent full playlist reads."""

    def __init__(self):
        self.playlists = {}
        self.calls = []
        self.unavailable = set()
        self.fail_add = None
        self.reject_add = set()
        self.apply_before_error = False
        self.fail_read = False
        self.fail_remove = False
        self.keep_old = False

    def create_playlist(self, title, description, privacy_status):
        self.calls.append(("create", title, description, privacy_status))
        pid = f"playlist-{len(self.playlists)}"
        self.playlists[pid] = {"tracks": [], "privacy": privacy_status}
        return pid

    def get_playlist(self, pid, limit):
        assert limit is None
        self.calls.append(("read", pid))
        if self.fail_read:
            raise TimeoutError("offline")
        if pid not in self.playlists:
            from ytmusicapi.exceptions import YTMusicServerError
            raise YTMusicServerError("Server returned HTTP 404: Not Found.\n")
        return deepcopy(self.playlists[pid])

    def edit_playlist(self, pid, privacyStatus):
        self.calls.append(("privacy", pid, privacyStatus))
        self.playlists[pid]["privacy"] = privacyStatus
        return "STATUS_SUCCEEDED"

    def remove_playlist_items(self, pid, videos):
        self.calls.append(("remove", pid, deepcopy(videos)))
        if self.fail_remove:
            raise TimeoutError("unknown removal result")
        if not self.keep_old:
            removed = {item["setVideoId"] for item in videos}
            self.playlists[pid]["tracks"] = [
                item for item in self.playlists[pid]["tracks"] if item["setVideoId"] not in removed
            ]
        return "STATUS_SUCCEEDED"

    def add_playlist_items(self, pid, ids, duplicates):
        self.calls.append(("add", pid, list(ids), duplicates))
        number = sum(call[0] == "add" for call in self.calls)
        self.adds = getattr(self, "adds", 0) + 1  # entry ids stay unique, like YouTube's
        if number in self.reject_add:
            return {"status": "STATUS_FAILED"}
        if number != self.fail_add or self.apply_before_error:
            self.playlists[pid]["tracks"].extend(
                {"videoId": vid, "setVideoId": f"set-{self.adds}-{index}"}
                for index, vid in enumerate(ids) if vid not in self.unavailable
            )
        if number == self.fail_add:
            raise TimeoutError("unknown insertion result")
        return {"status": "STATUS_SUCCEEDED"}

    def get_library_playlists(self, limit):
        self.calls.append(("library",))
        return [{"playlistId": "LM", "title": "Liked Music"}] + [{"playlistId": pid} for pid in self.playlists]


@pytest.fixture
def provider():
    return FakeYTMusic()


def test_create_stores_id_and_verifies_all_chunks(provider):
    store = {}
    ids = [f"v{i}" for i in range(123)]
    report = youtube.YTMusicSink(lambda: provider, store).replace_playlist("Discovery", ids, "A dig")
    assert store == {"Discovery": report.playlist_id}
    assert report.created and report.verified
    assert report.requested == report.added == ids
    assert report.missing == report.failed == report.skipped == []
    assert provider.calls[0] == ("create", "Discovery", "A dig", "PRIVATE")
    assert [(len(c[2]), c[3]) for c in provider.calls if c[0] == "add"] == [(50, False), (50, False), (23, False)]
    assert report.to_dict()["playlist_url"] == f"https://www.youtube.com/playlist?list={report.playlist_id}"


def test_youtube_target_urls_and_explicit_target_isolation(provider, monkeypatch, tmp_path):
    from cosine_digger import api
    from ytmusicapi.exceptions import YTMusicServerError

    monkeypatch.setenv("COSINE_DIGGER_HOME", str(tmp_path))
    monkeypatch.setenv("COSINE_DIGGER_NO_DOTENV", "1")
    assert youtube.playlist_url("PL1") == "https://www.youtube.com/playlist?list=PL1"
    assert youtube.playlist_url("PL1", account=1) == "https://www.youtube.com/playlist?list=PL1&authuser=1"
    assert youtube.account_link("https://www.youtube.com/playlist", 1) == (
        "https://www.youtube.com/playlist?authuser=1")
    assert youtube.account_link("https://www.youtube.com/playlist?list=PL1&authuser=1", 1) == (
        "https://www.youtube.com/playlist?list=PL1&authuser=1")
    assert youtube.watch_url("abc123") == "https://www.youtube.com/watch?v=abc123"
    assert youtube.search_url("Basic Channel BCD") == "https://www.youtube.com/results?search_query=Basic%20Channel%20BCD"

    assert youtube.playlist_url("PL1", target="youtube_music") == "https://music.youtube.com/playlist?list=PL1"
    assert youtube.watch_url("abc123", target="youtube_music") == "https://music.youtube.com/watch?v=abc123"
    assert youtube.search_url("Basic Channel BCD", target="youtube_music") == (
        "https://music.youtube.com/search?q=Basic%20Channel%20BCD")

    # Helpers, YTMusicSink, and _export_error never read ambient env/disk config on their own:
    # even a broken COSINE_DIGGER_YOUTUBE_TARGET never breaks URL building or error redaction.
    monkeypatch.setenv("COSINE_DIGGER_YOUTUBE_TARGET", "invalid-target")
    default_report = youtube.YTMusicSink(lambda: provider).replace_playlist("DefaultMode", ["v1"])
    assert default_report.playlist_url == f"https://www.youtube.com/playlist?list={default_report.playlist_id}"
    music_report = youtube.YTMusicSink(lambda: provider, target="youtube_music").replace_playlist("MusicMode", ["v1"])
    assert music_report.playlist_url == f"https://music.youtube.com/playlist?list={music_report.playlist_id}"
    assert youtube.watch_url("abc123") == "https://www.youtube.com/watch?v=abc123"

    auth_err = youtube.YouTubeAuthMissing("missing")
    signed_out_err = YTMusicServerError("Server returned HTTP 401: Cookie: SECRET")
    generic_err = RuntimeError("Cookie: SECRET")
    assert api._export_error(auth_err, target="youtube").startswith("YouTube login missing:")
    assert api._export_error(auth_err, target="youtube_music").startswith("YouTube Music login missing:")
    assert api._export_error(signed_out_err, target="youtube") == youtube.signed_out_message("youtube")
    assert api._export_error(signed_out_err, target="youtube_music") == youtube.signed_out_message("youtube_music")
    assert youtube.is_signed_out_message(youtube.signed_out_message("youtube"))
    assert youtube.is_signed_out_message(youtube.signed_out_message("youtube_music"))
    assert api._export_error(generic_err, target="youtube").startswith("YouTube export failed (RuntimeError).")
    assert api._export_error(generic_err, target="youtube_music").startswith("YouTube Music export failed (RuntimeError).")
    assert "SECRET" not in api._export_error(signed_out_err, target="youtube_music")
    assert "SECRET" not in api._export_error(generic_err, target="youtube_music")


def test_replace_removes_every_occurrence_by_set_video_id(provider):
    sink = youtube.YTMusicSink(lambda: provider)
    first = sink.replace_playlist("Discovery", ["old"])
    provider.playlists[first.playlist_id]["tracks"].append({"videoId": "old", "setVideoId": "second-old"})
    provider.calls.clear()
    report = sink.replace_playlist("Discovery", ["new", "old"])
    assert not report.created and report.verified
    assert report.playlist_id == first.playlist_id
    assert [c[0] for c in provider.calls] == ["read", "remove", "add", "read"]
    assert provider.calls[1][2] == [
        {"videoId": "old", "setVideoId": "set-1-0"},
        {"videoId": "old", "setVideoId": "second-old"},
    ]
    assert report.added == ["new", "old"]


def test_a_keyed_playlist_is_found_by_its_key_not_its_title(provider):
    store = {}
    sink = youtube.YTMusicSink(lambda: provider, store)
    first = sink.replace_playlist("Digger: Same Seed", ["a"], key="dig:1")
    other = sink.replace_playlist("Digger: Same Seed", ["b"], key="dig:2")
    again = sink.replace_playlist("Digger: Same Seed", ["c"], key="dig:1")
    assert store == {"dig:1": first.playlist_id, "dig:2": other.playlist_id}
    assert first.playlist_id != other.playlist_id and again.playlist_id == first.playlist_id and not again.created
    assert [item["videoId"] for item in provider.playlists[other.playlist_id]["tracks"]] == ["b"]
    assert [call[1] for call in provider.calls if call[0] == "create"] == ["Digger: Same Seed"] * 2


def test_stale_id_falls_back_to_create(provider):
    store = {"Discovery": "deleted"}
    report = youtube.YTMusicSink(lambda: provider, store).replace_playlist("Discovery", ["a"])
    assert report.created and report.verified
    assert store["Discovery"] == report.playlist_id != "deleted"


@pytest.mark.parametrize("listing", [None, {}, [], [{}]])
def test_a_404_with_an_unreadable_library_keeps_the_mapping(provider, listing):
    provider.get_library_playlists = lambda limit: listing
    store = {"Discovery": "deleted"}
    with pytest.raises(youtube.YouTubeExportError) as err:
        youtube.YTMusicSink(lambda: provider, store).replace_playlist("Discovery", ["a"])
    assert store == {"Discovery": "deleted"} and provider.playlists == {} and not err.value.changed


def test_uncertain_read_keeps_the_playlist_and_changes_nothing(provider):
    first = youtube.YTMusicSink(lambda: provider).replace_playlist("Discovery", ["a"])
    store = {"Discovery": first.playlist_id}
    provider.fail_read = True
    with pytest.raises(youtube.YouTubeExportError):
        youtube.YTMusicSink(lambda: provider, store).replace_playlist("Discovery", ["b"])
    assert store == {"Discovery": first.playlist_id} and len(provider.playlists) == 1


def test_export_with_no_playable_tracks_is_refused(provider):
    with pytest.raises(youtube.YouTubeExportError):
        youtube.YTMusicSink(lambda: provider).replace_playlist("Discovery", [])
    assert provider.playlists == {}


def test_partial_availability_is_not_verified(provider):
    provider.unavailable = {"b"}
    report = youtube.YTMusicSink(lambda: provider).replace_playlist("Discovery", ["a", "b", "c"])
    assert report.added == ["a", "c"]
    assert report.missing == ["b"]
    assert report.failed == []
    assert not report.verified


@pytest.mark.parametrize("applied", [False, True])
def test_exception_mid_write_stops_without_retry_and_still_reads(provider, applied):
    provider.fail_add = 2
    provider.apply_before_error = applied
    ids = [f"v{i}" for i in range(120)]
    report = youtube.YTMusicSink(lambda: provider).replace_playlist("Discovery", ids + ["v50"])
    assert report.failed == ids[50:]
    assert report.added == ids[:100 if applied else 50]
    assert report.missing == ids[100 if applied else 50:]
    assert report.skipped == ["v50"]
    assert not report.verified
    assert sum(c[0] == "add" for c in provider.calls) == 2
    assert provider.calls[-1][0] == "read"


def test_duplicates_preserve_order(provider):
    report = youtube.YTMusicSink(lambda: provider).replace_playlist("Discovery", ["b", "a", "b", "a", "b"])
    assert report.requested == report.added == ["b", "a"]
    assert report.skipped == ["b", "a", "b"]
    assert report.verified


def test_read_failure_never_claims_verified(provider):
    provider.fail_read = True
    report = youtube.YTMusicSink(lambda: provider).replace_playlist("Discovery", ["a"])
    assert not report.verified
    assert report.added == []
    assert report.missing == ["a"]
    assert report.failed == []


def test_remove_failure_stops_additions(provider):
    sink = youtube.YTMusicSink(lambda: provider)
    sink.replace_playlist("Discovery", ["old"])
    provider.calls.clear()
    provider.fail_remove = True
    report = sink.replace_playlist("Discovery", ["new"])
    assert report.failed == report.missing == ["new"]
    assert not report.verified
    assert [c[0] for c in provider.calls] == ["read", "remove", "read"]


def test_empty_request_never_empties_an_existing_playlist(provider):
    sink = youtube.YTMusicSink(lambda: provider)
    first = sink.replace_playlist("Discovery", ["old"])
    with pytest.raises(youtube.YouTubeExportError):
        sink.replace_playlist("Discovery", [])
    assert [t["videoId"] for t in provider.playlists[first.playlist_id]["tracks"]] == ["old"]


def test_existing_public_playlist_becomes_private(provider):
    provider.playlists["public"] = {"tracks": [], "privacy": "PUBLIC"}
    report = youtube.YTMusicSink(lambda: provider, {"Discovery": "public"}).replace_playlist("Discovery", ["a"])
    assert report.verified
    assert provider.playlists["public"]["privacy"] == "PRIVATE"


def test_returned_error_stops_writes(provider):
    provider.reject_add = {1}
    ids = [str(i) for i in range(51)]
    report = youtube.YTMusicSink(lambda: provider).replace_playlist("Discovery", ids)
    assert report.failed == ids
    assert report.missing == ids
    assert not report.verified
    assert sum(c[0] == "add" for c in provider.calls) == 1


HEADERS = "cookie: __Secure-3PAPISID=super-secret-cookie\nx-goog-authuser: 0\n"


@pytest.mark.parametrize("source", ["stdin", "file"])
def test_login_real_parser_saves_private_credentials_without_echo(monkeypatch, tmp_path, provider, source):
    path = tmp_path / "auth" / "browser.json"
    path.parent.mkdir()
    path.write_text("{}")
    path.chmod(0o644)
    monkeypatch.setenv("COSINE_DIGGER_YTMUSIC_AUTH", str(path))
    monkeypatch.setattr(youtube, "YTMusic", lambda auth: provider)
    args = ["youtube", "login"]
    if source == "file":
        headers_file = tmp_path / "headers.txt"
        headers_file.write_text(HEADERS)
        args += ["--file", str(headers_file)]
    result = CliRunner().invoke(app, args, input=HEADERS if source == "stdin" else None)
    assert result.exit_code == 0, result.output
    assert "ok:" in result.output
    assert "super-secret" not in result.output
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(path.read_text())["cookie"] == "__Secure-3PAPISID=super-secret-cookie"
    assert provider.calls == [("library",)]


def test_login_failure_hides_library_secrets_and_preserves_existing_auth(monkeypatch, tmp_path):
    path = tmp_path / "auth" / "browser.json"
    path.parent.mkdir()
    path.write_text("previous auth")
    monkeypatch.setenv("COSINE_DIGGER_YTMUSIC_AUTH", str(path))

    def broken_setup(**kwargs):
        print("super-secret-cookie")
        raise ValueError("super-secret-cookie")

    monkeypatch.setattr(youtube.ytmusicapi, "setup", broken_setup)
    result = CliRunner().invoke(app, ["youtube", "login"], input=HEADERS)
    assert result.exit_code == 1
    assert "super-secret" not in result.output
    assert path.read_text() == "previous auth"
    assert list(path.parent.iterdir()) == [path]


def test_login_no_verify_and_status_offline_never_connect(monkeypatch, tmp_path):
    path = tmp_path / "auth" / "browser.json"
    monkeypatch.setenv("COSINE_DIGGER_YTMUSIC_AUTH", str(path))

    def unexpected(*args):
        pytest.fail("offline commands must not connect")

    monkeypatch.setattr(youtube, "YTMusic", unexpected)
    runner = CliRunner()
    result = runner.invoke(app, ["youtube", "login", "--no-verify"], input=HEADERS)
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["youtube", "status", "--offline", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"auth_present": True, "verified": None, "offline": True}


def test_live_status_and_login_failures_do_not_leak(monkeypatch, tmp_path):
    monkeypatch.setenv("COSINE_DIGGER_YTMUSIC_AUTH", str(tmp_path / "auth" / "browser.json"))

    def rejected(*args):
        print("super-secret-cookie")
        raise ValueError("super-secret-cookie")

    monkeypatch.setattr(youtube, "YTMusic", rejected)
    runner = CliRunner()
    result = runner.invoke(app, ["youtube", "login"], input=HEADERS)
    assert result.exit_code == 1
    assert "verification failed" in result.output
    assert "super-secret" not in result.output
    result = runner.invoke(app, ["youtube", "status", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {"auth_present": True, "verified": False, "offline": False}


def test_missing_auth_has_actionable_error_and_status(monkeypatch, tmp_path):
    monkeypatch.setenv("COSINE_DIGGER_YTMUSIC_AUTH", str(tmp_path / "absent.json"))
    with pytest.raises(youtube.YouTubeAuthMissing, match="cosine-digger youtube login"):
        youtube.YTMusicSink()
    result = CliRunner().invoke(app, ["youtube", "status", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output)["auth_present"] is False


CURL = """curl 'https://music.youtube.com/youtubei/v1/browse?prettyPrint=false' \\
  -H 'accept: */*' \\
  -H 'authorization: SAPISIDHASH 1_fakehash' \\
  -b 'SID=fake-sid; __Secure-3PAPISID=fake-papisid' \\
  -H 'x-goog-authuser: 0' \\
  --data-raw '{"context":{}}'"""
FORMATTED = "Accept\n*/*\nAuthorization\nSAPISIDHASH 1_fakehash\nCookie\nSID=fake-sid; __Secure-3PAPISID=fake-papisid\nX-Goog-Authuser\n0\n"
TABBED = "accept\t*/*\nauthorization\tSAPISIDHASH 1_fakehash\ncookie\tSID=fake-sid; __Secure-3PAPISID=fake-papisid\nx-goog-authuser\t0\n"
RAW = "accept: */*\nauthorization: SAPISIDHASH 1_fakehash\ncookie: SID=fake-sid; __Secure-3PAPISID=fake-papisid\nx-goog-authuser: 0\n"


@pytest.mark.parametrize("pasted", [CURL, FORMATTED, TABBED, RAW], ids=["curl", "formatted", "tabbed", "raw"])
def test_login_accepts_every_way_a_browser_copies_headers(tmp_path, pasted):
    import json
    saved = json.loads(youtube.login(pasted, tmp_path / "auth.json").read_text())
    saved = {k.lower(): v for k, v in saved.items()}
    assert saved["cookie"] == "SID=fake-sid; __Secure-3PAPISID=fake-papisid"
    assert saved["authorization"] == "SAPISIDHASH 1_fakehash" and saved["x-goog-authuser"] == "0"
    assert (tmp_path / "auth.json").stat().st_mode & 0o777 == 0o600


def test_login_normalizes_normal_youtube_com_headers(tmp_path):
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.types import AuthType

    yt_curl = """curl 'https://www.youtube.com/youtubei/v1/browse?prettyPrint=false' \\
      -H 'accept: */*' \\
      -H 'authorization: SAPISIDHASH 1_webhash' \\
      -H 'origin: https://www.youtube.com' \\
      -H 'x-origin: https://www.youtube.com' \\
      -H 'referer: https://www.youtube.com/feed/playlists' \\
      -H 'x-youtube-client-name: 1' \\
      -H 'x-youtube-client-version: 2.20261122.01.00' \\
      -H 'x-youtube-bootstrap-logged-in: true' \\
      -H 'x-goog-visitor-id: CgtWebVisitor' \\
      -H 'x-goog-authuser: 1' \\
      -H 'x-goog-pageid: 10987654321' \\
      -b 'SID=fake-sid; __Secure-3PAPISID=fake-papisid'"""
    path = youtube.login(yt_curl, tmp_path / "auth.json")
    saved = {k.lower(): v for k, v in json.loads(path.read_text()).items()}
    assert saved["origin"] == "https://music.youtube.com"
    assert saved["x-goog-authuser"] == "1"
    assert saved["x-goog-pageid"] == "10987654321"
    for dropped in ("x-origin", "referer", "x-youtube-client-name", "x-youtube-client-version",
                    "x-youtube-bootstrap-logged-in", "x-goog-visitor-id"):
        assert dropped not in saved
    assert YTMusic(str(path)).auth_type == AuthType.BROWSER

    # Even a minimal youtube.com header copy (cookie only, no x-goog-authuser or authorization)
    # gets defaults so ytmusicapi accepts it as a browser login.
    minimal = "cookie: SID=fake-sid; __Secure-3PAPISID=fake-papisid\norigin: https://www.youtube.com\n"
    min_path = youtube.login(minimal, tmp_path / "minimal.json")
    min_saved = {k.lower(): v for k, v in json.loads(min_path.read_text()).items()}
    assert min_saved["x-goog-authuser"] == "0"
    assert min_saved["authorization"].startswith("SAPISIDHASH ")
    assert YTMusic(str(min_path)).auth_type == AuthType.BROWSER


def test_login_accepts_firefox_curl_ansi_c_quoting_and_cookie_quirks(tmp_path):
    """Firefox 'Copy as cURL' uses POSIX $'...' ANSI-C quoting whenever a header
    or --data-raw payload has an apostrophe or non-ASCII text, emits Alt-Used/TE/
    Priority headers, and under ETP can send SAPISID without __Secure-3PAPISID."""
    from ytmusicapi import YTMusic
    from ytmusicapi.auth.types import AuthType

    # 1. Firefox Copy as cURL with $'...' in --data-raw (escaped apostrophe & unicode)
    #    and $'...' in -H headers, plus Alt-Used/TE/Priority and trailing space after \.
    ff_curl = (
        "curl 'https://www.youtube.com/youtubei/v1/browse?prettyPrint=false' \\   \n"
        "  --compressed \\\n"
        "  -X POST \\\n"
        "  -H 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:132.0) Firefox/132.0' \\\n"
        "  -H 'Accept: */*' \\\n"
        "  -H 'Origin: https://www.youtube.com' \\\n"
        "  -H 'Alt-Used: www.youtube.com' \\\n"
        "  -H 'Connection: keep-alive' \\\n"
        "  -H 'TE: trailers' \\\n"
        "  -H 'Priority: u=4' \\\n"
        "  -H $'Referer: https://www.youtube.com/watch?v=abc&q=don\\'t' \\\n"
        "  -H $'Cookie: FOO=\"bar baz\"; SID=fake-sid; SAPISID=fake/papisid; __Secure-1PAPISID=fake/papisid' \\\n"
        "  --data-raw $'{\"context\":{},\"query\":\"don\\'t stop \\u2014 live\"}'"
    )
    path = youtube.login(ff_curl, tmp_path / "ff_curl.json")
    saved = {k.lower(): v for k, v in json.loads(path.read_text()).items()}
    assert saved["x-goog-authuser"] == "0"
    assert "__Secure-3PAPISID=fake/papisid" in saved["cookie"]
    assert "SID=fake-sid" in saved["cookie"]
    assert "FOO=" not in saved["cookie"]
    for dropped in ("alt-used", "connection", "te", "priority", "referer"):
        assert dropped not in saved
    ytm = YTMusic(str(path))
    assert ytm.auth_type == AuthType.BROWSER and ytm.sapisid == "fake/papisid"

    # 2. Firefox 'Copy Request Headers' (starts with HTTP/3 request line) and no-space colons.
    ff_raw = (
        "POST /youtubei/v1/browse?prettyPrint=false HTTP/3\n"
        "Host: www.youtube.com\n"
        "Cookie:SID=fake-sid; __Secure-3PAPISID=fake/raw\n"
        "X-Goog-AuthUser:2\n"
    )
    raw_path = youtube.login(ff_raw, tmp_path / "ff_raw.json")
    raw_saved = {k.lower(): v for k, v in json.loads(raw_path.read_text()).items()}
    assert raw_saved["x-goog-authuser"] == "2"
    assert YTMusic(str(raw_path)).sapisid == "fake/raw"

    # 3. Multiple Cookie headers + truncated --data-raw quote still succeed.
    multi = (
        "curl 'https://www.youtube.com/youtubei/v1/browse' \\\n"
        "  -H 'Cookie: SID=fake-sid' \\\n"
        "  -H 'Cookie: __Secure-3PAPISID=fake/split' \\\n"
        "  --data-raw '{\"truncated\": \"unclosed"
    )
    multi_path = youtube.login(multi, tmp_path / "multi.json")
    assert YTMusic(str(multi_path)).sapisid == "fake/split"

    # 4. Windows 'Copy as cURL (cmd)' with ^-escaping.
    win_curl = (
        'curl ^"https://www.youtube.com/youtubei/v1/browse^" ^\r\n'
        '  -H ^"Cookie: SID=fake-sid; __Secure-3PAPISID=fake/win^" ^\r\n'
        '  --data-raw ^"{}^"'
    )
    win_path = youtube.login(win_curl, tmp_path / "win.json")
    assert YTMusic(str(win_path)).sapisid == "fake/win"

    # 5. Copying an OPTIONS preflight (no Cookie header) gives a clear, secret-free error.
    preflight = (
        "curl 'https://www.youtube.com/youtubei/v1/browse' -X OPTIONS \\\n"
        "  -H 'Access-Control-Request-Method: POST' \\\n"
        "  -H 'Origin: https://www.youtube.com'"
    )
    with pytest.raises(youtube.YouTubeLoginError, match="No Cookie header found"):
        youtube.login(preflight, tmp_path / "preflight.json")


def test_verification_waits_for_youtube_and_accepts_its_substitutions(provider):
    """Seen live: the first re-read after writing can be empty, YouTube may
    store a song under its official-audio id, and it declines some videos."""
    real_get = provider.get_playlist
    reads = []
    provider.unavailable = {"b"}

    def lagging_get(pid, limit=None):
        reads.append(pid)
        playlist = deepcopy(real_get(pid, limit))
        if len(reads) == 1:
            playlist["tracks"] = []
        for item in playlist["tracks"]:
            item["title"] = f"Song {item['videoId']}"
            if item["videoId"] == "d":
                item.update(videoId="d-audio", title="Song D (Official Audio)")
        return playlist
    provider.get_playlist = lagging_get
    ids = ["a", "b", "c", "d", "e"]
    report = youtube.YTMusicSink(lambda: provider).replace_playlist(
        "Discovery", ids, titles={v: f"Song {v}" for v in ids})
    assert report.verified and report.substituted == {"d": "d-audio"}
    assert report.missing == ["b"] and report.added == ["a", "c", "d", "e"]
    assert len(reads) == 2


def test_substitutes_need_the_same_song_title(provider):
    """Seen live: "Slow" came back as another id titled "(slow)"; "At the Bay"
    came back as another song, "Tris". A same-titled substitute counts as
    added; another recording is listed as inserted, never counted as added."""
    real_get = provider.get_playlist
    provider.unavailable = {"b"}
    swaps = {"d": ("d-audio", "(song d)"), "e": ("e-audio", "Song E (Official Audio)"),
             "g": ("g-other", "Completely Other Name"), "h": ("h-remix", "Song H (Someone Else Remix)")}

    def get(pid, limit=None):
        playlist = deepcopy(real_get(pid, limit))
        for item in playlist["tracks"]:
            new_id, title = swaps.get(item["videoId"], (item["videoId"], f"Song {item['videoId']}"))
            item.update(videoId=new_id, title=title, artists=[{"name": "Someone"}])
        return playlist
    provider.get_playlist = get
    ids = [chr(c) for c in range(ord("a"), ord("a") + 20)]
    titles = {v: f"Song {v}" for v in ids} | {"h": "Song H (Original Mix)"}
    report = youtube.YTMusicSink(lambda: provider).replace_playlist("Discovery", ids, titles=titles)
    assert report.substituted == {"d": "d-audio", "e": "e-audio"}
    assert report.missing == ["b", "g", "h"] and len(report.added) == 17
    assert [i["video_id"] for i in report.inserted] == ["g-other", "h-remix"]
    assert report.verified  # 17/20 of the request, nothing left from before


def test_a_failed_write_still_reports_what_youtube_put_in(provider):
    """The first chunk landed with one song swapped for another, then the second
    chunk failed: the swapped-in song is on the playlist and must be reported."""
    real_get = provider.get_playlist

    def get(pid, limit=None):
        playlist = deepcopy(real_get(pid, limit))
        for item in playlist["tracks"]:
            if item["videoId"] == "v3":
                item.update(videoId="tris", title="Tris", artists=[{"name": "Lanark Artefax"}])
        return playlist
    provider.get_playlist = get
    provider.fail_add = 2
    ids = [f"v{i}" for i in range(60)]
    report = youtube.YTMusicSink(lambda: provider).replace_playlist("Discovery", ids)
    assert not report.verified and report.failed == ids[50:]
    assert report.inserted == [{"video_id": "tris", "title": "Tris", "artist": "Lanark Artefax"}]
    assert "v3" in report.missing and "tris" not in report.added


def test_a_stale_read_is_never_verified_but_a_same_song_re_add_is(provider):
    """Right after a replace YouTube can still show the old entries: those are
    stray, so the sink keeps re-reading. Seen live: re-adding the same songs
    brings back the same substitute ids, in fresh entries; that is fine."""
    sink = youtube.YTMusicSink(lambda: provider)
    titles = {"a": "Song A", "b": "Song B"}
    real_get = provider.get_playlist

    def substituting(pid, limit=None):
        playlist = deepcopy(real_get(pid, limit))
        for item in playlist["tracks"]:
            if item["videoId"] == "b":
                item.update(videoId="b-audio", title="Song B")
        return playlist
    provider.get_playlist = substituting
    first = sink.replace_playlist("Discovery", ["a", "b"], titles=titles)
    assert first.verified and first.substituted == {"b": "b-audio"}
    old_entries, reads = substituting(first.playlist_id)["tracks"], []

    def lagging(pid, limit=None):
        reads.append(pid)
        playlist = substituting(pid, limit)
        if len(reads) == 2:  # the read right after the write still shows the old entries
            playlist["tracks"] = deepcopy(old_entries)
        return playlist
    provider.get_playlist = lagging
    second = sink.replace_playlist("Discovery", ["a", "b"], titles=titles)
    assert second.verified and second.substituted == {"b": "b-audio"} and len(reads) == 3


def test_an_old_entry_whose_video_went_unavailable_still_blocks_verification(provider):
    """Seen by review: read-back dropped entries without a videoId before the
    stray check, so an old entry that survived the replace went unnoticed."""
    sink = youtube.YTMusicSink(lambda: provider)
    first = sink.replace_playlist("Discovery", ["a", "b"])
    old_entry = dict(provider.playlists[first.playlist_id]["tracks"][0], videoId=None)
    real_get = provider.get_playlist

    def with_old_entry(pid, limit=None):
        playlist = real_get(pid, limit)
        playlist["tracks"].append(dict(old_entry))  # removal claimed success, entry remains
        return playlist
    provider.get_playlist = with_old_entry
    second = sink.replace_playlist("Discovery", ["c", "d"])
    assert second.added == ["c", "d"] and not second.verified


def test_title_key_keeps_versions_and_drops_noise():
    key = youtube._title_key
    assert key("Tune (Original Mix)") == key("Tune") == key("Tune - Original Mix") == key("TUNE [Official Audio]")
    assert key("Tune (Remastered 2011)") == key("Tune (feat. Someone)") == key("Tune")
    assert key("Tune (Someone Else Remix)") != key("Tune (Original Mix)")
    assert key("Tune feat. Singer") == key("Tune ft. Singer (Official Audio)") == key("Tune")
    assert key("Tune feat. Singer (DJ Remix)") == key("Tune (DJ Remix)") != key("Tune")
    assert key("Tune ft. Singer - DJ Remix") == key("Tune - DJ Remix") != key("Tune")
    assert key("(slow)") == key("Slow") and key("November X-Ray Mexico") == key("November X Ray Mexico")


def test_a_playlist_that_cannot_be_prepared_is_left_as_it_was(provider):
    sink = youtube.YTMusicSink(lambda: provider)
    sink.replace_playlist("Discovery", ["old"])
    for playlist in provider.playlists.values():
        playlist["tracks"][0]["setVideoId"] = None
    with pytest.raises(youtube.YouTubeExportError) as err:
        sink.replace_playlist("Discovery", ["new"])
    assert not err.value.changed and not any(c[0] in ("remove", "add") for c in provider.calls[-2:])


@pytest.mark.parametrize(("library", "verified"), [([], False), ([{"playlistId": "LM", "title": "Liked Music"}], True)])
def test_status_fails_when_youtube_answers_signed_out(monkeypatch, tmp_path, library, verified):
    # An expired login still gets a library listing, just an empty one.
    monkeypatch.setenv("COSINE_DIGGER_YTMUSIC_AUTH", str(tmp_path / "auth" / "browser.json"))

    class Session:
        def __init__(self, *args):
            pass

        def get_library_playlists(self, limit=25):
            return library

    monkeypatch.setattr(youtube, "YTMusic", Session)
    runner = CliRunner()
    assert runner.invoke(app, ["youtube", "login", "--no-verify"], input=HEADERS).exit_code == 0
    result = runner.invoke(app, ["youtube", "status", "--json"])
    assert json.loads(result.output) == {"auth_present": True, "verified": verified, "offline": False}
    assert result.exit_code == (0 if verified else 1)


def test_expired_login_on_an_existing_playlist_says_so_and_changes_nothing():
    # Digger Daily/Weekly already exist, so an expired login first fails the read.
    from ytmusicapi.exceptions import YTMusicServerError

    class SignedOut(FakeYTMusic):
        def get_playlist(self, pid, limit):
            raise YTMusicServerError("Server returned HTTP 401: Unauthorized.\nCookie: SID=super-secret-cookie")
    ytm = SignedOut()
    sink = youtube.YTMusicSink(ytm_factory=lambda: ytm, id_store={"Digger Daily": "playlist-0"})
    with pytest.raises(youtube.YouTubeExportError) as raised:
        sink.replace_playlist("Digger Daily", ["v1", "v2"])
    assert str(raised.value) == youtube.SIGNED_OUT and raised.value.changed is False
    assert sink.id_store == {"Digger Daily": "playlist-0"} and not ytm.calls


@pytest.mark.parametrize(("stored", "library", "message"), [
    (True, [], youtube.SIGNED_OUT),
    (False, [], youtube.SIGNED_OUT),
    (True, [{"playlistId": "LM", "title": "Liked Music"}], "Could not read the existing playlist 'Digger Daily' (KeyError)"),
], ids=["existing-signed-out", "new-signed-out", "existing-signed-in"])
def test_an_expired_login_without_a_401_still_says_signed_out(stored, library, message):
    # YouTube often answers an expired login with an ordinary page, not HTTP 401:
    # ytmusicapi can't parse it (KeyError), creation returns no ID, and the
    # library lists nothing (a signed-in library always holds Liked Music).
    class QuietlySignedOut(FakeYTMusic):
        def get_playlist(self, pid, limit):
            self.calls.append(("read", pid))
            raise KeyError("Unable to find 'contents' using path ['contents', 'twoColumnBrowseResultsRenderer']")

        def create_playlist(self, title, description, privacy_status):
            self.calls.append(("create", title))
            return {}

        def get_library_playlists(self, limit):
            self.calls.append(("library",))
            return library
    ytm = QuietlySignedOut()
    store = {"Digger Daily": "playlist-0"} if stored else {}
    sink = youtube.YTMusicSink(ytm_factory=lambda: ytm, id_store=dict(store))
    with pytest.raises(youtube.YouTubeExportError) as raised:
        sink.replace_playlist("Digger Daily", ["v1", "v2"])
    assert str(raised.value).startswith(message) and raised.value.changed is False
    assert sink.id_store == store
    assert not any(call[0] in ("privacy", "remove", "add") for call in ytm.calls)


@pytest.mark.parametrize(("status", "body", "expected"), [
    (503, b"<html>Service Unavailable</html>", "unreachable"),
    (500, b'{"error": {"code": 500}}', "unreachable"),
    (401, b'{"error": {"message": "Request had invalid authentication credentials."}}', "rejected"),
    (403, b"<html>Forbidden</html>", "rejected"),
], ids=["html-503", "json-500-no-message", "json-401", "html-403"])
def test_a_login_check_rejects_only_on_youtubes_refusal(monkeypatch, tmp_path, status, body, expected):
    """Seen by review (deep A1 #5): ytmusicapi reads the body before the status, so an error page
    raised something other than a server error and read as a rejected login. The real ytmusicapi
    request path runs here; only YouTube's answer is canned."""
    from http import HTTPStatus

    import requests

    class Answer(requests.adapters.BaseAdapter):
        def send(self, request, **kwargs):
            response = requests.Response()
            response.status_code, response.reason, response._content = status, HTTPStatus(status).phrase, body
            response.request, response.url = request, request.url
            return response

        def close(self):
            pass

    class Session(requests.Session):  # a Session still, as ytmusicapi checks
        def __init__(self):
            super().__init__()
            self.mount("https://", Answer())

    monkeypatch.setattr(youtube.requests, "Session", Session)
    path = youtube.login(RAW, tmp_path / "auth.json")
    assert youtube.check_login(path) == expected
    # Headers that can't make a request at all are a login YouTube will refuse.
    broken = youtube.login(RAW.replace("__Secure-3PAPISID=fake-papisid", "OTHER=x"), tmp_path / "broken.json")
    assert youtube.check_login(broken) == "rejected"
