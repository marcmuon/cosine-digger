import Foundation

// The wire shapes of Digger's JSON API (src/cosine_digger/slice_types.py and storage.py).
// Keys are spelled out: track keys such as "artist|title" are dictionary keys in the live
// overlays, and a snake_case key strategy would rewrite them.

/// A seed or move origin: a track, label, artist or credited person, with where it came from.
public struct Point: Codable, Hashable, Sendable {
    public var pointID: String
    public var kind: String
    public var name: String
    public var trackKey: String?
    public var cosineID: String?
    public var artist: String?
    public var title: String?
    public var videoID: String?
    public var discogsID: String?
    public var url: String?
    public var libraryID: Int?
    public var indexed: Bool?
    public var librarySource: String?
    public var sourceStepID: String?
    public var sourceRank: Int?
    public var resolution: String?
    public var resolvedTracks: [ResolvedTrack]?

    public init(pointID: String, kind: String, name: String, trackKey: String? = nil, cosineID: String? = nil,
                artist: String? = nil, title: String? = nil, videoID: String? = nil, discogsID: String? = nil,
                url: String? = nil, libraryID: Int? = nil, indexed: Bool? = nil, librarySource: String? = nil,
                sourceStepID: String? = nil, sourceRank: Int? = nil) {
        self.pointID = pointID
        self.kind = kind
        self.name = name
        self.trackKey = trackKey
        self.cosineID = cosineID
        self.artist = artist
        self.title = title
        self.videoID = videoID
        self.discogsID = discogsID
        self.url = url
        self.libraryID = libraryID
        self.indexed = indexed
        self.librarySource = librarySource
        self.sourceStepID = sourceStepID
        self.sourceRank = sourceRank
    }

    enum CodingKeys: String, CodingKey {
        case pointID = "point_id", kind, name, trackKey = "track_key", cosineID = "cosine_id", artist, title
        case videoID = "video_id", discogsID = "discogs_id", url, libraryID = "library_id", indexed
        case librarySource = "library_source", sourceStepID = "source_step_id", sourceRank = "source_rank"
        case resolution, resolvedTracks = "resolved_tracks"
    }

    /// The point as a request sends it: resolution is the server's, and library_source only
    /// words a library result in the seed box.
    public var requested: Point {
        var point = self
        point.resolution = nil
        point.resolvedTracks = nil
        point.librarySource = nil
        return point
    }
}

public struct ResolvedTrack: Codable, Hashable, Sendable {
    public var cosineID: String
    public var trackKey: String
    public var artist: String
    public var title: String
    public var videoID: String?

    enum CodingKeys: String, CodingKey {
        case cosineID = "cosine_id", trackKey = "track_key", artist, title, videoID = "video_id"
    }
}

public struct FactEntity: Codable, Hashable, Sendable {
    public var id: String?
    public var name: String

    public init(id: String?, name: String) {
        self.id = id
        self.name = name
    }
}

public struct Credit: Codable, Hashable, Sendable {
    public var id: String?
    public var name: String
    public var roles: [String]
    public var scope: String?
}

/// What Discogs says about a pick's record. Present only when `factsState` is "ready".
public struct Facts: Codable, Hashable, Sendable {
    public var releaseID: String?
    public var releaseURL: String?
    public var labels: [FactEntity]
    public var year: Int?
    public var styles: [String]
    public var artists: [FactEntity]
    public var credits: [Credit]
    public var thumb: String?

    enum CodingKeys: String, CodingKey {
        case releaseID = "release_id", releaseURL = "release_url", labels, year, styles, artists, credits, thumb
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        releaseID = c.lenient(String.self, forKey: .releaseID)
        releaseURL = c.lenient(String.self, forKey: .releaseURL)
        labels = c.lenient([FactEntity].self, forKey: .labels) ?? []
        year = c.lenient(Int.self, forKey: .year)
        styles = c.lenient([String].self, forKey: .styles) ?? []
        artists = c.lenient([FactEntity].self, forKey: .artists) ?? []
        credits = c.lenient([Credit].self, forKey: .credits) ?? []
        thumb = c.lenient(String.self, forKey: .thumb)
    }
}

/// How a step found a pick. Every field is optional: the route is the server's free-form record.
public struct Route: Codable, Hashable, Sendable {
    public var why: String?
    public var via: String?
    public var hop: Int?
    public var seedScores: [String: Double]?
    public var closeSeedIDs: [String]?
    public var nearestSeed: String?
    public var layoutSeed: String?
    public var seedCount: Int?
    public var parentPickRanks: [Int]?

    enum CodingKeys: String, CodingKey {
        case why, via, hop, seedScores = "seed_scores", closeSeedIDs = "close_seed_ids", nearestSeed = "nearest_seed"
        case layoutSeed = "layout_seed", seedCount = "seed_count", parentPickRanks = "parent_pick_ranks"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        why = c.lenient(String.self, forKey: .why)
        via = c.lenient(String.self, forKey: .via)
        hop = c.lenient(Int.self, forKey: .hop)
        seedScores = c.lenient([String: Double].self, forKey: .seedScores)
        closeSeedIDs = c.lenient([String].self, forKey: .closeSeedIDs)
        nearestSeed = c.lenient(String.self, forKey: .nearestSeed)
        layoutSeed = c.lenient(String.self, forKey: .layoutSeed)
        seedCount = c.lenient(Int.self, forKey: .seedCount)
        parentPickRanks = c.lenient([Int].self, forKey: .parentPickRanks)
    }
}

/// One ranked candidate in a step. Its identity is "stepID:rank".
public struct Pick: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var stepID: String
    public var rank: Int
    public var trackKey: String
    public var cosineID: String
    public var artist: String
    public var title: String
    public var videoID: String?
    public var score: Double
    public var batch: Int
    public var route: Route
    public var facts: Facts?
    public var factsState: String

    enum CodingKeys: String, CodingKey {
        case id, stepID = "step_id", rank, trackKey = "track_key", cosineID = "cosine_id", artist, title
        case videoID = "video_id", score, batch, route, facts, factsState = "facts_state"
    }

    public var name: String { artist.isEmpty ? title : "\(artist) - \(title)" }
}

/// A label or credited person that two or more picks of a step share.
public struct Hub: Codable, Hashable, Sendable {
    public var hubKey: String
    public var kind: String
    public var name: String
    public var discogsID: String?
    public var pickRanks: [Int]

    enum CodingKeys: String, CodingKey {
        case hubKey = "hub_key", kind, name, discogsID = "discogs_id", pickRanks = "pick_ranks"
    }
}

public struct StepContext: Codable, Hashable, Sendable {
    public var sourceSelectionID: String?
    public var sourcePoint: Point?
    public var theseScope: String?
    public var requestedCount: Int?

    enum CodingKeys: String, CodingKey {
        case sourceSelectionID = "source_selection_id", sourcePoint = "source_point"
        case theseScope = "these_scope", requestedCount = "requested_count"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        sourceSelectionID = c.lenient(String.self, forKey: .sourceSelectionID)
        sourcePoint = c.lenient(Point.self, forKey: .sourcePoint)
        theseScope = c.lenient(String.self, forKey: .theseScope)
        requestedCount = c.lenient(Int.self, forKey: .requestedCount)
    }
}

/// A note or error the engine wrote for people: `message` is ready to show.
public struct StepNote: Codable, Hashable, Sendable {
    public var code: String?
    public var message: String?
    /// It carries a running move's progress, which isn't a note to show.
    public var isProgress: Bool

    public init(code: String? = nil, message: String? = nil, isProgress: Bool = false) {
        self.code = code
        self.message = message
        self.isProgress = isProgress
    }

    enum CodingKeys: String, CodingKey { case code, message, progress }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        code = c.lenient(String.self, forKey: .code)
        message = c.lenient(String.self, forKey: .message)
        isProgress = c.contains(.progress) && (try? c.decodeNil(forKey: .progress)) == false
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(code, forKey: .code)
        try c.encodeIfPresent(message, forKey: .message)
    }
}

public struct StepCounts: Codable, Hashable, Sendable {
    public var picks: Int
    public var kept: Int
    public var passed: Int
}

/// One move in a dig: its origins, header, status and (in a dig listing) its counts.
public struct Step: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var digID: String
    public var number: Int
    public var parentID: String?
    public var rerunOf: String?
    public var origins: [Point]
    public var context: StepContext
    public var move: String
    public var further: Bool
    public var status: String
    public var note: [StepNote]
    public var error: StepNote?
    public var header: String
    public var currentJobID: String?
    public var counts: StepCounts?
    /// When its picks were all in, and when it finished: a step whose picks came after a dig was
    /// sent to YouTube Music isn't in that playlist.
    public var picksDoneAt: String?
    public var finishedAt: String?

    enum CodingKeys: String, CodingKey {
        case id, digID = "dig_id", number, parentID = "parent_id", rerunOf = "rerun_of", origins, context, move
        case further, status, note, error, header, currentJobID = "current_job_id", counts
        case picksDoneAt = "picks_done_at", finishedAt = "finished_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        digID = try c.decode(String.self, forKey: .digID)
        number = try c.decode(Int.self, forKey: .number)
        parentID = try c.decodeIfPresent(String.self, forKey: .parentID)
        rerunOf = try c.decodeIfPresent(String.self, forKey: .rerunOf)
        origins = try c.decode([Point].self, forKey: .origins)
        context = try c.decode(StepContext.self, forKey: .context)
        move = try c.decode(String.self, forKey: .move)
        further = try c.decode(Bool.self, forKey: .further)
        status = try c.decode(String.self, forKey: .status)
        note = c.lenient([StepNote].self, forKey: .note) ?? []
        error = c.lenient(StepNote.self, forKey: .error)
        header = try c.decode(String.self, forKey: .header)
        currentJobID = c.lenient(String.self, forKey: .currentJobID)
        counts = c.lenient(StepCounts.self, forKey: .counts)
        picksDoneAt = c.lenient(String.self, forKey: .picksDoneAt)
        finishedAt = c.lenient(String.self, forKey: .finishedAt)
    }

    public var isRunning: Bool { status == "running" }
    public var isFailed: Bool { status == "failed" || status == "stopped" }
}

public struct Batch: Codable, Hashable, Sendable {
    public var hop: Int
    public var startRank: Int
    public var label: String?
    public var count: Int

    enum CodingKeys: String, CodingKey { case hop, startRank = "start_rank", label, count }
}

public struct Snapshot: Codable, Hashable, Sendable {
    public var step: Step
    public var picks: [Pick]
    public var hubs: [Hub]
    public var batches: [Batch]

    enum CodingKeys: String, CodingKey { case step, picks, hubs, batches }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        step = try c.decode(Step.self, forKey: .step)
        picks = try c.decode([Pick].self, forKey: .picks)
        hubs = c.lenient([Hub].self, forKey: .hubs) ?? []
        batches = c.lenient([Batch].self, forKey: .batches) ?? []
    }
}

public enum Verdict: String, Codable, Hashable, Sendable {
    case keep, pass
}

/// A track's effective keep or pass, wherever it was made, with the revision a change must name.
public struct Judgment: Codable, Hashable, Sendable {
    public var verdict: Verdict?
    public var stepID: String?
    public var at: String?
    public var source: String?
    public var revision: Int?

    enum CodingKeys: String, CodingKey { case verdict, stepID = "step_id", at, source, revision }

    public init(verdict: Verdict?, stepID: String?, at: String? = nil, source: String? = nil, revision: Int? = nil) {
        self.verdict = verdict
        self.stepID = stepID
        self.at = at
        self.source = source
        self.revision = revision
    }

    public static let none = Judgment(verdict: nil, stepID: nil, source: "none", revision: 0)
}

public struct HeardMark: Codable, Hashable, Sendable {
    public var trackKey: String
    public var firstStepID: String
    public var at: String

    enum CodingKeys: String, CodingKey { case trackKey = "track_key", firstStepID = "first_step_id", at }
}

/// Another step (maybe in another dig) where the same track was a pick.
public struct Appearance: Codable, Hashable, Sendable {
    public var digID: String
    public var digName: String
    public var stepID: String
    public var number: Int
    public var rank: Int
    public var header: String

    enum CodingKeys: String, CodingKey {
        case digID = "dig_id", digName = "dig_name", stepID = "step_id", number, rank, header
    }
}

/// The marks that change after a step is saved: keeps and passes, heard, and other appearances.
public struct Live: Codable, Hashable, Sendable {
    public var judgments: [String: Judgment]
    public var heard: [String: HeardMark]
    public var alsoIn: [String: [Appearance]]

    enum CodingKeys: String, CodingKey { case judgments, heard, alsoIn = "also_in" }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        judgments = try c.decode([String: Judgment].self, forKey: .judgments)
        heard = c.lenient([String: HeardMark].self, forKey: .heard) ?? [:]
        alsoIn = c.lenient([String: [Appearance]].self, forKey: .alsoIn) ?? [:]
    }
}

public struct JobRef: Codable, Hashable, Sendable {
    public var id: String
    public var state: String
    public var attempt: Int?
    public var cursor: Int

    public var isTerminal: Bool { ["done", "failed", "stopped", "error"].contains(state) }
}

/// GET /api/steps/{id}: the frozen snapshot, its live overlay and the job cursor, read together.
public struct StepEnvelope: Codable, Hashable, Sendable {
    public var snapshot: Snapshot
    public var live: Live
    public var job: JobRef

    public var step: Step { snapshot.step }
    public var picks: [Pick] { snapshot.picks }
}

public struct DigSummary: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var name: String
    public var createdAt: String
    public var rootStepID: String?
    public var lastStepID: String?
    public var stepCount: Int
    public var keptCount: Int

    enum CodingKeys: String, CodingKey {
        case id, name, createdAt = "created_at", rootStepID = "root_step_id", lastStepID = "last_step_id"
        case stepCount = "step_count", keptCount = "kept_count"
    }
}

public struct Dig: Codable, Hashable, Identifiable, Sendable {
    public var id: String
    public var name: String
    public var createdAt: String
    public var rootStepID: String?
    public var lastStepID: String?
    public var stepCount: Int
    public var keptCount: Int
    public var steps: [Step]
    /// Its own private YouTube Music playlist, once it was sent there (the link names the
    /// login's account), and when the playlist last read the dig.
    public var youtubePlaylistURL: String?
    public var youtubeSentAt: String?

    enum CodingKeys: String, CodingKey {
        case id, name, createdAt = "created_at", rootStepID = "root_step_id", lastStepID = "last_step_id"
        case stepCount = "step_count", keptCount = "kept_count", steps
        case youtubePlaylistURL = "youtube_playlist_url", youtubeSentAt = "youtube_sent_at"
    }
}

public struct DigList: Codable, Sendable {
    public var digs: [DigSummary]
}

public struct StepIdentity: Codable, Hashable, Sendable {
    public var digID: String
    public var stepID: String
    public var jobID: String
    public var created: Bool

    enum CodingKeys: String, CodingKey { case digID = "dig_id", stepID = "step_id", jobID = "job_id", created }
}

public struct JudgmentResult: Codable, Hashable, Sendable {
    public var trackKey: String
    public var effective: Judgment
    public var revision: Int
    public var changeID: String?

    enum CodingKeys: String, CodingKey {
        case trackKey = "track_key", effective, revision, changeID = "change_id"
    }
}

public struct SearchNote: Codable, Hashable, Sendable {
    public var code: String?
    public var message: String
    public var state: String?
}

public struct SearchResult: Codable, Hashable, Sendable {
    public var query: String
    public var items: [Point]
    public var notes: [SearchNote]
}

public struct KeyStatus: Codable, Hashable, Sendable {
    public var isSet: Bool
    public var malformed: Bool?
    public var checked: String?

    enum CodingKeys: String, CodingKey { case isSet = "set", malformed, checked }

    /// Set, sendable and not refused: what moves and searches need.
    public var usable: Bool { isSet && malformed != true && checked != "rejected" }
}

public struct ServerSettings: Codable, Hashable, Sendable {
    public var cosineToken: KeyStatus?
    public var discogsToken: KeyStatus?
    public var hasDigs: Bool?
    /// Absent from a Digger older than the YouTube Music send.
    public var youtube: YouTubeStatus?

    enum CodingKeys: String, CodingKey {
        case cosineToken = "cosine_token", discogsToken = "discogs_token", hasDigs = "has_digs", youtube
    }
}

extension ServerSettings {
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cosineToken = try c.decodeIfPresent(KeyStatus.self, forKey: .cosineToken)
        discogsToken = try c.decodeIfPresent(KeyStatus.self, forKey: .discogsToken)
        hasDigs = try c.decodeIfPresent(Bool.self, forKey: .hasDigs)
        youtube = c.lenient(YouTubeStatus.self, forKey: .youtube)
    }
}

/// The YouTube Music login on the computer: whether one is saved, what Digger's last check of
/// it said (connected, rejected, unreachable), and whether that Mac's Chrome can log in.
public struct YouTubeStatus: Codable, Hashable, Sendable {
    public var loggedIn: Bool?
    public var saved: Bool?
    public var checked: String?
    public var chrome: Bool?

    enum CodingKeys: String, CodingKey { case loggedIn = "logged_in", saved, checked, chrome }
}

/// Why a dig can't go to YouTube Music, as the web client tells: no login saved on the
/// computer, or YouTube Music signed Digger out.
public enum YouTubeLoginProblem: Equatable, Sendable {
    case missing
    case rejected

    /// From a failed send's message ("YouTube Music login missing", "... signed out").
    public init?(message: String) {
        let text = message.lowercased()
        if text.contains("signed out") { self = .rejected } else if text.contains("login missing") { self = .missing } else { return nil }
    }
}

/// What a send of a dig to its YouTube Music playlist did (the export job's result).
public struct DigSend: Codable, Hashable, Sendable {
    public var playlistURL: String?
    public var sentAt: String?
    /// YouTube Music listed every track it was given when Digger read the playlist back.
    public var verified: Bool
    public var added: Int
    public var missing: Int
    public var passed: Int
    public var noVideo: Int

    enum CodingKeys: String, CodingKey {
        case playlistURL = "playlist_url", sentAt = "sent_at", verified, added, missing, passed, noVideo = "no_video"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        playlistURL = c.lenient(String.self, forKey: .playlistURL)
        sentAt = c.lenient(String.self, forKey: .sentAt)
        verified = c.lenient(Bool.self, forKey: .verified) ?? false
        added = c.lenient(Int.self, forKey: .added) ?? 0
        missing = c.lenient(Int.self, forKey: .missing) ?? 0
        passed = c.lenient(Int.self, forKey: .passed) ?? 0
        noVideo = c.lenient(Int.self, forKey: .noVideo) ?? 0
    }
}

/// GET /api/jobs/{id} for one of Digger's older jobs, such as a send to YouTube Music.
public struct BackgroundJob: Decodable, Sendable {
    public var id: String
    public var state: String
    public var result: DigSend?
    public var error: String?

    enum CodingKeys: String, CodingKey { case id, state, result, error }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        state = try c.decode(String.self, forKey: .state)
        result = c.lenient(DigSend.self, forKey: .result)
        error = c.lenient(String.self, forKey: .error)
    }

    public var isFinished: Bool { state != "running" }
}

/// POST /api/youtube/login/chrome: the login's state (connected, rejected, unreachable, missing
/// or saved) and the settings as they are now. The cookies stay on the computer.
public struct YouTubeLoginResult: Decodable, Sendable {
    public var loggedIn: Bool
    public var state: String
    public var settings: ServerSettings?

    enum CodingKeys: String, CodingKey { case loggedIn = "logged_in", state, settings }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        loggedIn = c.lenient(Bool.self, forKey: .loggedIn) ?? false
        state = c.lenient(String.self, forKey: .state) ?? "saved"
        settings = c.lenient(ServerSettings.self, forKey: .settings)
    }
}

public struct CompanionInfo: Codable, Hashable, Sendable {
    public var ok: Bool
    public var name: String?
    public var version: String?
    public var hasDigs: Bool?

    enum CodingKeys: String, CodingKey { case ok, name, version, hasDigs = "has_digs" }
}

/// POST /api/steps.
public struct StepRequest: Codable, Hashable, Sendable {
    public var parentStepID: String?
    public var origins: [Point]
    public var move: String
    public var further: Bool
    public var sourceSelectionID: String?
    public var retryStepID: String?
    /// Make this move again as a new step beside the original (a step that stopped part way).
    public var rerunOf: String?
    public var scope: String?
    public var requestedCount: Int?

    public init(parentStepID: String?, origins: [Point], move: String, further: Bool,
                sourceSelectionID: String? = nil, retryStepID: String? = nil, rerunOf: String? = nil,
                scope: String? = nil, requestedCount: Int? = nil) {
        self.parentStepID = parentStepID
        self.origins = origins.map(\.requested)
        self.move = move
        self.further = further
        self.sourceSelectionID = sourceSelectionID
        self.retryStepID = retryStepID
        self.rerunOf = rerunOf
        self.scope = scope
        self.requestedCount = requestedCount
    }

    enum CodingKeys: String, CodingKey {
        case parentStepID = "parent_step_id", origins, move, further, sourceSelectionID = "source_selection_id"
        case retryStepID = "retry_step_id", rerunOf = "rerun_of", scope, requestedCount = "requested_count"
    }
}

extension KeyedDecodingContainer {
    /// A field that is absent, null or of an unexpected type reads as nil instead of failing the
    /// whole payload: these are display details, and the server may add to them.
    func lenient<T: Decodable>(_ type: T.Type, forKey key: Key) -> T? {
        (try? decodeIfPresent(type, forKey: key)) ?? nil
    }
}

extension Step {
    /// The notes worth showing on the step, chosen as the web client chooses them
    /// (steplist.js): its warnings (S-N-…), each Discogs problem once, and any other note
    /// written for people. Progress lines ("listening for neighbours · 0 of 25") aren't notes.
    public var warnings: [StepNote] {
        let factsCodes: Set<String> = ["discogs_budget_exhausted", "discogs_unavailable", "facts_interrupted"]
        var seenFacts: Set<String> = []
        return note.filter { note in
            guard note.message?.isEmpty == false else { return false }
            let code = note.code ?? ""
            if factsCodes.contains(code) { return seenFacts.insert(code).inserted }
            return code.hasPrefix("S-N-") || (!code.hasPrefix("S-") && !note.isProgress)
        }
    }
}
