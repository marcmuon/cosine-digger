"""Deterministic provider HTTP for the slice, with a startup-only env gate.

S10 calls fixture_from_environment with the actual bind host and scheduler
setting, then injects cosine_client/discogs_client into the ordinary engine.
This module installs no routes, changes no environment, and writes no files.
Explicit Python injection of SliceFixtures does not require an environment gate.

slice-v1's searchable seeds are seed-a/b/c, empty, missing, no-facts,
no-shared, rate-limited, retry-once, provider-failure, partial-failure,
discogs-down and discogs-rate-limited. point() returns their S1 origins;
seed_points and library_rows supply the three-seed and local-library inputs.
block("hop1"/"facts"/"search"/"enrichment") holds provider HTTP until the
returned gate's released Event is set. These controls are Python-only.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
from threading import Event, Lock

import httpx

from .. import chain, discogs, youtube
from ..client import BASE_URL, CosineClient
from ..httpcache import HttpCache
from ..models import Track
from ..slice_types import Point


class FixtureConfigurationError(ValueError):
    """A gated fixture selection is unsafe or unknown; refuse startup."""


def require_temporary_storage(home: Path, db_path: Path) -> None:
    """Check resolved paths before startup can open or recover a database."""
    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
        safe = all(path.expanduser().resolve().is_relative_to(temp_root) for path in (home, db_path))
    except (OSError, RuntimeError, ValueError):
        safe = False
    if not safe:
        raise FixtureConfigurationError("Test fixtures require temporary home and database paths")


def fixture_from_environment(*, host: str, scheduler: bool,
                             environ: Mapping[str, str] | None = None) -> SliceFixtures | None:
    """Ignore fixture selection without the exact test gate; fail closed with it.

    Only startup configuration is accepted. HTTP request data is never an
    authority source. Error messages deliberately omit untrusted env values.
    """
    env = os.environ if environ is None else environ
    if env.get("COSINE_DIGGER_TEST_MODE") != "1" or not env.get("COSINE_DIGGER_FIXTURE"):
        return None
    if env["COSINE_DIGGER_FIXTURE"] != "slice-v1":
        raise FixtureConfigurationError("Unknown test fixture")
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    if not loopback or scheduler is not False:
        raise FixtureConfigurationError("Test fixtures require loopback binding and a disabled scheduler")
    return SliceFixtures()


@dataclass
class FixtureGate:
    entered: Event = field(default_factory=Event)
    released: Event = field(default_factory=Event)

    def wait(self, request: httpx.Request) -> None:
        self.entered.set()
        if not self.released.wait(10):
            raise httpx.ReadTimeout("Fixture request timed out", request=request)


class FixtureCosineClient(CosineClient):
    def __init__(self, transport: httpx.BaseTransport):
        # Keep the real search/lookup/similar parsers and retry path, without
        # constructing a network client or loading any user's credentials.
        self._client = httpx.Client(base_url=BASE_URL, transport=transport, trust_env=False)

    def youtube_title(self, url: str) -> str | None:
        from ..seeds import _youtube_title

        return _youtube_title(url, fetch=self._client.get)


class FixturePlaylistSink:
    """Replace playlists in memory without constructing an authenticated client."""

    playlist_id = "PLsliceFixture"
    playlist_url = f"https://music.youtube.com/playlist?list={playlist_id}"

    def __init__(self, fixtures: SliceFixtures, id_store=None):
        self.fixtures, self.id_store = fixtures, id_store

    def replace_playlist(self, name, video_ids, description="", key=None, **kwargs) -> youtube.ExportReport:
        requested, skipped = youtube._dedupe(video_ids)
        key = key or name
        with self.fixtures._lock:
            created = key not in self.fixtures._playlists
            self.fixtures._playlists[key] = requested.copy()
        if self.id_store is not None:
            self.id_store[key] = self.playlist_id
        return youtube.ExportReport(
            playlist_id=self.playlist_id, playlist_url=self.playlist_url, created=created,
            requested=requested, added=requested.copy(), missing=[], skipped=skipped,
            failed=[], verified=True,
        )


class SliceFixtures:
    """One independent slice-v1 world, shared by its worker-owned clients."""

    name = "slice-v1"
    embed_errors = {"S9REFUSE101": 101, "Pso5YU_mntg": 150, "S9GONE00100": 100}

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self._lock = Lock()
        self._counts: dict[tuple[str, str], int] = {}
        self._gates: dict[str, FixtureGate] = {}
        self._tracks: dict[str, dict] = {}
        self._neighbors: dict[str, list[dict]] = {}
        self._releases: dict[str, dict] = {}
        self._directories: dict[str, list[dict]] = {}
        self._playlists: dict[str, list[str]] = {}
        self._build()

    def block(self, name: str) -> FixtureGate:
        if name not in {"hop1", "facts", "search", "enrichment"}:
            raise ValueError("Unknown fixture gate")
        gate = FixtureGate()
        self._gates[name] = gate
        return gate

    def release_all(self) -> None:
        for gate in self._gates.values():
            gate.released.set()

    def _wait(self, name: str, request: httpx.Request) -> None:
        gate = self._gates.get(name)
        if gate is not None:
            gate.wait(request)

    def cosine_client(self) -> CosineClient:
        return FixtureCosineClient(httpx.MockTransport(self._cosine))

    def youtube_sink(self, id_store=None) -> FixturePlaylistSink:
        return FixturePlaylistSink(self, id_store)

    def discogs_client(self, budget: int = 100, http_cache: HttpCache | None = None,
                       token: str | None = None, *, budget_tracker: chain.BudgetTracker | None = None,
                       request_slot=None) -> tuple[httpx.Client, chain.BudgetTracker]:
        """Match make_rate_limited_client's factory contract, including S2 slots.

        Substitute only the network transport; budget, cache, pacing and 429
        retry remain the production implementation used by moves/enrichment.
        """
        tracker = budget_tracker if budget_tracker is not None else chain.BudgetTracker(budget)
        transport = chain._RetryOn429Transport(
            wrapped=httpx.MockTransport(self._discogs), budget_tracker=tracker,
            http_cache=http_cache, token=token or "slice-discogs-key", request_slot=request_slot,
        )
        return httpx.Client(transport=transport, trust_env=False), tracker

    @property
    def seed_points(self) -> list[Point]:
        return [self.point(f"seed-{letter}") for letter in "abc"]

    def point(self, name: str) -> Point:
        from ..api import track_key

        if name == "missing":
            return Point(point_id="track:missing", kind="track", name="Absent Artist - Absent Recording",
                         artist="Absent Artist", title="Absent Recording")
        if name == "unindexed":
            return Point(point_id="library:2", kind="track", name="Fixture Artist A - Unindexed File",
                         artist="Fixture Artist A", title="Unindexed File", library_id=2, indexed=False)
        if name == "label":
            return Point(point_id="label:501", kind="label", name="Label A", discogs_id="501")
        if name == "person":
            return Point(point_id="person:601", kind="person", name="Person P", discogs_id="601")
        if name == "artist":
            return Point(point_id="artist:7000", kind="artist", name="Discovery Artist 0", discogs_id="7000")
        item = Track.model_validate(self._tracks[name])
        return Point(point_id=f"track:{track_key(item)}", kind="track", name=item.name,
                     track_key=track_key(item), cosine_id=item.id, artist=item.artist,
                     title=item.track, video_id=item.video_id, indexed=True)

    def _track(self, tid: str, artist: str, title: str, *, video: str | None = None,
               release: int | None = None, score: float | None = None) -> dict:
        item = {"id": tid, "name": f"{artist} - {title}", "artist": artist, "track": title,
                "score": score, "video_id": video, "source": "discogs",
                "video_uri": f"https://www.youtube.com/watch?v={video}" if video else None,
                "external_link": f"https://www.discogs.com/release/{release}" if release else None}
        self._tracks[tid] = item
        self._neighbors[tid] = []
        return item

    def _release(self, rid: int, item: dict, *, label: int = 501, label_name: str = "Label A",
                 person: bool = False, many: bool = False, artist_id: int | None = None,
                 year: int = 2026) -> dict:
        credits = [{"id": 601, "name": "Person P", "role": "Producer, Mixed By"}] if person else []
        if many:
            credits += [{"id": 610 + i, "name": f"Credited Person {i + 1}", "role": "Engineer"}
                        for i in range(11)]
        payload = {"id": rid, "title": item["track"], "year": year, "styles": ["Techno", "Dub Techno"],
                   "genres": ["Electronic"], "labels": [{"id": label, "name": label_name}],
                   "artists": [{"id": artist_id, "name": item["artist"]}], "extraartists": credits,
                   "tracklist": [{"position": "A1", "type_": "track", "title": item["track"],
                                  "artists": [{"id": artist_id, "name": item["artist"]}]}],
                   "thumb": "https://i.discogs.com/slice-fixture.jpg"}
        self._releases[f"/releases/{rid}"] = payload
        return payload

    def _build(self) -> None:
        normal = []
        special = ["shared-track", "refused-101", "refused-150", "unavailable-100", "no-video", "no-facts-1"]
        videos = ["jNQXAC9IVRw", "S9REFUSE101", "Pso5YU_mntg", "S9GONE00100", None, "S9VIDEO0005"]
        for i in range(30):
            tid = special[i] if i < len(special) else f"near-{i:02}"
            video = videos[i] if i < len(videos) else f"S9VIDEO{i:04}"
            item = self._track(tid, f"Discovery Artist {i}", f"Discovery {i}", video=video,
                               release=None if i == 5 else 2000 + i)
            normal.append(item)
            if i != 5:
                self._release(2000 + i, item, label=501 if i < 3 else 520 + i,
                              label_name="Label A" if i < 3 else f"Only Label {i}",
                              person=i < 3, many=i == 0, artist_id=7000 + i)
        owned = self._track("owned-label-track", "Owned Label Artist", "Old File", release=3100)
        self._release(3100, owned, year=1999)
        for recording in normal:
            self._neighbors[recording["id"]] = [dict(item, score=round(0.94 - i * 0.006, 3))
                for i, item in enumerate(normal) if item["id"] != recording["id"]] + [dict(owned, score=0.99)]
        for index, letter in enumerate("abc"):
            seed = self._track(f"seed-{letter}", f"Fixture Artist {letter.upper()}", f"Seed {letter.upper()}",
                               video=f"S9SEED{letter.upper()}0001")
            self._neighbors[seed["id"]] = [dict(item, score=round(0.96 - i * 0.008 - index * 0.01, 3))
                                          for i, item in enumerate(normal)] + [dict(owned, score=0.99), seed]
        standin = self._track("standin-a", "Fixture Artist A", "Indexed Stand-in", video="S9STANDA001")
        self._neighbors[standin["id"]] = self._neighbors["seed-a"][:30]

        newest = []
        for i in range(5):
            source = self._track(f"label-source-{i}", f"Newest Artist {i}", f"Newest Record {i}",
                                 release=3000 + i, video=f"S9NEWEST{i:03}")
            self._release(3000 + i, source, person=True, artist_id=7100 + i, year=2026 - i)
            newest.append({"id": 3000 + i, "artist": source["artist"], "title": source["track"],
                           "year": 2026 - i, "role": "Producer", "type": "release"})
        unindexed = []
        for i in range(5):
            recording = {"artist": f"Unindexed Label Artist {i}", "track": f"Unindexed Label Record {i}"}
            self._release(3050 + i, recording, year=2031 - i)
            unindexed.append({"id": 3050 + i, "artist": recording["artist"], "title": recording["track"],
                              "year": 2031 - i})
        self._directories["/labels/501/releases"] = unindexed + newest + [
            {"id": 3100, "artist": owned["artist"], "title": owned["track"], "year": 1999}]
        person_releases = []
        for i in range(5):
            source = self._track(f"person-source-{i}", f"Credited Record Artist {i}", f"Credited Record {i}",
                                 release=3300 + i)
            self._release(3300 + i, source, person=True, year=2026 - i)
            person_releases.append({"id": 3300 + i, "artist": source["artist"], "title": source["track"],
                                    "year": 2026 - i, "role": "Producer", "type": "release"})
        self._directories["/artists/601/releases"] = person_releases
        for i in range(11):
            self._directories[f"/artists/{610 + i}/releases"] = [dict(newest[0], role="Engineer")]
            self._releases["/releases/3000"]["extraartists"].append(
                {"id": 610 + i, "name": f"Credited Person {i + 1}", "role": "Engineer"})
        for i in range(30):
            source = self._track(f"artist-source-{i}", f"Discovery Artist {i}", f"Artist Record {i}",
                                 release=3200 + i)
            self._release(3200 + i, source, artist_id=7000 + i)
            self._directories[f"/artists/{7000 + i}/releases"] = [
                {"id": 3200 + i, "artist": source["artist"], "title": source["track"],
                 "year": 2026, "role": "Main", "type": "release"}]
        further = []
        for i in range(36):
            item = self._track(f"further-{i:02}", f"Further Artist {i}", f"Further Recording {i}",
                               video=f"S9FURTHER{i:02}", release=4000 + i)
            self._release(4000 + i, item, label=900 + i, label_name=f"Further Label {i}")
            further.append(dict(item, score=round(0.92 - i * 0.004, 3)))
        for i in range(5):
            self._neighbors[f"label-source-{i}"] = further[:12] + [normal[0]]
            self._neighbors[f"person-source-{i}"] = further[12:24]
        for i in range(30):
            self._neighbors[f"artist-source-{i}"] = further[24:]

        no_facts = [normal[5], self._track("no-facts-2", "No Release Artist", "No Release", video="S9NOFACT002")]
        no_shared = []
        for i in range(2):
            item = self._track(f"singleton-{i}", f"Solo Artist {i}", f"Solo Recording {i}", release=2300 + i)
            self._release(2300 + i, item, label=800 + i, label_name=f"Solo Label {i}")
            no_shared.append(item)
        partial = []
        for i in range(3):
            item = self._track(f"partial-near-{i}", f"Partial Artist {i}", f"Partial Recording {i}", release=2500 + i)
            self._release(2500 + i, item, label=502, label_name="Failure Label")
            partial.append(item)
        source = self._track("partial-hop1-source", "Failure Source Artist", "Failure Source Record")
        self._release(3500, source, label=502, label_name="Failure Label")  # newest release holds the source
        self._directories["/labels/502/releases"] = [
            {"id": 3500, "artist": source["artist"], "title": source["track"], "year": 2026}]
        down = self._track("down-pick", "Unavailable Facts Artist", "Unavailable Facts", release=2401)
        slow = self._track("limited-pick", "Limited Facts Artist", "Limited Facts", release=2400)
        self._release(2400, slow)
        for name, neighbors in {"empty": [], "no-facts": no_facts, "no-shared": no_shared,
                                "partial-failure": partial, "discogs-down": [down],
                                "discogs-rate-limited": [slow], "rate-limited": normal,
                                "retry-once": normal, "provider-failure": normal}.items():
            self._track(name, f"Fixture {name} Artist", f"Fixture {name}")
            self._neighbors[name] = [dict(item, score=round(0.95 - i * 0.01, 3))
                                     for i, item in enumerate(neighbors)]

        self.library_rows = (
            {"id": 1, "path": "/slice-fixture/library/old-file.flac", "artist": owned["artist"],
             "album": "Old Album", "title": owned["track"], "label": "Label A",
             "cosine_verified": 1, "cosine_track_id": owned["id"]},
            {"id": 2, "path": "/slice-fixture/library/unindexed.flac", "artist": "Fixture Artist A",
             "album": "Missing Album A", "title": "Unindexed File", "label": None, "cosine_verified": 0},
            {"id": 3, "path": "/slice-fixture/library/pending.flac", "artist": "Library Artist B",
             "album": "Missing Album B", "title": "Pending File", "label": None, "cosine_verified": None},
        )

    def _call(self, provider: str, request: httpx.Request) -> tuple[str, dict[str, str], int]:
        path = request.url.path.removeprefix("/api/v1") if provider == "cosine" else request.url.path
        params = dict(request.url.params)
        with self._lock:
            self.calls.append((provider, path, params))  # Never keep headers or credentials.
            key = provider, path
            count = self._counts[key] = self._counts.get(key, 0) + 1
        return path, params, count

    def _cosine(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.youtube.com":
            path, params, _ = self._call("youtube", request)
            if request.method == "GET" and path == "/oembed":
                url = params.get("url")
                if url == "https://www.youtube.com/watch?v=S9OEMBED001":
                    return httpx.Response(200, json={"title": self._tracks["seed-a"]["name"]})
                for item in self._tracks.values():
                    if item["video_uri"] and url == item["video_uri"]:
                        return httpx.Response(200, json={"title": item["name"]})
            return httpx.Response(404, json={})
        path, params, count = self._call("cosine", request)
        if request.method != "GET" or request.url.host != "cosine.club":
            return httpx.Response(404, json={"code": "NOT_FOUND"})
        if path == "/search":
            self._wait("search", request)
            query = params.get("q", "").casefold().strip()
            hits = [item for item in self._tracks.values() if query and
                    (query in item["name"].casefold() or query == f"{item['artist']} {item['track']}".casefold())]
            return httpx.Response(200, json={"data": hits[:int(params.get("limit", 20))]})
        if path == "/tracks/lookup":
            url = params.get("url", "")
            hits = [item for item in self._tracks.values() if url and
                    url in {item["external_link"], item["video_uri"]}]
            return httpx.Response(200, json={"data": hits})
        tid = path.removeprefix("/tracks/").removesuffix("/similar")
        if path != f"/tracks/{tid}/similar" or tid not in self._tracks:
            return httpx.Response(404, json={"code": "TRACK_NOT_FOUND"})
        if tid == "rate-limited" or (tid == "retry-once" and count == 1):
            return httpx.Response(429, headers={"Retry-After": "2"},
                                  json={"code": "RATE_LIMITED", "retry_after_s": 2})
        if tid in {"provider-failure", "partial-hop1-source"}:
            return httpx.Response(503, json={"code": "UNAVAILABLE"})
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 50))
        neighbors = self._neighbors[tid]
        return httpx.Response(200, json={"data": {"source_track": self._tracks[tid],
            "similar_tracks": neighbors[offset:offset + limit]},
            "meta": {"total": len(neighbors), "has_more": offset + limit < len(neighbors)}})

    def _discogs(self, request: httpx.Request) -> httpx.Response:
        path, params, count = self._call("discogs", request)
        if request.method != "GET" or request.url.host != "api.discogs.com":
            return httpx.Response(404, json={"message": "Not found"})
        if path == "/labels/501/releases":
            self._wait("hop1", request)
        if path == "/releases/2000":
            self._wait("facts", request)
        if path == "/releases/2401":
            return httpx.Response(503, json={"message": "Service unavailable"})
        if path == "/releases/2400" and count == 1:
            return httpx.Response(429, headers={"Retry-After": "5"}, json={"message": "Rate limited"})
        if path == "/database/search":
            query = params.get("q", "")
            results = []
            if query in {"Fixture Artist A Missing Album A", "Library Artist B Missing Album B"}:
                self._wait("enrichment", request)
                rid = 5001 if query.startswith("Fixture") else 5002
                title = ("Fixture Artist A - Missing Album A" if rid == 5001
                         else "Library Artist B - Missing Album B")
                results = [{"id": rid, "title": title,
                            "type": "release", "label": ["Filled Library Label"], "year": 2025,
                            "genre": ["Electronic"], "style": ["Techno"],
                            "resource_url": f"https://api.discogs.com/releases/{rid}"}]
            if params.get("type") == "artist":
                results = [{"id": 7000 + i, "title": f"Discovery Artist {i}"} for i in range(30)
                           if query.casefold() == f"Discovery Artist {i}".casefold()]
            return httpx.Response(200, json={"results": results, "pagination": {"pages": 1}})
        if path in self._directories:
            releases = self._directories[path]
            size, page = int(params.get("per_page", 50)), int(params.get("page", 1))
            return httpx.Response(200, json={"releases": releases[(page - 1) * size:page * size],
                "pagination": {"page": page, "pages": max(1, (len(releases) + size - 1) // size)}})
        if path in self._releases:
            return httpx.Response(200, json=self._releases[path])
        return httpx.Response(404, json={"message": "Not found"})
