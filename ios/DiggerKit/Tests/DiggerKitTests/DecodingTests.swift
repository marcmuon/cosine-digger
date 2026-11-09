import Foundation
import XCTest
@testable import DiggerKit

/// The fixtures are real answers from `cosine-digger serve --companion` in its slice-v1 test mode
/// (see ios/README.md), so these tests pin the client to what the server actually sends.
func fixture(_ name: String) throws -> Data {
    let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Fixtures"))
    return try Data(contentsOf: url)
}

func decode<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
    try JSONDecoder().decode(type, from: fixture(name))
}

final class DecodingTests: XCTestCase {
    func testSearchItemsAreSeedPoints() throws {
        let result = try decode(SearchResult.self, "search")
        let seed = try XCTUnwrap(result.items.first)
        XCTAssertEqual(seed.pointID, "track:fixture artist a|seed a")
        XCTAssertEqual(seed.cosineID, "seed-a")
        XCTAssertEqual(seed.indexed, true)
        XCTAssertNil(seed.libraryID)
    }

    func testStepSnapshotWithFactsHubsAndLiveMarks() throws {
        let envelope = try decode(StepEnvelope.self, "step1")
        XCTAssertEqual(envelope.step.number, 1)
        XCTAssertEqual(envelope.step.header, "Sounds like Fixture Artist A - Seed A")
        XCTAssertEqual(envelope.step.move, "seed")
        XCTAssertEqual(envelope.job.state, "done")
        XCTAssertTrue(envelope.job.isTerminal)
        XCTAssertEqual(envelope.picks.count, 25)
        let first = try XCTUnwrap(envelope.picks.first)
        XCTAssertEqual(first.id, "\(envelope.step.id):1")
        XCTAssertEqual(first.videoID, "jNQXAC9IVRw")
        XCTAssertEqual(first.factsState, "ready")
        XCTAssertEqual(first.facts?.labels.first?.name, "Label A")
        XCTAssertEqual(first.facts?.labels.first?.id, "501")
        XCTAssertEqual(first.facts?.credits.first?.roles, ["Producer", "Mixed By"])
        XCTAssertEqual(first.route.why, "close to Fixture Artist A - Seed A")
        XCTAssertEqual(first.route.seedScores?["track:fixture artist a|seed a"], 0.96)
        XCTAssertEqual(envelope.snapshot.hubs.first?.hubKey, "label:501")
        XCTAssertNotNil(envelope.live.judgments[first.trackKey])
        XCTAssertTrue(envelope.picks.contains { $0.videoID == nil })
        XCTAssertTrue(envelope.picks.contains { $0.factsState == "missing" })
    }

    func testLabelBranchCarriesItsSourceSelection() throws {
        let envelope = try decode(StepEnvelope.self, "step2")
        XCTAssertEqual(envelope.step.number, 2)
        XCTAssertEqual(envelope.step.move, "label")
        XCTAssertEqual(envelope.step.header, "Sounds like the label Label A")
        XCTAssertEqual(envelope.step.context.sourceSelectionID, "fact:1:label:501")
        XCTAssertEqual(envelope.step.context.sourcePoint?.discogsID, "501")
        XCTAssertNotNil(envelope.step.parentID)
        XCTAssertEqual(envelope.step.origins.first?.resolvedTracks?.count, 5)
    }

    func testAStepShowsItsWarningsButNotItsProgress() throws {
        let envelope = try decode(StepEnvelope.self, "step2")
        XCTAssertEqual(envelope.step.note.first?.code, "S-HDR-PROGRESS")
        XCTAssertEqual(envelope.step.warnings, [], "a progress line isn't a note to show")

        let notes = """
        [{"code": "S-HDR-PROGRESS", "message": "listening for neighbours · 0 of 25"},
         {"code": "S-N-SWAP", "message": "Two seeds were the same track."},
         {"code": "discogs_unavailable", "message": "Discogs didn't answer."},
         {"code": "discogs_unavailable", "message": "Discogs didn't answer."},
         {"code": "job_note", "message": "Still reading the label.", "progress": {"done": 3}},
         {"message": "Library scan is out of date."},
         {"code": "S-N-EMPTY"}]
        """
        var step = envelope.step
        step.note = try JSONDecoder().decode([StepNote].self, from: Data(notes.utf8))
        XCTAssertEqual(step.warnings.map(\.message),
                       ["Two seeds were the same track.", "Discogs didn't answer.", "Library scan is out of date."])
    }

    func testDigListsStepsWithCounts() throws {
        let dig = try decode(Dig.self, "dig")
        XCTAssertEqual(dig.steps.count, 2)
        XCTAssertEqual(dig.steps.first?.counts?.picks, 25)
        XCTAssertEqual(dig.steps.last?.parentID, dig.steps.first?.id)
        XCTAssertNotNil(dig.steps.first?.currentJobID)
        let list = try decode(DigList.self, "digs")
        XCTAssertEqual(list.digs.first?.id, dig.id)
    }

    func testSettingsJudgmentHeardAndCompanion() throws {
        let settings = try decode(ServerSettings.self, "settings")
        XCTAssertEqual(settings.discogsToken?.usable, true)
        XCTAssertEqual(settings.cosineToken?.isSet, true)
        let judgment = try decode(JudgmentResult.self, "judgment_put")
        XCTAssertEqual(judgment.effective.verdict, .keep)
        XCTAssertEqual(judgment.revision, 1)
        XCTAssertNotNil(judgment.changeID)
        let heard = try decode(HeardMark.self, "heard")
        XCTAssertEqual(heard.trackKey, "discovery artist 0|discovery 0")
        let info = try decode(CompanionInfo.self, "companion")
        XCTAssertTrue(info.ok)
    }

    func testRequestsEncodeOnlyTheFieldsTheServerAccepts() throws {
        var point = try XCTUnwrap(try decode(StepEnvelope.self, "step2").step.origins.first)
        point.librarySource = "file"
        let request = StepRequest(parentStepID: "p", origins: [point], move: "label", further: false,
                                  sourceSelectionID: "fact:1:label:501")
        let object = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(Set(object.keys), ["parent_step_id", "origins", "move", "further", "source_selection_id"])
        let origin = try XCTUnwrap((object["origins"] as? [[String: Any]])?.first)
        XCTAssertNil(origin["resolved_tracks"])
        XCTAssertNil(origin["resolution"])
        XCTAssertNil(origin["library_source"])
        XCTAssertEqual(origin["source_rank"] as? Int, 1)
        XCTAssertEqual(origin["discogs_id"] as? String, "501")
    }

    func testTrackKeysArePercentEncodedAsOnePathSegment() {
        XCTAssertEqual(APIClient.segment("discovery artist 0|discovery 0"), "discovery%20artist%200%7Cdiscovery%200")
        XCTAssertEqual(APIClient.segment("a/b?c"), "a%2Fb%3Fc")
    }
}
