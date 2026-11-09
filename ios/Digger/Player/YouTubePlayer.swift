import DiggerKit
import Observation
import SwiftUI
import UIKit
import WebKit

/// YouTube's IFrame player in one WKWebView that the app keeps for its whole life, so playback
/// survives layout changes: whichever `PlayerSurface` is on screen holds the web view.
///
/// The page loads from https://<bundle id>, so YouTube sees this app's identity as the embed's
/// Referer and origin (without one it refuses with error 152 or 153). Playback is inline and
/// visible only: no Picture in Picture, no AirPlay, nothing drawn over the player.
@Observable
@MainActor
final class YouTubePlayer: PlayerEngine {
    let webView: WKWebView
    @ObservationIgnored var onSignal: @MainActor (PlayerSignal) -> Void = { _ in }
    /// The last error YouTube's script or the web view reported, for the diagnostics screen.
    private(set) var lastProblem: String?

    private let bridge: Bridge
    @ObservationIgnored private var pageLoaded = false
    /// The load of the page in use. WebKit's reports about any other load (one this player gave
    /// up on, or replaced) are ignored, so a late "finished" can't bring back a dead page.
    @ObservationIgnored private var pageNavigation: WKNavigation?
    @ObservationIgnored private var pageRequested = false
    @ObservationIgnored private var queued: [String] = []

    /// The identity YouTube checks: https:// and this app's bundle id, lowercased.
    static var origin: String {
        "https://" + (Bundle.main.bundleIdentifier ?? "com.marcmuon.cosinedigger").lowercased()
    }

    init() {
        let bridge = Bridge()
        let configuration = WKWebViewConfiguration()
        configuration.allowsInlineMediaPlayback = true
        configuration.allowsPictureInPictureMediaPlayback = false
        configuration.allowsAirPlayForMediaPlayback = false
        // Playback still only starts from a tap in the app; this lets that tap reach the iframe.
        configuration.mediaTypesRequiringUserActionForPlayback = []
        configuration.userContentController.add(bridge, name: "player")
        let webView = WKWebView(frame: CGRect(x: 0, y: 0, width: 320, height: 200), configuration: configuration)
        webView.isOpaque = false
        webView.backgroundColor = .black
        webView.scrollView.isScrollEnabled = false
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.allowsLinkPreview = false
        webView.accessibilityLabel = "YouTube video"
        webView.navigationDelegate = bridge
        webView.uiDelegate = bridge
        self.bridge = bridge
        self.webView = webView
        bridge.player = self
    }

    func perform(_ command: PlayerCommand) {
        switch command {
        case .load(let videoID, let start):
            run("digger.load(\(Self.quoted(videoID)), \(start))")
        case .cue(let videoID, let start):
            run("digger.cue(\(Self.quoted(videoID)), \(start))")
        case .play: run("digger.play()")
        case .pause: run("digger.pause()")
        case .stop: run("digger.stop()")
        }
    }

    // MARK: Page

    private func run(_ script: String) {
        guard pageLoaded else {
            queued.append(script)
            loadPageIfNeeded()
            return
        }
        webView.evaluateJavaScript(script) { [weak self] _, error in
            if let error { Task { @MainActor in self?.lastProblem = error.localizedDescription } }
        }
    }

    private func loadPageIfNeeded() {
        guard !pageRequested else { return }
        pageRequested = true
        pageNavigation = webView.loadHTMLString(Self.page(origin: Self.origin), baseURL: URL(string: Self.origin + "/"))
    }

    fileprivate func pageDidLoad(_ navigation: WKNavigation?) {
        guard pageRequested, !pageLoaded, isCurrent(navigation) else { return }
        pageLoaded = true
        let scripts = queued
        queued = []
        scripts.forEach(run)
    }

    fileprivate func pageDidFail(_ navigation: WKNavigation?, _ error: Error) {
        // Starting the page again cancels the old load; that isn't a failure.
        guard pageRequested, isCurrent(navigation), (error as NSError).code != NSURLErrorCancelled else { return }
        lastProblem = error.localizedDescription
        resetPage()
        onSignal(.error(-1))
    }

    /// WebKit's report is about the page in use (any report, if WebKit gave no handle for it).
    private func isCurrent(_ navigation: WKNavigation?) -> Bool {
        pageNavigation == nil || navigation === pageNavigation
    }

    /// Forget the page so the next command loads it again.
    private func resetPage() {
        pageLoaded = false
        pageRequested = false
        pageNavigation = nil
        queued = []
    }

    fileprivate func receive(_ body: Any) {
        guard let message = body as? [String: Any], let type = message["type"] as? String else { return }
        switch type {
        case "ready": onSignal(.ready)
        case "state":
            switch message["value"] as? Int {
            case 0: onSignal(.ended)
            case 1: onSignal(.playing)
            case 2: onSignal(.paused)
            case 3: onSignal(.buffering)
            case 5: onSignal(.cued)
            default: break
            }
        case "error":
            let code = message["value"] as? Int ?? -1
            // -2: YouTube's player never got going on this page. Load it again on the next tap.
            if code == -2 { resetPage() }
            onSignal(.error(code))
        case "time":
            let position = (message["position"] as? NSNumber)?.doubleValue ?? 0
            let duration = (message["duration"] as? NSNumber)?.doubleValue ?? 0
            onSignal(.time(position: position, duration: duration))
        case "problem":
            lastProblem = message["value"] as? String
        default: break
        }
    }

    fileprivate func contentProcessEnded() {
        // iOS reclaimed the web content process: the video is gone. Play loads it again.
        lastProblem = "iOS stopped the video player to save memory."
        resetPage()
        onSignal(.lost)
    }

    static func quoted(_ value: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [value])) ?? Data("[\"\"]".utf8)
        let array = String(decoding: data, as: UTF8.self)
        return String(array.dropFirst().dropLast())
    }

    static func page(origin: String) -> String {
        """
        <!doctype html>
        <html><head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
        <meta name="referrer" content="strict-origin-when-cross-origin">
        <style>html,body{margin:0;padding:0;height:100%;background:#000;overflow:hidden}
        #player{position:absolute;top:0;left:0;width:100%;height:100%}</style>
        </head><body><div id="player"></div>
        <script>
        (function () {
          var player = null, ready = false, gaveUp = false, waiting = [];
          function post(message) { window.webkit.messageHandlers.player.postMessage(message); }
          function giveUp(why) {
            if (ready || gaveUp) { return; }
            gaveUp = true;
            post({type: 'problem', value: why});
            post({type: 'error', value: -2});
          }
          function whenReady(work) { if (ready) { work(); } else { waiting.push(work); } }
          window.onerror = function (message) { post({type: 'problem', value: String(message)}); };
          window.onYouTubeIframeAPIReady = function () {
            player = new YT.Player('player', {
              width: '100%', height: '100%',
              playerVars: {playsinline: 1, rel: 0, enablejsapi: 1,
                           origin: \(quoted(origin)), widget_referrer: \(quoted(origin))},
              events: {
                onReady: function () {
                  if (gaveUp) { return; }
                  ready = true;
                  post({type: 'ready'});
                  waiting.splice(0).forEach(function (work) { work(); });
                },
                onStateChange: function (event) { post({type: 'state', value: event.data}); },
                onError: function (event) { post({type: 'error', value: event.data}); }
              }
            });
          };
          window.digger = {
            load: function (id, start) { whenReady(function () { player.loadVideoById({videoId: id, startSeconds: start}); }); },
            cue: function (id, start) { whenReady(function () { player.cueVideoById({videoId: id, startSeconds: start}); }); },
            play: function () { whenReady(function () { player.playVideo(); }); },
            pause: function () { whenReady(function () { player.pauseVideo(); }); },
            stop: function () { whenReady(function () { player.stopVideo(); }); }
          };
          setInterval(function () {
            if (ready && player.getPlayerState && player.getPlayerState() === 1) {
              post({type: 'time', position: player.getCurrentTime(), duration: player.getDuration()});
            }
          }, 1000);
          var script = document.createElement('script');
          script.src = 'https://www.youtube.com/iframe_api';
          script.onerror = function () { giveUp("YouTube's player script did not load."); };
          document.head.appendChild(script);
          setTimeout(function () { giveUp("YouTube's player did not start within 15 seconds."); }, 15000);
        })();
        </script>
        </body></html>
        """
    }
}

/// WebKit's delegates, kept off the observable player.
@MainActor
private final class Bridge: NSObject, WKScriptMessageHandler, WKNavigationDelegate, WKUIDelegate {
    weak var player: YouTubePlayer?

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        player?.receive(message.body)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        player?.pageDidLoad(navigation)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        player?.pageDidFail(navigation, error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        player?.pageDidFail(navigation, error)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        player?.contentProcessEnded()
    }

    /// Links in the player (the YouTube logo, "Watch on YouTube") open YouTube, not this page.
    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction) async -> WKNavigationActionPolicy {
        if navigationAction.navigationType == .linkActivated, let url = navigationAction.request.url {
            // The completion-handler form: in this async method the bare call is the async overload.
            UIApplication.shared.open(url, options: [:], completionHandler: nil)
            return .cancel
        }
        return .allow
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url { UIApplication.shared.open(url) }
        return nil
    }
}

/// Where the player shows. Only one is on screen at a time; the one in a window holds the web view.
struct PlayerSurface: UIViewRepresentable {
    let player: YouTubePlayer

    func makeUIView(context: Context) -> PlayerContainer { PlayerContainer(player: player) }

    func updateUIView(_ container: PlayerContainer, context: Context) { container.adopt() }

    final class PlayerContainer: UIView {
        let player: YouTubePlayer

        init(player: YouTubePlayer) {
            self.player = player
            super.init(frame: .zero)
            backgroundColor = .black
            clipsToBounds = true
        }

        required init?(coder: NSCoder) { fatalError("init(coder:) is not used") }

        override func didMoveToWindow() {
            super.didMoveToWindow()
            adopt()
        }

        func adopt() {
            guard window != nil, player.webView.superview !== self else { return }
            player.webView.removeFromSuperview()
            player.webView.frame = bounds
            player.webView.autoresizingMask = [.flexibleWidth, .flexibleHeight]
            addSubview(player.webView)
        }
    }
}
