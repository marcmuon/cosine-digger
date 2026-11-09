import Foundation
import Observation

/// One track in the play queue, with the step it came from ("from step 2: sounds like …").
public struct QueueItem: Hashable, Identifiable, Sendable {
    public var id: String
    public var stepID: String
    public var stepNumber: Int
    public var stepHeader: String
    public var trackKey: String
    public var artist: String
    public var title: String
    public var videoID: String?

    public init(pick: Pick, step: Step) {
        id = pick.id
        stepID = pick.stepID
        stepNumber = step.number
        stepHeader = step.header
        trackKey = pick.trackKey
        artist = pick.artist
        title = pick.title
        videoID = pick.videoID
    }

    public var name: String { artist.isEmpty ? title : "\(artist) - \(title)" }
}

/// What the app asks the embedded YouTube player to do.
public enum PlayerCommand: Equatable, Sendable {
    case load(videoID: String, start: Double)
    /// Load without playing: the track waits, paused, for a tap.
    case cue(videoID: String, start: Double)
    case play
    case pause
    case stop
}

/// What the embedded YouTube player reports (IFrame API states, errors and time).
public enum PlayerSignal: Equatable, Sendable {
    case ready
    case playing
    case paused
    case buffering
    case ended
    case cued
    case error(Int)
    case time(position: Double, duration: Double)
    /// The player itself went away (iOS reclaimed the web view): the track must load again.
    case lost
}

/// The visible YouTube player the app drives. The iOS app's is a WKWebView with the IFrame API.
@MainActor
public protocol PlayerEngine: AnyObject {
    func perform(_ command: PlayerCommand)
}

/// What's playing, kept apart from what's selected: selecting a track never changes playback.
/// Playback starts only from a tap, then carries on through the step as each track ends; the app
/// pauses it whenever the player can't be seen.
@Observable
@MainActor
public final class Playback {
    public enum Status: Equatable, Sendable {
        case idle
        case loading
        case playing
        case paused
        case ended
        /// The video won't play here; the message says why and what to do.
        case refused(String)
    }

    public private(set) var current: QueueItem?
    public private(set) var upNext: [QueueItem] = []
    public private(set) var status: Status = .idle
    public private(set) var position: Double = 0
    public private(set) var duration: Double = 0
    /// Set when the player was paused for the app itself (backgrounded, covered), not by a tap.
    public private(set) var pausedForApp = false

    @ObservationIgnored public weak var engine: PlayerEngine?
    @ObservationIgnored public var isPassed: (String) -> Bool = { _ in false }
    @ObservationIgnored public var onHeard: (QueueItem) -> Void = { _ in }
    @ObservationIgnored public var onRefused: (QueueItem, String) -> Void = { _, _ in }
    /// How long a refusal stays on screen before the next track starts (the web waits 2 s).
    @ObservationIgnored public var refusalDelay: Double = 2.5
    /// Listening time after which a track counts as heard (the web's 30 s).
    @ObservationIgnored public var heardAfter: Double = 30

    @ObservationIgnored private var previous: [QueueItem] = []
    @ObservationIgnored private var listened: Double = 0
    @ObservationIgnored private var lastPosition: Double?
    @ObservationIgnored private var reportedHeard = false
    @ObservationIgnored private var generation = 0
    /// The current track came from the queue moving on, not from a tap.
    @ObservationIgnored private var reachedAutomatically = false
    /// The player lost the track (see `PlayerSignal.lost`); play loads it again where it was.
    @ObservationIgnored private var needsReload = false

    public init() {}

    /// The player must be on screen while this is true.
    public var isActive: Bool { current != nil }
    public var isPlaying: Bool { status == .playing || status == .loading }

    public func isCurrent(_ pickID: String) -> Bool { current?.id == pickID }

    /// Play `items[index]`, then the rest of `items` (a step from that pick on). Explicit taps only.
    public func play(_ items: [QueueItem], startAt index: Int) {
        guard items.indices.contains(index) else { return }
        let chosen = items[index]
        if let current, current.id != chosen.id { previous.append(current) }
        upNext = items[(index + 1)...].filter { $0.videoID != nil && $0.id != chosen.id }
        start(chosen, automatically: false)
    }

    /// Pause or resume the current track.
    public func toggle() {
        guard let current else { return }
        switch status {
        case .playing, .loading:
            pause()
        case .paused:
            pausedForApp = false
            if needsReload {
                start(current, from: position, automatically: false)
            } else {
                status = .loading
                engine?.perform(.play)
            }
        case .ended, .refused, .idle:
            start(current, automatically: false)
        }
    }

    public func pause() {
        guard current != nil, isPlaying else { return }
        pausedForApp = false
        status = .paused
        engine?.perform(.pause)
    }

    /// The app went to the background or the player was covered: no hidden playback.
    public func pauseForApp() {
        guard current != nil, isPlaying else { return }
        status = .paused
        pausedForApp = true
        engine?.perform(.pause)
    }

    /// Skip to the next track (a tap): it plays.
    public func next() {
        advance(automatically: false)
    }

    public func previousTrack() {
        guard let last = previous.popLast() else {
            if let current { start(current, automatically: false) }
            return
        }
        if let current { upNext.insert(current, at: 0) }
        start(last, automatically: false)
    }

    public func stop() {
        generation += 1
        engine?.perform(.stop)
        current = nil
        upNext = []
        previous = []
        status = .idle
        position = 0
        duration = 0
    }

    /// Drop a passed track from what's coming, and skip it if it's in the player. A paused player
    /// stays paused: the next track waits for a tap.
    public func passed(trackKey: String) {
        upNext.removeAll { $0.trackKey == trackKey }
        guard current?.trackKey == trackKey else { return }
        if isPlaying {
            next()
        } else if status == .paused {
            advance(automatically: false, cueOnly: true)
        }
    }

    public func handle(_ signal: PlayerSignal) {
        guard current != nil else { return }
        switch signal {
        case .ready, .cued, .buffering:
            break
        case .playing:
            if status == .paused && pausedForApp {
                // A play the app didn't ask for while it can't be seen: hold it.
                engine?.perform(.pause)
                return
            }
            status = .playing
        case .paused:
            if status == .playing || status == .loading { status = .paused }
        case .ended:
            advance(automatically: true)
        case .error(let code):
            // While the queue is playing on its own, a video that can't play here gives way to the
            // next. One you just pressed play on stays, so nothing you didn't choose starts; and a
            // player that can't play anything stays put rather than skipping through the step.
            refuse(Self.refusal(code), moveOn: Self.isAboutTheVideo(code) && reachedAutomatically)
        case .time(let position, let duration):
            tick(position: position, duration: duration)
        case .lost:
            needsReload = true
            if isPlaying { status = .paused }
        }
    }

    /// Errors about one video (gone, refused by its owner), not about the player as a whole.
    public static func isAboutTheVideo(_ code: Int) -> Bool { [2, 5, 100, 101, 150].contains(code) }

    /// The IFrame API's error codes, worded for people.
    public static func refusal(_ code: Int) -> String {
        switch code {
        // YouTube also answers 150 for a video that's gone (seen with an ID that doesn't
        // exist), so this can't say which.
        case 101, 150: return "YouTube won't play this video in the app. It may be gone, or only allowed on YouTube itself."
        case 100: return "This video is gone from YouTube, or private."
        case 2: return "YouTube didn't recognize this video."
        case 5: return "YouTube's player couldn't play this video here."
        case 152, 153: return "YouTube didn't accept this app's embedded player (error \(code))."
        case -1: return "The video player stopped. Press play to try again."
        case -2: return "YouTube's player didn't load. Check this device's internet connection."
        default: return "YouTube couldn't play this video (error \(code))."
        }
    }

    // MARK: Private

    private func start(_ item: QueueItem, from start: Double = 0, automatically: Bool, cueOnly: Bool = false) {
        generation += 1
        let resuming = start > 0 && current?.id == item.id
        current = item
        position = resuming ? start : 0
        duration = resuming ? duration : 0
        listened = resuming ? listened : 0
        lastPosition = nil
        reportedHeard = resuming ? reportedHeard : false
        pausedForApp = false
        needsReload = false
        reachedAutomatically = automatically
        guard let video = item.videoID else {
            refuse("No YouTube video for this track. Search for it in YouTube Music.", moveOn: automatically)
            return
        }
        if cueOnly {
            status = .paused
            engine?.perform(.cue(videoID: video, start: start))
        } else {
            status = .loading
            engine?.perform(.load(videoID: video, start: start))
            engine?.perform(.play)
        }
    }

    /// On to the next track that isn't passed. The one playing goes into Previous only when
    /// another takes its place; at the end of the step it stays in the player.
    private func advance(automatically: Bool, cueOnly: Bool = false) {
        while let candidate = upNext.first {
            upNext.removeFirst()
            if !isPassed(candidate.trackKey) {
                if let current { previous.append(current) }
                start(candidate, automatically: automatically, cueOnly: cueOnly)
                return
            }
        }
        status = .ended
        engine?.perform(.pause)
    }

    private func refuse(_ message: String, moveOn: Bool) {
        guard let item = current else { return }
        status = .refused(message)
        onRefused(item, message)
        guard moveOn, !upNext.isEmpty else { return }
        let refusedGeneration = generation
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64((self?.refusalDelay ?? 0) * 1_000_000_000))
            guard let self, self.generation == refusedGeneration, case .refused = self.status else { return }
            self.advance(automatically: true)
        }
    }

    private func tick(position: Double, duration: Double) {
        self.position = position
        self.duration = duration
        defer { lastPosition = position }
        guard status == .playing, let last = lastPosition else { return }
        let delta = position - last
        if delta > 0 && delta <= 3 { listened += delta }  // a seek never counts as listening
        if !reportedHeard, listened >= heardAfter, let current {
            reportedHeard = true
            onHeard(current)
        }
    }
}
