import DiggerKit
import SwiftUI
import UIKit

/// The faceplate: the YouTube player, the LCD with what's playing, and the transport keys. It is
/// on screen whenever a track is loaded, the video at least 200 points tall with nothing drawn
/// over it (the bevel sits around it). Closing it stops playback.
struct PlayerPanel: View {
    @Environment(DigSession.self) private var session
    @Environment(YouTubePlayer.self) private var player
    /// Called after "go to the playing track", so a phone can pop back to the step.
    let onGoToPlaying: () -> Void

    var body: some View {
        let playback = session.playback
        VStack(alignment: .leading, spacing: 8) {
            Color.black
                .aspectRatio(16.0 / 9.0, contentMode: .fit)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, minHeight: 200)
                .overlay { PlayerSurface(player: player) }
                .accessibilityElement(children: .contain)
                .accessibilityLabel("YouTube video")
                .padding(2)
                .bevel(sunken: true)

            if let item = playback.current {
                VStack(alignment: .leading, spacing: 2) {
                    NowPlayingTitle(item: item, onGoToPlaying: onGoToPlaying)
                    PlaybackStatus(item: item)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 4)
                .frame(maxWidth: .infinity, alignment: .leading)
                .lcdScreen()
                Transport()
            }
        }
        .padding(8)
        .background(LinearGradient(colors: [Theme.caseMid, Theme.caseMain], startPoint: .top, endPoint: .bottom))
        .bevel()
    }
}

/// The LCD before anything plays: what the web client's player says, and how to start.
struct IdlePlayerPanel: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("nothing playing yet")
                .font(.lcd(24))
                .foregroundStyle(Theme.lcd)
                .lcdGlow()
            Text("Press \u{25B6}\u{FE0E} on a pick, or Play step.")
                .font(.lcd(19))
                .foregroundStyle(Theme.lcdSoft)
        }
        .lineLimit(1)
        .minimumScaleFactor(0.8)
        .padding(.horizontal, 10)
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
        .lcdScreen()
        .padding(8)
        .background(LinearGradient(colors: [Theme.caseMid, Theme.caseMain], startPoint: .top, endPoint: .bottom))
        .bevel()
        .accessibilityElement(children: .combine)
    }
}

/// What's playing and the step it's from, in the LCD; pressing it shows the track in its step.
struct NowPlayingTitle: View {
    @Environment(DigSession.self) private var session
    let item: QueueItem
    let onGoToPlaying: () -> Void

    var body: some View {
        Button {
            session.goToPlaying()
            onGoToPlaying()
        } label: {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .center, spacing: 8) {
                    Image(systemName: stateSymbol)
                        .font(.system(size: 15, weight: .bold))
                        .frame(width: 18)
                    Text(item.title)
                        .font(.lcd(26))
                        .lineLimit(1)
                }
                .foregroundStyle(Theme.lcd)
                .lcdGlow()
                Text("\(item.artist) \u{00B7} step \(item.stepNumber)")
                    .font(.lcd(20))
                    .foregroundStyle(Theme.lcdSoft)
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity, minHeight: Theme.tap, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Playing \(item.name), from step \(item.stepNumber)")
        .accessibilityHint("Shows this track in its step.")
    }

    private var stateSymbol: String {
        switch session.playback.status {
        case .playing: return "play.fill"
        case .paused: return "pause.fill"
        case .loading: return "ellipsis"
        case .refused: return "exclamationmark"
        default: return "stop.fill"
        }
    }
}

/// The LCD's note line: loading, refused, paused while away, the end of the step, or what's next.
struct PlaybackStatus: View {
    @Environment(DigSession.self) private var session
    let item: QueueItem

    var body: some View {
        let playback = session.playback
        Group {
            switch playback.status {
            case .refused(let message):
                HStack(alignment: .center, spacing: 8) {
                    Text("! \(message)")
                        .foregroundStyle(Theme.amber)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .fixedSize(horizontal: false, vertical: true)
                    if let url = musicURL {
                        Link(destination: url) {
                            Text("YT Music")
                        }
                        .buttonStyle(.key(.youtube, size: .small))
                        .accessibilityLabel("YouTube Music")
                        .accessibilityHint("Opens this track in YouTube Music.")
                    }
                }
            case .paused where playback.pausedForApp:
                Text("Paused while you were away. Press \u{25B6}\u{FE0E} to carry on.")
                    .foregroundStyle(Theme.lcdSoft)
            case .ended:
                Text("That's the end of this step's picks.")
                    .foregroundStyle(Theme.lcdSoft)
            case .loading:
                LCDBusy(text: "loading from YouTube", size: 20)
            default:
                if let next = playback.upNext.first {
                    Text("next: \(next.name)")
                        .foregroundStyle(Theme.lcdSoft)
                        .lineLimit(1)
                }
            }
        }
        .font(.lcd(20))
        .padding(.bottom, 2)
    }
}

extension PlaybackStatus {
    /// Where the track can still be heard when it won't play here.
    var musicURL: URL? {
        if let video = item.videoID { return URL(string: "https://music.youtube.com/watch?v=\(video)") }
        var components = URLComponents(string: "https://music.youtube.com/search")
        components?.queryItems = [URLQueryItem(name: "q", value: item.name)]
        return components?.url
    }
}

/// The transport keys: previous, play or pause, next, keep and pass the playing track, and stop
/// (which closes the player). Keep and Pass spell themselves out when there's room.
struct Transport: View {
    @Environment(DigSession.self) private var session

    var body: some View {
        ViewThatFits(in: .horizontal) {
            keys(spelled: true)
            keys(spelled: false)
        }
    }

    private func keys(spelled: Bool) -> some View {
        let playback = session.playback
        let pick = playback.current.flatMap { session.pick(id: $0.id) }
        let verdict = pick.flatMap { session.verdict(for: $0.trackKey) }
        return HStack(spacing: 6) {
            key("backward.end.fill", "Previous track") { playback.previousTrack() }
            key(playback.isPlaying ? "pause.fill" : "play.fill", playback.isPlaying ? "Pause" : "Play") { playback.toggle() }
            key("forward.end.fill", "Next track") { playback.next() }
                .disabled(playback.upNext.isEmpty)
            if let pick {
                Button {
                    Task { await session.judge(pick, .keep) }
                } label: {
                    Text(spelled ? (verdict == .keep ? "\u{2605} Kept" : "\u{2605} Keep") : "\u{2605}")
                }
                .buttonStyle(.key(verdict == .keep ? .kept : .normal))
                .accessibilityLabel(verdict == .keep ? "Remove keep" : "Keep")
                Button {
                    Task { await session.judge(pick, .pass) }
                } label: {
                    Text(spelled ? (verdict == .pass ? "\u{2715} Passed" : "\u{2715} Pass") : "\u{2715}")
                }
                .buttonStyle(.key(verdict == .pass ? .passed : .normal))
                .accessibilityLabel(verdict == .pass ? "Remove pass" : "Pass")
            }
            Spacer(minLength: 0)
            key("stop.fill", "Close player") { playback.stop() }
        }
    }

    private func key(_ symbol: String, _ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 15, weight: .bold))
        }
        .buttonStyle(.key)
        .accessibilityLabel(label)
    }
}

/// Keeps, passes and problems, with Undo or a link where one helps, in the skin's alert box. It
/// sits below the content (a safe-area inset), so it never covers the last pick or the player.
struct NoticeBanner: View {
    @Environment(DigSession.self) private var session

    var body: some View {
        if let notice = session.notice {
            AlertBox(level: notice.isError ? .error : .ok) {
                Text(notice.message)
                    .font(.footnote)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
                if notice.canUndo && session.canUndo {
                    Button("Undo") {
                        Task { await session.undo() }
                    }
                    .buttonStyle(.key(.normal, size: .small))
                }
                if let link = notice.link {
                    Link(destination: link) { Text("Open") }
                        .buttonStyle(.key(.normal, size: .small))
                }
                Button {
                    withAnimation { session.notice = nil }
                } label: {
                    Text(verbatim: "\u{2715}")
                }
                .buttonStyle(.key(.normal, size: .small))
                .accessibilityLabel("Dismiss")
            }
            .hardShadow()
            .frame(maxWidth: 560)
            .padding(.horizontal, 10)
            .padding(.bottom, 8)
            .transition(.move(edge: .bottom).combined(with: .opacity))
            .accessibilityElement(children: .contain)
            .task(id: notice.id) {
                AccessibilityNotification.Announcement(notice.message).post()
                // With VoiceOver or Switch Control it stays until dismissed: reaching it takes time.
                guard !UIAccessibility.isVoiceOverRunning, !UIAccessibility.isSwitchControlRunning else { return }
                let seconds: Double = notice.canUndo ? 10 : notice.isError ? 8 : 5
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
                if session.notice?.id == notice.id {
                    withAnimation { session.notice = nil }
                }
            }
        }
    }
}
