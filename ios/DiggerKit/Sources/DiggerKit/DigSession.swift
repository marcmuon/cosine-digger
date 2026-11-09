import Foundation
import Observation

/// Where the app keeps its pairing and where you were. The iOS app stores the token in the
/// Keychain and the rest in UserDefaults; tests use `MemoryStore`.
@MainActor
public protocol SessionStore: AnyObject {
    func loadConnection() -> Connection?
    func saveConnection(_ connection: Connection?)
    var lastDigID: String? { get set }
    func lastStepID(dig: String) -> String?
    func setLastStepID(_ stepID: String?, dig: String)
}

@MainActor
public final class MemoryStore: SessionStore {
    public var connection: Connection?
    public var lastDigID: String?
    public var steps: [String: String] = [:]

    public init(connection: Connection? = nil) { self.connection = connection }
    public func loadConnection() -> Connection? { connection }
    public func saveConnection(_ connection: Connection?) { self.connection = connection }
    public func lastStepID(dig: String) -> String? { steps[dig] }
    public func setLastStepID(_ stepID: String?, dig: String) { steps[dig] = stepID }
}

/// A short message at the bottom of the screen, sometimes with Undo or a link.
public struct Notice: Identifiable, Equatable, Sendable {
    public let id = UUID()
    public var message: String
    public var canUndo = false
    public var link: URL?
    public var isError = false

    public init(_ message: String, canUndo: Bool = false, link: URL? = nil, isError: Bool = false) {
        self.message = message
        self.canUndo = canUndo
        self.link = link
        self.isError = isError
    }
}

/// A dig's send to YouTube Music, as this session follows it.
public enum YouTubeSend: Equatable, Sendable {
    case sending
    case sent(DigSend)
    case failed(String)
}

/// What the last "Log in from Chrome" said.
public struct YouTubeLoginReport: Equatable, Sendable {
    public var message: String
    public var isError: Bool
}

/// A pick to bring into view: going back to where a move started, or to what's playing.
public struct Reveal: Equatable, Sendable {
    public let stepID: String
    public let pickID: String
    let id = UUID()
}

/// A step in the trail, with its depth in the dig's tree.
public struct TrailNode: Identifiable, Hashable, Sendable {
    public var step: Step
    public var depth: Int
    public var id: String { step.id }
}

/// The whole digging session the views render: the paired Digger, the open dig and its steps,
/// what's selected in each step, keeps and passes, heard marks, and playback.
///
/// Digger stays the source of truth. Steps are read as the server's snapshot (picks, live
/// marks and job cursor in one transaction) and re-read while their job runs, so a step that
/// was running when the phone slept is whole again when it wakes.
@Observable
@MainActor
public final class DigSession {
    public enum Phase: Equatable, Sendable {
        case unpaired
        case connecting
        case ready
        case offline(String)
        case unauthorized(String)
    }

    public private(set) var phase: Phase
    public private(set) var connection: Connection?
    public private(set) var activeURL: URL?
    public private(set) var serverName: String?
    public private(set) var settings: ServerSettings?
    public private(set) var recentDigs: [DigSummary] = []
    public private(set) var dig: Dig?
    public private(set) var currentStepID: String?
    public private(set) var envelopes: [String: StepEnvelope] = [:]
    public private(set) var judgments: [String: Judgment] = [:]
    public private(set) var heard: [String: HeardMark] = [:]
    public private(set) var stepErrors: [String: String] = [:]
    /// A move being submitted, as "pickID/move", so its button can show progress.
    public private(set) var submitting: String?
    public private(set) var loadingDig = false
    /// The dig being opened, so its row can show progress.
    public private(set) var openingDigID: String?
    /// Why the list of digs couldn't be read (nil when it was, or hasn't been tried).
    public private(set) var homeError: String?
    /// The list of digs is being read, so an empty list doesn't mean there are none.
    public private(set) var loadingHome = false
    /// The latest pick to scroll into view, set when a step opens with a pick selected.
    public private(set) var reveal: Reveal?
    public var notice: Notice?
    /// The pick selected in each step: going back to a step brings its selection back.
    public var selectionByStep: [String: String] = [:]
    /// The pick at the top of each step's list when you left it.
    public var scrollAnchorByStep: [String: String] = [:]
    /// Each dig's send to YouTube Music while this session follows it, and how it ended.
    public private(set) var youtubeSends: [String: YouTubeSend] = [:]
    /// Waiting for the computer to log in from its Chrome (macOS asks there first).
    public private(set) var youtubeLoggingIn = false
    public private(set) var youtubeLoginReport: YouTubeLoginReport?

    public let playback: Playback

    @ObservationIgnored private let store: SessionStore
    @ObservationIgnored private let makeClient: (URL, String) -> APIClient
    @ObservationIgnored private var api: APIClient?
    private var pending: [String: Verdict?] = [:]
    private var undoStack: [(trackKey: String, stepID: String, changeID: String, revision: Int, name: String, what: String)] = []
    @ObservationIgnored private var followers: [String: Task<Void, Never>] = [:]
    /// The latest keep, pass or undo asked for on each track (see `oneAtATime`).
    @ObservationIgnored private var judging: [String: Task<Void, Never>] = [:]
    /// The newest pair() or connect(): only it may set the phase when it finishes.
    @ObservationIgnored private var attempt = 0
    /// Seconds between reads of a running step.
    @ObservationIgnored public var pollInterval: Double = 1.0

    public init(store: SessionStore, playback: Playback? = nil,
                makeClient: @escaping (URL, String) -> APIClient = { APIClient(baseURL: $0, token: $1) }) {
        self.store = store
        self.makeClient = makeClient
        self.playback = playback ?? Playback()
        let saved = store.loadConnection()
        self.connection = saved
        self.phase = saved == nil ? .unpaired : .connecting
        self.playback.isPassed = { [weak self] key in self?.verdict(for: key) == .pass }
        self.playback.onHeard = { [weak self] item in self?.markHeard(item) }
        self.playback.onRefused = { [weak self] item, message in
            let link = URL(string: item.videoID.map { "https://music.youtube.com/watch?v=\($0)" }
                           ?? "https://music.youtube.com/search?q=\(APIClient.segment(item.name))")
            self?.notice = Notice("\(item.title): \(message)", link: link, isError: true)
        }
    }

    // MARK: Pairing and connection

    /// Pair with a Digger: the first address that answers with this token wins.
    /// A link that doesn't work never undoes a pairing that does.
    public func pair(_ connection: Connection) async throws {
        attempt += 1
        let mine = attempt
        let before = phase
        phase = .connecting
        let reached = await reach(connection.urls, token: connection.token)
        guard mine == attempt else { throw CancellationError() }
        let failure: Error
        switch reached {
        case .success(let (client, url, info)):
            // Another computer's Digger: nothing read from the old one carries over.
            if let old = self.connection, Set(old.urls).isDisjoint(with: connection.urls) { closeEverything() }
            self.connection = connection
            store.saveConnection(connection)
            adopt(client, url: url, name: info.name ?? connection.name)
            await loadHome()
            return
        case .failure(let error):
            failure = error
        }
        // The link didn't work: the pairing this device had stands, as it was.
        if self.connection == nil {
            phase = .unpaired
        } else if before == .connecting {
            // This attempt cut short the saved pairing's reconnect, whose answer is unknown: ask again.
            Task { [weak self] in await self?.connect() }
        } else {
            // Ready, unreachable or refused before, and still so; the new link's own problem is thrown.
            phase = before
        }
        throw failure
    }

    /// Reconnect with the saved pairing (at launch, or when the app comes back).
    public func connect() async {
        guard let connection else { phase = .unpaired; return }
        attempt += 1
        let mine = attempt
        if phase != .ready { phase = .connecting }
        let ordered = activeURL.map { url in [url] + connection.urls.filter { $0 != url } } ?? connection.urls
        let reached = await reach(ordered, token: connection.token)
        // A pairing that started meanwhile (a new link opened) owns the outcome.
        guard mine == attempt else { return }
        switch reached {
        case .success(let (client, url, info)):
            let first = api == nil
            adopt(client, url: url, name: info.name ?? connection.name)
            if first || dig == nil { await loadHome() } else { await refresh() }
        case .failure(let failure):
            phase = phaseFor(failure)
            if case .unauthorized = phase { playback.pauseForApp() }
        }
    }

    /// The first of `urls` whose Digger answers this token. They're tried together, so an address
    /// that can't be reached from here (the home network, away from home) doesn't hold up one
    /// that can (the tailnet). A refused token speaks for the Digger; otherwise the problem
    /// reported is the first address's.
    private func reach(_ urls: [URL], token: String) async -> Result<(APIClient, URL, CompanionInfo), Error> {
        guard !urls.isEmpty else { return .failure(APIError.unreachable("No address to try.")) }
        let clients = urls.map { makeClient($0, token) }
        return await withTaskGroup(of: (Int, Result<CompanionInfo, Error>).self) { group in
            for (index, client) in clients.enumerated() {
                group.addTask {
                    do { return (index, .success(try await client.companion())) } catch { return (index, .failure(error)) }
                }
            }
            var failures: [Int: Error] = [:]
            for await (index, result) in group {
                switch result {
                case .success(let info):
                    group.cancelAll()
                    return .success((clients[index], urls[index], info))
                case .failure(let error):
                    failures[index] = error
                }
            }
            let refused = failures.sorted { $0.key < $1.key }.first { entry in
                if case .unauthorized? = entry.value as? APIError { return true }
                return false
            }
            return .failure(refused?.value ?? failures.sorted { $0.key < $1.key }.first?.value
                            ?? APIError.unreachable("No address to try."))
        }
    }

    /// Unpair this device: forget the token, stop playback, close the dig.
    public func forget() {
        attempt += 1
        closeEverything()
        store.saveConnection(nil)
        connection = nil
        api = nil
        activeURL = nil
        serverName = nil
        settings = nil
        phase = .unpaired
    }

    /// Drop everything read from a Digger: on unpairing, or pairing with another one.
    private func closeEverything() {
        playback.stop()
        followers.values.forEach { $0.cancel() }
        followers = [:]
        judging = [:]
        dig = nil
        envelopes = [:]
        currentStepID = nil
        recentDigs = []
        judgments = [:]
        heard = [:]
        stepErrors = [:]
        pending = [:]
        undoStack = []
        selectionByStep = [:]
        scrollAnchorByStep = [:]
        youtubeSends = [:]
        youtubeLoggingIn = false
        youtubeLoginReport = nil
        notice = nil
        reveal = nil
        homeError = nil
    }

    private func adopt(_ client: APIClient, url: URL, name: String?) {
        api = client
        activeURL = url
        serverName = name
        phase = .ready
    }

    private func phaseFor(_ error: Error) -> Phase {
        if case APIError.unauthorized(let message)? = error as? APIError { return .unauthorized(message) }
        return .offline((error as? LocalizedError)?.errorDescription ?? "Can't reach Digger.")
    }

    /// Route a failed request: a refused token or a lost connection changes the whole screen.
    @discardableResult
    private func report(_ error: Error, stepID: String? = nil) -> String {
        if error is CancellationError { return "" }
        let message = (error as? LocalizedError)?.errorDescription ?? String(describing: error)
        switch error as? APIError {
        case .unauthorized?:
            phase = .unauthorized(message)
            // The dig (and the player with it) leaves the screen: nothing plays unseen.
            playback.pauseForApp()
        case .unreachable?: phase = .offline(message)
        default: break
        }
        if let stepID { stepErrors[stepID] = message }
        return message
    }

    /// Digger answered: a banner saying it can't be reached is out of date.
    private func reached() {
        if case .offline = phase { phase = .ready }
    }

    // MARK: Loading

    /// Settings, recent digs, and the dig you were in.
    public func loadHome() async {
        guard let api else { return }
        loadingHome = true
        defer { loadingHome = false }
        do {
            async let settings = api.settings()
            async let digs = api.digs(limit: 20)
            self.settings = try await settings
            self.recentDigs = try await digs
            homeError = nil
            reached()
        } catch {
            homeError = report(error)
            return
        }
        let preferred = dig?.id ?? store.lastDigID
        if let id = preferred, recentDigs.contains(where: { $0.id == id }) || dig?.id == id {
            await openDig(id)
        } else if let first = recentDigs.first {
            await openDig(first.id)
        }
    }

    public func reloadRecentDigs() async {
        guard let api else { return }
        do {
            recentDigs = try await api.digs(limit: 20)
            homeError = nil
            reached()
        } catch {
            homeError = report(error)
        }
    }

    /// Open a dig at a step (the one you were last on in it, or its newest).
    @discardableResult
    public func openDig(_ id: String, at stepID: String? = nil) async -> Bool {
        guard let api else { return false }
        loadingDig = true
        openingDigID = id
        defer {
            loadingDig = false
            openingDigID = nil
        }
        do {
            let loaded = try await api.dig(id)
            reached()
            if dig?.id != id {
                envelopes = [:]
                followers.values.forEach { $0.cancel() }
                followers = [:]
            }
            dig = loaded
            store.lastDigID = id
            let target = stepID ?? store.lastStepID(dig: id).flatMap { saved in
                loaded.steps.contains { $0.id == saved } ? saved : nil
            } ?? loaded.lastStepID
            if let target { open(stepID: target) }
            // Every step of the dig, for its keeps and marks in the trail, after the one on screen.
            let others = loaded.steps.map(\.id).filter { envelopes[$0] == nil && $0 != target }
            Task { [weak self] in
                for stepID in others {
                    guard let self, self.dig?.id == id else { return }
                    if self.envelopes[stepID] == nil { await self.refreshStep(stepID) }
                }
            }
            return true
        } catch {
            if case .rejected(status: 404, _)? = error as? APIError {
                // This Digger no longer has it: its data was reset, or the dig was deleted there.
                // What's on screen from it goes, and the newest dig it does have opens instead.
                reached()
                let wasOpen = dig?.id == id
                if wasOpen { closeDig() }
                if store.lastDigID == id { store.lastDigID = nil }
                recentDigs.removeAll { $0.id == id }
                notice = Notice("That dig isn't on Digger any more.", isError: true)
                if wasOpen, let newest = recentDigs.first { await openDig(newest.id) }
                return false
            }
            let message = report(error)
            if !message.isEmpty, case .ready = phase { notice = Notice("Couldn't open that dig. \(message)", isError: true) }
            return false
        }
    }

    /// Leave the open dig, keeping the pairing and the list of digs.
    private func closeDig() {
        playback.stop()
        followers.values.forEach { $0.cancel() }
        followers = [:]
        dig = nil
        envelopes = [:]
        currentStepID = nil
        stepErrors = [:]
        judgments = [:]
        heard = [:]
        undoStack = []
        selectionByStep = [:]
        scrollAnchorByStep = [:]
        reveal = nil
    }

    /// Show a step; `select` restores a selection (going back to where a move started).
    public func open(stepID: String, select pickID: String? = nil) {
        currentStepID = stepID
        if let pickID {
            selectionByStep[stepID] = pickID
            reveal = Reveal(stepID: stepID, pickID: pickID)
        }
        if let digID = dig?.id { store.setLastStepID(stepID, dig: digID) }
        if envelopes[stepID] == nil || envelopes[stepID]?.job.isTerminal == false {
            follow(stepID)
        }
    }

    /// The step's list scrolled `reveal` into view; a later visit keeps its own scroll position.
    public func revealed(_ shown: Reveal) {
        if reveal == shown { reveal = nil }
    }

    /// The step this one was made from, with the pick the move started at selected again.
    public func openParent() {
        guard let step = currentStep, let parentID = step.parentID else { return }
        let picks = envelopes[parentID]?.picks ?? []
        let source = Moves.sourcePickID(selectionID: step.context.sourceSelectionID, parentStepID: parentID, picks: picks)
        open(stepID: parentID, select: source ?? selectionByStep[parentID])
    }

    /// Jump to what's playing: its step, with it selected.
    public func goToPlaying() {
        guard let item = playback.current else { return }
        open(stepID: item.stepID, select: item.id)
    }

    /// Read the dig and the current step again (when the app comes back to the foreground).
    public func refresh() async {
        guard let api else { return }
        // A YouTube Music login made on the computer meanwhile shows here too.
        if let fresh = try? await api.settings() {
            settings = fresh
            settleLoginProblems()
        }
        if let digID = dig?.id {
            await refreshDig(digID)
        } else {
            await reloadRecentDigs()
        }
        if let stepID = currentStepID { await refreshStep(stepID) }
        for (id, envelope) in envelopes where !envelope.job.isTerminal { follow(id) }
    }

    public func refreshStep(_ stepID: String) async {
        guard let api else { return }
        do {
            let envelope = try await api.step(stepID)
            envelopes[stepID] = envelope
            stepErrors[stepID] = nil
            merge(envelope.live)
            reached()
        } catch {
            report(error, stepID: stepID)
        }
    }

    private func refreshDig(_ digID: String) async {
        guard let api else { return }
        do {
            let loaded = try await api.dig(digID)
            if dig?.id == digID { dig = loaded }
            reached()
        } catch {
            report(error)
        }
    }

    /// Re-read a running step until its job ends, then the dig (for the trail's counts).
    private func follow(_ stepID: String) {
        guard followers[stepID] == nil else { return }
        followers[stepID] = Task { [weak self] in
            var failures = 0
            while !Task.isCancelled {
                guard let self else { return }
                await self.refreshStep(stepID)
                if let envelope = self.envelopes[stepID], envelope.job.isTerminal {
                    if let digID = self.dig?.id { await self.refreshDig(digID) }
                    break
                }
                failures = self.stepErrors[stepID] == nil ? 0 : failures + 1
                if failures >= 8 { break }  // Keep the error on screen; Retry or coming back reads it again.
                let wait = self.pollInterval * (failures == 0 ? 1 : Double(min(failures + 1, 5)))
                try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            }
            self?.followers[stepID] = nil
        }
    }

    private func merge(_ live: Live) {
        for (key, judgment) in live.judgments {
            if let known = judgments[key], (known.revision ?? 0) > (judgment.revision ?? 0) { continue }
            judgments[key] = judgment
        }
        heard.merge(live.heard) { _, new in new }
    }

    // MARK: What the views read

    public var currentEnvelope: StepEnvelope? { currentStepID.flatMap { envelopes[$0] } }

    public var currentStep: Step? {
        guard let id = currentStepID else { return nil }
        return envelopes[id]?.step ?? dig?.steps.first { $0.id == id }
    }

    public func step(_ id: String) -> Step? { envelopes[id]?.step ?? dig?.steps.first { $0.id == id } }

    public func picks(in stepID: String) -> [Pick] { envelopes[stepID]?.picks ?? [] }

    public func pick(id: String) -> Pick? {
        guard let separator = id.lastIndex(of: ":") else { return nil }
        let stepID = String(id[..<separator])
        return envelopes[stepID]?.picks.first { $0.id == id }
    }

    /// The selected pick in the current step (nil: the step itself).
    public var selectedPickID: String? {
        get { currentStepID.flatMap { selectionByStep[$0] } }
        set {
            guard let stepID = currentStepID else { return }
            selectionByStep[stepID] = newValue
        }
    }

    public var selectedPick: Pick? { selectedPickID.flatMap(pick(id:)) }

    /// A track's keep or pass, counting a change still on its way to Digger.
    public func verdict(for trackKey: String) -> Verdict? {
        if let waiting = pending[trackKey] { return waiting }
        return judgments[trackKey]?.verdict
    }

    public func isHeard(_ trackKey: String) -> Bool { heard[trackKey] != nil }

    public var discogsUsable: Bool { settings?.discogsToken?.usable ?? true }

    public func choices(for pick: Pick) -> [MoveChoice] { Moves.choices(for: pick, discogsUsable: discogsUsable) }

    /// The dig's steps as a tree, depth first in the order they were made.
    public var trail: [TrailNode] {
        guard let dig else { return [] }
        let steps = dig.steps.map { listed in envelopes[listed.id].map { merged($0.step, into: listed) } ?? listed }
        let children = Dictionary(grouping: steps, by: { $0.parentID ?? "" })
        var result: [TrailNode] = []
        func visit(_ parent: String, depth: Int) {
            for step in (children[parent] ?? []).sorted(by: { $0.number < $1.number }) {
                result.append(TrailNode(step: step, depth: depth))
                visit(step.id, depth: depth + 1)
            }
        }
        visit("", depth: 0)
        // Steps whose parent is missing still show, at the top level.
        let shown = Set(result.map(\.id))
        result += steps.filter { !shown.contains($0.id) }.map { TrailNode(step: $0, depth: 0) }
        return result
    }

    private func merged(_ fresh: Step, into listed: Step) -> Step {
        var step = fresh
        step.counts = listed.counts
        step.currentJobID = listed.currentJobID
        return step
    }

    /// Every pick kept in this dig, once per track, in step order.
    public var keptInDig: [Pick] {
        guard let dig else { return [] }
        var seen = Set<String>()
        var result: [Pick] = []
        for step in dig.steps.sorted(by: { $0.number < $1.number }) {
            for pick in picks(in: step.id) where verdict(for: pick.trackKey) == .keep && seen.insert(pick.trackKey).inserted {
                result.append(pick)
            }
        }
        return result
    }

    public func counts(for stepID: String) -> (kept: Int, passed: Int) {
        let picks = picks(in: stepID)
        guard !picks.isEmpty else {
            let counts = dig?.steps.first { $0.id == stepID }?.counts
            return (counts?.kept ?? 0, counts?.passed ?? 0)
        }
        return (picks.filter { verdict(for: $0.trackKey) == .keep }.count,
                picks.filter { verdict(for: $0.trackKey) == .pass }.count)
    }

    public func children(of stepID: String) -> [Step] {
        (dig?.steps ?? []).filter { $0.parentID == stepID }.sorted { $0.number < $1.number }
    }

    // MARK: Listening

    /// Play a pick, then the rest of its step.
    public func play(_ pick: Pick) {
        guard let step = step(pick.stepID) else { return }
        let picks = picks(in: pick.stepID)
        let items = picks.map { QueueItem(pick: $0, step: step) }
        guard let index = picks.firstIndex(where: { $0.id == pick.id }) else { return }
        playback.play(items, startAt: index)
    }

    /// Play a step from its first pick that isn't passed.
    public func playStep(_ stepID: String) {
        guard let step = step(stepID) else { return }
        let picks = picks(in: stepID).filter { verdict(for: $0.trackKey) != .pass && $0.videoID != nil }
        guard !picks.isEmpty else {
            notice = Notice("Nothing in this step can play here.")
            return
        }
        playback.play(picks.map { QueueItem(pick: $0, step: step) }, startAt: 0)
    }

    private func markHeard(_ item: QueueItem) {
        guard heard[item.trackKey] == nil, let api else { return }
        Task {
            do {
                heard[item.trackKey] = try await api.heard(trackKey: item.trackKey, stepID: item.stepID)
                reached()
            } catch {
                report(error)
            }
        }
    }

    // MARK: Keep and pass

    /// Keep or pass `pick`; the same verdict again clears it. The change names the revision it
    /// read, so a change made meanwhile elsewhere is refused instead of overwritten. Taps on one
    /// track go one at a time, each reading what the one before it made (a double tap is keep,
    /// then clear, not a conflict with itself).
    public func judge(_ pick: Pick, _ verdict: Verdict) async {
        await oneAtATime(pick.trackKey) { [weak self] in await self?.send(pick, verdict) }
    }

    /// Changes to one track's judgment, in the order they were asked for.
    private func oneAtATime(_ key: String, _ work: @escaping @MainActor () async -> Void) async {
        let previous = judging[key]
        let task = Task { @MainActor in
            await previous?.value
            await work()
        }
        judging[key] = task
        await task.value
        if judging[key] == task { judging[key] = nil }
    }

    private func send(_ pick: Pick, _ verdict: Verdict) async {
        guard let api else { return }
        let key = pick.trackKey
        let before = self.verdict(for: key)
        let desired: Verdict? = before == verdict ? nil : verdict
        pending[key] = .some(desired)
        if desired == .pass { playback.passed(trackKey: key) }
        defer { pending[key] = nil }
        do {
            let result = try await api.judge(trackKey: key, stepID: pick.stepID, verdict: desired,
                                             expectedRevision: judgments[key]?.revision ?? 0)
            var effective = result.effective
            effective.revision = result.revision
            judgments[key] = effective
            reached()
            let wording = desired == .keep ? (before == .pass ? "Kept, no longer passed: \(pick.title)" : "Kept \(pick.title)")
                : desired == .pass ? "Passed \(pick.title)" : "Cleared \(pick.title)"
            if let change = result.changeID {
                undoStack.append((key, pick.stepID, change, result.revision, pick.title, wording))
            }
            notice = Notice(wording, canUndo: result.changeID != nil)
        } catch APIError.conflict {
            await refreshStep(pick.stepID)
            notice = Notice("\(pick.title) was changed elsewhere, maybe on your computer. Look again and retry.", isError: true)
        } catch {
            notice = Notice(report(error), isError: true)
        }
    }

    public var canUndo: Bool { !undoStack.isEmpty }

    /// The track the next Undo puts back.
    public var undoName: String? { undoStack.last?.name }

    /// What the next Undo takes back, as its notice said it ("Kept …", "Passed …").
    public var undoSummary: String? { undoStack.last?.what }

    /// Undo the latest keep or pass made on this device. It names the track's current revision
    /// (each change and undo moves it on), so undoing two changes in a row works.
    public func undo() async {
        guard let key = undoStack.last?.trackKey else { return }
        await oneAtATime(key) { [weak self] in await self?.sendUndo(key) }
    }

    /// Undo the latest change, if it's still on `key` (the track whose changes this waited behind).
    private func sendUndo(_ key: String) async {
        guard let api, undoStack.last?.trackKey == key, let last = undoStack.popLast() else { return }
        do {
            let result = try await api.judge(trackKey: last.trackKey, stepID: last.stepID, verdict: nil,
                                             expectedRevision: judgments[last.trackKey]?.revision ?? last.revision,
                                             undoChangeID: last.changeID)
            var effective = result.effective
            effective.revision = result.revision
            judgments[last.trackKey] = effective
            reached()
            notice = Notice("Undid \(last.name)")
        } catch APIError.conflict {
            await refreshStep(last.stepID)
            notice = Notice("\(last.name) changed again since; nothing to undo.", isError: true)
        } catch {
            // Nothing changed on Digger: Undo is still there for when it can be reached.
            undoStack.append(last)
            notice = Notice(report(error), isError: true)
        }
    }

    // MARK: Moves

    /// Make a move from a pick (sounds like this track, its label, artist or a credited person)
    /// and open the new step. Making the same move again opens the step already made.
    public func branch(from pick: Pick, move: MoveKind, origin: Point, further: Bool = false) async {
        guard let api, submitting == nil else { return }
        let token = "\(pick.id)/\(move.rawValue)"
        submitting = token
        defer { submitting = nil }
        let request = StepRequest(parentStepID: pick.stepID, origins: [origin], move: move.rawValue, further: further,
                                  sourceSelectionID: Moves.selectionID(for: origin))
        do {
            let identity = try await api.createStep(request, idempotencyKey: UUID().uuidString)
            reached()
            selectionByStep[pick.stepID] = pick.id
            await refreshDig(identity.digID)
            open(stepID: identity.stepID)
            if !identity.created { notice = Notice("You made this move before: here it is.") }
        } catch {
            notice = Notice(report(error), isError: true)
        }
    }

    public func isSubmitting(_ pick: Pick, _ move: MoveKind) -> Bool { submitting == "\(pick.id)/\(move.rawValue)" }

    /// What's being made right now, for a progress line.
    public var workingDescription: String? {
        guard let token = submitting else { return nil }
        if token.hasPrefix("rerun/") {
            let stepID = String(token.dropFirst("rerun/".count))
            return "Making \(step(stepID).map { "step \($0.number)" } ?? "the step") again…"
        }
        guard let slash = token.lastIndex(of: "/"), let move = MoveKind(rawValue: String(token[token.index(after: slash)...])),
              let source = pick(id: String(token[..<slash]))?.title
        else { return "Making the step…" }
        switch move {
        case .track: return "Finding what sounds like \(source)…"
        case .label: return "Digging into the label of \(source)…"
        case .artist: return "Digging into the artist of \(source)…"
        case .person: return "Following a credit on \(source)…"
        }
    }

    /// Read a step again, and keep reading it while its job runs (after a refresh or Try again).
    public func resume(_ stepID: String) async {
        await refreshStep(stepID)
        if envelopes[stepID]?.job.isTerminal == false { follow(stepID) }
    }

    /// Make a step that stopped part way again, as a new step beside it (its picks stay).
    public func rerun(_ step: Step) async {
        guard let api, !step.isRunning, submitting == nil else { return }
        submitting = "rerun/\(step.id)"
        defer { submitting = nil }
        let request = StepRequest(parentStepID: step.parentID, origins: step.origins, move: step.move, further: step.further,
                                  rerunOf: step.id, scope: step.context.theseScope,
                                  requestedCount: step.context.requestedCount)
        do {
            let identity = try await api.createStep(request, idempotencyKey: UUID().uuidString)
            reached()
            await refreshDig(identity.digID)
            open(stepID: identity.stepID)
        } catch {
            notice = Notice(report(error, stepID: step.id), isError: true)
        }
    }

    public var isRerunning: Bool { submitting?.hasPrefix("rerun/") == true }

    /// Run a failed or stopped step that found nothing again, in place.
    public func retry(_ step: Step) async {
        guard let api, step.isFailed else { return }
        let request = StepRequest(parentStepID: step.parentID, origins: step.origins, move: step.move, further: step.further,
                                  retryStepID: step.id, scope: step.context.theseScope,
                                  requestedCount: step.context.requestedCount)
        do {
            let identity = try await api.createStep(request, idempotencyKey: UUID().uuidString)
            stepErrors[step.id] = nil
            open(stepID: identity.stepID)
            follow(identity.stepID)
        } catch {
            notice = Notice(report(error, stepID: step.id), isError: true)
        }
    }

    // MARK: YouTube Music

    /// Why this dig can't be sent to YouTube Music, as the web client tells: a send that failed
    /// for want of a login, else the computer's login as Digger last checked it. Nil when it can.
    public func youtubeLoginProblem(digID: String) -> YouTubeLoginProblem? {
        if case .failed(let message)? = youtubeSends[digID], let problem = YouTubeLoginProblem(message: message) {
            return problem
        }
        guard let youtube = settings?.youtube else { return nil }
        if youtube.saved == false { return .missing }
        return youtube.checked == "rejected" ? .rejected : nil
    }

    /// Forget a dig's failed send, as the web client does when Connect is pressed: the fix is
    /// under way, and the settings say whether it worked.
    public func clearYouTubeSend(digID: String) {
        if case .failed? = youtubeSends[digID] { youtubeSends[digID] = nil }
    }

    /// Settings read again: a send that failed for want of a login no longer blocks its dig once
    /// the computer's login was fixed (in the browser app's Setup, say).
    private func settleLoginProblems() {
        guard let youtube = settings?.youtube else { return }
        youtubeSends = youtubeSends.filter { _, send in
            guard case .failed(let message) = send, let problem = YouTubeLoginProblem(message: message) else { return true }
            switch problem {
            case .missing: return youtube.saved != true
            case .rejected: return youtube.checked != "connected"
            }
        }
    }

    private enum SendFollowed {
        case finished(BackgroundJob)
        /// Digger no longer knows the job: it restarted, and the dig says whether the send got there.
        case gone
        /// Unpaired, or paired with another Digger, meanwhile.
        case unpaired
        case tooLong
    }

    /// Send the open dig to its own private YouTube Music playlist (or bring the playlist up to
    /// date: every step's picks in order, passes left out) and follow the send until it ends.
    public func sendToYouTubeMusic() async {
        guard let api, let dig, let pairing = connection else { return }
        let digID = dig.id, name = dig.name
        if youtubeSends[digID] == .sending { return }
        youtubeSends[digID] = .sending
        do {
            let jobID = try await api.sendToYouTubeMusic(digID: digID)
            reached()
            let followed = try await followSend(jobID, pairing: pairing)
            guard connection == pairing else { youtubeSends[digID] = nil; return }
            switch followed {
            case .finished(let job):
                if job.state == "done", let result = job.result {
                    youtubeSends[digID] = .sent(result)
                    notice = Notice("\(name) is in YouTube Music: \(Self.tracks(result.added)).", link: result.playlistURL.flatMap(Self.playlistLink))
                } else if job.state == "done" {
                    youtubeSends[digID] = nil  // sent, but its report didn't read: the dig has the link
                    notice = Notice("\(name) is in YouTube Music.")
                } else {
                    failSend(digID, name: name, job.error ?? "YouTube Music didn't take the playlist.")
                }
                await refreshAfterSend(digID)
            case .gone:
                youtubeSends[digID] = nil
                await refreshAfterSend(digID)
                if youtubePlaylist(digID: digID) == nil {
                    notice = Notice("Digger restarted while sending \(name), so it may not be in YouTube Music. Send it again.",
                                    isError: true)
                }
            case .unpaired:
                youtubeSends[digID] = nil
            case .tooLong:
                failSend(digID, name: name, "Digger is still sending this dig. Open YouTube Music to check, or send it again later.")
                await refreshAfterSend(digID)
            }
        } catch {
            guard connection == pairing else { youtubeSends[digID] = nil; return }
            let message = report(error)
            if message.isEmpty { youtubeSends[digID] = nil } else { failSend(digID, name: name, message) }
        }
    }

    /// Re-read the send's job until it ends, on whichever client the pairing has now (coming back
    /// to the app reconnects with a new one). A dropped answer or two doesn't end it: the send goes
    /// on on the computer.
    private func followSend(_ jobID: String, pairing: Connection) async throws -> SendFollowed {
        let deadline = Date().addingTimeInterval(15 * 60)
        var dropped = 0
        while true {
            guard connection == pairing, let api else { return .unpaired }
            do {
                let job = try await api.backgroundJob(jobID)
                dropped = 0
                if job.isFinished { return .finished(job) }
            } catch APIError.rejected(status: 404, _) {
                return .gone
            } catch let error as APIError where error.isTransport && dropped < 5 {
                dropped += 1
            }
            // After a poll, not before: back from 15 minutes away, the send has likely finished.
            if Date() >= deadline { return .tooLong }
            try await Task.sleep(nanoseconds: UInt64(pollInterval * 1_000_000_000))
        }
    }

    private func refreshAfterSend(_ digID: String) async {
        if let api, let fresh = try? await api.settings() { settings = fresh }
        settleLoginProblems()
        await refreshDig(digID)
    }

    private func failSend(_ digID: String, name: String, _ message: String) {
        youtubeSends[digID] = .failed(message)
        switch YouTubeLoginProblem(message: message) {
        case .rejected?: notice = Notice("YouTube Music signed Digger out. Log in again to send \(name).", isError: true)
        case .missing?: notice = Notice("Connect YouTube Music to send \(name).", isError: true)
        case nil: notice = Notice("Couldn't send \(name) to YouTube Music: \(message)", isError: true)
        }
    }

    private static func tracks(_ count: Int) -> String { count == 1 ? "1 track" : "\(count) tracks" }

    /// A playlist link Digger gave, opened only as a web link (YouTube Music's app takes it over).
    private static func playlistLink(_ text: String) -> URL? {
        guard let url = URL(string: text), url.scheme == "https" else { return nil }
        return url
    }

    /// The open dig's YouTube Music playlist: the dig's, or a send's that finished before the dig
    /// was read again.
    public func youtubePlaylist(digID: String) -> URL? {
        if let dig, dig.id == digID, let link = dig.youtubePlaylistURL { return Self.playlistLink(link) }
        if case .sent(let result)? = youtubeSends[digID] { return result.playlistURL.flatMap(Self.playlistLink) }
        return nil
    }

    /// Steps of the open dig that aren't in its playlist: their picks came after the playlist
    /// last read the dig, or are still coming.
    public func youtubeStepsBehind(digID: String) -> Int {
        guard let dig, dig.id == digID, youtubePlaylist(digID: digID) != nil else { return 0 }
        var cutoff = dig.youtubeSentAt
        if case .sent(let result)? = youtubeSends[digID], let sentAt = result.sentAt { cutoff = sentAt }
        guard let sentAt = cutoff.flatMap(Self.date) else { return 0 }
        return dig.steps.filter { step in
            guard let done = (step.picksDoneAt ?? step.finishedAt).flatMap(Self.date) else { return true }
            return done > sentAt
        }.count
    }

    /// One of Digger's timestamps, which carry microseconds and an offset: ISO 8601 reading here
    /// takes milliseconds at most.
    nonisolated public static func date(_ text: String) -> Date? {
        let trimmed = text.replacingOccurrences(of: #"(\.\d{3})\d+"#, with: "$1", options: .regularExpression)
        let reader = ISO8601DateFormatter()
        reader.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = reader.date(from: trimmed) { return date }
        reader.formatOptions = [.withInternetDateTime]
        return reader.date(from: trimmed)
    }

    /// Ask the computer to log in to YouTube Music from its own Chrome. macOS asks there first, to
    /// share Chrome's cookie key; the cookies stay on the computer and only the login's state
    /// comes back. A send that failed for want of a login can then go again.
    public func logInYouTubeFromChrome() async {
        guard let api, let pairing = connection, !youtubeLoggingIn else { return }
        youtubeLoggingIn = true
        youtubeLoginReport = nil
        defer { youtubeLoggingIn = false }
        do {
            let result = try await api.logInYouTubeFromChrome()
            guard connection == pairing else { return }
            reached()
            if let fresh = result.settings { settings = fresh } else if let fresh = try? await api.settings() { settings = fresh }
            youtubeLoginReport = Self.loginReport(result.state)
            if result.loggedIn {
                // Just logged in: a send that failed for want of a login can go again; it says if it can't.
                youtubeSends = youtubeSends.filter { _, send in
                    if case .failed(let message) = send { return YouTubeLoginProblem(message: message) == nil }
                    return true
                }
            }
        } catch let error as APIError where error.isTransport {
            // The answer waits on macOS's prompt on the computer: no answer in time says nothing
            // about the connection, so this doesn't take the dig off the screen.
            guard connection == pairing else { return }
            youtubeLoginReport = YouTubeLoginReport(message: "Digger didn't answer in time. If macOS is asking on your Mac, "
                                                    + "click Allow there, then press Reconnect to see the login here.", isError: true)
        } catch {
            guard connection == pairing else { return }
            let message = report(error)
            if !message.isEmpty { youtubeLoginReport = YouTubeLoginReport(message: message, isError: true) }
        }
    }

    static func loginReport(_ state: String) -> YouTubeLoginReport {
        switch state {
        case "connected":
            return YouTubeLoginReport(message: "Logged in from Chrome. Digger can send digs to YouTube Music.", isError: false)
        case "rejected":
            return YouTubeLoginReport(message: "YouTube Music didn't accept Chrome's login. Sign in at music.youtube.com "
                                      + "in Chrome on your computer, then try again.", isError: true)
        case "missing":
            return YouTubeLoginReport(message: "Chrome isn't signed in to YouTube Music. Sign in at music.youtube.com "
                                      + "in Chrome on your computer, then try again.", isError: true)
        case "unreachable":
            return YouTubeLoginReport(message: "Saved Chrome's login, but YouTube Music didn't answer to check it. "
                                      + "If a send fails, log in again.", isError: false)
        default:
            return YouTubeLoginReport(message: "Saved Chrome's login. Digger checks it when it next sends.", isError: false)
        }
    }

    // MARK: New digs

    public func search(_ query: String) async throws -> SearchResult {
        guard let api else { throw APIError.unreachable("Not connected to Digger.") }
        do {
            let result = try await api.search(query)
            reached()
            return result
        } catch {
            report(error)
            throw error
        }
    }

    /// Start a dig from up to 10 seeds and open its first step.
    public func startDig(seeds: [Point]) async throws {
        guard let api else { throw APIError.unreachable("Not connected to Digger.") }
        do {
            let identity = try await api.createDig(seeds: seeds, idempotencyKey: UUID().uuidString)
            await openDig(identity.digID, at: identity.stepID)
            await reloadRecentDigs()
        } catch {
            report(error)
            throw error
        }
    }
}
