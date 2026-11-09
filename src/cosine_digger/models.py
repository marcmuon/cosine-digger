from pydantic import BaseModel, ConfigDict


class CosineBaseModel(BaseModel):
    # The cosine API occasionally returns numeric IDs for playlist resources.
    model_config = ConfigDict(extra="ignore", coerce_numbers_to_str=True)


class Track(CosineBaseModel):
    id: str
    name: str
    artist: str
    track: str
    score: float | None = None
    video_id: str | None = None
    video_uri: str | None = None
    external_link: str | None = None
    source: str | None = None


class SimilarTracksData(CosineBaseModel):
    source_track: Track
    similar_tracks: list[Track]


class Meta(CosineBaseModel):
    total: int | None = None
    count: int | None = None
    limit: int | None = None
    offset: int | None = None
    has_more: bool | None = None
    request_id: str | None = None
    timestamp: str | int | None = None


class SearchResponse(CosineBaseModel):
    data: list[Track]
    meta: Meta | None = None


class LookupResponse(CosineBaseModel):
    data: list[Track]
    meta: Meta | None = None


class SimilarTracksResponse(CosineBaseModel):
    data: SimilarTracksData
    meta: Meta | None = None


class Playlist(CosineBaseModel):
    id: str
    name: str
    track_count: int = 0
    is_public: bool = False
    url: str | None = None


class PlaylistDetail(CosineBaseModel):
    id: str
    name: str
    track_count: int = 0
    is_public: bool = False
    url: str | None = None
    tracks: list[Track] = []


class PlaylistListResponse(CosineBaseModel):
    data: list[Playlist]
    meta: Meta | None = None


class PlaylistDetailResponse(CosineBaseModel):
    data: PlaylistDetail
    meta: Meta | None = None


class PlaylistResponse(CosineBaseModel):
    data: Playlist
    meta: Meta | None = None
