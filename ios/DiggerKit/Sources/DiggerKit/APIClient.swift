import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public enum APIError: Error, Equatable, LocalizedError {
    /// No answer: Digger isn't running in companion mode, or this device isn't on its network.
    case unreachable(String)
    /// The token was refused: Digger made a new one, or this is another Digger.
    case unauthorized(String)
    case forbidden(String)
    /// The saved state changed elsewhere (another device, or the Mac): reload and try again.
    case conflict(String)
    case rejected(status: Int, message: String)
    case invalidResponse(String)

    public var errorDescription: String? {
        switch self {
        case .unreachable(let message), .unauthorized(let message), .forbidden(let message),
             .conflict(let message), .invalidResponse(let message): return message
        case .rejected(_, let message): return message
        }
    }

    public var isTransport: Bool { if case .unreachable = self { return true } else { return false } }
}

/// Digger's JSON API over HTTP, with the pairing token on every request. Requests that create
/// steps carry an Idempotency-Key and are retried with the same key after a dropped connection,
/// so a retried tap never makes a second step.
public final class APIClient: @unchecked Sendable {
    public let baseURL: URL
    private let token: String
    private let session: URLSession

    public init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    // MARK: Endpoints

    /// Who's there. Not retried: pairing tries each address in turn, and a dead one shouldn't hold it up.
    public func companion() async throws -> CompanionInfo { try await get("/api/companion", timeout: 6, retries: 0) }
    public func settings() async throws -> ServerSettings { try await get("/api/settings") }
    public func digs(limit: Int = 20) async throws -> [DigSummary] {
        let list: DigList = try await get("/api/digs", query: [URLQueryItem(name: "limit", value: String(limit))])
        return list.digs
    }
    public func dig(_ id: String) async throws -> Dig { try await get("/api/digs/\(Self.segment(id))") }
    public func step(_ id: String) async throws -> StepEnvelope { try await get("/api/steps/\(Self.segment(id))") }

    public func search(_ query: String) async throws -> SearchResult {
        try await get("/api/search", query: [URLQueryItem(name: "q", value: query), URLQueryItem(name: "source", value: "all")],
                      timeout: 12, retries: 0)
    }

    public func createDig(seeds: [Point], idempotencyKey: String) async throws -> StepIdentity {
        struct Body: Encodable { let seeds: [Point] }
        return try await send("POST", "/api/digs", body: Body(seeds: seeds.map(\.requested)), idempotencyKey: idempotencyKey)
    }

    public func createStep(_ request: StepRequest, idempotencyKey: String) async throws -> StepIdentity {
        try await send("POST", "/api/steps", body: request, idempotencyKey: idempotencyKey)
    }

    /// Keep or pass (`verdict`), clear (`nil`), or undo the latest change (`undoChangeID`).
    public func judge(trackKey: String, stepID: String, verdict: Verdict?, expectedRevision: Int,
                      undoChangeID: String? = nil) async throws -> JudgmentResult {
        struct Put: Encodable {
            let step_id: String, verdict: String, expected_revision: Int
        }
        struct Delete: Encodable {
            let step_id: String, expected_revision: Int, undo_change_id: String?
        }
        let path = "/api/judgments/\(Self.segment(trackKey))"
        if let verdict, undoChangeID == nil {
            return try await send("PUT", path, body: Put(step_id: stepID, verdict: verdict.rawValue, expected_revision: expectedRevision))
        }
        return try await send("DELETE", path, body: Delete(step_id: stepID, expected_revision: expectedRevision,
                                                           undo_change_id: undoChangeID))
    }

    public func heard(trackKey: String, stepID: String) async throws -> HeardMark {
        struct Body: Encodable { let track_key: String, step_id: String }
        return try await send("POST", "/api/heard", body: Body(track_key: trackKey, step_id: stepID))
    }

    /// Send the dig to its own private YouTube Music playlist, or bring that playlist up to date.
    /// Answers the send's job id; a send already going for this dig answers its own.
    public func sendToYouTubeMusic(digID: String) async throws -> String {
        struct Empty: Encodable {}
        struct Started: Decodable { let job_id: String }
        let started: Started = try await send("POST", "/api/digs/\(Self.segment(digID))/export", body: Empty())
        return started.job_id
    }

    public func backgroundJob(_ id: String) async throws -> BackgroundJob { try await get("/api/jobs/\(Self.segment(id))") }

    /// Ask the computer to log in to YouTube Music from its own Chrome. It waits while macOS asks,
    /// on the computer, to share Chrome's cookie key (up to two minutes), so it waits longer than
    /// other requests and isn't retried.
    public func logInYouTubeFromChrome() async throws -> YouTubeLoginResult {
        struct Empty: Encodable {}
        return try await send("POST", "/api/youtube/login/chrome", body: Empty(), timeout: 150)
    }

    // MARK: Transport

    /// Why Digger didn't answer, and what to check. On iOS, Local Network access turned off looks
    /// the same as a computer that isn't there, so say where to turn it on.
    static func unreachable(_ url: URL, _ error: Error) -> String {
        // The port too: Digger on another port is a common reason.
        let place = url.host.map { host in url.port.map { "\(host):\($0)" } ?? host } ?? url.absoluteString
        var text = "Can't reach Digger at \(place). Check that it's running with "
            + "cosine-digger serve --companion, and that this device is on the same network."
        let localCodes: [URLError.Code] = [.notConnectedToInternet, .cannotConnectToHost, .timedOut,
                                           .networkConnectionLost, .cannotFindHost]
        if let code = (error as? URLError)?.code, localCodes.contains(code) {
            text += " If you turned off Local Network for Digger, turn it on in Settings > Privacy & Security > Local Network."
        }
        return text
    }

    /// One path segment, percent-encoded: track keys hold "|", spaces and more.
    public static func segment(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: unreserved) ?? value
    }

    /// RFC 3986's unreserved characters, ASCII only (`.alphanumerics` would let "é" through).
    static let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    func get<T: Decodable>(_ path: String, query: [URLQueryItem] = [], timeout: TimeInterval = 15,
                           retries: Int = 1) async throws -> T {
        var components = URLComponents(string: baseURL.absoluteString + path)
        // Encoded here: URLComponents leaves "+" as it is, and the server reads a "+" as a space.
        if !query.isEmpty {
            components?.percentEncodedQueryItems = query.map { URLQueryItem(name: Self.segment($0.name), value: $0.value.map(Self.segment)) }
        }
        guard let url = components?.url else { throw APIError.invalidResponse("Couldn't build the address for \(path).") }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = "GET"
        return try await perform(request, retries: retries)
    }

    func send<T: Decodable, B: Encodable>(_ method: String, _ path: String, body: B,
                                          idempotencyKey: String? = nil, timeout: TimeInterval = 20) async throws -> T {
        guard let url = URL(string: baseURL.absoluteString + path) else {
            throw APIError.invalidResponse("Couldn't build the address for \(path).")
        }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let idempotencyKey { request.setValue(idempotencyKey, forHTTPHeaderField: "Idempotency-Key") }
        request.httpBody = try JSONEncoder().encode(body)
        // Only a request the server can recognize as a repeat is retried after a dropped connection.
        return try await perform(request, retries: idempotencyKey == nil ? 0 : 2)
    }

    private func perform<T: Decodable>(_ request: URLRequest, retries: Int) async throws -> T {
        var request = request
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        var attempt = 0
        while true {
            do {
                let (data, response) = try await session.data(for: request)
                return try decode(data, response)
            } catch let error as APIError {
                throw error
            } catch is CancellationError {
                throw CancellationError()
            } catch let error as URLError where error.code == .cancelled {
                throw CancellationError()
            } catch {
                if attempt >= retries { throw APIError.unreachable(Self.unreachable(baseURL, error)) }
                attempt += 1
                try await Task.sleep(nanoseconds: UInt64(attempt) * 600_000_000)
            }
        }
    }

    private func decode<T: Decodable>(_ data: Data, _ response: URLResponse) throws -> T {
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse("Digger sent no HTTP answer.") }
        guard (200..<300).contains(http.statusCode) else {
            let message = Self.message(in: data) ?? "Digger answered HTTP \(http.statusCode)."
            switch http.statusCode {
            case 401: throw APIError.unauthorized(message)
            case 403: throw APIError.forbidden(message)
            case 409: throw APIError.conflict(message)
            default: throw APIError.rejected(status: http.statusCode, message: message)
            }
        }
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw APIError.invalidResponse("Digger's answer didn't read as expected (\(Self.describe(error))). "
                                           + "Digger on your computer may be a different version.")
        }
    }

    static func message(in data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        if let text = object["error"] as? String { return text }
        if let text = object["detail"] as? String { return text }
        return (object["detail"] as? [String: Any])?["message"] as? String
    }

    static func describe(_ error: Error) -> String {
        guard let error = error as? DecodingError else { return String(describing: error) }
        switch error {
        case .keyNotFound(let key, let context):
            return "missing \(key.stringValue) at \(context.codingPath.map(\.stringValue).joined(separator: "."))"
        case .typeMismatch(_, let context), .valueNotFound(_, let context), .dataCorrupted(let context):
            return "\(context.debugDescription) at \(context.codingPath.map(\.stringValue).joined(separator: "."))"
        @unknown default:
            return String(describing: error)
        }
    }
}
