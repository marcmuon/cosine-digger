import DiggerKit
import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

/// A stand-in Digger for SwiftUI previews and screenshots, answering from the recorded fixture
/// answers in DiggerKit's tests (real `serve --companion` output in slice-v1 test mode).
///
/// Launch the app with `-DiggerDemo` to use it, plus one of `-DiggerDemoOffline` (every request
/// fails), `-DiggerDemoSlow` (answers take 8 s) or `-DiggerDemoEmpty` (no digs yet) to see
/// those states. Keeps and passes are remembered for the run. YouTube Music is connected unless
/// `-DiggerDemoYTDisconnected` is given; logging in from Chrome then connects it after a moment,
/// and a send makes a stand-in playlist.
final class DemoServer: URLProtocol {
    enum Mode: String { case normal, offline, slow, empty }

    nonisolated(unsafe) static var mode: Mode = .normal
    private static let lock = NSLock()
    nonisolated(unsafe) private static var revisions: [String: Int] = [:]
    nonisolated(unsafe) private static var youtubeConnected = !ProcessInfo.processInfo.arguments.contains("-DiggerDemoYTDisconnected")
    /// A send's job: polls answered so far (the first says running), and when the dig was sent.
    nonisolated(unsafe) private static var sendPolls = 0
    nonisolated(unsafe) private static var sentAt: String?
    private static let sendJob = "5e7d0b1c2a3f4e5d6c7b8a9f0e1d2c3b"
    private static let playlist = "https://music.youtube.com/playlist?list=PLdiggerdemo"

    static let baseURL = URL(string: "http://demo.digger.invalid:8765")!
    static let connection = Connection(urls: [baseURL], token: String(repeating: "d", count: 43), name: "Demo")

    static var isRequested: Bool { ProcessInfo.processInfo.arguments.contains("-DiggerDemo") }

    static func modeFromArguments() -> Mode {
        let arguments = ProcessInfo.processInfo.arguments
        if arguments.contains("-DiggerDemoOffline") { return .offline }
        if arguments.contains("-DiggerDemoSlow") { return .slow }
        if arguments.contains("-DiggerDemoEmpty") { return .empty }
        return .normal
    }

    /// A session that talks to this stand-in.
    @MainActor
    static func session(mode: Mode = .normal, store: SessionStore? = nil) -> DigSession {
        self.mode = mode
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [DemoServer.self]
        let urlSession = URLSession(configuration: configuration)
        let session = DigSession(store: store ?? MemoryStore(connection: connection),
                                 makeClient: { url, token in APIClient(baseURL: url, token: token, session: urlSession) })
        session.pollInterval = 0.5
        return session
    }

    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == baseURL.host }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func stopLoading() {}

    override func startLoading() {
        let request = self.request
        // Slow: long enough to see each loading state, short of each request's timeout (the
        // pairing check gives up after 6 s, the rest after 12 s or more).
        // Logging in from Chrome waits on macOS's prompt on the computer: long enough to see it waiting.
        let delay: Double = Self.mode == .slow ? (request.url?.path == "/api/companion" ? 2 : 8)
            : request.url?.path == "/api/youtube/login/chrome" ? 2.5 : 0.15
        DispatchQueue.global().asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self else { return }
            if Self.mode == .offline {
                self.client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet))
                return
            }
            let (status, body) = Self.answer(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                                           headerFields: ["Content-Type": "application/json"])!
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: body)
            self.client?.urlProtocolDidFinishLoading(self)
        }
    }

    private static func fixture(_ name: String) -> Data {
        let url = Bundle.main.url(forResource: name, withExtension: "json", subdirectory: "Fixtures")
        return url.flatMap { try? Data(contentsOf: $0) } ?? Data("{}".utf8)
    }

    private static func json(_ object: Any) -> Data {
        (try? JSONSerialization.data(withJSONObject: object)) ?? Data("{}".utf8)
    }

    private static let digID = "b4c9290274724d5f99e8d06083e99012"
    private static let firstStep = "113030b851f24fd287a0b57b4fedd118"
    private static let labelStep = "4afd76d987ce47a7b6162088fba8b211"

    private static func answer(_ request: URLRequest) -> (Int, Data) {
        let path = request.url?.path ?? ""
        let method = request.httpMethod ?? "GET"
        switch (method, path) {
        case ("GET", "/api/companion"): return (200, fixture("companion"))
        case ("GET", "/api/settings"): return (200, settings())
        case ("GET", "/api/digs"): return (200, mode == .empty ? json(["digs": []]) : fixture("digs"))
        case ("GET", "/api/digs/\(digID)"): return (200, startingAtFirstStep(fixture("dig")))
        case ("POST", "/api/digs/\(digID)/export"):
            lock.lock(); sendPolls = 0; lock.unlock()
            return (200, json(["job_id": sendJob]))
        case ("GET", "/api/jobs/\(sendJob)"): return (200, sendJobAnswer())
        case ("POST", "/api/youtube/login/chrome"):
            lock.lock(); youtubeConnected = true; lock.unlock()
            return (200, json(["ok": true, "logged_in": true, "state": "connected",
                               "settings": (try? JSONSerialization.jsonObject(with: settings())) ?? [:]]))
        case ("GET", "/api/steps/\(firstStep)"): return (200, fixture("step1"))
        case ("GET", "/api/steps/\(labelStep)"): return (200, fixture("step2"))
        case ("GET", "/api/search"): return (200, fixture("search"))
        case ("POST", "/api/digs"):
            return (200, json(["dig_id": digID, "step_id": firstStep, "job_id": "sl-demo", "created": true]))
        case ("POST", "/api/steps"):
            return (200, json(["dig_id": digID, "step_id": labelStep, "job_id": "sl-demo", "created": true]))
        case ("POST", "/api/heard"):
            return (200, fixture("heard"))
        case (_, let judgment) where judgment.hasPrefix("/api/judgments/"):
            return judge(request, trackKey: String(judgment.dropFirst("/api/judgments/".count)))
        default:
            return (404, json(["error": "The demo Digger doesn't know \(method) \(path)."]))
        }
    }

    /// The recorded dig ended on its label step; the demo opens where the journey begins. Once
    /// sent, it has its stand-in playlist.
    private static func startingAtFirstStep(_ data: Data) -> Data {
        guard var dig = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return data }
        dig["last_step_id"] = firstStep
        lock.lock(); let sent = sentAt; lock.unlock()
        if let sent {
            dig["youtube_playlist_url"] = playlist
            dig["youtube_sent_at"] = sent
        }
        return json(dig)
    }

    /// The recorded settings (from a Linux test run: no Chrome, no login), with the demo's login.
    private static func settings() -> Data {
        guard var settings = (try? JSONSerialization.jsonObject(with: fixture("settings"))) as? [String: Any] else {
            return fixture("settings")
        }
        lock.lock(); let connected = youtubeConnected; lock.unlock()
        settings["youtube"] = ["logged_in": connected, "saved": connected,
                               "checked": connected ? "connected" as Any : NSNull(), "chrome": true]
        return json(settings)
    }

    /// The send runs for three polls, long enough to see it, then makes the stand-in playlist.
    private static func sendJobAnswer() -> Data {
        lock.lock()
        defer { lock.unlock() }
        sendPolls += 1
        guard sendPolls > 3 else { return json(["id": sendJob, "kind": "playlist", "state": "running", "events": 1]) }
        let sent = ISO8601DateFormatter().string(from: Date())
        sentAt = sent
        return json(["id": sendJob, "kind": "playlist", "state": "done", "events": 3,
                     "result": ["dig_id": digID, "playlist_url": playlist, "sent_at": sent, "created": true, "verified": true,
                                "sent": 36, "added": 35, "missing": 0, "passed": 2, "no_video": 1]])
    }

    private static func judge(_ request: URLRequest, trackKey: String) -> (Int, Data) {
        let body = (request.httpBody ?? request.httpBodyStream.map(read)).flatMap {
            try? JSONSerialization.jsonObject(with: $0) as? [String: Any]
        } ?? [:]
        lock.lock()
        defer { lock.unlock() }
        let revision = (revisions[trackKey] ?? 0) + 1
        revisions[trackKey] = revision
        let verdict = request.httpMethod == "PUT" ? body["verdict"] as? String : nil
        let effective: [String: Any] = ["verdict": verdict.map { $0 as Any } ?? NSNull(), "step_id": body["step_id"] ?? NSNull(),
                                        "source": verdict == nil ? "none" : "user", "revision": revision]
        return (200, json(["track_key": trackKey, "effective": effective, "revision": revision,
                           "change_id": "demo-\(trackKey)-\(revision)"]))
    }

    private static func read(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(buffer, count: count)
        }
        return data
    }
}
