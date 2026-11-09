"""
Taste DB: infer taste from a local music library for use as a discovery input
to cosine-digger.

The taste DB is a seed bank, not a filter. It answers questions like:
- What artists/labels/genres do I buy from?
- What have I bought recently?
- What tracks in my library could be good seeds for a dig?

Safety guarantees:
- NEVER writes to the music directory
- NEVER calls mutagen.save() or any tag-writing function
- All output (manifest, SQLite DB) goes to a user-specified path outside the music dir
- File timestamps are verified unchanged after reading
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import warnings
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import storage
# Retain the historical schema imports for existing session callers.
from .storage import _SCHEMA, _SESSION_SCHEMA


# ---------------------------------------------------------------------------
# Manifest: read-only scan of music files → JSONL text artifact
# ---------------------------------------------------------------------------

@dataclass
class ManifestEntry:
    path: str
    filename: str
    folder: str
    size_bytes: int
    mtime: str  # ISO 8601
    artist: str | None = None
    album: str | None = None
    title: str | None = None
    track_no: str | None = None
    label: str | None = None
    year: str | None = None
    genre: str | None = None
    duration_s: float | None = None
    bitrate: int | None = None
    source: str = "filename"  # "filename" or "id3"
    title_source: str | None = None  # "id3", "filename_fallback", "album_match"


AUDIO_EXTENSIONS = {".mp3", ".flac", ".wav", ".aiff", ".aif", ".ogg", ".m4a", ".opus"}

# Folder names that should not be used as album fallback
_SKIP_FOLDERS = frozenset({
    "aiff", "mp3", "flac", "wav", "tracks", "music", "ambient",
    "other", "downloads", "soundcloud",
})


def _parse_from_filename(filepath: Path) -> ManifestEntry:
    """Extract what we can from the filesystem alone (no tag reading)."""
    stat = os.stat(filepath)
    mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return ManifestEntry(
        path=str(filepath),
        filename=filepath.name,
        folder=filepath.parent.name,
        size_bytes=stat.st_size,
        mtime=mtime_dt.isoformat(),
        source="filename",
    )


def _parse_from_filename_pattern(filename: str) -> dict[str, str]:
    """Extract artist, title, album, track_no from common filename patterns.

    Returns a dict with whatever fields could be extracted. Empty dict if no match.

    Patterns handled (in priority order):
      "Artist - Album - NN Title.ext"   → artist, album, title, track_no
      "NN_Artist_Title.ext"             → artist, title, track_no
      "NN Artist - Title.ext"           → artist, title, track_no
      "Artist - Title.ext"              → artist, title
    """
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    # Pattern 1: "Artist - Album - NN Title" (AIFF 3-part, Bandcamp)
    m = re.match(r"^(.+?)\s+-\s+(.+?)\s+-\s+(\d+)\s+(.+)$", stem)
    if m:
        return {
            "artist": m.group(1).strip(),
            "album": m.group(2).strip(),
            "title": m.group(4).strip(),
            "track_no": m.group(3),
        }

    # Pattern 2: "NN_Artist_Title" (Bandcamp mp3)
    m = re.match(r"^(\d+)_([^_]+)_(.+)$", stem)
    if m:
        return {
            "artist": m.group(2).replace("_", " ").strip(),
            "title": m.group(3).replace("_", " ").strip(),
            "track_no": m.group(1),
        }

    # Pattern 3: "NN Artist - Title" (leading track number)
    m = re.match(r"^(\d+)\s+(.+?)\s+-\s+(.+)$", stem)
    if m:
        return {
            "artist": m.group(2).strip(),
            "title": m.group(3).strip(),
            "track_no": m.group(1),
        }

    # Pattern 4: "Artist - Title" (simple 2-part, space-dash-space)
    m = re.match(r"^(.+?)\s+-\s+(.+)$", stem)
    if m:
        return {"artist": m.group(1).strip(), "title": m.group(2).strip()}

    return {}


_tag_reader_warning_emitted = False


def _enrich_from_id3(entry: ManifestEntry) -> ManifestEntry:
    """Read ID3/Vorbis tags via mutagen. Read-only — never calls save().

    Verifies file mtime is unchanged after reading.
    """
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        global _tag_reader_warning_emitted
        if not _tag_reader_warning_emitted:
            _tag_reader_warning_emitted = True
            warnings.warn("tag reader unavailable — filename-only scan", stacklevel=2)
        return entry

    filepath = entry.path
    pre_stat = os.stat(filepath)

    try:
        audio = MutagenFile(filepath, easy=True)
    except Exception:
        return entry  # unreadable; leave as filename-only

    if audio is None:
        return entry

    # Verify no timestamp change
    post_stat = os.stat(filepath)
    if pre_stat.st_mtime_ns != post_stat.st_mtime_ns:
        raise RuntimeError(
            f"SAFETY VIOLATION: mtime changed after reading {filepath}. "
            f"Before: {pre_stat.st_mtime_ns}, After: {post_stat.st_mtime_ns}"
        )

    # Extract tags — handle both EasyID3 keys ("artist") and raw ID3
    # frame names ("TPE1"). AIFF files return raw frames even with easy=True.
    # Map: easy key → raw ID3 frame name
    _RAW_KEYS = {
        "artist": "TPE1", "album": "TALB", "title": "TIT2",
        "tracknumber": "TRCK", "organization": "TPUB", "label": "TPUB",
        "date": "TDRC", "genre": "TCON",
    }

    def _first(key: str) -> str | None:
        vals = audio.get(key)
        if vals:
            return str(vals[0]) if isinstance(vals, list) else str(vals)
        # Fallback: try raw ID3 frame name (for AIFF, WAV)
        raw_key = _RAW_KEYS.get(key)
        if raw_key:
            frame = audio.get(raw_key)
            if frame:
                # Raw ID3 frames have a .text list
                if hasattr(frame, "text") and frame.text:
                    return str(frame.text[0])
                return str(frame)
        return None

    entry.artist = _first("artist") or entry.artist
    entry.album = _first("album") or entry.album
    entry.title = _first("title") or entry.title
    entry.track_no = _first("tracknumber") or entry.track_no
    entry.label = _first("organization") or _first("label") or entry.label
    if not entry.label and Path(filepath).suffix.lower() == ".m4a":
        # EasyMP4 omits publisher/freeform tags. Inspect this file's raw
        # atoms without registering global mappings or writing any tags.
        try:
            raw = MutagenFile(filepath, easy=False)
            for key in ("----:com.apple.iTunes:LABEL", "----:com.apple.iTunes:Label",
                        "----:com.apple.iTunes:label", "----:com.apple.iTunes:publisher", "©pub"):
                for value in raw.get(key, []) if raw is not None else []:
                    text = (value.decode("utf-8", errors="replace")
                            if isinstance(value, bytes) else str(value)).strip()
                    if text:
                        entry.label = text
                        break
                if entry.label:
                    break
        except Exception:
            pass  # Other readable tags still belong in the manifest.
        if pre_stat.st_mtime_ns != os.stat(filepath).st_mtime_ns:
            raise RuntimeError(f"SAFETY VIOLATION: mtime changed after reading {filepath}")
    entry.year = _first("date") or entry.year
    entry.genre = _first("genre") or entry.genre

    if hasattr(audio, "info") and audio.info:
        entry.duration_s = getattr(audio.info, "length", None)
        entry.bitrate = getattr(audio.info, "bitrate", None)

    entry.source = "id3"
    entry.title_source = "id3"

    # --- Filename fallback: detect title==album and fill missing fields ---
    fn_parsed = _parse_from_filename_pattern(entry.filename)

    # Bug fix 1: When title == album, the tag title is probably the album name,
    # not the track name. Try the filename for a better title.
    if (
        entry.title
        and entry.album
        and entry.title.strip().lower() == entry.album.strip().lower()
        and fn_parsed.get("title")
    ):
        fn_title = fn_parsed["title"]
        if fn_title.strip().lower() != entry.album.strip().lower():
            # Filename gives a different title — use it
            entry.title = fn_title
            entry.title_source = "filename_fallback"
        else:
            # Filename gives the same title — it's a genuine album-match
            entry.title_source = "album_match"

    # Bug fix 2: Fill any still-missing fields from filename parse
    if not entry.title and fn_parsed.get("title"):
        entry.title = fn_parsed["title"]
        entry.title_source = "filename_fallback"
    if not entry.artist and fn_parsed.get("artist"):
        entry.artist = fn_parsed["artist"]
    if not entry.album and fn_parsed.get("album"):
        entry.album = fn_parsed["album"]
    if not entry.album and entry.folder:
        # Use parent folder name as album fallback, but skip generic format dirs
        if entry.folder.strip().lower() not in _SKIP_FOLDERS:
            entry.album = entry.folder
    if not entry.track_no and fn_parsed.get("track_no"):
        entry.track_no = fn_parsed["track_no"]

    return entry


def export_manifest(
    music_dir: str | Path,
    output_path: str | Path,
    read_tags: bool = False,
) -> int:
    """Scan a music directory (read-only) and write a JSONL manifest.

    Args:
        music_dir: Path to the music directory (treated as read-only).
        output_path: Where to write the manifest JSONL file.
                     MUST be outside music_dir.
        read_tags: If True, use mutagen to read embedded tags.
                   If False, only use filesystem metadata.

    Returns:
        Number of tracks exported.
    """
    music_dir = Path(music_dir).resolve()
    output_path = Path(output_path).resolve()

    # Safety: refuse to write inside the music directory
    if output_path.is_relative_to(music_dir):
        raise ValueError(
            f"Output path {output_path} is inside music directory {music_dir}. "
            "Refusing to write inside the music directory."
        )

    count = 0
    with open(output_path, "w") as f:
        for root, _dirs, files in os.walk(music_dir):
            for fname in sorted(files):
                filepath = Path(root) / fname
                if filepath.suffix.lower() not in AUDIO_EXTENSIONS:
                    continue

                entry = _parse_from_filename(filepath)
                if read_tags:
                    entry = _enrich_from_id3(entry)

                f.write(json.dumps(asdict(entry)) + "\n")
                count += 1

    return count


# ---------------------------------------------------------------------------
# Taste DB: manifest → SQLite with aggregated taste signals
# ---------------------------------------------------------------------------

def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Upgrade a taste DB through the shared, versioned storage migrations."""
    storage.migrate(conn)


def incremental_scan(
    music_dir: str | Path,
    db_path: str | Path,
    read_tags: bool = True,
    on_progress: callable | None = None,
) -> tuple[int, int]:
    """Scan a music directory and add only new files to the taste DB.

    Walks the directory (fast — just readdir), checks each path against the DB,
    and only reads tags for files not already present. Recomputes taste signals
    at the end.

    Args:
        music_dir: Path to the music directory (read-only).
        db_path: Path to the SQLite taste database (created if missing).
        read_tags: If True, use mutagen to read embedded tags.
        on_progress: Optional callback(new_count) called after each new file.

    Returns:
        (new_count, total_count) — files added this run and total in DB.
    """
    music_dir = Path(music_dir).resolve()
    db_path = Path(db_path)

    conn = storage.connect(db_path)
    _migrate_schema(conn)

    # Load known paths into a set — this is fast even for large libraries
    known_paths = {
        row[0] for row in conn.execute("SELECT path FROM tracks").fetchall()
    }

    new_count = 0
    for root, _dirs, files in os.walk(music_dir):
        for fname in sorted(files):
            filepath = Path(root) / fname
            if filepath.suffix.lower() not in AUDIO_EXTENSIONS:
                continue

            path_str = str(filepath)
            if path_str in known_paths:
                continue

            entry = _parse_from_filename(filepath)
            if read_tags:
                entry = _enrich_from_id3(entry)

            conn.execute(
                """INSERT OR REPLACE INTO tracks
                   (path, filename, folder, size_bytes, mtime,
                    artist, album, title, track_no, label, year, genre,
                    duration_s, bitrate, source, title_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry.path, entry.filename, entry.folder,
                    entry.size_bytes, entry.mtime,
                    entry.artist, entry.album, entry.title,
                    entry.track_no, entry.label, entry.year,
                    entry.genre, entry.duration_s, entry.bitrate,
                    entry.source, entry.title_source,
                ),
            )
            new_count += 1
            if on_progress:
                on_progress(new_count)

    if new_count > 0:
        _compute_signals(conn)
        conn.commit()

    total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    conn.close()
    return new_count, total


def build_taste_db(manifest_path: str | Path, db_path: str | Path) -> sqlite3.Connection:
    """Read a JSONL manifest and build a SQLite taste database.

    Args:
        manifest_path: Path to the JSONL manifest file.
        db_path: Where to create/overwrite the SQLite database.

    Returns:
        Open connection to the taste database.
    """
    manifest_path = Path(manifest_path)
    db_path = Path(db_path)

    conn = storage.connect(db_path)
    _migrate_schema(conn)

    # Load tracks from manifest
    with open(manifest_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            conn.execute(
                """INSERT OR REPLACE INTO tracks
                   (path, filename, folder, size_bytes, mtime,
                    artist, album, title, track_no, label, year, genre,
                    duration_s, bitrate, source, title_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    entry["path"], entry["filename"], entry["folder"],
                    entry["size_bytes"], entry["mtime"],
                    entry.get("artist"), entry.get("album"), entry.get("title"),
                    entry.get("track_no"), entry.get("label"), entry.get("year"),
                    entry.get("genre"), entry.get("duration_s"), entry.get("bitrate"),
                    entry.get("source"), entry.get("title_source"),
                ),
            )

    # Compute taste signals
    _compute_signals(conn)
    conn.commit()
    return conn


_VALID_SIGNAL_COLUMNS = {"artist", "label", "genre"}


def _compute_signals(conn: sqlite3.Connection) -> None:
    """Aggregate per-artist, per-label, per-genre taste signals with recency weighting."""
    now = datetime.now(tz=timezone.utc)
    cutoff_90d = (now - timedelta(days=90)).isoformat()
    cutoff_365d = (now - timedelta(days=365)).isoformat()

    conn.execute("DELETE FROM taste_signals")

    # Compute totals once (shared across all signal types)
    total_all = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    total_90d = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE mtime >= ?", (cutoff_90d,)
    ).fetchone()[0]
    total_365d = conn.execute(
        "SELECT COUNT(*) FROM tracks WHERE mtime >= ?", (cutoff_365d,)
    ).fetchone()[0]

    for signal_type, column in [("artist", "artist"), ("label", "label"), ("genre", "genre")]:
        if column not in _VALID_SIGNAL_COLUMNS:
            raise ValueError(f"Invalid signal column: {column}")

        # Single query per signal type: compute all window counts in one pass
        rows = conn.execute(
            f"""SELECT {column},
                       COUNT(*) as track_count,
                       COUNT(DISTINCT album) as album_count,
                       MIN(mtime) as earliest,
                       MAX(mtime) as latest,
                       SUM(CASE WHEN mtime >= ? THEN 1 ELSE 0 END) as count_90d,
                       SUM(CASE WHEN mtime >= ? THEN 1 ELSE 0 END) as count_365d
                FROM tracks
                WHERE {column} IS NOT NULL AND {column} != ''
                GROUP BY {column}""",
            (cutoff_90d, cutoff_365d),
        ).fetchall()

        for value, track_count, album_count, earliest, latest, count_90d, count_365d in rows:
            score_90d = count_90d / total_90d if total_90d > 0 else 0.0
            score_365d = count_365d / total_365d if total_365d > 0 else 0.0
            score_all = track_count / total_all if total_all > 0 else 0.0

            conn.execute(
                """INSERT OR REPLACE INTO taste_signals
                   (signal_type, signal_value, track_count, album_count,
                    earliest_mtime, latest_mtime,
                    recency_score_90d, recency_score_365d, recency_score_all)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (signal_type, value, track_count, album_count,
                 earliest, latest, score_90d, score_365d, score_all),
            )


# ---------------------------------------------------------------------------
# Discogs enrichment: fill in missing label/year/genre from Discogs API
# ---------------------------------------------------------------------------

def enrich_from_discogs(
    db_path: str | Path,
    max_age_days: int = 365,
    dry_run: bool = False,
    *,
    token: str | None = None,
    client=None,
    budget: int | None = None,
    cancelled=None,
    on_progress=None,
    resume: dict | None = None,
    quiet: bool = False,
) -> dict:
    """Enrich taste DB tracks with label/year/genre from Discogs search.

    For each unique (artist, album) pair in the taste DB that is:
    - missing label data
    - purchased within max_age_days

    Searches Discogs for the release and stores label, year, genre in:
    1. The discogs_cache table (permanent lookup cache)
    2. The tracks table (updates label, year, genre columns)

    Then recomputes taste_signals so hot_labels() works.

    NEVER writes anything to music files. All data stays in the SQLite DB.

    Args:
        db_path: Path to the taste SQLite database.
        max_age_days: Only enrich tracks purchased within this many days.
        dry_run: If True, print what would be looked up without calling API.

    Returns:
        The default CLI counts: looked_up, found, not_found, cached, errors.
        GUI collaborators select an all-age, missing-only run returning
        status and cumulative pair/track counters, with a fresh request budget.
        resume is the work checkpoint plus its running job_id; a callable
        client has make_rate_limited_client's factory signature.
    """
    # Opting into GUI collaborators leaves the historical positional CLI
    # path below (including its return counts and age window) unchanged.
    if (quiet or token is not None or client is not None or budget is not None
            or cancelled is not None or on_progress is not None or resume is not None):
        return _enrich_labels(db_path, token=token, client=client, budget=100 if budget is None else budget,
                              cancelled=cancelled, on_progress=on_progress,
                              resume=resume, dry_run=dry_run)

    import time
    import httpx
    from .config import load_env

    load_env()
    token = os.environ.get("DISCOGS_TOKEN")
    if not token and not dry_run:
        raise RuntimeError("DISCOGS_TOKEN not found in environment. Set it in .env")

    conn = storage.connect(db_path)
    conn.row_factory = sqlite3.Row

    _migrate_schema(conn)

    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)).isoformat()

    # Find unique (artist, album) pairs needing enrichment
    pairs = conn.execute(
        """SELECT DISTINCT artist, album FROM tracks
           WHERE (label IS NULL OR label = '')
             AND artist IS NOT NULL AND artist != ''
             AND album IS NOT NULL AND album != ''
             AND mtime >= ?
           ORDER BY mtime DESC""",
        (cutoff,),
    ).fetchall()

    stats = {"looked_up": 0, "found": 0, "not_found": 0, "cached": 0, "errors": 0}
    user_agent = os.environ.get("DISCOGS_USER_AGENT", "cosine-digger-taste/0.1")

    for row in pairs:
        artist, album = row["artist"], row["album"]

        # Check cache first
        cached = conn.execute(
            "SELECT * FROM discogs_cache WHERE artist = ? AND album = ?",
            (artist, album),
        ).fetchone()

        if cached:
            if cached["discogs_label"]:
                _apply_enrichment(conn, artist, album, cached)
                stats["cached"] += 1
            continue

        if dry_run:
            print(f"  [dry-run] Would search Discogs for: {artist} - {album}")
            stats["looked_up"] += 1
            continue

        # Search Discogs
        query = f"{artist} {album}"
        try:
            resp = httpx.get(
                "https://api.discogs.com/database/search",
                params={"q": query, "type": "release", "per_page": 3},
                headers={
                    "Authorization": f"Discogs token={token}",
                    "User-Agent": user_agent,
                },
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            stats["looked_up"] += 1
        except Exception as e:
            print(f"  [error] Discogs search failed for {artist} - {album}: {e}")
            stats["errors"] += 1
            continue

        if results:
            hit = results[0]
            label_list = hit.get("label", [])
            genre_list = hit.get("genre", []) + hit.get("style", [])
            enrichment = {
                "discogs_release_id": str(hit.get("id", "")),
                "discogs_label": label_list[0] if label_list else None,
                "discogs_label_id": None,  # would need a separate lookup
                "discogs_year": str(hit.get("year", "")) if hit.get("year") else None,
                "discogs_genre": ", ".join(genre_list[:3]) if genre_list else None,
                "discogs_url": hit.get("resource_url", ""),
            }

            # Store in cache
            conn.execute(
                """INSERT OR REPLACE INTO discogs_cache
                   (artist, album, discogs_release_id, discogs_label,
                    discogs_label_id, discogs_year, discogs_genre, discogs_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (artist, album, enrichment["discogs_release_id"],
                 enrichment["discogs_label"], enrichment["discogs_label_id"],
                 enrichment["discogs_year"], enrichment["discogs_genre"],
                 enrichment["discogs_url"]),
            )

            # Apply to tracks
            _apply_enrichment(conn, artist, album, enrichment)
            label_str = enrichment["discogs_label"] or "(none)"
            print(f"  [found] {artist} - {album} → label={label_str}")
            stats["found"] += 1
        else:
            # Cache the miss so we don't re-search
            conn.execute(
                """INSERT OR REPLACE INTO discogs_cache
                   (artist, album) VALUES (?, ?)""",
                (artist, album),
            )
            print(f"  [miss]  {artist} - {album} → no Discogs match")
            stats["not_found"] += 1

        # Respect Discogs rate limit (60 req/min)
        time.sleep(1.1)

    # Recompute taste signals with new label/genre data
    if stats["found"] > 0 or stats["cached"] > 0:
        _compute_signals(conn)

    conn.commit()
    conn.close()
    return stats


def _apply_enrichment(conn: sqlite3.Connection, artist: str, album: str, data,
                      *, only_missing: bool = False) -> int:
    """Update tracks table with enrichment data from Discogs."""
    label = data["discogs_label"] if isinstance(data, dict) else data["discogs_label"]
    year = data["discogs_year"] if isinstance(data, dict) else data["discogs_year"]
    genre = data["discogs_genre"] if isinstance(data, dict) else data["discogs_genre"]

    updates = []
    params = []
    if label:
        updates.append("label = ?")
        params.append(label)
    if year:
        updates.append("year = CASE WHEN year IS NULL OR trim(year) = '' THEN ? ELSE year END"
                       if only_missing else "year = ?")
        params.append(year)
    if genre:
        updates.append("genre = CASE WHEN genre IS NULL OR trim(genre) = '' THEN ? ELSE genre END"
                       if only_missing else "genre = ?")
        params.append(genre)

    if updates:
        params.extend([artist, album])
        cursor = conn.execute(
            f"UPDATE tracks SET {', '.join(updates)} WHERE artist = ? AND album = ?"
            + (" AND (label IS NULL OR trim(label) = '')" if only_missing else ""),
            params,
        )
        return cursor.rowcount if label else 0
    return 0


_MISSING_LABEL = "(label IS NULL OR trim(label) = '')"
_ELIGIBLE_LABEL = "artist IS NOT NULL AND trim(artist) != '' AND album IS NOT NULL AND trim(album) != ''"


def _label_candidates(conn: sqlite3.Connection) -> tuple[list[list[str]], list[int]]:
    pairs = conn.execute(
        f"SELECT artist,album FROM tracks WHERE {_MISSING_LABEL} AND {_ELIGIBLE_LABEL} "
        "GROUP BY artist,album ORDER BY MAX(mtime) DESC,artist,album"
    ).fetchall()
    ineligible = conn.execute(
        f"SELECT id FROM tracks WHERE {_MISSING_LABEL} AND NOT ({_ELIGIBLE_LABEL}) ORDER BY id"
    ).fetchall()
    return [list(pair) for pair in pairs], [row[0] for row in ineligible]


class _LabelHttpCache:
    """Stage successful HTTP bodies for the album/checkpoint transaction."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.pending = {}

    def get(self, key):
        if key in self.pending:
            return self.pending[key][0]
        with closing(storage.connect(self.db_path)) as conn:
            return storage.cache_get(conn, key)

    def put(self, key, body, ttl_s):
        import time
        self.pending[key] = (body, ttl_s, time.time())

    def flush(self, conn):
        conn.executemany(
            """INSERT INTO http_cache (key,body,ttl_s,fetched_at) VALUES (?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET body=excluded.body,
                 ttl_s=excluded.ttl_s,fetched_at=excluded.fetched_at""",
            [(key, body, ttl, at) for key, (body, ttl, at) in self.pending.items()],
        )


class _LabelCancelled(Exception):
    pass


def _label_counters(work: dict) -> dict:
    import math
    return {key: work[key] for key in (
        "processed_pairs", "total_pairs", "filled_tracks", "missing_tracks",
        "ineligible_tracks", "errors", "requests_used", "budget", "requests_total",
    )} | {"remaining_pairs": len(work["pending_pairs"]),
         "counts_scope": "cumulative", "requests_scope": "run",
         "estimated_seconds": math.ceil(1.1 * min(len(work["pending_pairs"]),
                                                   work["budget"] - work["requests_used"]))}


def _label_note(work: dict, code: str) -> dict:
    progress = _label_counters(work)
    params = {"i": progress["processed_pairs"], "n": progress["total_pairs"],
              "t": f"{progress['estimated_seconds']} s", "k": progress["filled_tracks"],
              "u": progress["missing_tracks"]}
    messages = {
        "S-SET-FILL-RUN": "Looking up labels on Discogs: {i} of {n}, about {t} left.",
        "S-SET-FILL-DONE": "✓ Filled {k} labels. Discogs had no label for {u} tracks.",
        "S-SET-FILL-STOPPED": "Stopped after {i} of {n}; {k} labels filled. The next run picks up here.",
        "S-SET-FILL-ERR": "! Discogs isn't answering. {k} labels filled so far; try again later.",
        "S-SET-FILL-NOTOKEN": "Needs a Discogs token (Keys, above), so this is skipped.",
        "library_labels_ineligible": "Skipped {u} tracks without an artist or album.",
    }
    if code == "library_labels_ineligible":
        params["u"] = progress["ineligible_tracks"]
    return {"code": code, "params": params, "message": messages[code].format(**params),
            "phase": "enrichment", "progress": progress}


_label_checkpoint_redactor = ContextVar("label_checkpoint_redactor", default=None)


@contextmanager
def label_checkpoint_redaction(redact):
    """Carry the runtime's redactor through the fixed library adapter contract."""
    token = _label_checkpoint_redactor.set(redact)
    try:
        yield
    finally:
        _label_checkpoint_redactor.reset(token)


def label_checkpoint_work(conn, work):
    """Upgrade metadata identities before redaction, retaining existing row IDs."""
    work = dict(work)
    for prefix in ("pending", "completed"):
        identities = f"{prefix}_pair_ids"
        if identities not in work and f"{prefix}_pairs" in work:
            work[identities] = []
            for artist, album in work[f"{prefix}_pairs"]:
                row_id = conn.execute("SELECT MIN(id) FROM tracks WHERE artist=? AND album=?",
                                      (artist, album)).fetchone()[0]
                if row_id is not None:
                    work[identities].append(row_id)
    return work


def _enrich_labels(db_path, *, token, client, budget, cancelled, on_progress, resume, dry_run):
    """The GUI path: short transactions, no file writes and durable album work.

    A callable client uses make_rate_limited_client's keyword/tuple contract.
    An injected (client, tracker) is caller-owned; a factory result is owned here.
    resume carries the saved work plus job_id; request identity stays in storage.
    """
    from . import chain, discogs
    from .config import usable_key
    from .seeds import _fold, _norm_artist

    if type(budget) is not int or not 1 <= budget <= 500:
        raise ValueError("label enrichment budget must be an integer from 1 to 500")
    cancelled = cancelled or (lambda: False)
    work = dict(resume or {})
    job_id = work.pop("job_id", None)
    with closing(storage.connect(db_path)) as conn:
        storage.migrate(conn)
        pairs, ineligible_ids = _label_candidates(conn)
        work = label_checkpoint_work(conn, work)
        rows = conn.execute(f"SELECT id,artist,album,{_MISSING_LABEL} FROM tracks "
                            f"WHERE {_ELIGIBLE_LABEL} ORDER BY id DESC").fetchall()
    pairs_by_id = {row_id: [artist, album] for row_id, artist, album, _ in rows}
    ids_by_pair = {(artist, album): row_id for row_id, artist, album, _ in rows}
    missing_by_pair = {}
    for row_id, artist, album, missing in rows:
        if missing:
            missing_by_pair.setdefault((artist, album), set()).add(row_id)
    # Retain row identities for completed misses: new rows reopen the album,
    # while old missing rows must not inflate cumulative counts on resume.
    completed_missing_ids = dict(work.get("completed_missing_ids", {}))
    completed_ids = list(work.get("completed_pair_ids", []))
    pending_ids = [row_id for row_id in work.get("pending_pair_ids", []) if row_id in pairs_by_id]
    completed = [pairs_by_id[row_id] for row_id in completed_ids if row_id in pairs_by_id]
    pending = [pairs_by_id[row_id] for row_id in pending_ids]
    seen = {tuple(pair) for pair in pending}
    for pair in completed:
        key = tuple(pair)
        if not missing_by_pair.get(key, set()) - set(completed_missing_ids.get(str(ids_by_pair[key]), [])):
            seen.add(key)
    for pair in pairs:
        if tuple(pair) not in seen:
            pending.append(pair)
            pending_ids.append(ids_by_pair[tuple(pair)])
    ineligible_ids = sorted(set(work.get("ineligible_ids", [])) | set(ineligible_ids))
    work.update(pending_pairs=pending, completed_pairs=completed,
                pending_pair_ids=pending_ids, completed_pair_ids=completed_ids,
                completed_missing_ids=completed_missing_ids,
                cursor=len(completed_ids), processed_pairs=len(completed_ids), total_pairs=len(completed_ids) + len(pending),
                ineligible_ids=ineligible_ids, ineligible_tracks=len(ineligible_ids), budget=budget,
                requests_used=0, in_flight_pair=None, in_flight_pair_id=None, reason=None)
    for key in ("filled_tracks", "missing_tracks", "errors", "requests_total"):
        work.setdefault(key, 0)
    cache = _LabelHttpCache(db_path)

    def save(conn):
        if job_id:
            storage.update_slice_progress(conn, job_id, {"work": work | _label_counters(work)},
                                          redact=_label_checkpoint_redactor.get() or (lambda value: value))

    def checkpoint():
        with closing(storage.connect(db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with conn:
                save(conn)

    def signals():
        if work.get("signals_pending"):
            with closing(storage.connect(db_path)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                with conn:
                    _compute_signals(conn)
                    work["signals_pending"] = False
                    save(conn)

    def notify(code):
        note = _label_note(work, code)
        if on_progress:
            on_progress(note)
        return note

    def finish(reason=None):
        work["reason"] = reason
        work["in_flight_pair"] = None
        work["in_flight_pair_id"] = None
        checkpoint()
        code = ("S-SET-FILL-ERR" if reason == "error" else
                "S-SET-FILL-NOTOKEN" if reason == "no_token" else
                "S-SET-FILL-STOPPED" if reason else "S-SET-FILL-DONE")
        result = {"status": "stopped" if reason else "done", **_label_counters(work),
                  "reason": reason, "note": notify(code)}
        if reason == "no_token":
            result["skipped"] = True
        if reason == "error":
            result["error"] = {"code": "discogs_unavailable", "message": result["note"]["message"],
                               "retry_after_s": None, "retry_at": None}
        return result

    checkpoint()
    signals()  # Repair a restart between a committed fill and signal refresh.
    try:
        token = usable_key(token, "DISCOGS_TOKEN")
    except ValueError:
        work["errors"] += 1
        return finish("error")
    if not token:
        return finish("no_token")
    if dry_run:
        return finish("dry_run")
    if ineligible_ids:
        notify("library_labels_ineligible")

    resource = None
    owned = False
    original_send = original_cache = original_transport = None
    try:
        notify("S-SET-FILL-RUN")
        while pending:
            if cancelled():
                return finish("cancelled")
            artist, album = pending[0]
            with closing(storage.connect(db_path)) as conn:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM tracks WHERE artist=? AND album=? AND {_MISSING_LABEL}",
                    (artist, album),
                ).fetchone()[0]
                conn.row_factory = sqlite3.Row
                cached = conn.execute("SELECT * FROM discogs_cache WHERE artist=? AND album=?",
                                      (artist, album)).fetchone()
            # Legacy positive rows have no release-match provenance. Recheck
            # the search payload (the HTTP cache retains its result titles),
            # using the existing request budget if it needs to be fetched.
            if cached is not None and cached["discogs_label"]:
                cached = None
            enrichment = dict(cached) if cached is not None else None
            if count and cached is None:
                if resource is None:
                    factory = client if callable(client) else chain.make_rate_limited_client
                    supplied = (factory(budget=budget, http_cache=cache, token=token)
                                if client is None or callable(client) else client)
                    owned = client is None or callable(client)
                    if isinstance(supplied, tuple):
                        resource, tracker = supplied
                    else:
                        resource = supplied
                        tracker = getattr(resource._transport, "_budget_tracker", None) or chain.BudgetTracker(budget)
                    transport = resource._transport
                    if not isinstance(transport, chain._RetryOn429Transport):
                        original_transport = transport
                        transport = chain._RetryOn429Transport(
                            wrapped=transport, budget_tracker=tracker, http_cache=cache, token=token,
                        )
                        resource._transport = transport
                    original_send, original_cache = transport._send, transport._http_cache
                    transport._http_cache = cache

                    def send(request, backoff=0.0):
                        # S8 precedes S4: use the same _send seam as the S2
                        # queue tests, keeping cache/retry and the single pacer.
                        if cancelled():
                            raise _LabelCancelled
                        with discogs.FactsQueue.shared().request_slot(priority="library", cancelled=cancelled):
                            if cancelled():
                                raise _LabelCancelled
                            if tracker.remaining <= 0 or work["requests_used"] >= budget:
                                raise chain.BudgetExhausted
                            discogs.pace(backoff)
                            if cancelled():
                                raise _LabelCancelled
                            tracker.spend()
                            work["requests_used"] += 1
                            work["requests_total"] += 1
                            work["in_flight_pair"] = pending[0]
                            work["in_flight_pair_id"] = pending_ids[0]
                            checkpoint()  # Reserve before an in-flight request.
                            request.headers["Authorization"] = f"Discogs token={token}"
                            return transport._wrapped.handle_request(request)

                    transport._send = send
                response = resource.get(
                    f"{discogs.BASE_URL}/database/search",
                    params={"q": f"{artist} {album}", "type": "release", "per_page": 3},
                    headers={"User-Agent": discogs.DEFAULT_USER_AGENT}, timeout=10,
                )
                response.raise_for_status()
                payload = response.json()
                results = payload.get("results") if isinstance(payload, dict) else None
                if not isinstance(results, list) or any(not isinstance(hit, dict) for hit in results):
                    raise ValueError("Invalid Discogs search response")
                wanted_artist, wanted_album = _norm_artist(artist), _fold(album)
                hit = {}
                for candidate in results:
                    title = candidate.get("title")
                    if not isinstance(title, str):
                        continue
                    found_artist, separator, found_album = title.partition(" - ")
                    if (separator and _norm_artist(found_artist) == wanted_artist
                            and _fold(found_album) == wanted_album):
                        hit = candidate
                        break
                labels = hit.get("label", [])
                genres = hit.get("genre", []) + hit.get("style", [])
                if (not isinstance(labels, list) or not isinstance(genres, list)
                        or any(not isinstance(value, str) for value in labels + genres)):
                    raise ValueError("Invalid Discogs labels or genres")
                enrichment = {"discogs_release_id": str(hit.get("id", "")),
                              "discogs_label": next((label.strip() for label in labels if label.strip()), None),
                              "discogs_label_id": None,
                              "discogs_year": str(hit["year"]) if hit.get("year") else None,
                              "discogs_genre": ", ".join(genres[:3]) or None,
                              "discogs_url": hit.get("resource_url", "")}

            with closing(storage.connect(db_path)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                with conn:
                    # Recheck after HTTP: tags may have arrived while we waited.
                    missing_ids = [row[0] for row in conn.execute(
                        f"SELECT id FROM tracks WHERE artist=? AND album=? AND {_MISSING_LABEL} ORDER BY id",
                        (artist, album),
                    )]
                    pair_id = str(pending_ids[0])
                    if enrichment is not None:
                        if cached is None:
                            conn.execute(
                                """INSERT OR REPLACE INTO discogs_cache
                                   (artist,album,discogs_release_id,discogs_label,discogs_label_id,
                                    discogs_year,discogs_genre,discogs_url) VALUES (?,?,?,?,?,?,?,?)""",
                                (artist, album, *(enrichment[key] for key in (
                                    "discogs_release_id", "discogs_label", "discogs_label_id",
                                    "discogs_year", "discogs_genre", "discogs_url"))),
                            )
                        if enrichment["discogs_label"]:
                            filled = _apply_enrichment(conn, artist, album, enrichment, only_missing=True)
                            work["filled_tracks"] += filled
                            work["signals_pending"] = work.get("signals_pending", False) or filled > 0
                            missing_ids = []
                        else:
                            work["missing_tracks"] += len(set(missing_ids) - set(completed_missing_ids.get(pair_id, [])))
                    completed_missing_ids[pair_id] = missing_ids
                    completed.append(pending.pop(0))
                    completed_ids.append(pending_ids.pop(0))
                    work.update(cursor=len(completed_ids), processed_pairs=len(completed_ids),
                                in_flight_pair=None, in_flight_pair_id=None)
                    cache.flush(conn)
                    save(conn)
            cache.pending.clear()
            signals()
            notify("S-SET-FILL-RUN")
        return finish("cancelled" if cancelled() else None)
    except (chain.BudgetExhausted, _LabelCancelled, discogs._FactsCancelled) as exc:
        cache.pending.clear()
        return finish("budget" if isinstance(exc, chain.BudgetExhausted) else "cancelled")
    except sqlite3.Error:
        # A rolled-back group still belongs to the persisted pending work.
        # Let the runtime fail the job; do not checkpoint in-memory counts
        # from a transaction that did not commit.
        raise
    except Exception:
        # Never persist provider exceptions: they may include headers or keys.
        cache.pending.clear()
        work["errors"] += 1
        return finish("error")
    finally:
        if resource is not None:
            if original_send is not None:
                resource._transport._send = original_send
                resource._transport._http_cache = original_cache
            if original_transport is not None:
                resource._transport = original_transport
            if owned:
                resource.close()


# ---------------------------------------------------------------------------
# Artist normalization (shared with cli.py for gap detection)
# ---------------------------------------------------------------------------

def normalize_artist(name: str) -> str:
    """Normalize artist name for matching — lowercase, strip (2)/(3), collapse whitespace."""
    s = re.sub(r"\s*\(\d+\)\s*$", "", name.strip().lower())
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------------------
# Query interface
# ---------------------------------------------------------------------------

@dataclass
class TasteSignal:
    signal_type: str
    signal_value: str
    track_count: int
    album_count: int
    score_90d: float
    score_365d: float
    score_all: float


_VALID_WINDOWS = {"90d": "recency_score_90d", "365d": "recency_score_365d", "all": "recency_score_all"}


class TasteProfile:
    """Read-only interface to a taste database.

    Use this to query your library and generate cosine-digger commands:
    - generate_dig_seeds() → pick diverse seeds from recent purchases
    - hot_labels() → identify labels you buy from frequently
    - bridge_seeds() → find seeds that bridge different artist pockets
    - owned_artists() → set of artists for post-filtering results
    """

    def __init__(self, db_path: str | Path):
        self.conn = storage.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        _migrate_schema(self.conn)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _score_col(self, window: str) -> str:
        if window not in _VALID_WINDOWS:
            raise ValueError(f"Invalid window '{window}'; expected one of {set(_VALID_WINDOWS)}")
        return _VALID_WINDOWS[window]

    def get_signal(
        self, signal_type: str, value: str, window: str = "all"
    ) -> float:
        """Look up the recency-weighted score for a signal.

        Args:
            signal_type: "artist", "label", or "genre"
            value: The artist name, label name, etc.
            window: "90d", "365d", or "all"

        Returns:
            Score in [0.0, 1.0] — fraction of tracks in that window matching this value.
            Returns 0.0 if no match.
        """
        score_col = self._score_col(window)

        row = self.conn.execute(
            f"SELECT {score_col} FROM taste_signals WHERE signal_type = ? AND signal_value = ?",
            (signal_type, value),
        ).fetchone()

        return float(row[0]) if row else 0.0

    def get_all_signals(self, signal_type: str, window: str = "all", min_score: float = 0.0) -> list[TasteSignal]:
        """Get all signals of a type, sorted by score descending."""
        score_col = self._score_col(window)

        rows = self.conn.execute(
            f"""SELECT signal_type, signal_value, track_count, album_count,
                       recency_score_90d, recency_score_365d, recency_score_all
                FROM taste_signals
                WHERE signal_type = ? AND {score_col} >= ?
                ORDER BY {score_col} DESC""",
            (signal_type, min_score),
        ).fetchall()

        return [
            TasteSignal(
                signal_type=r["signal_type"],
                signal_value=r["signal_value"],
                track_count=r["track_count"],
                album_count=r["album_count"],
                score_90d=r["recency_score_90d"],
                score_365d=r["recency_score_365d"],
                score_all=r["recency_score_all"],
            )
            for r in rows
        ]

    def owned_artists(self) -> set[str]:
        """Return the set of all artist names in the library (case-preserved)."""
        rows = self.conn.execute(
            "SELECT DISTINCT signal_value FROM taste_signals WHERE signal_type = 'artist'"
        ).fetchall()
        return {r[0] for r in rows}

    def labels_for_artist(self, artist: str) -> set[str]:
        """Return all labels associated with a given artist in the library."""
        rows = self.conn.execute(
            "SELECT DISTINCT label FROM tracks WHERE artist = ? AND label IS NOT NULL AND label != ''",
            (artist,),
        ).fetchall()
        return {r[0] for r in rows}

    def artists_on_label(self, label: str) -> set[str]:
        """Return all artists the user owns from a given label."""
        rows = self.conn.execute(
            "SELECT DISTINCT artist FROM tracks WHERE label = ? AND artist IS NOT NULL AND artist != ''",
            (label,),
        ).fetchall()
        return {r[0] for r in rows}

    def summary(self, window: str = "all", top_n: int = 10) -> dict:
        """Return a human-readable summary of the taste profile."""
        result = {}
        for signal_type in ("artist", "label", "genre"):
            signals = self.get_all_signals(signal_type, window)[:top_n]
            result[signal_type] = [
                {"value": s.signal_value, "tracks": s.track_count, "albums": s.album_count,
                 "score": round(getattr(s, f"score_{window}"), 4)}
                for s in signals
            ]
        return result

    # ------------------------------------------------------------------
    # Seed generation: use the library to drive cosine-digger searches
    # ------------------------------------------------------------------

    def recent_tracks(self, window: str = "365d", max_per_artist: int = 0) -> list[dict]:
        """Return tracks purchased within a time window, newest first.

        Args:
            window: "90d", "365d", or "all"
            max_per_artist: If > 0, cap tracks returned per artist.

        Returns:
            List of dicts with artist, album, title, mtime, folder.
        """
        cutoffs = {
            "90d": (datetime.now(tz=timezone.utc) - timedelta(days=90)).isoformat(),
            "365d": (datetime.now(tz=timezone.utc) - timedelta(days=365)).isoformat(),
            "all": "1970-01-01T00:00:00+00:00",
        }
        if window not in cutoffs:
            raise ValueError(f"Invalid window '{window}'; expected one of {set(cutoffs)}")
        cutoff = cutoffs[window]

        rows = self.conn.execute(
            """SELECT artist, album, title, mtime, folder
               FROM tracks
               WHERE artist IS NOT NULL AND artist != ''
                 AND title IS NOT NULL AND title != ''
                 AND mtime >= ?
               ORDER BY mtime DESC""",
            (cutoff,),
        ).fetchall()

        results = []
        artist_counts: dict[str, int] = {}
        for r in rows:
            artist = r["artist"]
            if max_per_artist > 0:
                count = artist_counts.get(artist, 0)
                if count >= max_per_artist:
                    continue
                artist_counts[artist] = count + 1
            results.append({
                "artist": artist,
                "album": r["album"],
                "title": r["title"],
                "mtime": r["mtime"],
                "folder": r["folder"],
            })
        return results

    def generate_dig_seeds(
        self, window: str = "365d", max_seeds: int = 6, max_per_artist: int = 2,
        verified_only: bool = True,
    ) -> list[str]:
        """Pick diverse seeds from recent purchases for a cosine-digger dig.

        Selects tracks that maximize artist diversity within the time window,
        favoring recent purchases. Returns "Artist - Title" strings ready
        for the `dig` command.

        Args:
            window: Time window ("90d", "365d", "all")
            max_seeds: Maximum number of seeds to return
            max_per_artist: Maximum tracks from any single artist
            verified_only: If True, only include tracks verified to resolve on cosine.club.
                          Falls back to unverified if no verified tracks exist.

        Returns:
            List of "Artist - Title" seed strings.
        """
        tracks = self.recent_tracks(window=window, max_per_artist=max_per_artist)

        if verified_only:
            verified_pairs = {
                (r[0], r[1])
                for r in self.conn.execute(
                    "SELECT artist, title FROM tracks WHERE cosine_verified = 1"
                ).fetchall()
            }
            verified = [
                t for t in tracks
                if (t["artist"], t["title"]) in verified_pairs
            ]
            if verified:
                tracks = verified

        seeds = []
        for t in tracks:
            if len(seeds) >= max_seeds:
                break
            seed = f"{t['artist']} - {t['title']}"
            seeds.append(seed)
        return seeds

    def _is_verified(self, artist: str, title: str) -> bool:
        """Check if a track is verified to resolve on cosine.club."""
        row = self.conn.execute(
            "SELECT cosine_verified FROM tracks WHERE artist = ? AND title = ? LIMIT 1",
            (artist, title),
        ).fetchone()
        return bool(row and row[0] == 1)

    def generate_dig_command(
        self, window: str = "365d", max_seeds: int = 6, max_per_artist: int = 2,
        limit: int = 20, deep: bool = True, verified_only: bool = True,
    ) -> str:
        """Generate a ready-to-run cosine-digger dig command from recent purchases.

        Returns:
            A shell command string like:
            uv run cosine-digger dig "Artist - Track" "Artist2 - Track2" --deep --limit 20
        """
        seeds = self.generate_dig_seeds(window, max_seeds, max_per_artist, verified_only=verified_only)
        if not seeds:
            return "# No tracks found in the taste DB for this time window."
        quoted = " ".join(f'"{s}"' for s in seeds)
        flags = []
        if deep:
            flags.append("--deep")
        flags.append(f"--limit {limit}")
        return f"uv run cosine-digger dig {quoted} {' '.join(flags)}"

    def hot_labels(self, window: str = "365d", min_tracks: int = 1) -> list[TasteSignal]:
        """Identify labels the user buys from frequently within a time window.

        Returns labels sorted by recency-weighted score, filtered to those
        with at least min_tracks in the window.
        """
        signals = self.get_all_signals("label", window)
        return [s for s in signals if s.track_count >= min_tracks]

    def generate_label_dig_commands(
        self, window: str = "365d", min_tracks: int = 2,
        limit: int = 15, deep: bool = True,
    ) -> list[dict]:
        """Generate label-dig commands for hot labels.

        Returns a list of dicts with 'label', 'track_count', 'command'.
        If the discogs_cache has a label_id, produces a ready-to-run command.
        Otherwise, produces a template with placeholder.
        """
        labels = self.hot_labels(window, min_tracks)
        results = []
        for lb in labels:
            # Try to find Discogs label ID from cache
            row = self.conn.execute(
                "SELECT DISTINCT discogs_label_id FROM discogs_cache WHERE discogs_label = ? AND discogs_label_id IS NOT NULL",
                (lb.signal_value,),
            ).fetchone()

            flags = []
            if deep:
                flags.append("--deep")
            flags.append(f"--limit {limit}")
            flag_str = " ".join(flags)

            if row:
                label_id = row[0]
                url = f"https://www.discogs.com/label/{label_id}"
                command = f'uv run cosine-digger label-dig "{url}" {flag_str}'
            else:
                command = f"uv run cosine-digger label-dig <find-discogs-url-for-{lb.signal_value}> {flag_str}"

            results.append({
                "label": lb.signal_value,
                "track_count": lb.track_count,
                "album_count": lb.album_count,
                "command": command,
            })
        return results

    def bridge_seeds(
        self, max_seeds: int = 6, max_per_artist: int = 1, window: str = "all"
    ) -> list[str]:
        """Pick seeds that bridge different artists in the library.

        Selects one track per artist, spread across as many different artists
        as possible. This maximizes the chance of finding the shared pocket
        between different parts of your collection.

        Returns "Artist - Title" seed strings.
        """
        return self.generate_dig_seeds(
            window=window, max_seeds=max_seeds, max_per_artist=max_per_artist
        )

    def generate_bridge_command(
        self, max_seeds: int = 6, window: str = "all",
        limit: int = 20, deep: bool = True,
    ) -> str:
        """Generate a dig command that bridges different parts of the collection.

        Uses 1 track per artist to maximize diversity of seed neighborhoods.
        """
        seeds = self.bridge_seeds(max_seeds=max_seeds, max_per_artist=1, window=window)
        if not seeds:
            return "# No tracks found in the taste DB."
        quoted = " ".join(f'"{s}"' for s in seeds)
        flags = []
        if deep:
            flags.append("--deep")
        flags.append(f"--limit {limit}")
        return f"uv run cosine-digger dig {quoted} {' '.join(flags)}"

    # ------------------------------------------------------------------
    # Seed verification: check that seeds resolve on cosine.club
    # ------------------------------------------------------------------

    def verify_seed(self, artist: str, title: str) -> dict:
        """Check if "Artist - Title" resolves to the intended track on cosine.club.

        Returns dict with:
          verified: bool — True if search found the intended track
          cosine_track_id: str | None — the matched track ID if verified
          resolved_artist: str | None — what cosine.club returned
          resolved_title: str | None — what cosine.club returned
        """
        from .client import CosineClient

        query = f"{artist} - {title}"
        with CosineClient() as client:
            results = client.search(query, limit=5)

        if not results:
            return {"verified": False, "cosine_track_id": None,
                    "resolved_artist": None, "resolved_title": None}

        # Check if any result matches artist AND title
        def _norm(s: str) -> str:
            """Normalize for comparison: lowercase, underscores→spaces, strip parens."""
            s = s.strip().lower().replace("_", " ")
            # Remove trailing parenthetical like "(Original Mix)"
            s = re.sub(r"\s*\([^)]*\)\s*$", "", s)
            # Collapse multiple spaces
            return re.sub(r"\s+", " ", s).strip()

        artist_norm = _norm(artist)
        title_norm = _norm(title)
        for track in results:
            track_artist = _norm(track.artist or "")
            track_title = _norm(track.track or track.name or "")
            # Artist match: exact or substring (min 4 chars to avoid false positives)
            if artist_norm == track_artist:
                artist_ok = True
            elif len(artist_norm) >= 4 and len(track_artist) >= 4:
                artist_ok = artist_norm in track_artist or track_artist in artist_norm
            else:
                artist_ok = False
            # Title match: exact or substring (min 4 chars)
            if title_norm == track_title:
                title_ok = True
            elif len(title_norm) >= 4 and len(track_title) >= 4:
                title_ok = title_norm in track_title or track_title in title_norm
            else:
                title_ok = False
            if artist_ok and title_ok:
                return {
                    "verified": True,
                    "cosine_track_id": track.id,
                    "resolved_artist": track.artist,
                    "resolved_title": track.track or track.name,
                }

        # No match — return what was found for debugging
        top = results[0]
        return {
            "verified": False,
            "cosine_track_id": None,
            "resolved_artist": top.artist,
            "resolved_title": top.track or top.name,
        }

    def verify_library(
        self, limit: int = 0, delay: float = 1.0, recheck: bool = False,
    ) -> dict:
        """Batch-verify all tracks against cosine.club. Rate-limited.

        Args:
            limit: Max tracks to verify (0 = all unverified).
            delay: Seconds between API calls.
            recheck: If True, re-verify already-checked tracks.

        Returns:
            Dict with counts: checked, verified, failed, skipped, errors.
        """
        import time

        if recheck:
            query = """
                SELECT DISTINCT artist, title FROM tracks
                WHERE artist IS NOT NULL AND artist != ''
                  AND title IS NOT NULL AND title != ''
                ORDER BY mtime DESC
            """
        else:
            query = """
                SELECT DISTINCT artist, title FROM tracks
                WHERE artist IS NOT NULL AND artist != ''
                  AND title IS NOT NULL AND title != ''
                  AND cosine_verified IS NULL
                ORDER BY mtime DESC
            """
        rows = self.conn.execute(query).fetchall()
        if limit > 0:
            rows = rows[:limit]

        stats = {"checked": 0, "verified": 0, "failed": 0, "skipped": 0, "errors": 0}

        for row in rows:
            artist, title = row["artist"], row["title"]
            try:
                result = self.verify_seed(artist, title)
                stats["checked"] += 1

                verified = 1 if result["verified"] else 0
                track_id = result["cosine_track_id"]

                self.conn.execute(
                    "UPDATE tracks SET cosine_verified = ?, cosine_track_id = ? WHERE artist = ? AND title = ?",
                    (verified, track_id, artist, title),
                )
                self.conn.commit()

                if result["verified"]:
                    stats["verified"] += 1
                    print(f"  [ok]   {artist} - {title} → {track_id}")
                else:
                    stats["failed"] += 1
                    ra = result["resolved_artist"] or "?"
                    rt = result["resolved_title"] or "?"
                    print(f"  [miss] {artist} - {title} → got {ra} - {rt}")

                time.sleep(delay)
            except Exception as e:
                stats["errors"] += 1
                print(f"  [err]  {artist} - {title} → {e}")

        return stats

    def verification_stats(self) -> dict:
        """Return counts of verified/failed/unchecked tracks."""
        total = self.conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        verified = self.conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE cosine_verified = 1"
        ).fetchone()[0]
        failed = self.conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE cosine_verified = 0"
        ).fetchone()[0]
        unchecked = self.conn.execute(
            "SELECT COUNT(*) FROM tracks WHERE cosine_verified IS NULL"
        ).fetchone()[0]
        return {
            "total": total,
            "verified": verified,
            "failed": failed,
            "unchecked": unchecked,
        }

    def gap_artists(self, result_artists: set[str]) -> set[str]:
        """Return artists from results that are NOT in the user's collection.

        Both sides normalized via normalize_artist() before comparison.
        """
        owned = {normalize_artist(a) for a in self.owned_artists()}
        return {a for a in result_artists if normalize_artist(a) not in owned}

    def close(self):
        self.conn.close()
