"""Slice HTTP boundaries; the engine and durable runtime own the work."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import re
import sqlite3
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .. import api, storage, youtube
from ..client import CosineClientError, CosineHTTPError, CosineKeyMissing, failure
from ..config import usable_key
from ..slice_types import MoveKind, Point, StepIdentity
from .slice_jobs import SliceRuntime
from .slice_library import label_overview, run_label_enrichment


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InputPoint(Point):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _never_saves_library_source(cls, data):
        # library_source is how a library result reads in the seed box, never saved with a step:
        # an older Digger's Re-run, Retry and branches refuse a saved origin that carries it.
        if isinstance(data, dict) and "library_source" in data:
            data = {key: value for key, value in data.items() if key != "library_source"}
        return data


class DigBody(Body):
    seeds: list[InputPoint] = Field(min_length=1, max_length=10)


class StepBody(Body):
    parent_step_id: str | None = None
    origins: list[InputPoint] = Field(min_length=1, max_length=10)
    source_selection_id: str | None = Field(default=None, min_length=1, max_length=1024)
    move: MoveKind
    further: bool
    scope: Literal["step", "dig"] | None = None
    requested_count: int | None = Field(default=None, ge=1)
    rerun_of: str | None = None
    retry_step_id: str | None = None

    @model_validator(mode="after")
    def _source_selection(self):
        if "source_selection_id" not in self.model_fields_set:
            return self
        token = self.source_selection_id
        if token is None:
            raise ValueError("source selection must be a token")
        point_id = token.removeprefix("origin:")
        if token.startswith("fact:"):
            match = re.fullmatch(r"fact:[1-9][0-9]*:(.+)", token)
            point_id = match[1] if match else None
        if point_id != self.origins[0].point_id:
            raise ValueError("source selection must match the first origin")
        return self

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        data = handler(self)
        if self.source_selection_id is None:
            # Keep pre-token request hashes valid for idempotent transport retries.
            data.pop("source_selection_id", None)
        return data


class JudgmentBody(Body):
    step_id: str = Field(min_length=1)
    expected_revision: int = Field(ge=0)


class PutJudgment(JudgmentBody):
    verdict: Literal["keep", "pass"]


class DeleteJudgment(JudgmentBody):
    undo_change_id: str | None = Field(default=None, min_length=1)


class HeardBody(Body):
    track_key: str = Field(min_length=1)
    step_id: str = Field(min_length=1)


class EnrichmentBody(Body):
    budget: int = Field(default=100, ge=1, le=500)
    resume_job_id: str | None = Field(default=None, min_length=1)


class EmptyBody(Body):
    pass


def _link(url: str) -> str:
    """Recognize supported public music links, without fetching the URL."""
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port is not None or len(url) > 2000):
            raise ValueError
        host = parsed.hostname
        if host in {"cosine.club", "www.cosine.club"}:
            valid = re.fullmatch(r"/track/[A-Za-z0-9_-]+/?", parsed.path)
        elif host in {"discogs.com", "www.discogs.com"}:
            valid = re.fullmatch(r"/(?:[a-z]{2}/)?(?:release|master|label|artist)/[1-9][0-9]*(?:-[^/]+)?/?", parsed.path)
        elif host == "youtu.be":
            valid = re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path)
        elif host in {"youtube.com", "www.youtube.com", "music.youtube.com"}:
            valid = (parsed.path == "/watch" and
                     re.fullmatch(r"[A-Za-z0-9_-]+", parse_qs(parsed.query).get("v", [""])[0]))
            valid = valid or re.fullmatch(r"/(?:shorts|embed)/[A-Za-z0-9_-]+/?", parsed.path)
        else:
            valid = False
        if not valid:
            raise ValueError
    except ValueError:
        raise HTTPException(422, "Unsupported seed link") from None
    return url


def _track_point(*, artist, title, cosine_id=None, video_id=None, **extra) -> Point:
    key = api._key(artist, title)
    return Point(point_id=f"track:{key}", kind="track", track_key=key,
                 name=f"{artist} - {title}" if artist else title, artist=artist, title=title,
                 cosine_id=cosine_id, video_id=video_id, **extra)


IMPORT_SOURCE = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # Point.library_source; importers write free text


def _library_point(row) -> Point:
    artist, title = row["artist"] or "", row["title"] or ""
    indexed = bool(row["cosine_verified"]) if row["cosine_verified"] is not None else None
    # Scanned files have a filename; imported rows (path "<source>:<id>") always name their catalogue.
    source = (row["source"] or "").strip().lower()
    library_source = ("file" if row["filename"] or not source
                      else source if IMPORT_SOURCE.fullmatch(source) else "import")
    point = _track_point(artist=artist, title=title, indexed=indexed,
                         cosine_id=row["cosine_track_id"] if indexed else None, library_id=row["id"],
                         library_source=library_source)
    return point.model_copy(update={"point_id": f"library:{row['id']}"})


# Library seed matches, best first: exact artist, exact title or "Artist - Title",
# artist prefix, title prefix, then artist/title anywhere, then album only.
LIBRARY_SEARCH_LIMIT = 8
LIBRARY_SEARCH_SQL = """
    SELECT *, CASE
        WHEN artist LIKE :exact ESCAPE '\\' THEN 0
        WHEN title LIKE :exact ESCAPE '\\' OR artist || ' - ' || title LIKE :exact ESCAPE '\\'
             OR artist || ' ' || title LIKE :exact ESCAPE '\\' THEN 1
        WHEN artist LIKE :prefix ESCAPE '\\' THEN 2
        WHEN title LIKE :prefix ESCAPE '\\' THEN 3
        WHEN COALESCE(artist,'') || ' ' || COALESCE(title,'') || ' | ' || COALESCE(artist,'') || ' - ' ||
             COALESCE(title,'') LIKE :contains ESCAPE '\\' THEN 4
        ELSE 5 END AS match_rank
    FROM tracks
    WHERE COALESCE(artist,'') || ' ' || COALESCE(title,'') || ' ' || COALESCE(album,'') || ' | ' ||
          COALESCE(artist,'') || ' - ' || COALESCE(title,'') LIKE :contains ESCAPE '\\'
    ORDER BY match_rank, artist COLLATE NOCASE, title COLLATE NOCASE, id
    LIMIT :limit"""


SEARCH_NOTES = {
    "missing": ("S-SB-NOKEY", "Add your cosine.club key to search."),
    "rejected": ("S-SB-KEY-REJECTED", "cosine.club didn't accept your key. Fix it to search and dig."),
    "busy": ("S-SB-BUSY", "cosine.club is busy. Try again in {s} s."),
    "server_error": ("S-SB-SERVER-ERR", "cosine.club didn't answer: server error. Try again in {s} s."),
    "unreachable": ("S-SB-UNREACHABLE", "Couldn't reach cosine.club. Check your connection."),
}


def _search_note(exc: Exception) -> dict:
    """One note per cause, so the seed box can offer Add key, Fix key or a countdown."""
    state, wait = failure(exc)
    code, message = SEARCH_NOTES[state]
    params = {"s": wait} if wait is not None else {}
    return {"code": code, "message": message.format(**params), "params": params, "state": state}


def _require_discogs(config, points):
    # Discogs is optional: a missing or malformed token never blocks a track dig (P9).
    try:
        if usable_key(config.discogs_token, "DISCOGS_TOKEN"):
            return
        malformed = False
    except ValueError:
        malformed = True
    for point in points:
        kind = point.kind
        if point.url and urlsplit(point.url).hostname in {"discogs.com", "www.discogs.com"}:
            match = re.search(r"/(label|artist)/", urlsplit(point.url).path)
            kind = match[1] if match else kind
        if kind in {"label", "artist", "person"}:
            target = {"label": "its label", "artist": "its artist", "person": "a credited person"}[kind]
            raise HTTPException(422, f"Sounds like {target} needs Discogs, and the Discogs token has a space or line "
                                     "break in it. Paste it again in Setup." if malformed else
                                f"Sounds like {target} needs a Discogs token. Add one in Setup.")


def _points(conn, values, *, parent_id=None, scope=None):
    points = []
    for value in values:
        point = Point.model_validate(value.to_dict()).model_copy(update={"resolved_tracks": None, "resolution": None})
        if point.url:
            _link(point.url)
        if point.cosine_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", point.cosine_id):
            raise ValueError("invalid cosine id")
        if point.discogs_id and not re.fullmatch(r"[1-9][0-9]*", point.discogs_id):
            raise ValueError("invalid Discogs id")
        if point.kind in {"label", "person"} and not point.discogs_id:
            raise ValueError("label and person origins require a Discogs id")
        if parent_id:
            storage.validate_point_source(conn, parent_id=parent_id, point=point, these_scope=scope)
            if point.source_rank is not None and point.kind == "track":
                row = conn.execute("SELECT artist,title,cosine_id,video_id FROM step_picks WHERE step_id=? AND rank=?",
                                   (point.source_step_id, point.source_rank)).fetchone()
                point = _track_point(artist=row[0], title=row[1], cosine_id=row[2], video_id=row[3],
                                     indexed=True, source_step_id=point.source_step_id, source_rank=point.source_rank)
            elif point.source_rank is not None or point.kind in {"label", "artist", "person"}:
                # Verified names/IDs above establish facts, never caller track/URL metadata.
                point = Point(point_id=point.point_id, kind=point.kind, name=point.name,
                              discogs_id=point.discogs_id, source_step_id=point.source_step_id,
                              source_rank=point.source_rank)
            else:
                original = storage.get_step_content(conn, point.source_step_id)["step"]["origins"]
                saved = next(item for item in original if item["point_id"] == point.point_id)
                point = Point.model_validate(saved).model_copy(update={"source_step_id": point.source_step_id,
                                                                       "resolved_tracks": None, "resolution": None})
        elif point.library_id is not None:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM tracks WHERE id=?", (point.library_id,)).fetchone()
            if row is None:
                raise ValueError("library row does not exist")
            # library_source words a result in the seed box only: a saved origin never carries it.
            point = _library_point(row).model_copy(update={"library_source": None})
            conn.row_factory = None
        elif point.source_step_id is not None or point.source_rank is not None:
            raise ValueError("root seeds cannot claim step provenance")
        points.append(point)
    if len({point.point_id for point in points}) != len(points):
        raise ValueError("duplicate origins")
    return points


def _with_playlist(conn, dig, account=0):
    """A dig with its YouTube Music playlist's link (in the login's account) and when it was last sent, once it was."""
    row = conn.execute("SELECT playlist_id, updated_at FROM sink_playlists WHERE sink = ? AND name = ?",
                       (api.DbIdStore(conn).sink, api.dig_playlist_key(dig["id"]))).fetchone()
    url = f"https://music.youtube.com/playlist?list={row[0]}" if row else None
    return {**dig, "youtube_playlist_url": youtube.account_link(url, account), "youtube_sent_at": row[1] if row else None}


def _keeps(conn, parent_id, scope):
    parent = storage.get_step_content(conn, parent_id)["step"]
    rows = conn.execute("""SELECT p.track_key,s.id,p.rank FROM step_picks p JOIN steps s ON s.id=p.step_id
                           WHERE s.dig_id=? AND (?='dig' OR s.id=?) ORDER BY s.number,p.rank""",
                        (parent["dig_id"], scope, parent_id)).fetchall()
    eligible = {}
    legacy = storage.legacy_judgments(conn)
    for key, step_id, rank in rows:
        effective = storage.effective_judgment(conn, key, legacy=legacy)
        if effective["verdict"] == "keep":
            eligible.setdefault(key, effective["at"])
    # Stable tie break: newest verdict first, then canonical track key.
    ordered = sorted(sorted(eligible), key=lambda key: eligible[key], reverse=True)
    return [f"track:{key}" for key in ordered[:10]], len(ordered)


def install_slice(app, *, get_config, client_factory, redact, discogs_client_factory=None,
                  key_result=lambda token, state: None) -> SliceRuntime:
    runtime = SliceRuntime(get_config=get_config, client_factory=client_factory, redact=redact,
                           discogs_client_factory=discogs_client_factory, key_result=key_result)

    def read(action):
        with closing(storage.connect(get_config().db_path)) as conn:
            try:
                return redact(action(conn))
            except ValueError:
                raise HTTPException(404, "Saved item not found") from None

    def write(action):
        with closing(storage.connect(get_config().db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                with conn:
                    return redact(action(conn))
            except storage.SliceConflict:
                raise HTTPException(409, "This judgment changed elsewhere; try again.") from None
            except ValueError:
                raise HTTPException(422, "Invalid judgment or step membership") from None

    def submit(request, body, operation, create):
        raw_key = request.headers.get("idempotency-key")
        if raw_key is not None and (not raw_key.strip() or len(raw_key) > 200):
            raise HTTPException(422, "Invalid idempotency key")
        key = hashlib.sha256(raw_key.encode()).hexdigest() if raw_key is not None else None
        body_hash = hashlib.sha256(storage.canonical_json(body.model_dump(mode="json")).encode()).hexdigest()

        def admit(conn):
            if key:
                row = conn.execute("""SELECT progress FROM slice_jobs WHERE json_extract(progress,'$.request.key')=?
                                      OR EXISTS (SELECT 1 FROM json_each(slice_jobs.progress,'$.request_aliases')
                                                 WHERE json_extract(value,'$.key')=?)""", (key, key)).fetchone()
                if row:
                    progress = json.loads(row[0])
                    saved = next(record for record in [progress.get("request", {}), *progress.get("request_aliases", [])]
                                 if record.get("key") == key)
                    if saved["operation"] != operation or saved["body_hash"] != body_hash:
                        raise storage.SliceConflict("idempotency request differs")
                    return StepIdentity.model_validate(saved["response"]), False
            identity, launch = create(conn)
            if key:
                progress = storage.get_slice_job(conn, identity.job_id)["progress"]
                record = {"key": key, "operation": operation, "body_hash": body_hash, "response": identity.to_dict()}
                if "request" in progress:
                    # Fingerprint duplicates can have distinct transport keys.
                    # Add their aliases without changing the producing request,
                    # including when that job is already terminal.
                    progress.setdefault("request_aliases", []).append(record)
                else:
                    progress["request"] = record
                conn.execute("UPDATE slice_jobs SET progress=? WHERE id=?",
                             (storage.canonical_json(redact(progress)), identity.job_id))
            return identity, launch
        try:
            identity = runtime.submit_step(admit)
        except storage.SliceConflict:
            raise HTTPException(409, "Request conflicts with saved state") from None
        except ValueError:
            raise HTTPException(422, "Invalid move or source provenance") from None
        return {key: value for key, value in identity.to_dict().items() if key != "number"}

    @app.get("/api/search")
    def search(q: str, source: Literal["library", "cosine", "all"] = "all"):
        query = q.strip()
        is_url = "://" in query
        if is_url:
            _link(query)
        elif not 2 <= len(query) <= 200:
            raise HTTPException(422, "Search needs 2 to 200 characters")
        items, notes = [], []
        if source in {"library", "all"} and not is_url:
            with closing(storage.connect(get_config().db_path)) as conn:
                conn.row_factory = sqlite3.Row
                exact = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                rows = conn.execute(LIBRARY_SEARCH_SQL, {"exact": exact, "prefix": exact + "%", "contains": "%" + exact + "%",
                                                         "limit": LIBRARY_SEARCH_LIMIT}).fetchall()
                items.extend(_library_point(row).to_dict() for row in rows)
        if source in {"cosine", "all"}:
            config = get_config()  # one snapshot: the verdict below is about the key this search sends
            token = config.cosine_token
            try:
                key_missing = not usable_key(token, "COSINE_API_TOKEN")
            except ValueError:
                key_missing = False  # a key that can't be sent reads as not accepted (the client refuses it)
            try:
                if key_missing:
                    raise CosineKeyMissing("COSINE_API_TOKEN is not set")
                with closing(client_factory(config)) as client:
                    # The seed box answers at once: no retries, and a 429 says how long to wait.
                    tracks = client.lookup(query, retry=False) if is_url else client.search(query, limit=8, retry=False)
                key_result(token, "connected")
                items.extend(_track_point(artist=t.artist, title=t.track or t.name, cosine_id=t.id,
                                          video_id=t.video_id, indexed=True).to_dict() for t in tracks[:8])
            except CosineHTTPError as exc:
                if exc.status_code == 404:  # a link cosine.club doesn't know is no match, not an error
                    key_result(token, "connected")
                else:
                    notes.append(_search_note(exc))
            except CosineClientError as exc:  # cosine.club's side only; a bug in Digger stays an error
                note = _search_note(exc)
                if note["state"] in {"missing", "rejected"}:
                    # From which settings revision the server holds this refusal (None: the key it's
                    # about was replaced meanwhile), so the page never takes it for a newer key's.
                    note["key_revision"] = key_result(token, note["state"])
                notes.append(note)
        indexed = {item["track_key"]: item for item in items if item["library_id"] is None and item["cosine_id"]}
        for item in items:
            if item["library_id"] is not None and (match := indexed.get(item["track_key"])) is not None:
                item.update(indexed=True, cosine_id=match["cosine_id"], video_id=match["video_id"])
        return redact({"query": query, "source": source, "items": items, "notes": notes, "more": False})

    @app.post("/api/digs")
    def dig_create(body: DigBody, request: Request):
        def create(conn):
            points = _points(conn, body.seeds)
            _require_discogs(get_config(), points)
            points = [Point.model_validate(item) for item in redact([p.to_dict() for p in points])]
            dig_id = storage.create_dig(conn, points)
            identity = api.record_step(conn, dig_id=dig_id, parent_id=None, origins=points, move="seed", further=False)
            return identity, True
        return submit(request, body, "root", create)

    @app.get("/api/digs")
    def digs_read(limit: int = Query(10, ge=1, le=100)):
        account = youtube.login_account(get_config().ytmusic_auth_path)
        return {"digs": read(lambda conn: [_with_playlist(conn, dig, account) for dig in storage.list_digs(conn, limit=limit)])}

    @app.get("/api/digs/{dig_id}")
    def dig_read(dig_id: str):
        account = youtube.login_account(get_config().ytmusic_auth_path)
        return read(lambda conn: _with_playlist(conn, storage.get_dig(conn, dig_id), account))

    @app.get("/api/steps/{step_id}")
    def step_read(step_id: str):
        return read(lambda conn: storage.get_step_snapshot(conn, step_id))

    @app.post("/api/steps")
    def step_create(body: StepBody, request: Request):
        def create(conn):
            original_id = body.rerun_of or body.retry_step_id
            original = storage.get_step_content(conn, original_id)["step"] if original_id else None
            parent_id = body.parent_step_id
            if original:
                if parent_id is not None and parent_id != original["parent_id"]:
                    raise ValueError("original parent differs")
                parent_id = original["parent_id"]
            if original is None and parent_id is None:
                raise ValueError("parent is required")
            parent = storage.get_step_content(conn, parent_id)["step"] if parent_id else original
            scope = body.scope or (original["context"].get("these_scope") if original else None)
            if original:
                # record_step compares these requested fields with the saved Point.
                # Refreshing library/parent metadata would change the original move.
                points = [point.model_copy(update={"resolved_tracks": None, "resolution": None})
                          for point in body.origins]
            else:
                points = _points(conn, body.origins, parent_id=parent_id, scope=scope)
            context = None
            if body.move == "these" and not original:
                if scope is None or body.requested_count is None:
                    raise ValueError("keeps scope and count are required")
                ids, count = _keeps(conn, parent_id, scope)
                if set(ids) != {p.point_id for p in points} or count != body.requested_count:
                    raise ValueError("keeps changed or do not match the request")
                points.sort(key=lambda point: ids.index(point.point_id))
                context = {"these_scope": scope, "requested_count": count}
            elif original:
                if body.scope is not None and body.scope != original["context"].get("these_scope"):
                    raise ValueError("original scope differs")
                if body.requested_count is not None and body.requested_count != original["context"].get("requested_count"):
                    raise ValueError("original count differs")
            elif body.scope is not None or body.requested_count is not None:
                raise ValueError("scope and count apply only to keeps")
            if not original and body.source_selection_id is not None:
                context = {**(context or {}), "source_selection_id": body.source_selection_id}
            _require_discogs(get_config(), points)
            points = [Point.model_validate(item) for item in redact([p.to_dict() for p in points])]
            identity = api.record_step(conn, dig_id=parent["dig_id"], parent_id=parent_id, origins=points,
                move=body.move, further=body.further, rerun_of=body.rerun_of,
                reuse_empty_step_id=body.retry_step_id, context=context)
            launch = identity.created or bool(body.retry_step_id)
            if launch and body.move == "these":
                context = storage.get_step_content(conn, identity.step_id)["step"]["context"]
                count = context["requested_count"]
                if count > 10:
                    storage.append_slice_event(conn, identity.job_id, "note", {
                        "code": "S-N-KEEPS-CAP", "params": {"k": count}, "phase": "resolve",
                        "message": f"Using your 10 most recent keeps of {count}."
                    }, redact=redact)
            return identity, launch
        operation = "retry" if body.retry_step_id else "rerun" if body.rerun_of else "move"
        return submit(request, body, operation, create)

    @app.put("/api/judgments/{track_key:path}")
    def judgment_put(track_key: str, body: PutJudgment):
        return write(lambda conn: storage.change_judgment(conn, track_key=track_key, **body.model_dump()))

    @app.delete("/api/judgments/{track_key:path}")
    def judgment_delete(track_key: str, body: DeleteJudgment):
        return write(lambda conn: storage.change_judgment(conn, track_key=track_key, **body.model_dump()))

    @app.post("/api/heard")
    def heard(body: HeardBody):
        return write(lambda conn: storage.record_heard(conn, **body.model_dump()))

    @app.get("/api/library/labels")
    def labels():
        return read(label_overview)

    @app.post("/api/library/labels/enrich")
    def enrich(body: EnrichmentBody):
        try:
            return runtime.submit_enrichment(**body.model_dump(), runner=run_label_enrichment)
        except ValueError:
            raise HTTPException(422, "Invalid enrichment resume") from None

    @app.post("/api/library/labels/jobs/{job_id}/cancel")
    def cancel(job_id: str, body: EmptyBody):
        return runtime.cancel_enrichment(job_id)

    return runtime
