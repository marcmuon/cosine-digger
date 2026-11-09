import DiggerKit
import SwiftUI
import UIKit

/// One pick in a step. Selecting it (tapping the title) never plays it; play, keep and pass are
/// their own buttons, and each mark (selected, playing, heard, kept, passed) looks different.
struct PickRow: View {
    @Environment(DigSession.self) private var session
    let pick: Pick
    let openDetail: ((Pick) -> Void)?

    private var isSelected: Bool { session.selectionByStep[pick.stepID] == pick.id }
    private var verdict: Verdict? { session.verdict(for: pick.trackKey) }

    var body: some View {
        row
            .contextMenu { PickActions(pick: pick, openDetail: openDetail) }
            .modifier(PickRowAccessibility(pick: pick, isSelected: isSelected, verdict: verdict,
                                           moveActions: moveActions, hasDetail: openDetail != nil, select: select))
    }

    private var row: some View {
        HStack(alignment: .center, spacing: 8) {
            RankMark(pick: pick)
            Button(action: select) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(pick.title)
                        .font(.subheadline.weight(.medium))
                        .strikethrough(verdict == .pass)
                        .foregroundStyle(titleColor)
                        .lineLimit(2)
                    Text(pick.artist)
                        .font(.footnote)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(1)
                    Text(Explain.short(pick))
                        .font(.footnote)
                        .foregroundStyle(Theme.muted)
                        .lineLimit(2)
                    PickMarks(pick: pick)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            PlayButton(pick: pick)
            JudgeButtons(pick: pick)
        }
        .padding(.leading, 6)
        .padding(.trailing, 4)
        .padding(.vertical, 6)
        .background(isSelected ? Theme.panel3 : Color.clear)
        .overlay {
            // The web client's selection: a dashed yellow frame just inside the row.
            if isSelected {
                Rectangle()
                    .strokeBorder(Theme.selected, style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
                    .padding(1)
                    .allowsHitTesting(false)
            }
        }
    }

    /// White, or grey once it's been heard or passed, as on the web client.
    private var titleColor: Color {
        verdict == .pass || session.isHeard(pick.trackKey) ? Theme.muted : Theme.white
    }

    /// A move from this pick, for VoiceOver's actions.
    struct MoveAction: Identifiable {
        let name: String
        let move: MoveKind
        let point: Point
        var id: String { "\(move.rawValue)/\(point.pointID)" }
    }

    /// Every move the pick offers, one per place it can start.
    private var moveActions: [MoveAction] {
        session.choices(for: pick).filter(\.isAvailable).flatMap { choice in
            choice.candidates.map { MoveAction(name: Explain.moveTitle(choice.move, point: $0), move: choice.move, point: $0) }
        }
    }

    /// What someone might say for this pick: its title, without "(Original Mix)" and the like.
    static func spokenNames(_ pick: Pick) -> [String] {
        let bare = pick.title
            .replacingOccurrences(of: #"\s*[\(\[][^\)\]]*[\)\]]"#, with: "", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
        var names = [bare.isEmpty ? pick.title : bare]
        if !bare.isEmpty && bare != pick.title { names.append(pick.title) }
        if !pick.artist.isEmpty { names.append("\(names[0]) by \(pick.artist)") }
        return names
    }

    private func select() {
        session.selectionByStep[pick.stepID] = pick.id
        openDetail?(pick)
    }
}

/// VoiceOver reads a pick row as one pick, with play, keep, pass, the moves and details as its
/// actions; Voice Control answers to its title ("Tap …" selects, "Long press …" opens every action).
struct PickRowAccessibility: ViewModifier {
    @Environment(DigSession.self) private var session
    let pick: Pick
    let isSelected: Bool
    let verdict: Verdict?
    let moveActions: [PickRow.MoveAction]
    let hasDetail: Bool
    let select: () -> Void

    func body(content: Content) -> some View {
        let label: String = "\(pick.title), \(pick.artist)"
        let value: String = session.spokenMarks(for: pick, selected: isSelected)
        let hint: String = Explain.short(pick)
        let traits: AccessibilityTraits = isSelected ? [.isButton, .isSelected] : .isButton
        let names: [String] = PickRow.spokenNames(pick)
        let playing: Bool = session.playback.isCurrent(pick.id) && session.playback.isPlaying
        return content
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(label)
            .accessibilityValue(value)
            .accessibilityHint(hint)
            .accessibilityAddTraits(traits)
            .accessibilityInputLabels(names)
            .accessibilityAction(.default) { select() }
            // iOS offers a view's actions last-declared first, so they're written here in reverse:
            // VoiceOver offers Play, Keep, Pass, Details, then the moves in the order details lists them.
            .accessibilityActions {
                ForEach(moveActions.reversed()) { action in
                    Button(action.name) {
                        Task { await session.branch(from: pick, move: action.move, origin: action.point) }
                    }
                }
                if hasDetail {
                    Button("Details") { select() }
                }
                Button(verdict == .pass ? "Remove pass" : "Pass") {
                    Task { await session.judge(pick, .pass) }
                }
                Button(verdict == .keep ? "Remove keep" : "Keep") {
                    Task { await session.judge(pick, .keep) }
                }
                if pick.videoID != nil {
                    Button(playing ? "Pause" : "Play") { PlayButton.press(pick, in: session) }
                }
            }
    }
}

/// The marks after a pick's why, as the web client words them: playing or paused in green, kept
/// in magenta, passed and heard in grey, and no video. Each is a word as well as a color.
struct PickMarks: View {
    @Environment(DigSession.self) private var session
    let pick: Pick

    var body: some View {
        let playing = session.playback.isCurrent(pick.id)
        let verdict = session.verdict(for: pick.trackKey)
        let heard = session.isHeard(pick.trackKey)
        if playing || verdict != nil || heard || pick.videoID == nil {
            HStack(spacing: 10) {
                if playing {
                    let word = session.playback.markWord
                    Text(word == "paused" ? "\u{275A}\u{275A} paused" : "\u{25B6}\u{FE0E} \(word)")
                        .foregroundStyle(word == "paused" ? Theme.lcdSoft : Theme.playing)
                }
                if verdict == .keep {
                    Text("\u{2605} kept").foregroundStyle(Theme.kept)
                } else if verdict == .pass {
                    Text("\u{2715} passed").foregroundStyle(Theme.passed)
                }
                if heard {
                    Text("heard").foregroundStyle(Theme.muted)
                }
                if pick.videoID == nil {
                    Text("No video")
                        .font(.ui(12))
                        .textCase(.uppercase)
                        .foregroundStyle(Color(hex: 0x8A93A7))
                }
            }
            .font(.footnote)
            .padding(.top, 2)
        }
    }
}

/// The rank, in amber on the LCD face (green while it plays, soft green paused), over the
/// nearness bar: how close cosine.club scored it, filled in cyan.
struct RankMark: View {
    @Environment(DigSession.self) private var session
    let pick: Pick

    var body: some View {
        let current = session.playback.isCurrent(pick.id)
        let color = !current ? Theme.amber : session.playback.isPlaying ? Theme.playing : Theme.lcdSoft
        VStack(alignment: .trailing, spacing: 3) {
            Text("\(pick.rank)")
                .font(.lcd(22))
                .foregroundStyle(color)
                .shadow(color: current ? Theme.lcdGlow : .clear, radius: 4)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Theme.line
                .frame(width: 26, height: 5)
                .overlay(alignment: .leading) {
                    Theme.nearness.frame(width: 26 * min(max(pick.score, 0), 1))
                }
        }
        .frame(width: 30, alignment: .trailing)
        .accessibilityHidden(true)
    }
}

/// Play this pick (then the rest of its step), or pause it if it's the one playing. The key
/// stays pressed in and lit while its track is in the player.
struct PlayButton: View {
    @Environment(DigSession.self) private var session
    let pick: Pick

    var body: some View {
        let current = session.playback.isCurrent(pick.id)
        let playing = current && session.playback.isPlaying
        let playable = pick.videoID != nil
        Button {
            Self.press(pick, in: session)
        } label: {
            Image(systemName: !playable ? "play.slash" : playing ? "pause.fill" : "play.fill")
                .font(.system(size: 14, weight: .bold))
        }
        .buttonStyle(.key(current ? .active : .normal, size: .small))
        .disabled(!playable)
        .accessibilityLabel(!playable ? "No video for \(pick.title)" : playing ? "Pause \(pick.title)" : "Play \(pick.title)")
    }

    @MainActor
    static func press(_ pick: Pick, in session: DigSession) {
        if session.playback.isCurrent(pick.id) {
            session.playback.toggle()
        } else {
            session.play(pick)
        }
    }
}

/// Keep and pass, as the web client's slim list shows them: a star and a cross. The one that's on
/// is lit (magenta for kept, red for passed); pressing it again clears it.
struct JudgeButtons: View {
    @Environment(DigSession.self) private var session
    let pick: Pick

    var body: some View {
        let verdict = session.verdict(for: pick.trackKey)
        HStack(spacing: 0) {
            Button {
                Task { await session.judge(pick, .keep) }
            } label: {
                Image(systemName: verdict == .keep ? "star.fill" : "star")
                    .foregroundStyle(verdict == .keep ? Theme.kept : Theme.muted)
                    .shadow(color: verdict == .keep ? Theme.kept.opacity(0.5) : .clear, radius: 4)
                    .frame(width: Theme.tap, height: Theme.tap)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel(verdict == .keep ? "Remove keep from \(pick.title)" : "Keep \(pick.title)")
            Button {
                Task { await session.judge(pick, .pass) }
            } label: {
                Image(systemName: "xmark")
                    .foregroundStyle(verdict == .pass ? Color(hex: 0xFF9AA5) : Theme.muted)
                    .frame(width: 30, height: 30)
                    .background(verdict == .pass ? Color(hex: 0x8A2330) : Color.clear)
                    .frame(width: Theme.tap, height: Theme.tap)
                    .contentShape(Rectangle())
            }
            .accessibilityLabel(verdict == .pass ? "Remove pass from \(pick.title)" : "Pass \(pick.title)")
        }
        .font(.system(size: 17, weight: .bold))
        .buttonStyle(.plain)
    }
}

/// Everything you can do with a pick, for its long-press menu.
struct PickActions: View {
    @Environment(DigSession.self) private var session
    let pick: Pick
    let openDetail: ((Pick) -> Void)?

    var body: some View {
        let verdict = session.verdict(for: pick.trackKey)
        let playing = session.playback.isCurrent(pick.id) && session.playback.isPlaying
        Button { PlayButton.press(pick, in: session) } label: {
            Label(playing ? "Pause" : "Play", systemImage: playing ? "pause" : "play")
        }
        .disabled(pick.videoID == nil)
        Button { Task { await session.judge(pick, .keep) } } label: {
            Label(verdict == .keep ? "Remove keep" : "Keep", systemImage: verdict == .keep ? "star.slash" : "star")
        }
        Button { Task { await session.judge(pick, .pass) } } label: {
            Label(verdict == .pass ? "Remove pass" : "Pass", systemImage: "xmark")
        }
        Menu {
            MoveMenuItems(pick: pick)
        } label: {
            Label("Sounds like…", systemImage: "arrow.triangle.branch")
        }
        if let openDetail {
            Button { session.selectionByStep[pick.stepID] = pick.id; openDetail(pick) } label: {
                Label("Details", systemImage: "info.circle")
            }
        }
        if let url = Explain.youTubeMusicURL(pick) {
            Link(destination: url) { Label("Open in YouTube Music", systemImage: "arrow.up.right.square") }
        }
    }
}

/// The moves a pick offers, as menu items. A move that can't start says why.
struct MoveMenuItems: View {
    @Environment(DigSession.self) private var session
    let pick: Pick

    var body: some View {
        ForEach(session.choices(for: pick), id: \.move) { choice in
            if !choice.isAvailable {
                Button {} label: {
                    Text(choice.move.title)
                    Text(choice.unavailableReason ?? "Not available")
                }
                .disabled(true)
            } else if choice.candidates.count == 1, let point = choice.candidates.first {
                Button { Task { await session.branch(from: pick, move: choice.move, origin: point) } } label: {
                    Text(Explain.moveTitle(choice.move, point: point))
                }
            } else {
                Menu(choice.move.title) {
                    ForEach(choice.candidates, id: \.pointID) { point in
                        Button { Task { await session.branch(from: pick, move: choice.move, origin: point) } } label: {
                            Text(point.name)
                            let roles = Moves.roles(of: FactEntity(id: point.discogsID, name: point.name), in: pick)
                            if !roles.isEmpty { Text(roles.joined(separator: ", ")) }
                        }
                    }
                }
            }
        }
    }
}
