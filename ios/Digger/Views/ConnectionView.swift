import DiggerKit
import SwiftUI
import UIKit

/// Which Digger this device is paired with, how playback works, and unpairing.
struct ConnectionView: View {
    @Environment(DigSession.self) private var session
    @Environment(YouTubePlayer.self) private var player
    @AppStorage("layoutOverride") private var layoutOverride = "automatic"
    @State private var reconnecting = false
    @State private var confirmingUnpair = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Deck(title: "Digger", status: session.phase == .ready ? "online" : nil) {
                    fact("Computer", session.serverName ?? session.connection?.displayName ?? "Unknown")
                    if let url = session.activeURL {
                        fact("Address", url.absoluteString)
                            .textSelection(.enabled)
                    }
                    HStack(spacing: 8) {
                        LED(on: session.phase == .ready, color: statusColor)
                        fact("Status", status)
                    }
                    HStack(spacing: 6) {
                        Button {
                            Task {
                                reconnecting = true
                                await session.connect()
                                reconnecting = false
                            }
                        } label: {
                            HStack(spacing: 8) {
                                Text("Reconnect")
                                if reconnecting { ProgressView().tint(Theme.lcd) }
                            }
                        }
                        .buttonStyle(.key)
                        .disabled(reconnecting)
                        Button("Open Settings") {
                            if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                        }
                        .buttonStyle(.key)
                    }
                    Text("If Digger can't be reached, check that Local Network is on for Digger in Settings > Privacy & Security > Local Network.")
                        .font(.footnote)
                        .foregroundStyle(Theme.muted)
                }

                youtubeDeck

                Deck(title: "Playback") {
                    Text("Videos play in YouTube's own player, only while it's on screen. Digger pauses them when you leave the app or lock the screen. Keys, logins and setup stay on your computer.")
                        .font(.footnote)
                        .foregroundStyle(Theme.muted)
                    PanelLink(title: "YouTube Terms of Service", destination: URL(string: "https://www.youtube.com/t/terms")!)
                    if let problem = player.lastProblem {
                        AlertBox(level: .warning) {
                            Text("Last player problem: \(problem)").fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }

                if UIDevice.current.userInterfaceIdiom == .pad {
                    Deck(title: "Layout") {
                        // The web client's mode keys: the one that's on is pressed in with its lamp lit.
                        HStack(spacing: 6) {
                            layoutKey("Follow", "automatic")
                            layoutKey("Narrow", "narrow")
                            layoutKey("Wide", "wide")
                        }
                        Text("Digger switches layout when the window gets narrow. Force one here to compare them.")
                            .font(.footnote)
                            .foregroundStyle(Theme.muted)
                    }
                }

                Deck(title: "This device") {
                    Button("Unpair this device") { confirmingUnpair = true }
                        .buttonStyle(.key(.danger))
                    Text("Your digs, keeps and passes stay on your computer.")
                        .font(.footnote)
                        .foregroundStyle(Theme.muted)
                }
            }
            .padding(12)
            .frame(maxWidth: 640, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .caseBackground()
        .skinTitle("Connection")
        .forgetDialog(isPresented: $confirmingUnpair)
    }

    private func fact(_ name: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(name)
                .font(.ui(13))
                .textCase(.uppercase)
                .foregroundStyle(Theme.muted)
                .frame(width: 92, alignment: .leading)
            Text(value)
                .font(.subheadline)
                .foregroundStyle(Theme.text)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .accessibilityElement(children: .combine)
    }

    private func layoutKey(_ title: String, _ value: String) -> some View {
        let on = layoutOverride == value
        return Button(title) { layoutOverride = value }
            .buttonStyle(.key(.mode(on: on)))
            .accessibilityLabel(value == "automatic" ? "Follow the window" : title)
            .accessibilityAddTraits(on ? .isSelected : [])
    }

    /// The computer's YouTube Music login, and logging in from that Mac's Chrome: the cookies are
    /// read and kept there, and only the login's state comes here.
    private var youtubeDeck: some View {
        let youtube = session.settings?.youtube
        let connected = youtube?.saved == true && youtube?.checked == "connected"
        return Deck(title: "YouTube Music", status: connected ? "connected" : nil) {
            HStack(spacing: 8) {
                LED(on: youtube?.saved == true && youtube?.checked != "rejected", color: youtubeColor(youtube))
                fact("Login", youtubeStatus(youtube))
            }
            if let youtube, youtube.saved == true, youtube.checked != "rejected" {
                // Logging in again from here would replace a login that works; that stays in Setup.
                Text("To log in again or use another account, use Digger's Setup on your computer.")
                    .font(.footnote)
                    .foregroundStyle(Theme.muted)
            } else if youtube?.chrome == true {
                Button {
                    Task { await session.logInYouTubeFromChrome() }
                } label: {
                    HStack(spacing: 8) {
                        Text(session.youtubeLoggingIn ? "Waiting for your Mac\u{2026}" : "Log in from Chrome on your Mac")
                        if session.youtubeLoggingIn { ProgressView().tint(Theme.lcd) }
                    }
                }
                .buttonStyle(.key(.youtube))
                .disabled(session.youtubeLoggingIn)
                if session.youtubeLoggingIn {
                    AlertBox(level: .warning) {
                        Text("Click Allow on your Mac when macOS asks to use \u{201C}Chrome Safe Storage\u{201D}.")
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Text("Digger on your Mac reads YouTube Music's sign-in from Chrome there and keeps it there. The login never comes to this device.")
                    .font(.footnote)
                    .foregroundStyle(Theme.muted)
            } else if youtube != nil {
                Text("Connect YouTube Music in Digger's Setup on your computer: log in from Chrome, or paste request headers there.")
                    .font(.footnote)
                    .foregroundStyle(Theme.muted)
            } else {
                Text("This Digger doesn't say how YouTube Music is connected. Update Digger on your computer to send digs from here.")
                    .font(.footnote)
                    .foregroundStyle(Theme.muted)
            }
            if let report = session.youtubeLoginReport {
                AlertBox(level: report.isError ? .error : .ok) {
                    Text(report.message).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }

    private func youtubeStatus(_ youtube: YouTubeStatus?) -> String {
        guard let youtube else { return "Unknown" }
        guard youtube.saved == true else { return "Not connected" }
        switch youtube.checked {
        case "connected": return "Connected"
        case "rejected": return "Signed out"
        case "unreachable": return "Saved; YouTube Music didn't answer its check"
        default: return "Saved, not checked yet"
        }
    }

    private func youtubeColor(_ youtube: YouTubeStatus?) -> Color {
        guard youtube?.saved == true else { return Theme.red }
        switch youtube?.checked {
        case "connected": return Theme.lcd
        case "rejected": return Theme.red
        default: return Theme.amber
        }
    }

    private var statusColor: Color {
        switch session.phase {
        case .ready: return Theme.lcd
        case .connecting: return Theme.amber
        default: return Theme.red
        }
    }

    private var status: String {
        switch session.phase {
        case .ready: return "Connected"
        case .connecting: return "Connecting…"
        case .offline: return "Can't reach Digger"
        case .unauthorized: return "Not paired"
        case .unpaired: return "Not paired"
        }
    }
}
