import Foundation

/// The sentences the inspector shows, worded as the web inspector words them (static/inspector.js).
public enum Explain {
    static func score(_ value: Double?) -> String {
        guard let value, value.isFinite else { return "similarity unavailable" }
        return String(format: "%.2f", value)
    }

    /// "Why it's here": how this step found the pick, from its saved route.
    public static func why(_ pick: Pick, in step: Step, pickCount: Int) -> String {
        let route = pick.route
        let names = Dictionary(step.origins.map { ($0.pointID, $0.name) }, uniquingKeysWith: { first, _ in first })
        let close = route.closeSeedIDs ?? []
        let scores = route.seedScores ?? [:]
        let count = max(pickCount, pick.rank)
        let rank = "Rank \(pick.rank) of \(count)."
        let seedCount = route.seedCount ?? step.origins.count
        let nearest = (route.nearestSeed ?? route.layoutSeed).flatMap { names[$0] } ?? step.origins.first?.name ?? ""
        if route.hop == 1 {
            var subject = route.why ?? ""
            if let range = subject.range(of: "one step further: sounds like ", options: [.caseInsensitive, .anchored]) {
                subject.removeSubrange(range)
            }
            return "One step further: sounds like \(subject), which is on \(route.parentPickRanks?.count ?? 0) picks above."
        }
        if close.isEmpty {
            return route.nearestSeed == nil ? "Suggested from \(nearest); cosine.club similarity unavailable. \(rank)"
                : "Nearest to \(nearest). \(rank)"
        }
        if step.origins.count == 1 {
            var sentence = step.header
            if sentence.hasSuffix(".") { sentence.removeLast() }
            return "\(sentence). \(rank)"
        }
        if close.count == 1 {
            return "Close to \(names[close[0]] ?? close[0]): cosine.club similarity \(score(scores[close[0]]))."
        }
        let listed = close.map { "\(names[$0] ?? $0) \(score(scores[$0]))" }.joined(separator: ", ")
        return close.count == seedCount
            ? "Close to all \(seedCount) seeds, by cosine.club similarity: \(listed). \(rank)"
            : "Close to \(close.count) of your \(seedCount) seeds, by cosine.club similarity: \(listed). \(rank)"
    }

    /// The pick's Digger score line.
    public static func scoreLine(_ pick: Pick) -> String {
        pick.route.hop == 1
            ? "Digger score \(score(pick.score)): Digger's own order for the picks one step further."
            : "Digger score \(score(pick.score)): Digger's own order, merged from each origin's cosine.club list."
    }

    /// One short line for a row: the route's own why, or the nearest seed.
    public static func short(_ pick: Pick) -> String {
        if let why = pick.route.why, !why.isEmpty { return why.prefix(1).uppercased() + why.dropFirst() }
        return "Rank \(pick.rank)"
    }

    /// "Sounds like the label Label A": what a move would start, named for its button.
    public static func moveTitle(_ move: MoveKind, point: Point) -> String {
        switch move {
        case .track: return "Sounds like \(point.name)"
        case .label: return "Sounds like the label \(point.name)"
        case .artist: return "Sounds like the artist \(point.name)"
        case .person: return "Sounds like records \(point.name) is credited on"
        }
    }

    /// Where a pick can be heard outside the app.
    public static func youTubeMusicURL(_ pick: Pick) -> URL? {
        if let video = pick.videoID { return URL(string: "https://music.youtube.com/watch?v=\(video)") }
        var components = URLComponents(string: "https://music.youtube.com/search")
        components?.queryItems = [URLQueryItem(name: "q", value: pick.name)]
        return components?.url
    }
}
