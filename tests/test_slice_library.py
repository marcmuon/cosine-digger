"""S8 through tagged files, the real retry/cache transport and durable SQLite.

S10 owns the complete production TestClient scan/search/job scenario. These
tests exercise its exported library adapter before that runtime is merged.
"""

import asyncio
import base64
import hashlib
import json
import os
import sqlite3
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import httpx
import pytest
from mutagen.id3 import TALB, TCON, TDRC, TIT2, TPE1, TPUB
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.wave import WAVE

from cosine_digger import chain, discogs, playlists, profile, storage, taste
from cosine_digger.config import AppConfig, load_config
from cosine_digger.models import Track
from cosine_digger.web.slice_jobs import SliceRuntime
from cosine_digger.web.slice_library import label_overview, run_label_enrichment


# 30 ms of real AAC silence in an MP4 container, generated once with ffmpeg.
# Tests need only mutagen (already a production dependency), never ffmpeg.
_MP4 = (
    "AAAAHGZ0eXBNNEEgAAACAE00QSBpc29taXNvMgAAAwNtb292AAAAbG12aGQAAAAAAAAAAAAAAAAAAAPoAAAAHgABAAAB"
    "AAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAACAAACLXRyYWsAAABcdGtoZAAAAAMAAAAAAAAAAAAAAAEAAAAAAAAAHgAAAAAAAAAAAAAAAQEAAAAAAQAAAAAAAAAA"
    "AAAAAAAAAAEAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAACRlZHRzAAAAHGVsc3QAAAAAAAAAAQAAAB4AAAQAAAEAAAAA"
    "AaVtZGlhAAAAIG1kaGQAAAAAAAAAAAAAAAAAAB9AAAAE8FXEAAAAAAAtaGRscgAAAAAAAAAAc291bgAAAAAAAAAAAAAAAFNv"
    "dW5kSGFuZGxlcgAAAAFQbWluZgAAABBzbWhkAAAAAAAAAAAAAAAkZGluZgAAABxkcmVmAAAAAAAAAAEAAAAMdXJsIAAAAAEA"
    "AAEUc3RibAAAAGpzdHNkAAAAAAAAAAEAAABabXA0YQAAAAAAAAABAAAAAAAAAAAAAQAQAAAAAB9AAAAAAAA2ZXNkcwAAAAAD"
    "gICAJQABAASAgIAXQBUAAAAAAD6AAAAE8QWAgIAFFYhW5QAGgICAAQIAAAAgc3R0cwAAAAAAAAACAAAAAQAABAAAAAABAAAA"
    "8AAAABxzdHNjAAAAAAAAAAEAAAABAAAAAgAAAAEAAAAcc3RzegAAAAAAAAAAAAAAAgAAABUAAAAEAAAAFHN0Y28AAAAAAAAA"
    "AQAAAy8AAAAac2dwZAEAAAByb2xsAAAAAgAAAAH//wAAABxzYmdwAAAAAHJvbGwAAAABAAAAAgAAAAEAAABidWR0YQAAAFpt"
    "ZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAAJal0b28AAAAdZGF0YQAAAAEAAAAA"
    "TGF2ZjYyLjEyLjEwMQAAAAhmcmVlAAAAIW1kYXTeAgBMYXZjNjIuMjguMTAxAAIwQA4BGCAH"
)


def tagged_file(path, *, artist=None, album=None, title="Track", label=None, year=None, genre=None):
    """Only fixture construction writes tags; the scan/enrichment never does."""
    if path.suffix == ".m4a":
        path.write_bytes(base64.b64decode(_MP4))
        audio = MP4(path)
        for key, value in (("©ART", artist), ("©alb", album), ("©nam", title),
                           ("©day", year), ("©gen", genre)):
            if value:
                audio[key] = [value]
        if label:
            audio["----:com.apple.iTunes:LABEL"] = [MP4FreeForm(label.encode())]
    else:
        with wave.open(str(path), "wb") as stream:
            stream.setparams((1, 2, 8000, 80, "NONE", "not compressed"))
            stream.writeframes(b"\0\0" * 80)
        audio = WAVE(path)
        audio.add_tags()
        for frame, value in ((TPE1, artist), (TALB, album), (TIT2, title),
                             (TPUB, label), (TDRC, year), (TCON, genre)):
            if value:
                audio.tags.add(frame(encoding=3, text=[value]))
    audio.save()
    return path


def identity(folder):
    return {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in folder.iterdir()}


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch):
    monkeypatch.setattr("cosine_digger.config.load_env", lambda: None)
    monkeypatch.delenv("DISCOGS_TOKEN", raising=False)

    def denied(request):
        raise AssertionError("Tests must install their own fake provider")

    monkeypatch.setattr(chain.httpx, "HTTPTransport", lambda: httpx.MockTransport(denied))
    clock = {"now": 100.0, "waits": []}

    def sleep(seconds):
        clock["waits"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(discogs.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(discogs.time, "sleep", sleep)
    yield clock
    if discogs.FactsQueue._shared is not None:
        discogs.FactsQueue._shared.close()


@pytest.fixture
def library(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    tagged_file(music / "01.m4a", artist="Alpha", album="Album A", title="Tagged",
                label="  File Label  ", year="1999", genre="File Genre")
    tagged_file(music / "02.wav", artist="Alpha", album="Album A", title="Missing A",
                year="1998", genre="Original Genre")
    tagged_file(music / "03.wav", artist="Beta", album="Album B", title="Old Missing")
    tagged_file(music / "04.wav", artist="Gamma", album="Album C", title="No Match")
    tagged_file(music / "05.wav", title="Ineligible")
    for index, path in enumerate(sorted(music.iterdir())):
        timestamp = (1_000_000_000 if path.name == "03.wav" else 1_900_000_000 - index) * 10**9
        os.utime(path, ns=(timestamp, timestamp))
    before = identity(music)
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", discogs_token="fixture-current")
    assert taste.incremental_scan(music, cfg.db_path) == (5, 5)
    assert identity(music) == before
    return cfg, music, before


def start(cfg, budget=100, resume=None):
    with closing(storage.connect(cfg.db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            return storage.create_enrichment_job(
                conn, progress={"request": {"key": "fixture-key", "operation": "label_enrichment",
                                           "body_hash": "fixture-body", "response": {"job_id": "reserved"}},
                                "work": {"budget": budget}}, resume_job_id=resume,
            )


def terminal(cfg, job_id, result):
    with closing(storage.connect(cfg.db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            storage.append_slice_event(conn, job_id, "error" if result.get("error") else "done",
                                       result, redact=lambda value: value)


def run(cfg, job_id, *, cancelled=lambda: False, progress=None, get_config=None, factory=None):
    def emit(note):
        # This is the real journal path S7 will use. Read from another
        # connection first to prove that checkpointing preceded publication.
        with closing(storage.connect(cfg.db_path)) as conn:
            job = storage.get_slice_job(conn, job_id)
            assert job["progress"]["work"]["processed_pairs"] == note["progress"]["processed_pairs"]
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                storage.append_slice_event(conn, job_id, "note", note, redact=lambda value: value)
        if progress:
            progress(note)

    result = run_label_enrichment(job_id=job_id, get_config=get_config or (lambda: cfg),
                                  client_factory=factory or chain.make_rate_limited_client,
                                  cancelled=cancelled, on_progress=emit)
    terminal(cfg, job_id, result)
    return result


def provider(monkeypatch, handler=None):
    calls = []

    def respond(request):
        calls.append(request)
        if handler:
            return handler(request, len(calls))
        query = request.url.params["q"]
        results = [] if query == "Gamma Album C" else [
            {"id": 42, "title": query.replace(" ", " - ", 1), "label": ["Discogs Label"], "year": 2024,
             "genre": ["Electronic"], "style": ["Techno"], "resource_url": "https://api.discogs.com/releases/42"}
        ]
        return httpx.Response(200, json={"results": results})

    monkeypatch.setattr(chain.httpx, "HTTPTransport", lambda: httpx.MockTransport(respond))
    return calls


def test_tagged_folder_all_ages_only_missing_labels_and_no_file_writes(library, monkeypatch, capsys):
    cfg, music, before = library
    calls = provider(monkeypatch)
    clients = []

    def factory(**kwargs):
        value = chain.make_rate_limited_client(**kwargs)
        clients.append(value[0])
        return value

    with closing(storage.connect(cfg.db_path)) as conn:
        overview = label_overview(conn)
        sources = overview.pop("sources")
        assert overview == {"total_tracks": 5, "missing_tracks": 4, "eligible_pairs": 3,
                            "active_job_id": None, "last_job": None, "budget_default": 100}
        # Setup's library line: scanned files count as "file", whatever tag source they used.
        assert [(row["source"], row["count"]) for row in sources] == [("file", 5)] and sources[0]["imported_at"]
        assert conn.execute("SELECT label FROM tracks WHERE filename='01.m4a'").fetchone()[0] == "File Label"
        exclusions = playlists.load_exclusions(conn, profile.build_feed_profile(conn), scope="slice")
        assert exclusions.reason(Track(id="owned", artist="Alpha", name="Tagged", track="Tagged")) == "owned"
        # Source-agnostic storage preserves the unknown index flags for S10 search.
        assert conn.execute("SELECT COUNT(*) FROM tracks WHERE cosine_verified IS NULL").fetchone()[0] == 5
    job = start(cfg)
    assert start(cfg) == job  # The repository enforces one active label job.
    result = run(cfg, job, factory=factory)
    assert result["status"] == "done"
    assert (result["processed_pairs"], result["total_pairs"], result["filled_tracks"],
            result["missing_tracks"], result["ineligible_tracks"]) == (3, 3, 2, 1, 1)
    assert result["requests_used"] == 3 == len(calls)
    assert [request.url.params["q"] for request in calls] == ["Alpha Album A", "Gamma Album C", "Beta Album B"]
    assert all(client.is_closed for client in clients)
    assert capsys.readouterr().out == ""
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT label,year,genre FROM tracks WHERE filename='01.m4a'").fetchone() == (
            "File Label", "1999", "File Genre")
        assert conn.execute("SELECT label,year,genre FROM tracks WHERE filename='02.wav'").fetchone() == (
            "Discogs Label", "1998", "Original Genre")
        assert conn.execute("SELECT label,year,genre FROM tracks WHERE filename='03.wav'").fetchone() == (
            "Discogs Label", "2024", "Electronic, Techno")
        assert conn.execute("SELECT track_count FROM taste_signals WHERE signal_type='label' AND signal_value='Discogs Label'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM judgments").fetchone()[0] == 0
        overview = label_overview(conn)
        assert overview["active_job_id"] is None
        assert overview["last_job"]["result"]["filled_tracks"] == 2
        assert overview["missing_tracks"] == 2
    assert identity(music) == before


@pytest.mark.parametrize("matching_release", [False, True])
def test_gui_revalidates_legacy_positive_cache_and_preserves_budget_resume(tmp_path, monkeypatch, matching_release):
    music = tmp_path / "music"
    music.mkdir()
    tagged_file(music / "01.wav", artist="Wanted Artist", album="Wanted Album", title="CLI Track")
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", discogs_token="fixture-current")
    before = identity(music)
    assert taste.incremental_scan(music, cfg.db_path) == (1, 1)
    wrong = {"id": 999, "title": "Unrelated Artist - Unrelated Album", "label": ["Wrong Label"],
             "year": 2025, "genre": ["Wrong Genre"], "resource_url": "https://api.discogs.com/releases/999"}
    legacy_calls = []

    def legacy_get(url, **kwargs):
        legacy_calls.append(kwargs["params"]["q"])
        return httpx.Response(200, request=httpx.Request("GET", url), json={"results": [wrong]})

    monkeypatch.setenv("DISCOGS_TOKEN", "fixture-legacy")
    monkeypatch.setattr(httpx, "get", legacy_get)
    legacy = taste.enrich_from_discogs(cfg.db_path)
    assert legacy["found"] == 1 and legacy_calls == ["Wanted Artist Wanted Album"]
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT discogs_release_id,discogs_label FROM discogs_cache").fetchone() == (
            "999", "Wrong Label")
    assert identity(music) == before

    wanted = tagged_file(music / "02.m4a", artist="Wanted Artist", album="Wanted Album", title="GUI Track")
    pending = tagged_file(music / "03.wav", artist="Pending Artist", album="Pending Album")
    os.utime(wanted, ns=(1_900_000_000 * 10**9,) * 2)
    os.utime(pending, ns=(1_800_000_000 * 10**9,) * 2)
    before = identity(music)
    assert taste.incremental_scan(music, cfg.db_path) == (2, 3)
    exact = {"id": 42, "title": "  WANTED ARTIST (2) - wanted   album  ", "label": ["Correct Label"],
             "year": 2024, "genre": ["Electronic"], "resource_url": "https://api.discogs.com/releases/42"}
    results = [wrong, exact] if matching_release else [wrong]
    calls = provider(monkeypatch, lambda request, number: httpx.Response(200, json={"results": results}))
    first = start(cfg, budget=1)
    stopped = run(cfg, first)
    expected = ("Correct Label", "2024", "Electronic") if matching_release else (None, None, None)
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT label,year,genre FROM tracks WHERE filename='02.m4a'").fetchone() == expected
        assert conn.execute("SELECT track_count FROM taste_signals WHERE signal_value='Wrong Label'").fetchone()[0] == 1
        work = storage.get_slice_job(conn, first)["progress"]["work"]
        assert work["completed_pairs"] == [["Wanted Artist", "Wanted Album"]]
        assert work["pending_pairs"] == [["Pending Artist", "Pending Album"]]
        assert work["cursor"] == 1 and work["in_flight_pair"] is None
        assert conn.execute("SELECT discogs_release_id,discogs_label FROM discogs_cache").fetchone() == (
            ("42", "Correct Label") if matching_release else ("", None))
    assert stopped["status"] == "stopped" and stopped["reason"] == "budget"
    assert stopped["requests_used"] == len(calls) == 1
    assert calls[0].url.params["q"] == "Wanted Artist Wanted Album"
    assert (stopped["processed_pairs"], stopped["filled_tracks"], stopped["missing_tracks"]) == (
        1, int(matching_release), int(not matching_release))
    assert identity(music) == before

    def match_pending(request, number):
        assert request.url.params["q"] == "Pending Artist Pending Album"
        return httpx.Response(200, json={"results": [{"title": "Pending Artist - Pending Album", "label": ["Pending Label"]}]})

    resumed_calls = provider(monkeypatch, match_pending)
    resumed = run(cfg, start(cfg, budget=1, resume=first))
    assert resumed["status"] == "done" and resumed["requests_used"] == len(resumed_calls) == 1
    assert resumed["requests_total"] == 2
    assert (resumed["processed_pairs"], resumed["filled_tracks"], resumed["missing_tracks"]) == (
        2, 1 + int(matching_release), int(not matching_release))
    assert identity(music) == before

    # Keep the verified search payload: another import can be checked for free.
    tagged_file(music / "04.wav", artist="Wanted Artist", album="Wanted Album", title="Later GUI Track")
    before = identity(music)
    assert taste.incremental_scan(music, cfg.db_path) == (1, 4)
    cached_calls = provider(monkeypatch, lambda request, number: pytest.fail("Cached verification must make no request"))
    cached = run(cfg, start(cfg, budget=1))
    assert cached["status"] == "done" and cached["requests_used"] == 0 and not cached_calls
    assert cached["filled_tracks"] == int(matching_release)
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT label,year,genre FROM tracks WHERE filename='04.wav'").fetchone() == expected
    assert identity(music) == before


@pytest.mark.parametrize("result_title", [
    "Other Artist - Wanted Album",
    "Wanted Artist - Other Album",
])
def test_nonmatching_discogs_results_are_cached_misses_for_checkpoint_and_resume(tmp_path, monkeypatch, result_title):
    music = tmp_path / "music"
    music.mkdir()
    wanted = tagged_file(music / "01.m4a", artist="Wanted Artist", album="Wanted Album")
    pending = tagged_file(music / "02.wav", artist="Pending Artist", album="Pending Album")
    os.utime(wanted, ns=(1_900_000_000 * 10**9,) * 2)
    os.utime(pending, ns=(1_800_000_000 * 10**9,) * 2)
    before = identity(music)
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", discogs_token="fixture-current")
    assert taste.incremental_scan(music, cfg.db_path) == (2, 2)

    wrong_calls = provider(monkeypatch, lambda request, number: httpx.Response(200, json={"results": [
        {"id": 999, "title": result_title, "label": ["Wrong Label"], "year": 2025,
         "genre": ["Wrong Genre"], "resource_url": "https://api.discogs.com/releases/999"},
    ]}))
    first = start(cfg, budget=1)
    stopped = run(cfg, first)
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT label,year,genre FROM tracks WHERE filename='01.m4a'").fetchone() == (None, None, None)
        assert conn.execute("SELECT COUNT(*) FROM taste_signals WHERE signal_value='Wrong Label'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT discogs_release_id,discogs_label,discogs_year,discogs_genre,discogs_url "
            "FROM discogs_cache WHERE artist='Wanted Artist' AND album='Wanted Album'"
        ).fetchone() == ("", None, None, None, "")
        work = storage.get_slice_job(conn, first)["progress"]["work"]
        assert work["cursor"] == 1 and work["completed_pairs"] == [["Wanted Artist", "Wanted Album"]]
        assert work["pending_pairs"] == [["Pending Artist", "Pending Album"]]
        assert work["in_flight_pair"] is None
    assert stopped["status"] == "stopped" and stopped["reason"] == "budget"
    assert (stopped["processed_pairs"], stopped["filled_tracks"], stopped["missing_tracks"], stopped["errors"]) == (1, 0, 1, 0)
    assert stopped["requests_used"] == len(wrong_calls) == 1

    def match_pending(request, number):
        assert request.url.params["q"] == "Pending Artist Pending Album"
        return httpx.Response(200, json={"results": [
            {"id": 42, "title": "Pending Artist - Pending Album", "label": ["Correct Label"]},
        ]})

    resumed_calls = provider(monkeypatch, match_pending)
    second = start(cfg, resume=first)
    done = run(cfg, second)
    assert done["status"] == "done"
    assert (done["processed_pairs"], done["total_pairs"], done["filled_tracks"], done["missing_tracks"]) == (2, 2, 1, 1)
    assert done["requests_used"] == len(resumed_calls) == 1 and done["requests_total"] == 2
    with closing(storage.connect(cfg.db_path)) as conn:
        assert storage.get_slice_job(conn, second)["progress"]["work"]["pending_pairs"] == []
        assert conn.execute("SELECT label,year,genre FROM tracks WHERE filename='01.m4a'").fetchone() == (None, None, None)
        assert conn.execute("SELECT signal_value FROM taste_signals WHERE signal_type='label'").fetchall() == [("Correct Label",)]

    # A fresh run also consumes the cached miss without issuing another request.
    cached = run(cfg, start(cfg), factory=lambda **kwargs: pytest.fail("Cached misses make no request"))
    assert cached["status"] == "done" and cached["requests_used"] == 0
    assert (cached["processed_pairs"], cached["filled_tracks"], cached["missing_tracks"]) == (1, 0, 1)
    assert identity(music) == before


@pytest.mark.parametrize("first_title", ["Other Artist - Album A", "Alpha - Other Album"])
def test_enrichment_selects_later_exact_normalized_artist_and_album(library, monkeypatch, first_title):
    cfg, music, before = library
    calls = provider(monkeypatch, lambda request, number: httpx.Response(200, json={"results": [
        {"id": 999, "title": first_title, "label": ["Wrong Label"], "year": 2025, "genre": ["Wrong Genre"]},
        {"id": 42, "title": "  ALPHA (2) - album   a  ", "label": ["Correct Label"], "year": 2024,
         "genre": ["Electronic"], "resource_url": "https://api.discogs.com/releases/42"},
    ]}))
    result = run(cfg, start(cfg, budget=1))
    assert result["reason"] == "budget" and result["filled_tracks"] == 1
    assert result["requests_used"] == len(calls) == 1
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT label,year,genre FROM tracks WHERE filename='02.wav'").fetchone() == (
            "Correct Label", "1998", "Original Genre")
        assert conn.execute("SELECT label,year,genre FROM tracks WHERE filename='01.m4a'").fetchone() == (
            "File Label", "1999", "File Genre")
        assert conn.execute("SELECT discogs_release_id,discogs_label,discogs_year,discogs_genre FROM discogs_cache").fetchone() == (
            "42", "Correct Label", "2024", "Electronic")
        assert conn.execute("SELECT COUNT(*) FROM taste_signals WHERE signal_value='Wrong Label'").fetchone()[0] == 0
        assert conn.execute("SELECT track_count FROM taste_signals WHERE signal_type='label' AND signal_value='Correct Label'").fetchone()[0] == 1
    assert identity(music) == before


@pytest.mark.parametrize("atom", ["----:com.apple.iTunes:LABEL", "----:com.apple.iTunes:Label",
                                  "----:com.apple.iTunes:label", "----:com.apple.iTunes:publisher", "©pub"])
def test_real_mp4_label_fallbacks_decode_trim_and_keep_atom_priority(tmp_path, atom):
    music = tmp_path / "music"
    music.mkdir()
    path = tagged_file(music / "tagged.m4a", artist="Artist", album="Album")
    audio = MP4(path)
    audio[atom] = (["   ", "  Étiquette  "] if atom == "©pub" else
                   [MP4FreeForm(b"  "), MP4FreeForm(b"  \xc3\x89tiquette\xff  ")])
    if atom != "©pub":
        audio["©pub"] = ["Lower priority"]
    audio.save()
    before = identity(music)
    assert taste.incremental_scan(music, tmp_path / "taste.db") == (1, 1)
    with closing(storage.connect(tmp_path / "taste.db")) as conn:
        assert conn.execute("SELECT label FROM tracks").fetchone()[0] == ("Étiquette" if atom == "©pub" else "Étiquette�")
    assert identity(music) == before


def test_no_token_skips_without_factory_or_requests_even_with_environment_token(library, monkeypatch):
    cfg, music, before = library
    monkeypatch.setenv("DISCOGS_TOKEN", "fixture-env")
    current = AppConfig(home=cfg.home, db_path=cfg.db_path)
    called = []
    result = run(cfg, start(cfg), get_config=lambda: current, factory=lambda **kwargs: called.append(kwargs))
    assert result["skipped"] and result["status"] == "stopped"
    assert result["note"]["code"] == "S-SET-FILL-NOTOKEN"
    assert result["requests_used"] == result["filled_tracks"] == 0
    assert not called
    with closing(storage.connect(cfg.db_path)) as conn:
        assert label_overview(conn)["missing_tracks"] == 4
        assert conn.execute("SELECT COUNT(*) FROM discogs_cache").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0
    assert identity(music) == before


def test_current_saved_token_is_used_and_requests_include_retry(library, monkeypatch, isolated_providers):
    cfg, _, _ = library
    (cfg.home / "settings.json").write_text(json.dumps({"discogs_token": "fixture-saved-current"}))

    def handler(request, number):
        return httpx.Response(429 if number == 1 else 200, json={"results": [{"title": "Alpha - Album A", "label": ["Found"]}]})

    calls = provider(monkeypatch, handler)
    job = start(cfg, budget=2)
    result = run(cfg, job, get_config=lambda: load_config({"COSINE_DIGGER_HOME": str(cfg.home)}))
    assert result["status"] == "stopped" and result["reason"] == "budget"
    assert result["requests_used"] == 2 == len(calls)
    assert result["processed_pairs"] == result["filled_tracks"] == 1
    assert all(request.headers["Authorization"] == "Discogs token=fixture-saved-current" for request in calls)
    assert any(wait >= 5 for wait in isolated_providers["waits"])
    with closing(storage.connect(cfg.db_path)) as conn:
        work = storage.get_slice_job(conn, job)["progress"]["work"]
        assert work["cursor"] == 1 and len(work["pending_pairs"]) == 2
        assert work["requests_used"] == 2


def test_budget_cannot_start_a_retry_and_failed_pair_stays_pending(library, monkeypatch):
    cfg, _, _ = library
    calls = provider(monkeypatch, lambda request, number: httpx.Response(429))
    job = start(cfg, budget=1)
    result = run(cfg, job)
    assert result["reason"] == "budget" and result["errors"] == 0
    assert result["requests_used"] == 1 == len(calls)
    assert result["processed_pairs"] == result["filled_tracks"] == result["missing_tracks"] == 0
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM discogs_cache").fetchone()[0] == 0
        work = storage.get_slice_job(conn, job)["progress"]["work"]
        assert work["pending_pairs"][0] == ["Alpha", "Album A"]


def test_resume_new_budget_skips_completed_misses_and_admits_new_pairs(library, monkeypatch):
    cfg, music, before = library
    calls = provider(monkeypatch)
    first = start(cfg, budget=2)
    stopped = run(cfg, first)
    assert stopped["reason"] == "budget"
    assert (stopped["filled_tracks"], stopped["missing_tracks"], stopped["processed_pairs"]) == (1, 1, 2)
    tagged_file(music / "06.wav", artist="Delta", album="Album D", title="New Missing")
    assert taste.incremental_scan(music, cfg.db_path) == (1, 6)
    second = start(cfg, budget=10, resume=first)
    assert second != first
    result = run(cfg, second)
    assert result["status"] == "done"
    assert (result["filled_tracks"], result["missing_tracks"], result["processed_pairs"], result["total_pairs"]) == (3, 1, 4, 4)
    assert result["requests_used"] == 2 and result["requests_total"] == 4
    assert result["counts_scope"] == "cumulative" and result["requests_scope"] == "run"
    assert [request.url.params["q"] for request in calls] == ["Alpha Album A", "Gamma Album C", "Beta Album B", "Delta Album D"]
    with closing(storage.connect(cfg.db_path)) as conn:
        job = storage.get_slice_job(conn, second)
        assert job["progress"]["request"]["key"] == "fixture-key"
        assert job["progress"]["work"]["resume_job_id"] == first
        assert job["progress"]["work"]["pending_pairs"] == []
    assert {key: value for key, value in identity(music).items() if key in before} == before


@pytest.mark.parametrize("response_status", [200, 429])
def test_cancel_allows_one_in_flight_response_and_never_starts_another(library, monkeypatch, response_status):
    cfg, music, before = library
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()

    def handler(request, number):
        entered.set()
        assert release.wait(5)
        return httpx.Response(response_status, json={"results": [{"title": "Alpha - Album A", "label": ["Found"]}]})

    calls = provider(monkeypatch, handler)
    job = start(cfg)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, cfg, job, cancelled=cancelled.is_set)
        assert entered.wait(5)
        with closing(storage.connect(cfg.db_path)) as conn:
            work = storage.get_slice_job(conn, job)["progress"]["work"]
            assert work["requests_used"] == 1 and work["in_flight_pair"] == ["Alpha", "Album A"]
        cancelled.set()
        release.set()
        result = future.result(timeout=5)
    assert result["status"] == "stopped" and result["reason"] == "cancelled"
    assert result["requests_used"] == 1 == len(calls)
    assert result["filled_tracks"] == (1 if response_status == 200 else 0)
    assert result["processed_pairs"] == (1 if response_status == 200 else 0)
    assert identity(music) == before


def test_outage_is_redacted_retryable_and_keeps_committed_fills(library, monkeypatch, capsys):
    cfg, _, _ = library

    def handler(request, number):
        if number == 2:
            raise httpx.ConnectError("Authorization: fixture-sensitive", request=request)
        return httpx.Response(200, json={"results": [{"title": "Alpha - Album A", "label": ["Found"]}]})

    calls = provider(monkeypatch, handler)
    first = start(cfg)
    result = run(cfg, first)
    assert result["status"] == "stopped" and result["reason"] == "error"
    assert result["filled_tracks"] == 1 and result["errors"] == 1
    assert result["missing_tracks"] == 0
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT artist FROM discogs_cache").fetchall() == [("Alpha",)]
        stored = conn.execute("SELECT progress,error FROM slice_jobs WHERE id=?", (first,)).fetchone()
        assert "fixture-sensitive" not in str(stored)
        assert "fixture-sensitive" not in str(storage.slice_events(conn, first))
        assert storage.get_slice_job(conn, first)["progress"]["work"]["pending_pairs"][0] == ["Gamma", "Album C"]
    assert "fixture-sensitive" not in capsys.readouterr().out
    resumed_calls = provider(monkeypatch)
    second = start(cfg, resume=first)
    done = run(cfg, second)
    assert done["status"] == "done" and done["filled_tracks"] == 2 and done["errors"] == 1
    assert len(calls) == len(resumed_calls) == 2


def test_restart_keeps_reserved_in_flight_request_and_can_resume(library, monkeypatch):
    cfg, _, _ = library

    class Interrupted(BaseException):
        pass

    def interrupted(request, number):
        raise Interrupted

    calls = provider(monkeypatch, interrupted)
    first = start(cfg)
    with pytest.raises(Interrupted):
        run(cfg, first)
    with closing(storage.connect(cfg.db_path)) as conn:
        work = storage.get_slice_job(conn, first)["progress"]["work"]
        assert work["in_flight_pair"] == ["Alpha", "Album A"]
        assert work["requests_used"] == 1 and work["processed_pairs"] == 0
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            storage.append_slice_event(conn, first, "error", {"status": "stopped", "error": {"code": "server_restarted"}},
                                       redact=lambda value: value)
    resumed_calls = provider(monkeypatch)
    second = start(cfg, resume=first)
    result = run(cfg, second)
    assert len(calls) == 1 and result["status"] == "done"
    assert result["requests_used"] == len(resumed_calls) == 3 and result["requests_total"] == 4


def test_restart_after_committed_fill_repairs_signals_on_resume(library, monkeypatch):
    cfg, _, _ = library

    class Interrupted(BaseException):
        pass

    original = taste._compute_signals

    def interrupt_signals(conn):
        raise Interrupted

    calls = provider(monkeypatch)
    first = start(cfg)
    monkeypatch.setattr(taste, "_compute_signals", interrupt_signals)
    with pytest.raises(Interrupted):
        run(cfg, first)
    with closing(storage.connect(cfg.db_path)) as conn:
        work = storage.get_slice_job(conn, first)["progress"]["work"]
        assert work["signals_pending"] and work["filled_tracks"] == 1 and work["processed_pairs"] == 1
        assert conn.execute("SELECT label FROM tracks WHERE filename='02.wav'").fetchone()[0] == "Discogs Label"
        assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM discogs_cache").fetchone()[0] == 1
        conn.execute("BEGIN IMMEDIATE")
        with conn:
            storage.append_slice_event(conn, first, "error", {"status": "stopped", "error": {"code": "server_restarted"}},
                                       redact=lambda value: value)
    monkeypatch.setattr(taste, "_compute_signals", original)
    second = start(cfg, resume=first)
    result = run(cfg, second)
    assert result["status"] == "done" and result["requests_used"] == 2 and len(calls) == 3
    with closing(storage.connect(cfg.db_path)) as conn:
        assert not storage.get_slice_job(conn, second)["progress"]["work"]["signals_pending"]
        assert conn.execute("SELECT track_count FROM taste_signals WHERE signal_value='Discogs Label'").fetchone()[0] == 2


@pytest.mark.parametrize("matched", [True, False], ids=["filled", "cached-miss"])
def test_resume_requeues_completed_album_with_new_missing_rows(tmp_path, monkeypatch, matched):
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", discogs_token="fixture-current")
    with closing(storage.connect(cfg.db_path)) as conn, conn:
        storage.migrate(conn)
        conn.executemany("INSERT INTO tracks(path,artist,album,title,mtime) VALUES (?,?,?,?,?)", [
            ("fixture:A1", "Artist", "A", "A1", "2026-10-02"),
            ("fixture:B1", "Artist", "B", "B1", "2026-10-01"),
        ])

    def respond(request, number):
        artist, album = request.url.params["q"].split(" ", 1)
        labels = ["Exact Label"] if matched or album == "B" else []
        return httpx.Response(200, json={"results": [
            {"id": 42, "title": f"{artist} - {album}", "label": labels},
        ]})

    calls = provider(monkeypatch, respond)
    runtime = SliceRuntime(get_config=lambda: cfg, client_factory=lambda config: None, redact=lambda value: value)
    runtime.startup()
    try:
        def finish(**kwargs):
            job_id = runtime.submit_enrichment(runner=run_label_enrichment, **kwargs)["job_id"]
            runtime._jobs[job_id].thread.join(timeout=5)
            job = runtime.job_snapshot(job_id)
            assert job["state"] != "running", job
            assert job["state"] != "error", job["error"]
            return job_id, job["result"]

        first_id, first = finish(budget=1)
        assert first["reason"] == "budget" and first["processed_pairs"] == 1
        with closing(storage.connect(cfg.db_path)) as conn, conn:
            conn.execute("INSERT INTO tracks(path,artist,album,title,mtime) VALUES (?,?,?,?,?)",
                         ("fixture:A2", "Artist", "A", "A2", "2026-10-03"))
        second_id, result = finish(budget=1, resume_job_id=first_id)
        assert result["status"] == "done" and result["remaining_pairs"] == 0
        assert result["filled_tracks"] == (3 if matched else 1)
        assert result["missing_tracks"] == (0 if matched else 2)
        assert result["requests_used"] == 1 and result["requests_total"] == 2
        assert [request.url.params["q"] for request in calls] == ["Artist A", "Artist B"]
        with closing(storage.connect(cfg.db_path)) as conn:
            label = "Exact Label" if matched else None
            assert conn.execute("SELECT title,label FROM tracks ORDER BY title").fetchall() == [
                ("A1", label), ("A2", label), ("B1", "Exact Label"),
            ]
        _, again = finish(budget=1, resume_job_id=second_id)
        assert again["processed_pairs"] == result["processed_pairs"]
        assert again["filled_tracks"] == result["filled_tracks"]
        assert again["missing_tracks"] == result["missing_tracks"]
        assert again["requests_used"] == 0 and len(calls) == 2
    finally:
        runtime.drain()


def test_cached_miss_and_shared_http_hit_are_free_even_when_budget_is_empty(library, monkeypatch):
    cfg, _, _ = library
    calls = provider(monkeypatch)
    # Cache the older album through the shared HTTP key, not the label cache.
    request = httpx.Request("GET", f"{discogs.BASE_URL}/database/search",
                            params={"q": "Beta Album B", "type": "release", "per_page": 3})
    with closing(storage.connect(cfg.db_path)) as conn:
        storage.cache_put(conn, "discogs:" + str(request.url), json.dumps({"results": [{"title": "Beta - Album B", "label": ["Shared Cache"]}]}), 604800)
        with conn:
            conn.execute("INSERT INTO discogs_cache (artist,album) VALUES ('Gamma','Album C')")
    result = run(cfg, start(cfg, budget=1))
    assert result["status"] == "done" and result["requests_used"] == 1 == len(calls)
    assert result["filled_tracks"] == 2 and result["missing_tracks"] == 1
    # A subsequent explicit run completes the durable miss without a factory.
    next_result = run(cfg, start(cfg), factory=lambda **kwargs: pytest.fail("Cached misses make no request"))
    assert next_result["status"] == "done" and next_result["requests_used"] == 0
    assert next_result["missing_tracks"] == 1


def test_pending_group_rechecks_missing_rows_after_response(library, monkeypatch):
    cfg, _, _ = library

    def handler(request, number):
        with closing(storage.connect(cfg.db_path)) as conn:
            with conn:
                conn.execute("UPDATE tracks SET label='Arrived meanwhile' WHERE artist='Alpha' AND label IS NULL")
        return httpx.Response(200, json={"results": [{"title": "Alpha - Album A", "label": ["Found"]}]})

    provider(monkeypatch, handler)
    result = run(cfg, start(cfg, budget=1))
    assert result["filled_tracks"] == 0
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT label FROM tracks WHERE filename='02.wav'").fetchone()[0] == "Arrived meanwhile"


def test_cancel_before_start_and_during_pacing_spends_no_request(library, monkeypatch):
    cfg, _, _ = library
    calls = provider(monkeypatch)
    cancelled = threading.Event()
    cancelled.set()
    first = run(cfg, start(cfg), cancelled=cancelled.is_set,
                factory=lambda **kwargs: pytest.fail("Cancelled groups do not create clients"))
    assert first["reason"] == "cancelled" and first["requests_used"] == 0
    cancelled.clear()
    discogs._next_slot = discogs.time.monotonic() + 10
    monkeypatch.setattr(discogs.time, "sleep", lambda seconds: cancelled.set())
    second = run(cfg, start(cfg), cancelled=cancelled.is_set)
    assert second["reason"] == "cancelled" and second["requests_used"] == 0
    assert not calls


def test_group_fill_cache_and_checkpoint_roll_back_together(library, monkeypatch):
    cfg, _, _ = library
    provider(monkeypatch)
    job = start(cfg)
    with closing(storage.connect(cfg.db_path)) as conn:
        with conn:
            conn.execute("""CREATE TRIGGER reject_group_checkpoint BEFORE UPDATE OF progress ON slice_jobs
                            WHEN json_extract(NEW.progress,'$.work.cursor') > 0
                            BEGIN SELECT RAISE(ABORT,'fixture checkpoint failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="fixture checkpoint failure"):
        run(cfg, job)
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT label FROM tracks WHERE filename='02.wav'").fetchone()[0] is None
        assert conn.execute("SELECT COUNT(*) FROM discogs_cache").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0
        work = storage.get_slice_job(conn, job)["progress"]["work"]
        assert work["processed_pairs"] == 0 and work["filled_tracks"] == 0
        assert work["requests_used"] == 1 and work["pending_pairs"][0] == ["Alpha", "Album A"]


def test_malformed_provider_response_is_retryable_and_never_cached_as_a_miss(library, monkeypatch):
    cfg, _, _ = library
    provider(monkeypatch, lambda request, number: httpx.Response(200, json={"unexpected": "payload"}))
    first = start(cfg)
    failed = run(cfg, first)
    assert failed["reason"] == "error" and failed["errors"] == 1 and failed["missing_tracks"] == 0
    with closing(storage.connect(cfg.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM discogs_cache").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0
    calls = provider(monkeypatch)
    done = run(cfg, start(cfg, resume=first))
    assert done["status"] == "done" and len(calls) == 3


@pytest.mark.parametrize("budget", [True])
def test_adapter_refuses_invalid_request_budgets_before_network(library, budget):
    cfg, _, _ = library
    job = start(cfg, budget=budget)
    with pytest.raises(ValueError, match="integer from 1 to 500"):
        run(cfg, job)


@pytest.mark.parametrize("compact_notes", [False], ids=["production"])
@pytest.mark.parametrize("albums", [100])
def test_enrichment_journal_counters_preserve_checkpoint(tmp_path, compact_notes, albums):
    """Bound replay and retain resume work through the real runtime writer.

    The counter-only case probes the writer with the specified note shape,
    independently of the current adapter's full-checkpoint notes.
    """
    cfg = AppConfig(home=tmp_path, db_path=tmp_path / "taste.db", discogs_token="fixture-current")
    with closing(storage.connect(cfg.db_path)) as conn, conn:
        storage.migrate(conn)
        conn.executemany(
            "INSERT INTO tracks(path,artist,album,title,mtime) VALUES (?,?,?,?,?)",
            [(f"fixture:{album}:{number}", f"Artist{album:04d}", f"Album{album:04d}",
              f"Track{number}", "2026-10-01") for album in range(albums) for number in range(10)],
        )
        conn.executemany(
            "INSERT INTO discogs_cache(artist,album) VALUES (?,?)",
            [(f"Artist{album:04d}", f"Album{album:04d}") for album in range(albums)],
        )
    counters = {"processed_pairs", "total_pairs", "filled_tracks", "missing_tracks",
                "ineligible_tracks", "errors", "requests_used", "budget", "remaining_pairs",
                "estimated_seconds", "requests_total", "counts_scope", "requests_scope"}

    def runner(**kwargs):
        publish = kwargs.pop("on_progress")

        def emit(note):
            if compact_notes:
                note = note | {"progress": {key: value for key, value in note["progress"].items()
                                           if key in counters}}
            publish(note)

        return run_label_enrichment(**kwargs, on_progress=emit)

    runtime = SliceRuntime(get_config=lambda: cfg, client_factory=lambda config: None, redact=lambda value: value)
    runtime.startup()
    try:
        job_id = runtime.submit_enrichment(budget=1, runner=runner)["job_id"]
        worker = runtime._jobs[job_id]
        worker.thread.join(timeout=30)
        assert not worker.thread.is_alive()
        job = runtime.job_snapshot(job_id)
        assert job["state"] == "done", job["error"]
        assert job["result"]["processed_pairs"] == albums
        assert job["result"]["requests_used"] == 0
        work = job["progress"]["work"]
        assert work.get("pending_pair_ids") == []
        assert len(work.get("completed_pair_ids", [])) == albums
        with closing(storage.connect(cfg.db_path)) as conn:
            events = storage.slice_events(conn, job_id)
        notes = [event["data"] for event in events if event["event"] == "note"]
        assert all(set(note["progress"]) <= counters for note in notes)

        async def replay():
            return [frame async for frame in runtime.job_events(job_id).body_iterator]

        frames = asyncio.run(replay())
        assert len(frames) == len(events) == albums + 3
        assert sum(len(frame.encode()) for frame in frames) < 2048 * len(frames)
    finally:
        runtime.drain()
