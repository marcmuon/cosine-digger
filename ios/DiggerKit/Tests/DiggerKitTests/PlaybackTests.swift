import XCTest
@testable import DiggerKit

@MainActor
final class RecordingEngine: PlayerEngine {
    var commands: [PlayerCommand] = []
    func perform(_ command: PlayerCommand) { commands.append(command) }
}

final class PlaybackTests: XCTestCase {
    @MainActor func items() throws -> [QueueItem] {
        let envelope = try decode(StepEnvelope.self, "step1")
        return envelope.picks.map { QueueItem(pick: $0, step: envelope.step) }
    }

    @MainActor func testPlayingStartsFromATapAndQueuesTheRestOfTheStep() async throws {
        let playback = Playback()
        let engine = RecordingEngine()
        playback.engine = engine
        let queue = try items()
        playback.play(queue, startAt: 0)
        XCTAssertEqual(playback.current?.videoID, "jNQXAC9IVRw")
        XCTAssertEqual(engine.commands, [.load(videoID: "jNQXAC9IVRw", start: 0), .play])
        XCTAssertEqual(playback.status, .loading)
        XCTAssertFalse(playback.upNext.contains { $0.videoID == nil }, "tracks without a video are skipped")
        playback.handle(.playing)
        XCTAssertEqual(playback.status, .playing)
        playback.toggle()
        XCTAssertEqual(playback.status, .paused)
        XCTAssertEqual(engine.commands.last, .pause)
    }

    @MainActor func testPassedTracksAreSkippedAndAPassOnThePlayingTrackMovesOn() async throws {
        let playback = Playback()
        let queue = try items()
        let passed = queue[1].trackKey
        playback.isPassed = { $0 == passed }
        playback.play(queue, startAt: 0)
        playback.handle(.playing)
        playback.handle(.ended)
        XCTAssertEqual(playback.current?.id, queue[2].id)
        playback.handle(.playing)
        playback.passed(trackKey: queue[2].trackKey)
        XCTAssertEqual(playback.current?.id, queue[3].id)
    }

    @MainActor func testPreviousAtTheEndOfTheStepGoesBackOneTrackWithoutRepeats() async throws {
        let playback = Playback()
        let queue = try items()
        let last = queue.count - 1
        playback.play(queue, startAt: last - 1)
        playback.handle(.playing)
        playback.handle(.ended)
        XCTAssertEqual(playback.current?.id, queue[last].id)
        playback.handle(.playing)
        playback.handle(.ended)  // nothing follows: the last track stays in the player
        XCTAssertEqual(playback.status, .ended)
        XCTAssertEqual(playback.current?.id, queue[last].id)
        playback.next()  // still nothing follows
        playback.previousTrack()
        XCTAssertEqual(playback.current?.id, queue[last - 1].id, "Previous goes back one, not to the track that ended")
        XCTAssertEqual(playback.upNext.map(\.id), [queue[last].id], "and the last track is up next once")
        playback.play(queue, startAt: last - 1)  // pressing play again on the same track
        playback.previousTrack()
        XCTAssertEqual(playback.current?.id, queue[last - 1].id, "nothing came before it: it starts over")
    }

    @MainActor func testThirtySecondsOfListeningMarksHeardOnceAndSeeksDontCount() async throws {
        let playback = Playback()
        var heard: [String] = []
        playback.onHeard = { heard.append($0.id) }
        let queue = try items()
        playback.play(queue, startAt: 0)
        playback.handle(.playing)
        playback.handle(.time(position: 0, duration: 200))
        playback.handle(.time(position: 120, duration: 200))  // a seek
        XCTAssertTrue(heard.isEmpty)
        var position = 120.0
        for _ in 0..<31 {
            position += 1
            playback.handle(.time(position: position, duration: 200))
        }
        XCTAssertEqual(heard, [queue[0].id])
        for _ in 0..<40 {
            position += 1
            playback.handle(.time(position: position, duration: 200))
        }
        XCTAssertEqual(heard.count, 1)
    }

    @MainActor func testARefusedVideoReachedByTheQueueSaysWhyThenMovesOn() async throws {
        let playback = Playback()
        playback.refusalDelay = 0.05
        var refused: [String] = []
        playback.onRefused = { item, _ in refused.append(item.id) }
        let queue = try items()
        playback.play(queue, startAt: 0)
        playback.handle(.playing)
        playback.handle(.ended)  // the queue moves on to queue[1] by itself
        XCTAssertEqual(playback.current?.id, queue[1].id)
        playback.handle(.error(101))
        XCTAssertEqual(playback.status, .refused("YouTube won't play this video in the app. It may be gone, or only allowed on YouTube itself."))
        XCTAssertEqual(refused, [queue[1].id])
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(playback.current?.id, queue[2].id)

        playback.handle(.error(153))  // the player itself was refused: stay, so the queue isn't skipped through
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(playback.current?.id, queue[2].id)
        XCTAssertEqual(refused.count, 2)
    }

    @MainActor func testAVideoYouPressedPlayOnThatWontPlayStaysPut() async throws {
        let playback = Playback()
        playback.refusalDelay = 0.05
        let queue = try items()
        playback.play(queue, startAt: 1)
        playback.handle(.error(150))
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(playback.current?.id, queue[1].id, "nothing you didn't choose starts")
        guard case .refused = playback.status else { return XCTFail("expected a refusal, got \(playback.status)") }

        let envelope = try decode(StepEnvelope.self, "step1")
        let noVideo = try XCTUnwrap(envelope.picks.firstIndex { $0.videoID == nil })
        let all = envelope.picks.map { QueueItem(pick: $0, step: envelope.step) }
        playback.play(all, startAt: noVideo)
        try await Task.sleep(nanoseconds: 300_000_000)
        XCTAssertEqual(playback.current?.id, all[noVideo].id)
        XCTAssertEqual(playback.status, .refused("No YouTube video for this track. Search for it in YouTube Music."))
    }

    @MainActor func testPassingThePausedTrackWaitsForATap() async throws {
        let playback = Playback()
        let engine = RecordingEngine()
        playback.engine = engine
        let queue = try items()
        playback.play(queue, startAt: 0)
        playback.handle(.playing)
        playback.pause()
        engine.commands = []
        playback.passed(trackKey: queue[0].trackKey)
        XCTAssertEqual(playback.current?.id, queue[1].id)
        XCTAssertEqual(playback.status, .paused)
        XCTAssertEqual(engine.commands, [.cue(videoID: try XCTUnwrap(queue[1].videoID), start: 0)])
        playback.toggle()
        XCTAssertEqual(engine.commands.last, .play)
    }

    @MainActor func testAReclaimedPlayerLoadsTheTrackAgainWhereItWas() async throws {
        let playback = Playback()
        let engine = RecordingEngine()
        playback.engine = engine
        let queue = try items()
        playback.play(queue, startAt: 0)
        playback.handle(.playing)
        playback.handle(.time(position: 40, duration: 200))
        playback.pauseForApp()
        playback.handle(.lost)
        engine.commands = []
        playback.toggle()
        XCTAssertEqual(engine.commands, [.load(videoID: try XCTUnwrap(queue[0].videoID), start: 40), .play])
        XCTAssertEqual(playback.current?.id, queue[0].id)
        XCTAssertEqual(playback.position, 40)
        playback.handle(.playing)
        engine.commands = []
        playback.pause()
        playback.toggle()
        XCTAssertEqual(engine.commands, [.pause, .play], "reloads only once")
    }

    @MainActor func testNoHiddenPlaybackWhenTheAppIsAway() async throws {
        let playback = Playback()
        let engine = RecordingEngine()
        playback.engine = engine
        playback.play(try items(), startAt: 0)
        playback.handle(.playing)
        playback.pauseForApp()
        XCTAssertEqual(playback.status, .paused)
        XCTAssertTrue(playback.pausedForApp)
        engine.commands = []
        playback.handle(.playing)  // a play the app didn't ask for
        XCTAssertEqual(engine.commands, [.pause])
        XCTAssertEqual(playback.status, .paused)
        playback.toggle()
        XCTAssertFalse(playback.pausedForApp)
        playback.handle(.playing)
        XCTAssertEqual(playback.status, .playing)
    }
}
