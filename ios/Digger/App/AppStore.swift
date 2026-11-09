import DiggerKit
import Foundation
import Security

/// The pairing token lives in the Keychain (this device only); where you were lives in UserDefaults.
@MainActor
final class AppStore: SessionStore {
    private let defaults = UserDefaults.standard
    private let service = "com.marcmuon.cosinedigger.pairing"
    private let account = "token"

    private enum Key {
        static let connection = "connection"
        static let lastDig = "lastDigID"
        static let lastSteps = "lastStepIDs"
    }

    func loadConnection() -> Connection? {
        guard let data = defaults.data(forKey: Key.connection),
              var connection = try? JSONDecoder().decode(Connection.self, from: data),
              let token = readToken() else { return nil }
        connection.token = token
        return connection
    }

    func saveConnection(_ connection: Connection?) {
        guard let connection else {
            defaults.removeObject(forKey: Key.connection)
            deleteToken()
            return
        }
        var stored = connection
        stored.token = ""
        defaults.set(try? JSONEncoder().encode(stored), forKey: Key.connection)
        writeToken(connection.token)
    }

    var lastDigID: String? {
        get { defaults.string(forKey: Key.lastDig) }
        set { defaults.set(newValue, forKey: Key.lastDig) }
    }

    func lastStepID(dig: String) -> String? {
        (defaults.dictionary(forKey: Key.lastSteps) as? [String: String])?[dig]
    }

    func setLastStepID(_ stepID: String?, dig: String) {
        var steps = (defaults.dictionary(forKey: Key.lastSteps) as? [String: String]) ?? [:]
        steps[dig] = stepID
        defaults.set(steps, forKey: Key.lastSteps)
    }

    // MARK: Keychain

    private var query: [String: Any] {
        [kSecClass as String: kSecClassGenericPassword,
         kSecAttrService as String: service,
         kSecAttrAccount as String: account]
    }

    private func readToken() -> String? {
        var request = query
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: AnyObject?
        guard SecItemCopyMatching(request as CFDictionary, &result) == errSecSuccess,
              let data = result as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private func writeToken(_ token: String) {
        deleteToken()
        var item = query
        item[kSecValueData as String] = Data(token.utf8)
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        SecItemAdd(item as CFDictionary, nil)
    }

    private func deleteToken() {
        SecItemDelete(query as CFDictionary)
    }
}
