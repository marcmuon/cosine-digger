"""GUI-only contracts and deterministic saved geometry.

Legacy Track/RankedTrack/Pick serializers remain independent of these wrappers.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from .fusion import RankedTrack
from .models import Track

MoveKind = Literal["seed", "track", "label", "artist", "person", "these"]
FactsState = Literal["loading", "ready", "missing", "unavailable", "no_token"]


class SliceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")


class ResolvedTrack(SliceModel):
    cosine_id: str
    track_key: str
    artist: str
    title: str
    video_id: str | None = None


class Point(SliceModel):
    point_id: str = Field(min_length=1)
    kind: Literal["track", "album", "label", "artist", "person", "link"]
    name: str = Field(min_length=1)
    track_key: str | None = None
    cosine_id: str | None = None
    artist: str | None = None
    title: str | None = None
    video_id: str | None = None
    discogs_id: str | None = None
    url: str | None = None
    library_id: int | None = Field(default=None, strict=True, gt=0, le=2**63 - 1)
    indexed: bool | None = Field(default=None, strict=True)
    library_source: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]{0,31}$")  # "file" or an importer
    source_step_id: str | None = None
    source_rank: int | None = Field(default=None, strict=True, gt=0, le=2**63 - 1)
    resolved_tracks: list[ResolvedTrack] | None = None
    resolution: Literal["exact", "substituted", "missing", "unavailable"] | None = None

    @model_serializer(mode="wrap")
    def _serialize(self, handler):
        # Requested Point wire fields include nulls; resolution adds these two
        # fields only when the engine has actually resolved the requested chip.
        # library_source appears only when known: steps saved before it existed
        # must still match their Re-run and Retry requests (and request hashes).
        data = handler(self)
        for key in ("resolved_tracks", "resolution", "library_source"):
            if data[key] is None:
                del data[key]
        return data


class FactEntity(SliceModel):
    id: str | None = None
    name: str


class Credit(FactEntity):
    roles: list[str]
    scope: Literal["release", "track"]


class Facts(SliceModel):
    source: Literal["discogs"] = "discogs"
    release_id: str | None = None
    release_url: str | None = None
    labels: list[FactEntity] = Field(default_factory=list)
    year: int | None = Field(default=None, strict=True)
    styles: list[str] = Field(default_factory=list)
    artists: list[FactEntity] = Field(default_factory=list)
    credits: list[Credit] = Field(default_factory=list)
    thumb: str | None = None


class MoveEvent(SliceModel):
    event: Literal["note", "batch", "pick"]
    data: dict


class MoveCandidate(SliceModel):
    ranked: RankedTrack
    route: dict
    facts: Facts | None = None
    facts_state: FactsState = "loading"


class MoveBatch(SliceModel):
    hop: int = Field(strict=True, ge=0, le=1)
    candidates: list[MoveCandidate]
    notes: list[dict] = Field(default_factory=list)
    hidden: dict[str, int] = Field(default_factory=dict)  # exclusion reason -> tracks left out


class Resolution(SliceModel):
    origins: list[Point]
    resolved_tracks: list[Track]
    seed_to_origin: dict[str, str]
    notes: list[dict] = Field(default_factory=list)


class StepIdentity(SliceModel):
    dig_id: str
    step_id: str
    number: int = Field(strict=True, gt=0)
    job_id: str
    created: bool


class PickLayout(SliceModel):
    radius: float = Field(ge=0.08, le=0.85)
    angle: float = Field(ge=0, lt=math.tau)


class HubLayout(SliceModel):
    radius: Literal[0.95] = 0.95
    angle: float = Field(ge=0, lt=math.tau)


class StepPick(SliceModel):
    id: str
    step_id: str
    rank: int = Field(strict=True, gt=0)
    track_key: str
    cosine_id: str
    artist: str
    title: str
    video_id: str | None = None
    score: float = Field(strict=True, ge=0, le=1)
    batch: int = Field(strict=True, ge=0, le=1)
    route: dict
    facts: Facts | None = None
    facts_state: FactsState = "loading"
    layout: PickLayout

    @model_validator(mode="after")
    def _identity_and_facts(self):
        if self.id != f"{self.step_id}:{self.rank}":
            raise ValueError("pick identity must be step_id:rank")
        if (self.facts_state == "ready") != (self.facts is not None):
            raise ValueError("only ready facts have a facts payload")
        return self


class Hub(SliceModel):
    hub_key: str
    kind: Literal["label", "person"]
    name: str
    discogs_id: str | None = None
    pick_ranks: list[Annotated[int, Field(strict=True, gt=0)]] = Field(min_length=2)
    layout: HubLayout

    @model_validator(mode="after")
    def _members(self):
        if any(type(rank) is not int or rank < 1 for rank in self.pick_ranks):
            raise ValueError("hub ranks must be positive integers")
        if self.pick_ranks != sorted(set(self.pick_ranks)):
            raise ValueError("hub ranks must be sorted and distinct")
        if self.hub_key != fact_key(self.kind, self.discogs_id, self.name):
            raise ValueError("hub identity does not match its fact")
        return self


def fact_key(kind: str, discogs_id: str | None, name: str) -> str:
    from .profile import normalize_artist

    return f"{kind}:{discogs_id}" if discogs_id else f"{kind}:name:{normalize_artist(name)}"


def pick_layout(step_id: str, rank: int, score: float, origins: list[Point],
                layout_seed: str | None = None, *, score_floor: float = 0.0,
                layout_version: int = 2) -> PickLayout:
    """Hash the saved pick id; neither viewport size nor later facts affect it."""
    if type(rank) is not int or rank < 1 or not origins or len(origins) > 10:
        raise ValueError("invalid rank or origins")
    if isinstance(score, bool) or not math.isfinite(score) or not 0 <= score <= 1:
        raise ValueError("score must be finite and between zero and one")
    if (type(score_floor) not in (int, float) or not math.isfinite(score_floor)
            or not 0 <= score_floor <= score):
        raise ValueError("score_floor must be finite, nonnegative and no higher than the pick score")
    if layout_version not in (1, 2):
        raise ValueError("unsupported layout version")
    hashed = 2166136261
    for byte in f"{step_id}:{rank}".encode("utf-8"):
        hashed = ((hashed ^ byte) * 16777619) & 0xFFFFFFFF
    if layout_version == 2:
        hashed ^= hashed >> 16
        hashed = (hashed * 0x85EBCA6B) & 0xFFFFFFFF
        hashed ^= hashed >> 13
        hashed = (hashed * 0xC2B2AE35) & 0xFFFFFFFF
        hashed ^= hashed >> 16
    u = hashed / 2**32
    origin_ids = [point.point_id for point in origins]
    if layout_seed is None:
        layout_seed = next((point.point_id for point in origins if point.resolved_tracks), origin_ids[0])
    if layout_seed not in origin_ids:
        raise ValueError("layout seed is not a requested origin")
    angle = math.tau * u if len(origins) == 1 else (
        math.tau * (origin_ids.index(layout_seed) + 1/6 + (2/3) * u) / len(origins)
    )
    if layout_version == 1:
        radius = 0.85 * (1 - score) / 0.5
    else:
        radius = 0.08 if score_floor == 1 else 0.08 + 0.77 * ((1 - score) / (1 - score_floor))
    return PickLayout(radius=max(0.08, min(0.85, radius)), angle=angle)


def island_position(*, parent: tuple[float, float], grandparent: tuple[float, float] | None,
                    sibling_index: int, existing: list[tuple[float, float]]) -> tuple[float, float]:
    """Fan in creation order and grow the radius without moving older islands."""
    direction = math.atan2(parent[1] - grandparent[1], parent[0] - grandparent[0]) if grandparent else 0
    fan = ((sibling_index + 1) // 2) * (1 if sibling_index % 2 else -1) * math.pi / 6
    distance = 420
    while True:
        x = parent[0] + distance * math.cos(direction + fan)
        y = parent[1] + distance * math.sin(direction + fan)
        if all(math.hypot(x - ex, y - ey) >= 340 for ex, ey in existing):
            return x, y
        distance += 140


def build_hubs(picks: list[StepPick | dict], existing_hubs: list[Hub | dict]) -> list[Hub]:
    """Only verified, ready shared facts qualify; saved angles never change."""
    from .profile import normalize_artist

    picks = [StepPick.model_validate(pick) for pick in picks]
    existing = {hub.hub_key: hub for hub in map(Hub.model_validate, existing_hubs)}
    members: dict[str, dict] = {}
    for pick in sorted(picks, key=lambda pick: pick.rank):
        if pick.facts_state != "ready":
            continue
        facts = pick.facts
        main_ids = {artist.id for artist in facts.artists if artist.id}
        main_names = {normalize_artist(artist.name) for artist in facts.artists}
        main_names.add(normalize_artist(pick.artist))
        people = [credit for credit in facts.credits if
                  credit.id not in main_ids and normalize_artist(credit.name) not in main_names]
        for kind, entities in (("label", facts.labels), ("person", people)):
            for entity in entities:
                key = fact_key(kind, entity.id, entity.name)
                member = members.setdefault(key, {"kind": kind, "name": entity.name,
                                                  "discogs_id": entity.id, "angles": {}})
                member["angles"][pick.rank] = pick.layout.angle
    result = dict(existing)
    occupied = [hub.layout.angle for hub in existing.values()]
    ordered = sorted(members.items(), key=lambda item: (item[1]["kind"], item[1]["name"].casefold(), item[0]))
    for key, member in ordered:
        ranks = sorted(member["angles"])
        if len(ranks) < 2:
            continue
        if key in existing:
            result[key] = existing[key].model_copy(update={"pick_ranks": ranks})
            continue
        angles = [member["angles"][rank] for rank in ranks]
        sine, cosine = sum(map(math.sin, angles)), sum(map(math.cos, angles))
        angle = (math.atan2(sine, cosine) % math.tau) if math.hypot(sine, cosine) > 1e-12 else angles[0]
        initial = angle
        for slot in range(45):
            angle = (initial + slot * math.radians(8)) % math.tau
            if all(abs((angle - other + math.pi) % math.tau - math.pi) * 136 >= 24 for other in occupied):
                break
        occupied.append(angle)
        result[key] = Hub(hub_key=key, kind=member["kind"], name=member["name"],
                          discogs_id=member["discogs_id"], pick_ranks=ranks, layout=HubLayout(angle=angle))
    return sorted(result.values(), key=lambda hub: (hub.kind, hub.name.casefold(), hub.hub_key))
