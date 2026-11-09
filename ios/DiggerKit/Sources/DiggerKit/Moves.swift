import Foundation

/// The moves a pick offers, and the origin points the server accepts for them. These mirror the
/// web client (static/state.js: pickPoint, factPoint, selectionId) so a phone's move is the same
/// move, with the same provenance the server checks in storage.validate_point_source.
public enum MoveKind: String, CaseIterable, Sendable {
    case track, label, artist, person

    public var title: String {
        switch self {
        case .track: return "This track"
        case .label: return "Its label"
        case .artist: return "Its artist"
        case .person: return "A credited person"
        }
    }
}

public struct MoveChoice: Hashable, Sendable {
    public var move: MoveKind
    /// The origins this move can start from: one for a track, one per label, artist or credit.
    public var candidates: [Point]
    /// Why the move can't start now, worded for people; nil when it can.
    public var unavailableReason: String?

    public var isAvailable: Bool { unavailableReason == nil && !candidates.isEmpty }
}

public enum Moves {
    /// storage.fact_key / taste.normalize_artist: an id when Discogs has one, else the normalized name.
    public static func factID(kind: String, entity: FactEntity) -> String {
        if let id = entity.id, !id.isEmpty { return "\(kind):\(id)" }
        return "\(kind):name:\(normalizeName(entity.name))"
    }

    static func normalizeName(_ name: String) -> String {
        var value = name.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if let range = value.range(of: #"\s*\(\d+\)\s*$"#, options: .regularExpression) {
            value.removeSubrange(range)
        }
        value = value.replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
        return value.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// A pick as a track origin, citing the step and rank it came from.
    public static func pickPoint(_ pick: Pick) -> Point {
        Point(pointID: "track:\(pick.trackKey)", kind: "track", name: pick.name, trackKey: pick.trackKey,
              cosineID: pick.cosineID, artist: pick.artist, title: pick.title, videoID: pick.videoID,
              indexed: true, sourceStepID: pick.stepID, sourceRank: pick.rank)
    }

    /// A label, artist or credited person from a pick's ready Discogs facts.
    public static func factPoint(kind: String, entity: FactEntity, stepID: String, rank: Int?) -> Point {
        Point(pointID: factID(kind: kind, entity: entity), kind: kind, name: entity.name,
              discogsID: entity.id, sourceStepID: stepID, sourceRank: rank)
    }

    /// The selection token a move names (and the server saves as the step's source_selection_id).
    public static func selectionID(for point: Point) -> String {
        if point.kind != "track", let rank = point.sourceRank { return "fact:\(rank):\(point.pointID)" }
        return point.pointID
    }

    /// The parent's pick a child step was made from, read from its saved selection token.
    public static func sourcePickID(selectionID: String?, parentStepID: String, picks: [Pick]) -> String? {
        guard let token = selectionID, !token.hasPrefix("origin:") else { return nil }
        if token.hasPrefix("fact:") {
            let parts = token.split(separator: ":", maxSplits: 2)
            guard parts.count == 3, let rank = Int(parts[1]) else { return nil }
            return picks.first { $0.rank == rank }?.id
        }
        return picks.first { "track:\($0.trackKey)" == token || $0.id == token }?.id
    }

    /// Every move a pick offers, each with its origins or the reason it can't start.
    /// Why a pick has no Discogs facts to show, or nil when it has them or they're on the way.
    public static func factsNote(_ pick: Pick, discogsUsable: Bool) -> String? {
        if pick.factsState == "ready" || pick.factsState == "loading" { return nil }
        if !discogsUsable || pick.factsState == "no_token" {
            return "Digger on your computer has no Discogs token, so this record's label and credits aren't known. Add one in Setup on your computer."
        }
        switch pick.factsState {
        case "unavailable": return "Discogs didn't answer for this pick, so its label and credits aren't known yet."
        case "missing": return "Discogs has no release for this track."
        default: return nil
        }
    }

    public static func choices(for pick: Pick, discogsUsable: Bool) -> [MoveChoice] {
        var result = [MoveChoice(move: .track, candidates: [pickPoint(pick)], unavailableReason: nil)]
        let facts = pick.factsState == "ready" ? pick.facts : nil
        for move in [MoveKind.label, .artist, .person] {
            let entities: [FactEntity]
            switch move {
            case .label: entities = facts?.labels ?? []
            case .artist: entities = facts?.artists ?? []
            case .person: entities = (facts?.credits ?? []).map { FactEntity(id: $0.id, name: $0.name) }
            case .track: entities = []
            }
            var candidates = entities.map { factPoint(kind: move.rawValue, entity: $0, stepID: pick.stepID, rank: pick.rank) }
            if move == .artist && candidates.isEmpty && !pick.artist.isEmpty {
                // The recording's own artist, by name: the server resolves it on Discogs.
                candidates = [factPoint(kind: "artist", entity: FactEntity(id: nil, name: pick.artist),
                                        stepID: pick.stepID, rank: pick.rank)]
            }
            var seen = Set<String>()
            candidates = candidates.filter { seen.insert($0.pointID).inserted }
            // Label and person moves need a Discogs id; the server refuses them without one.
            if move == .label || move == .person { candidates = candidates.filter { $0.discogsID != nil } }
            let reason: String?
            if !discogsUsable {
                reason = "Needs a Discogs token. Add one in Setup on your computer."
            } else if !candidates.isEmpty {
                reason = nil
            } else if pick.factsState == "loading" {
                reason = "Discogs facts are still arriving."
            } else if pick.factsState == "no_token" {
                reason = "Needs a Discogs token. Add one in Setup on your computer."
            } else if pick.factsState == "unavailable" {
                reason = "Discogs didn't answer for this pick, so its label and credits aren't known."
            } else if pick.factsState == "missing" {
                reason = "Discogs has no release for this track."
            } else {
                reason = move == .label ? "Discogs lists no label for this record." :
                    move == .person ? "Discogs credits nobody else on this record." : "Discogs lists no artist."
            }
            result.append(MoveChoice(move: move, candidates: candidates, unavailableReason: reason))
        }
        return result
    }

    /// The roles a credited person has on a pick's record, for showing next to their name.
    public static func roles(of entity: FactEntity, in pick: Pick) -> [String] {
        pick.facts?.credits.first { $0.id == entity.id && $0.name == entity.name }?.roles ?? []
    }
}
