import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif
import XCTest
@testable import DiggerKit

/// A Digger that answers from the recorded fixtures, with knobs for the failures these tests need.
final class StubDigger: URLProtocol {
    struct Rule {
        var status = 200
        var body: Data?
        var fail = false
        var delay: Double = 0
    }

    static let lock = NSLock()
    nonisolated(unsafe) static var goodToken = String(repeating: "g", count: 43)
    /// Per path: what to answer instead of the fixture. Consumed once when `once` is set.
    nonisolated(unsafe) static var rules: [String: Rule] = [:]
    nonisolated(unsafe) static var onceRules: [String: Rule] = [:]
    nonisolated(unsafe) static var tokenDelays: [String: Double] = [:]
    nonisolated(unsafe) static var bodies: [String: Data] = [:]
    /// Ports that can't be reached: each request waits this long, then fails.
    nonisolated(unsafe) static var deadPorts: [Int: Double] = [:]
    /// Each track's judgment, kept the way Digger keeps it: a revision, and changes that undo in order.
    struct Change { let id: String; let before: String?; var undone: Bool }
    nonisolated(unsafe) static var verdicts: [String: (revision: Int, verdict: String?, history: [Change])] = [:]
    nonisolated(unsafe) static var judgmentDelay: Double = 0
    /// Per path: the last query, as it went over the wire.
    nonisolated(unsafe) static var queries: [String: String] = [:]

    static func reset() {
        lock.lock(); defer { lock.unlock() }
        rules = [:]; onceRules = [:]; tokenDelays = [:]; bodies = [:]; deadPorts = [:]
        verdicts = [:]; judgmentDelay = 0; queries = [:]
        goodToken = String(repeating: "g", count: 43)
    }

    /// PUT and DELETE /api/judgments/{key}, as storage.change_judgment answers them.
    static func judgment(_ method: String, _ path: String, _ body: Data?) -> (Int, Data) {
        lock.lock(); defer { lock.unlock() }
        let key = String(path.dropFirst("/api/judgments/".count))
        let request = body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        var state = verdicts[key] ?? recorded(key)
        guard (request["expected_revision"] as? NSNumber)?.intValue == state.revision else {
            return (409, Data(#"{"error":"This judgment changed elsewhere; try again."}"#.utf8))
        }
        var changeID: String?
        if let undo = request["undo_change_id"] as? String {
            guard let index = state.history.lastIndex(where: { !$0.undone }), state.history[index].id == undo else {
                return (409, Data(#"{"error":"only the most recent applied change can be undone"}"#.utf8))
            }
            state.verdict = state.history[index].before
            state.history[index].undone = true
            state.revision += 1
            changeID = undo
        } else {
            let verdict = method == "PUT" ? request["verdict"] as? String : nil
            if verdict != state.verdict {
                let id = UUID().uuidString
                state.history.append(Change(id: id, before: state.verdict, undone: false))
                state.verdict = verdict
                state.revision += 1
                changeID = id
            }
        }
        verdicts[key] = state
        var effective: [String: Any] = ["verdict": state.verdict ?? NSNull()]
        if state.verdict != nil { effective["step_id"] = request["step_id"] ?? firstStep; effective["source"] = "gui" }
        let answer: [String: Any] = ["track_key": key, "effective": effective, "revision": state.revision,
                                     "change_id": changeID ?? NSNull()]
        return (200, (try? JSONSerialization.data(withJSONObject: answer)) ?? Data())
    }

    /// A track's judgment as the recorded step has it, before any change made here.
    static func recorded(_ key: String) -> (revision: Int, verdict: String?, history: [Change]) {
        let live = ((try? fixture("step1")).flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any])?["live"] as? [String: Any]
        let judgment = (live?["judgments"] as? [String: Any])?[key] as? [String: Any]
        return ((judgment?["revision"] as? NSNumber)?.intValue ?? 0, judgment?["verdict"] as? String, [])
    }

    static func set(_ path: String, _ rule: Rule, once: Bool = false) {
        lock.lock(); defer { lock.unlock() }
        if once { onceRules[path] = rule } else { rules[path] = rule }
    }

    static func unset(_ path: String) {
        lock.lock(); defer { lock.unlock() }
        rules[path] = nil
    }

    static func rule(for path: String) -> Rule? {
        lock.lock(); defer { lock.unlock() }
        if let rule = onceRules.removeValue(forKey: path) { return rule }
        return rules[path]
    }

    static func lastBodyData(_ path: String) -> Data? {
        lock.lock(); defer { lock.unlock() }
        return bodies[path]
    }

    static func lastBody(_ path: String) -> [String: Any]? {
        lock.lock(); defer { lock.unlock() }
        return bodies[path].flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
    }

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "stub.digger.invalid" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    /// A request the app gave up on (a slower address, a poll for a step no longer open) must not
    /// answer afterwards: Linux's URLSession crashes on a late answer.
    private let stopLock = NSRecursiveLock()
    nonisolated(unsafe) private var stopped = false
    override func stopLoading() {
        stopLock.lock(); stopped = true; stopLock.unlock()
    }

    private func answer(_ send: (URLProtocolClient) -> Void) {
        stopLock.lock(); defer { stopLock.unlock() }
        guard !stopped, let client else { return }
        send(client)
    }

    override func startLoading() {
        let request = self.request
        let path = request.url?.path ?? ""
        let token = request.value(forHTTPHeaderField: "Authorization")?.replacingOccurrences(of: "Bearer ", with: "") ?? ""
        if let body = request.httpBody ?? request.httpBodyStream.map(Self.read) {
            Self.lock.lock(); Self.bodies[path] = body; Self.lock.unlock()
        }
        Self.lock.lock()
        if let query = request.url?.query(percentEncoded: true) { Self.queries[path] = query }
        let dead = request.url?.port.flatMap { Self.deadPorts[$0] }
        let tokenDelay = Self.tokenDelays[token] ?? 0
        Self.lock.unlock()
        let rule = dead == nil ? Self.rule(for: path) : Rule(fail: true, delay: dead ?? 0)
        let judging = rule == nil && path.hasPrefix("/api/judgments/")
        Self.lock.lock(); let judgmentDelay = judging ? Self.judgmentDelay : 0; Self.lock.unlock()
        let body = request.httpBody ?? Self.lastBodyData(path)
        let delay = max(rule?.delay ?? 0, tokenDelay, judgmentDelay)
        let respond = { [self] in
            if rule?.fail == true {
                answer { $0.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost)) }
                return
            }
            var (status, body) = judging ? Self.judgment(request.httpMethod ?? "GET", path, body)
                : (rule?.status ?? 200, rule?.body ?? Self.answer(for: path, method: request.httpMethod ?? "GET"))
            if token != Self.goodToken {
                status = 401
                body = Data(#"{"error":"This device isn't paired with Digger."}"#.utf8)
            }
            // No WWW-Authenticate header: Linux's URLSession crashes on one.
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                           headerFields: ["Content-Type": "application/json"])!
            answer {
                $0.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                $0.urlProtocol(self, didLoad: body)
                $0.urlProtocolDidFinishLoading(self)
            }
        }
        // Answered right here when there's no wait: Linux's URLSession is only safe with answers
        // given on the queue that started the load, so only the deliberately slow ones go elsewhere.
        if delay == 0 {
            respond()
        } else {
            let task = self.task  // kept until the answer, for the same reason as the sessions
            DispatchQueue.global().asyncAfter(deadline: .now() + delay) { withExtendedLifetime(task) { respond() } }
        }
    }

    static let digID = "b4c9290274724d5f99e8d06083e99012"
    static let firstStep = "113030b851f24fd287a0b57b4fedd118"
    static let labelStep = "4afd76d987ce47a7b6162088fba8b211"

    static func answer(for path: String, method: String) -> Data {
        func load(_ name: String) -> Data { (try? fixture(name)) ?? Data("{}".utf8) }
        switch (method, path) {
        case ("GET", "/api/companion"): return load("companion")
        case ("GET", "/api/settings"): return load("settings")
        case ("GET", "/api/digs"): return load("digs")
        case ("GET", "/api/digs/\(digID)"):
            var dig = (try? JSONSerialization.jsonObject(with: load("dig"))) as? [String: Any] ?? [:]
            dig["last_step_id"] = firstStep
            return (try? JSONSerialization.data(withJSONObject: dig)) ?? Data()
        case ("GET", "/api/steps/\(firstStep)"): return load("step1")
        case ("GET", "/api/steps/\(labelStep)"): return load("step2")
        case ("POST", "/api/steps"):
            return Data(#"{"dig_id":"\#(digID)","step_id":"\#(labelStep)","job_id":"sl-x","created":true}"#.utf8)
        default: return Data(#"{"error":"not here"}"#.utf8)
        }
    }

    static func read(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}

final class SessionRecoveryTests: XCTestCase {
    static let url = URL(string: "http://stub.digger.invalid:8765")!
    @MainActor static var sessions: [URLSession] = []

    @MainActor
    func session(store: MemoryStore? = nil) -> DigSession {
        StubDigger.reset()
        let store = store ?? MemoryStore()
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubDigger.self]
        let urlSession = URLSession(configuration: configuration)
        // Kept for the whole run: Linux's URLSession crashes when a stubbed answer arrives after
        // its session is gone (a test over, its last poll still on its way).
        Self.sessions.append(urlSession)
        let session = DigSession(store: store, makeClient: { APIClient(baseURL: $0, token: $1, session: urlSession) })
        session.pollInterval = 0.05
        return session
    }

    func connection(token: String = StubDigger.goodToken) -> Connection {
        Connection(urls: [Self.url], token: token, name: "Stub")
    }

    @MainActor
    func waitFor(_ condition: @escaping () -> Bool, timeout: Double = 5) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            if Date() > deadline { return XCTFail("timed out") }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    @MainActor
    func testALinkThatDoesntWorkNeverUndoesAWorkingPairing() async throws {
        let session = session()
        try await session.pair(connection())
        XCTAssertEqual(session.phase, .ready)
        do {
            try await session.pair(connection(token: String(repeating: "b", count: 43)))
            XCTFail("a refused token doesn't pair")
        } catch let error as APIError {
            guard case .unauthorized = error else { return XCTFail("expected unauthorized, got \(error)") }
        }
        XCTAssertEqual(session.phase, .ready)
        XCTAssertEqual(session.connection?.token, StubDigger.goodToken)
        XCTAssertNotNil(session.dig)
    }

    @MainActor
    func testANewPairingWinsOverAReconnectStillWaitingWithTheOldToken() async throws {
        let store = MemoryStore(connection: connection(token: String(repeating: "o", count: 43)))
        let session = session(store: store)
        StubDigger.tokenDelays[String(repeating: "o", count: 43)] = 0.4
        let reconnect = Task { await session.connect() }  // the app coming forward when the link opens
        try await Task.sleep(nanoseconds: 50_000_000)
        try await session.pair(connection())
        await reconnect.value
        XCTAssertEqual(session.phase, .ready, "the old token's refusal arrived last and was ignored")
        XCTAssertEqual(store.connection?.token, StubDigger.goodToken)
    }

    @MainActor
    func testALinkThatDoesntWorkLeavesARefusedPairingRefused() async throws {
        let refused = String(repeating: "r", count: 43)
        let session = session(store: MemoryStore(connection: connection(token: refused)))
        await session.connect()
        guard case .unauthorized = session.phase else { return XCTFail("expected unauthorized, got \(session.phase)") }
        // A new link whose address can't be reached: the screen still says to pair again.
        let elsewhere = Connection(urls: [URL(string: "http://stub.digger.invalid:9999")!], token: StubDigger.goodToken, name: "Other")
        StubDigger.set("/api/companion", .init(fail: true))
        do {
            try await session.pair(elsewhere)
            XCTFail("an unreachable link doesn't pair")
        } catch let error as APIError {
            guard case .unreachable = error else { return XCTFail("expected unreachable, got \(error)") }
        }
        guard case .unauthorized = session.phase else { return XCTFail("expected unauthorized, got \(session.phase)") }
        XCTAssertEqual(session.connection?.token, refused)
    }

    @MainActor
    func testALinkThatDoesntWorkWhileReconnectingLetsTheReconnectFinish() async throws {
        let store = MemoryStore(connection: connection())
        let session = session(store: store)
        StubDigger.tokenDelays[StubDigger.goodToken] = 0.3
        let reconnect = Task { await session.connect() }
        try await Task.sleep(nanoseconds: 50_000_000)
        do {
            try await session.pair(connection(token: String(repeating: "b", count: 43)))
            XCTFail("a refused token doesn't pair")
        } catch is APIError {}
        await reconnect.value
        try await waitFor { session.phase == .ready }
        XCTAssertEqual(store.connection?.token, StubDigger.goodToken)
    }

    @MainActor
    func testAnAddressThatCantBeReachedDoesntHoldUpOneThatCan() async throws {
        let session = session()
        // Away from home: the home address hangs until it times out; the tailnet one answers.
        StubDigger.deadPorts[1] = 3
        let home = URL(string: "http://stub.digger.invalid:1")!
        let started = Date()
        try await session.pair(Connection(urls: [home, Self.url], token: StubDigger.goodToken, name: "Stub"))
        XCTAssertLessThan(Date().timeIntervalSince(started), 2, "the reachable address didn't wait for the other")
        XCTAssertEqual(session.phase, .ready)
        XCTAssertEqual(session.activeURL, Self.url)
    }

    @MainActor
    func testWhenNoAddressAnswersTheFirstOnesProblemIsShown() async throws {
        let store = MemoryStore(connection: Connection(urls: [URL(string: "http://stub.digger.invalid:1")!,
                                                              URL(string: "http://stub.digger.invalid:2")!],
                                                       token: StubDigger.goodToken, name: "Stub"))
        let session = session(store: store)
        StubDigger.deadPorts = [1: 0.05, 2: 0.01]
        await session.connect()
        guard case .offline(let message) = session.phase else { return XCTFail("expected offline, got \(session.phase)") }
        XCTAssertTrue(message.contains("stub.digger.invalid:1"), message)
    }

    @MainActor
    func testCantReachClearsOnTheNextAnswer() async throws {
        let session = session()
        try await session.pair(connection())
        StubDigger.set("/api/steps/\(StubDigger.firstStep)", .init(fail: true))
        await session.refreshStep(StubDigger.firstStep)
        guard case .offline(let message) = session.phase else { return XCTFail("expected offline, got \(session.phase)") }
        XCTAssertTrue(message.contains("Local Network"), message)
        StubDigger.reset()
        await session.refreshStep(StubDigger.firstStep)
        XCTAssertEqual(session.phase, .ready)
    }

    @MainActor
    func testARefusedTokenMidListenPausesThePlayer() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let pick = try XCTUnwrap(session.currentEnvelope?.picks.first)
        session.play(pick)
        session.playback.handle(.playing)
        StubDigger.goodToken = String(repeating: "n", count: 43)  // the computer made a new token
        defer { StubDigger.goodToken = String(repeating: "g", count: 43) }
        await session.refreshStep(pick.stepID)
        guard case .unauthorized = session.phase else { return XCTFail("expected unauthorized, got \(session.phase)") }
        XCTAssertEqual(session.playback.status, .paused)
        XCTAssertTrue(session.playback.pausedForApp)
    }

    @MainActor
    func testAnUnreadableDigListSaysWhyInsteadOfNoDigs() async throws {
        let session = session()
        StubDigger.set("/api/digs", .init(body: Data(#"{"digs":[{"id":1}]}"#.utf8)))
        try await session.pair(connection())
        XCTAssertTrue(session.recentDigs.isEmpty)
        XCTAssertTrue(session.homeError?.contains("different version") == true, session.homeError ?? "nil")
        StubDigger.rules = [:]
        await session.loadHome()
        XCTAssertNil(session.homeError)
        XCTAssertFalse(session.recentDigs.isEmpty)
    }

    @MainActor
    func testOpeningADigThatIsntThereSaysSo() async throws {
        let session = session()
        try await session.pair(connection())
        let opened = await session.openDig("gone")
        XCTAssertFalse(opened)
        XCTAssertEqual(session.notice?.isError, true)
        XCTAssertEqual(session.dig?.id, StubDigger.digID, "the open dig stays")
    }

    @MainActor
    func testRerunMakesTheMoveAgainBesideTheOriginal() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let step = try XCTUnwrap(session.currentStep)
        await session.rerun(step)
        let body = try XCTUnwrap(StubDigger.lastBody("/api/steps"))
        XCTAssertEqual(body["rerun_of"] as? String, step.id)
        XCTAssertNil(body["retry_step_id"])
        XCTAssertNil(body["source_selection_id"])
        XCTAssertEqual(session.currentStepID, StubDigger.labelStep)
    }

    @MainActor
    func testGoingBackRevealsThePickAMoveStartedFrom() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        session.open(stepID: StubDigger.labelStep)
        try await waitFor { session.currentEnvelope?.step.id == StubDigger.labelStep }
        session.openParent()
        let reveal = try XCTUnwrap(session.reveal)
        XCTAssertEqual(reveal.stepID, StubDigger.firstStep)
        XCTAssertEqual(reveal.pickID, session.selectedPickID)
        session.revealed(reveal)
        XCTAssertNil(session.reveal, "shown once; coming back later keeps where you scrolled")
    }

    @MainActor
    func testHomeSaysItsReadingBeforeItSaysThereAreNoDigs() async throws {
        let session = session()
        StubDigger.set("/api/digs", .init(delay: 0.3))
        let pairing = Task { try await session.pair(connection()) }
        try await waitFor { session.loadingHome }
        XCTAssertTrue(session.recentDigs.isEmpty)
        try await pairing.value
        XCTAssertFalse(session.loadingHome)
        XCTAssertFalse(session.recentDigs.isEmpty)
    }

    @MainActor
    func testADigTheDiggerNoLongerHasLeavesTheScreen() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        XCTAssertEqual(session.dig?.id, StubDigger.digID)

        // The same address, its data reset: the dig is gone and there are no digs.
        StubDigger.set("/api/digs/\(StubDigger.digID)", .init(status: 404, body: Data(#"{"error":"Dig not found."}"#.utf8)))
        StubDigger.set("/api/digs", .init(body: Data(#"{"digs":[]}"#.utf8)))
        try await session.pair(connection())
        XCTAssertNil(session.dig)
        XCTAssertNil(session.currentStepID)
        XCTAssertNil(session.currentEnvelope)
        XCTAssertTrue(session.recentDigs.isEmpty)
        XCTAssertEqual(session.phase, .ready)
        XCTAssertEqual(session.notice?.message, "That dig isn't on Digger any more.")
    }

    @MainActor
    func testUnpairingForgetsEverythingReadFromThatDigger() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let pick = try XCTUnwrap(session.currentEnvelope?.picks.first)
        session.selectedPickID = pick.id
        XCTAssertFalse(session.judgments.isEmpty)
        session.forget()
        XCTAssertEqual(session.phase, .unpaired)
        XCTAssertTrue(session.judgments.isEmpty)
        XCTAssertTrue(session.selectionByStep.isEmpty)
        XCTAssertNil(session.serverName)
        XCTAssertNil(session.dig)
    }

    @MainActor
    func testAMoveSaysWhatItsMakingUntilTheStepOpens() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let pick = try XCTUnwrap(session.currentEnvelope?.picks.first)
        StubDigger.set("/api/steps", .init(delay: 0.3))
        let move = Task { await session.branch(from: pick, move: .track, origin: Moves.pickPoint(pick)) }
        try await waitFor { session.submitting != nil }
        XCTAssertEqual(session.workingDescription, "Finding what sounds like \(pick.title)…")
        await move.value
        XCTAssertNil(session.workingDescription)
        XCTAssertEqual(session.currentStepID, StubDigger.labelStep)
    }

    @MainActor
    func testUndoNamesTheKeepItTakesBack() async throws {
        let session = session()
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        let pick = try XCTUnwrap(session.currentEnvelope?.picks.first { session.verdict(for: $0.trackKey) == nil })
        await session.judge(pick, .keep)
        XCTAssertEqual(session.undoSummary, "Kept \(pick.title)")
        XCTAssertEqual(session.undoName, pick.title)
    }

    func testAPickWithoutDiscogsFactsSaysWhy() throws {
        let envelope = try JSONDecoder().decode(StepEnvelope.self, from: try fixture("step1"))
        var pick = try XCTUnwrap(envelope.picks.first)
        pick.factsState = "ready"
        XCTAssertNil(Moves.factsNote(pick, discogsUsable: true))
        pick.factsState = "loading"
        XCTAssertNil(Moves.factsNote(pick, discogsUsable: true))
        pick.factsState = "missing"
        XCTAssertEqual(Moves.factsNote(pick, discogsUsable: true), "Discogs has no release for this track.")
        pick.factsState = "unavailable"
        XCTAssertTrue(Moves.factsNote(pick, discogsUsable: true)?.contains("didn't answer") == true)
        XCTAssertTrue(Moves.factsNote(pick, discogsUsable: false)?.contains("Discogs token") == true)
    }

    @MainActor
    func pickToJudge(_ session: DigSession) async throws -> Pick {
        try await session.pair(connection())
        try await waitFor { session.currentEnvelope != nil }
        return try XCTUnwrap(session.currentEnvelope?.picks.first { session.verdict(for: $0.trackKey) == nil })
    }

    @MainActor
    func testADoubleTapIsKeepThenClearNotAConflict() async throws {
        let session = session()
        let pick = try await pickToJudge(session)
        StubDigger.judgmentDelay = 0.15
        async let first: Void = session.judge(pick, .keep)
        async let second: Void = session.judge(pick, .keep)
        _ = await (first, second)
        XCTAssertNil(session.verdict(for: pick.trackKey), "keep, then the same again clears it")
        XCTAssertNil(StubDigger.verdicts[pick.trackKey]?.verdict)
        XCTAssertEqual(session.judgments[pick.trackKey]?.revision, 2)
        XCTAssertEqual(session.notice?.isError, false, session.notice?.message ?? "")
        XCTAssertEqual(session.undoSummary, "Cleared \(pick.title)")
    }

    @MainActor
    func testUndoingTwoChangesInARowOnOneTrack() async throws {
        let session = session()
        let pick = try await pickToJudge(session)
        await session.judge(pick, .keep)
        await session.judge(pick, .pass)
        XCTAssertEqual(session.verdict(for: pick.trackKey), .pass)
        XCTAssertEqual(session.undoSummary, "Passed \(pick.title)")
        await session.undo()
        XCTAssertEqual(session.notice?.isError, false, session.notice?.message ?? "")
        XCTAssertEqual(session.verdict(for: pick.trackKey), .keep)
        XCTAssertEqual(session.undoSummary, "Kept \(pick.title)")
        await session.undo()
        XCTAssertEqual(session.notice?.isError, false, session.notice?.message ?? "")
        XCTAssertNil(session.verdict(for: pick.trackKey))
        XCTAssertNil(StubDigger.verdicts[pick.trackKey]?.verdict)
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testAnUndoThatCantReachDiggerStaysAvailable() async throws {
        let session = session()
        let pick = try await pickToJudge(session)
        await session.judge(pick, .keep)
        let path = "/api/judgments/\(pick.trackKey)"
        StubDigger.set(path, .init(fail: true))
        await session.undo()
        XCTAssertEqual(session.notice?.isError, true)
        XCTAssertEqual(session.verdict(for: pick.trackKey), .keep)
        XCTAssertEqual(session.undoSummary, "Kept \(pick.title)", "Undo is still there")
        StubDigger.unset(path)
        await session.undo()
        XCTAssertNil(session.verdict(for: pick.trackKey))
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testAKeepMadeElsewhereMeanwhileIsntOverwritten() async throws {
        let session = session()
        let pick = try await pickToJudge(session)
        // Your computer passes it first.
        _ = StubDigger.judgment("PUT", "/api/judgments/\(pick.trackKey)",
                                Data(#"{"verdict":"pass","expected_revision":0}"#.utf8))
        await session.judge(pick, .keep)
        XCTAssertEqual(session.notice?.isError, true)
        XCTAssertEqual(StubDigger.verdicts[pick.trackKey]?.verdict, "pass")
        XCTAssertFalse(session.canUndo)
    }

    @MainActor
    func testABadLinkWhileReconnectingARefusedPairingLeavesItRefused() async throws {
        let refused = String(repeating: "r", count: 43)
        let store = MemoryStore(connection: connection(token: refused))
        let session = session(store: store)
        await session.connect()
        guard case .unauthorized = session.phase else { return XCTFail("expected unauthorized, got \(session.phase)") }
        StubDigger.tokenDelays[refused] = 0.3
        let reconnect = Task { await session.connect() }  // Try again
        try await Task.sleep(nanoseconds: 50_000_000)
        do {
            try await session.pair(connection(token: String(repeating: "b", count: 43)))
            XCTFail("a refused token doesn't pair")
        } catch is APIError {}
        await reconnect.value
        try await waitFor {
            if case .unauthorized = session.phase { return true }
            return false
        }
        XCTAssertEqual(store.connection?.token, refused)
    }

    @MainActor
    func testASearchReachesDiggerAsTyped() async throws {
        let session = session()
        try await session.pair(connection())
        StubDigger.set("/api/search", .init(body: Data(#"{"query":"x","items":[],"notes":[]}"#.utf8)))
        _ = try? await session.search("Hall & Oates + Björk = 100%")
        let query = try XCTUnwrap(StubDigger.queries["/api/search"])
        // Read the way Starlette reads it: "+" is a space, then percent escapes.
        let fields = Dictionary(uniqueKeysWithValues: query.split(separator: "&").map { field -> (String, String) in
            let parts = field.split(separator: "=", maxSplits: 1).map {
                String($0).replacingOccurrences(of: "+", with: " ").removingPercentEncoding ?? ""
            }
            return (parts[0], parts.count > 1 ? parts[1] : "")
        })
        XCTAssertEqual(fields["q"], "Hall & Oates + Björk = 100%")
        XCTAssertEqual(fields["source"], "all")
        XCTAssertTrue(APIClient.segment("Björk|Jóga").allSatisfy(\.isASCII))
    }
}
