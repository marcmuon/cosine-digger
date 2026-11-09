import Foundation
import XCTest
@testable import DiggerKit

/// The journey the app is for, against a running Digger in companion mode:
/// pair, choose a seed, inspect candidates, keep one, branch into its label and return.
///
/// Skipped unless DIGGER_LIVE_URL and DIGGER_LIVE_TOKEN are set. ios/README.md shows how to run
/// it against Digger's fixture mode, which needs no cosine.club or Discogs keys.
final class LiveJourneyTests: XCTestCase {
    @MainActor
    func connection() throws -> Connection {
        let environment = ProcessInfo.processInfo.environment
        guard let address = environment["DIGGER_LIVE_URL"], let token = environment["DIGGER_LIVE_TOKEN"] else {
            throw XCTSkip("Set DIGGER_LIVE_URL and DIGGER_LIVE_TOKEN to run against a live Digger.")
        }
        return try Pairing.connection(address: address, token: token)
    }

    @MainActor
    /// A label or person step reads Discogs at its real pace (about one request a second, even in
    /// fixture mode), so a cold Digger takes 25 s or so to finish one.
    func waitForStep(_ session: DigSession, timeout: Double = 60) async throws -> StepEnvelope {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if let envelope = session.currentEnvelope, envelope.job.isTerminal { return envelope }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        XCTFail("Step \(session.currentStepID ?? "?") didn't finish in \(timeout) s")
        throw CancellationError()
    }

    @MainActor
    func testSeedKeepBranchIntoTheLabelAndReturn() async throws {
        let store = MemoryStore()
        let session = DigSession(store: store)
        session.pollInterval = 0.2
        try await session.pair(try connection())
        XCTAssertEqual(session.phase, .ready)

        // Choose a seed and start a dig.
        let result = try await session.search("Seed A")
        let seed = try XCTUnwrap(result.items.first, "fixture mode finds Seed A")
        try await session.startDig(seeds: [seed])
        let first = try await waitForStep(session)
        XCTAssertEqual(first.step.move, "seed")
        XCTAssertFalse(first.picks.isEmpty)

        // Inspect a candidate: selecting it is not playing it.
        let pick = try XCTUnwrap(first.picks.first { session.choices(for: $0)[1].isAvailable }, "a pick with a Discogs label")
        session.selectedPickID = pick.id
        session.scrollAnchorByStep[first.step.id] = pick.id
        XCTAssertNil(session.playback.current)

        // Keep it (clearing a keep left by an earlier run first: judgments are per track, across digs).
        if session.verdict(for: pick.trackKey) == .keep { await session.judge(pick, .keep) }
        await session.judge(pick, .keep)
        XCTAssertEqual(session.verdict(for: pick.trackKey), .keep)
        XCTAssertEqual(session.notice?.canUndo, true)

        // The keep is Digger's, not the phone's: a fresh session reads it back.
        let reopened = DigSession(store: MemoryStore(connection: try connection()))
        await reopened.connect()
        XCTAssertEqual(reopened.phase, .ready)
        XCTAssertEqual(reopened.dig?.id, session.dig?.id, "the newest dig opens")
        await reopened.refreshStep(first.step.id)
        XCTAssertEqual(reopened.verdict(for: pick.trackKey), .keep)

        // Branch into its label.
        let label = try XCTUnwrap(session.choices(for: pick)[1].candidates.first)
        await session.branch(from: pick, move: .label, origin: label)
        XCTAssertNotEqual(session.currentStepID, first.step.id)
        let child = try await waitForStep(session)
        XCTAssertEqual(child.step.move, "label")
        XCTAssertEqual(child.step.parentID, first.step.id)
        XCTAssertEqual(child.step.context.sourceSelectionID, Moves.selectionID(for: label))
        XCTAssertNil(session.selectedPickID, "a new step starts with nothing selected")
        XCTAssertEqual(session.trail.map(\.depth), [0, 1])

        // Return: the parent step, with the pick the move started from selected again.
        session.openParent()
        XCTAssertEqual(session.currentStepID, first.step.id)
        XCTAssertEqual(session.selectedPickID, pick.id)
        XCTAssertEqual(session.scrollAnchorByStep[first.step.id], pick.id)
        XCTAssertEqual(session.children(of: first.step.id).map(\.id), [child.step.id])

        // Pass another pick and undo it.
        let other = try XCTUnwrap(first.picks.first { $0.trackKey != pick.trackKey && session.verdict(for: $0.trackKey) == nil })
        await session.judge(other, .pass)
        XCTAssertEqual(session.verdict(for: other.trackKey), .pass)
        await session.undo()
        XCTAssertNil(session.verdict(for: other.trackKey))
    }

    @MainActor
    func testARetriedMoveMakesOneStepAndAStaleKeepIsRefused() async throws {
        let connection = try connection()
        let api = APIClient(baseURL: try XCTUnwrap(connection.urls.first), token: connection.token)
        let found = try await api.search("Seed A")
        let seed = try XCTUnwrap(found.items.first)
        let key = UUID().uuidString
        let dig = try await api.createDig(seeds: [seed], idempotencyKey: key)
        let again = try await api.createDig(seeds: [seed], idempotencyKey: key)
        XCTAssertEqual(again.stepID, dig.stepID)
        XCTAssertEqual(again.digID, dig.digID, "a replayed request gets the first answer back")

        var envelope = try await api.step(dig.stepID)
        for _ in 0..<100 where !envelope.job.isTerminal {
            try await Task.sleep(nanoseconds: 100_000_000)
            envelope = try await api.step(dig.stepID)
        }
        let pick = try XCTUnwrap(envelope.picks.last)
        let revision = envelope.live.judgments[pick.trackKey]?.revision ?? 0
        let kept = try await api.judge(trackKey: pick.trackKey, stepID: pick.stepID, verdict: .keep, expectedRevision: revision)
        XCTAssertEqual(kept.effective.verdict, .keep)
        do {
            _ = try await api.judge(trackKey: pick.trackKey, stepID: pick.stepID, verdict: .pass, expectedRevision: revision)
            XCTFail("a change that read an old revision is refused")
        } catch let error as APIError {
            guard case .conflict = error else { return XCTFail("expected a conflict, got \(error)") }
        }
        _ = try await api.judge(trackKey: pick.trackKey, stepID: pick.stepID, verdict: nil, expectedRevision: kept.revision)

        #if !os(Linux)
        // Linux's URLSession crashes on a 401 that carries WWW-Authenticate; tests/test_companion.py covers it there.
        let wrong = APIClient(baseURL: api.baseURL, token: String(repeating: "x", count: 43))
        do {
            _ = try await wrong.companion()
            XCTFail("a wrong token is refused")
        } catch let error as APIError {
            guard case .unauthorized = error else { return XCTFail("expected unauthorized, got \(error)") }
        }
        #endif
    }

    /// Send a dig to its YouTube Music playlist through the real routes. Only with
    /// DIGGER_LIVE_FIXTURE=1, for fixture mode: there the playlist is a stand-in, while a real
    /// Digger would make one in your YouTube Music account.
    @MainActor
    func testSendingADigToYouTubeMusicMakesItsPlaylist() async throws {
        guard ProcessInfo.processInfo.environment["DIGGER_LIVE_FIXTURE"] == "1" else {
            throw XCTSkip("Set DIGGER_LIVE_FIXTURE=1 when DIGGER_LIVE_URL is a fixture-mode Digger.")
        }
        let session = DigSession(store: MemoryStore())
        session.pollInterval = 0.2
        try await session.pair(try connection())
        let result = try await session.search("Seed A")
        try await session.startDig(seeds: [try XCTUnwrap(result.items.first)])
        let first = try await waitForStep(session)
        let digID = try XCTUnwrap(session.dig?.id)

        await session.sendToYouTubeMusic()

        guard case .sent(let sent)? = session.youtubeSends[digID] else {
            return XCTFail("expected a finished send, got \(String(describing: session.youtubeSends[digID]))")
        }
        XCTAssertGreaterThan(sent.added, 0)
        XCTAssertLessThanOrEqual(sent.added + sent.missing + sent.passed + sent.noVideo, first.picks.count,
                                 "each pick at most once: sent, passed or with no video")
        let link = try XCTUnwrap(session.dig?.youtubePlaylistURL, "the dig is read again with its playlist")
        XCTAssertTrue(link.hasPrefix("https://music.youtube.com/playlist?list="))
        XCTAssertNotNil(session.dig?.youtubeSentAt)
        XCTAssertEqual(session.youtubeStepsBehind(digID: digID), 0)
    }
}
