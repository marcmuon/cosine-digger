#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from cosine_digger.client import CosineClient, CosineClientError
from cosine_digger.discogs import (
    DiscogsError,
    extract_artist_id,
    extract_label_id,
    fetch_artist_info,
    fetch_artist_releases,
    fetch_label_info,
    fetch_label_releases,
    fetch_release_artists,
    fetch_release_credits,
    fetch_release_tracks,
)


def _detect_kind(url: str) -> str:
    if extract_label_id(url):
        return "label"
    if extract_artist_id(url):
        return "artist"
    if re.search(r"/(release|master)/\d+", url, re.IGNORECASE):
        return "release"
    raise ValueError(f"Unsupported Discogs URL: {url}")


def _artist_matches(expected: str, actual: str) -> bool:
    e = expected.lower().strip()
    a = actual.lower().strip()
    return e in a or a in e


def _track_title(track: Any) -> str:
    return getattr(track, "track", None) or getattr(track, "name", "")


def _probe_queries(queries: list[tuple[str, str]], limit: int) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    resolved_count = 0
    matched_ids: set[str] = set()

    with CosineClient() as client:
        for expected_artist, query in queries[:limit]:
            item: dict[str, Any] = {
                "expected_artist": expected_artist,
                "query": query,
                "resolved": False,
                "match": None,
            }
            try:
                results = client.search(query, limit=3)
            except CosineClientError as exc:
                item["error"] = str(exc)
                items.append(item)
                continue

            candidates = []
            for result in results:
                candidate = {
                    "artist": result.artist,
                    "track": _track_title(result),
                    "id": result.id,
                    "artist_match": _artist_matches(expected_artist, result.artist),
                }
                candidates.append(candidate)
                if item["match"] is None and candidate["artist_match"]:
                    item["match"] = candidate
                    item["resolved"] = True

            item["candidates"] = candidates
            if item["resolved"]:
                resolved_count += 1
                match = item["match"]
                if isinstance(match, dict) and match.get("id"):
                    matched_ids.add(str(match["id"]))
            items.append(item)

    diagnosis = "healthy"
    if queries:
        ratio = resolved_count / min(len(queries), limit)
        if resolved_count < 3 or ratio < 0.4:
            diagnosis = "thin_or_poorly_indexed"
        elif matched_ids and len(matched_ids) <= max(1, resolved_count // 3):
            diagnosis = "repeat_heavy"

    return {
        "inspected_count": min(len(queries), limit),
        "resolved_count": resolved_count,
        "unique_match_count": len(matched_ids),
        "diagnosis": diagnosis,
        "items": items,
    }


def _label_summary(url: str, limit: int, with_cosine: bool) -> dict[str, Any]:
    label_id = extract_label_id(url)
    if not label_id:
        raise ValueError(f"Could not parse label ID from {url}")

    info = fetch_label_info(label_id)
    releases = fetch_label_releases(label_id, limit=limit)
    payload = {
        "kind": "label",
        "url": url,
        "label": {
            "id": info.id,
            "name": info.name,
            "release_count": info.release_count,
        },
        "recent_releases": [
            {
                "id": release.id,
                "artist": release.artist,
                "title": release.title,
                "year": release.year,
                "catno": release.catno,
            }
            for release in releases
        ],
    }
    if with_cosine:
        queries = [(release.artist, f"{release.artist} {release.title}".strip()) for release in releases]
        payload["cosine_probe"] = _probe_queries(queries, limit=limit)
    return payload


def _artist_summary(url: str, limit: int, with_cosine: bool) -> dict[str, Any]:
    artist_id = extract_artist_id(url)
    if not artist_id:
        raise ValueError(f"Could not parse artist ID from {url}")

    info = fetch_artist_info(artist_id)
    releases = fetch_artist_releases(artist_id, limit=limit)
    payload = {
        "kind": "artist",
        "url": url,
        "artist": {
            "id": info.id,
            "name": info.name,
        },
        "releases": [
            {
                "id": release.id,
                "artist": release.artist,
                "title": release.title,
                "year": release.year,
                "role": release.role,
                "type": release.type,
            }
            for release in releases
        ],
    }
    if with_cosine:
        queries = [(release.artist, f"{release.artist} {release.title}".strip()) for release in releases]
        payload["cosine_probe"] = _probe_queries(queries, limit=limit)
    return payload


def _release_summary(url: str, limit: int, with_cosine: bool) -> dict[str, Any]:
    artists = fetch_release_artists(url)
    tracks = fetch_release_tracks(url)
    payload: dict[str, Any] = {
        "kind": "release",
        "url": url,
        "artists": [
            {
                "id": artist.id,
                "name": artist.name,
            }
            for artist in artists
        ],
        "tracks": [
            {
                "artist": track.artist,
                "title": track.title,
                "query": track.query,
            }
            for track in tracks[:limit]
        ],
    }

    try:
        credits = fetch_release_credits(url)
    except DiscogsError as exc:
        payload["credits_error"] = str(exc)
    else:
        payload["credits"] = [
            {
                "id": credit.id,
                "name": credit.name,
                "role": credit.role_normalized,
            }
            for credit in credits[:limit]
        ]

    if with_cosine:
        queries = [(track.artist, track.query) for track in tracks[:limit]]
        payload["cosine_probe"] = _probe_queries(queries, limit=limit)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect a Discogs artist, label, or release and optionally probe cosine resolvability."
    )
    parser.add_argument("url", help="Discogs artist, label, release, or master URL")
    parser.add_argument("--limit", type=int, default=10, help="Maximum releases or tracks to inspect")
    parser.add_argument(
        "--with-cosine",
        action="store_true",
        help="Also probe cosine.club search resolvability for the inspected releases or tracks",
    )
    args = parser.parse_args()

    try:
        kind = _detect_kind(args.url)
        if kind == "label":
            payload = _label_summary(args.url, args.limit, args.with_cosine)
        elif kind == "artist":
            payload = _artist_summary(args.url, args.limit, args.with_cosine)
        else:
            payload = _release_summary(args.url, args.limit, args.with_cosine)
    except (DiscogsError, CosineClientError, ValueError) as exc:
        raise SystemExit(str(exc))

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
