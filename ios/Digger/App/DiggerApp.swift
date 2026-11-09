import DiggerKit
import SwiftUI

@main
struct DiggerApp: App {
    @State private var session: DigSession
    @State private var player: YouTubePlayer

    init() {
        let player = YouTubePlayer()
        // -DiggerDemo answers from recorded fixtures instead of a Digger (see DemoServer).
        let session = DemoServer.isRequested ? DemoServer.session(mode: DemoServer.modeFromArguments())
            : DigSession(store: AppStore())
        session.playback.engine = player
        player.onSignal = { [weak session] signal in session?.playback.handle(signal) }
        _session = State(initialValue: session)
        _player = State(initialValue: player)
        SkinChrome.install()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(session)
                .environment(player)
                // The web client's skin is dark only.
                .preferredColorScheme(.dark)
                .tint(Theme.lcdSoft)
        }
    }
}
