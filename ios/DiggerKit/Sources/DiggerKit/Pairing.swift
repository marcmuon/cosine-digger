import Foundation

/// Where the paired Digger is and the token that lets this device in. A pairing link lists the
/// computer's Bonjour name first and then its LAN addresses, so a new DHCP address still works.
public struct Connection: Codable, Hashable, Sendable {
    public var urls: [URL]
    public var token: String
    public var name: String?

    public init(urls: [URL], token: String, name: String? = nil) {
        self.urls = urls
        self.token = token
        self.name = name
    }

    public var displayName: String { name ?? urls.first?.host ?? "Digger" }
}

public enum PairingError: Error, Equatable, LocalizedError {
    case notALink
    case missingAddress
    case badAddress(String)
    case badToken

    public var errorDescription: String? {
        switch self {
        case .notALink:
            return "That isn't a Digger pairing link. On your computer, run cosine-digger companion --copy, then paste again."
        case .missingAddress: return "Add the address cosine-digger companion printed, such as http://192.168.1.20:8765."
        case .badAddress(let value): return "\(value) isn't an address Digger can use. It starts with http:// and names the computer and port."
        case .badToken: return "The token is the long line cosine-digger companion printed after Token."
        }
    }
}

public enum Pairing {
    public static let scheme = "cosinedigger"

    /// Read a `cosinedigger://pair?url=…&url=…&token=…&name=…` link (or text containing one).
    public static func parse(link text: String) throws -> Connection {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let start = trimmed.range(of: "\(scheme)://")?.lowerBound ?? trimmed.startIndex
        let candidate = trimmed[start...].split(whereSeparator: \.isWhitespace).first.map(String.init) ?? ""
        guard let components = URLComponents(string: candidate), components.scheme == scheme,
              components.host == "pair" else { throw PairingError.notALink }
        let items = components.queryItems ?? []
        let addresses = items.filter { $0.name == "url" }.compactMap(\.value)
        guard let token = items.first(where: { $0.name == "token" })?.value else { throw PairingError.badToken }
        return try connection(addresses: addresses, token: token, name: items.first { $0.name == "name" }?.value)
    }

    /// A connection typed by hand: one address (http:// optional) and the token.
    public static func connection(address: String, token: String) throws -> Connection {
        try connection(addresses: [address], token: token, name: nil)
    }

    static func connection(addresses: [String], token: String, name: String?) throws -> Connection {
        let urls = try addresses.map(normalize).filter { !$0.absoluteString.isEmpty }
        guard !urls.isEmpty else { throw PairingError.missingAddress }
        let token = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard token.count >= 32, token.count <= 128,
              token.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.contains($0) || $0 == "-" || $0 == "_" }),
              token.allSatisfy(\.isASCII) else { throw PairingError.badToken }
        return Connection(urls: urls, token: token, name: name?.isEmpty == false ? name : nil)
    }

    static func normalize(_ address: String) throws -> URL {
        var value = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { throw PairingError.missingAddress }
        if !value.contains("://") { value = "http://" + value }
        while value.hasSuffix("/") { value.removeLast() }
        guard let components = URLComponents(string: value), ["http", "https"].contains(components.scheme ?? ""),
              let host = components.host, !host.isEmpty, components.user == nil, components.password == nil,
              components.path.isEmpty, components.query == nil, components.fragment == nil,
              let url = components.url else { throw PairingError.badAddress(address) }
        if components.port == nil, components.scheme == "http" {
            // Digger's port unless the link says otherwise.
            var withPort = components
            withPort.port = 8765
            return withPort.url ?? url
        }
        return url
    }
}
