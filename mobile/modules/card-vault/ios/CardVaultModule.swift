import ExpoModulesCore
import Security

/// Secure storage on iOS, backed by the Keychain (SPEC.md §9).
///
/// `kSecClassGenericPassword`, one service per app, one account per key. The
/// Keychain *is* the protection: the value is stored as-is, because encrypting it
/// under a key we would then have to keep somewhere achieves nothing the Keychain
/// is not already doing better.
///
/// Two attribute choices carry the weight here, and both are the sort of thing that
/// stays invisible until it bites:
///
/// - **`kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`.** `WhenUnlocked` is
///   stricter and would break the app: a poll for a 3DS challenge can run with the
///   screen off, and reading the wallet key would fail with
///   `errSecInteractionNotAllowed` at exactly the moment it is needed.
///   `ThisDeviceOnly` is the half that matters for a secret — it keeps the item out
///   of iCloud Keychain and out of encrypted backups, so restoring this app onto a
///   new phone does not carry the key across with it.
/// - **Update before add.** `SecItemAdd` on an existing account answers
///   `errSecDuplicateItem` rather than overwriting, so an add-only implementation
///   keeps the first value a key is ever given and silently discards every later
///   one — which for a rotating reveal token means serving a stale credential
///   forever.
public class CardVaultModule: Module {
  /// Namespaced by bundle id, so two apps on one device never collide and the
  /// items are removed when the app is.
  private var service: String {
    Bundle.main.bundleIdentifier ?? "test.stablecard.rail"
  }

  public func definition() -> ModuleDefinition {
    Name("CardVault")

    Function("describe") { () -> [String: String] in
      ["backend": "keychain", "protection": "device-keystore"]
    }

    AsyncFunction("setItem") { (key: String, value: String) in
      guard let data = value.data(using: .utf8) else {
        throw CardVaultError.notUtf8
      }
      let status = self.upsert(key: key, data: data)
      guard status == errSecSuccess else {
        throw CardVaultError.keychain(status)
      }
    }

    AsyncFunction("getItem") { (key: String) -> String? in
      var query = self.baseQuery(key: key)
      query[kSecReturnData as String] = true
      query[kSecMatchLimit as String] = kSecMatchLimitOne

      var item: CFTypeRef?
      let status = SecItemCopyMatching(query as CFDictionary, &item)
      // A missing key is an ordinary answer rather than a failure: the app asks
      // before it has ever stored anything, on every cold start.
      if status == errSecItemNotFound {
        return nil
      }
      guard status == errSecSuccess, let data = item as? Data else {
        throw CardVaultError.keychain(status)
      }
      return String(data: data, encoding: .utf8)
    }

    AsyncFunction("deleteItem") { (key: String) in
      let status = SecItemDelete(self.baseQuery(key: key) as CFDictionary)
      // Deleting something absent is a no-op, not an error. Callers delete on
      // sign-out and on token expiry, and neither knows what is actually stored.
      guard status == errSecSuccess || status == errSecItemNotFound else {
        throw CardVaultError.keychain(status)
      }
    }
  }

  private func baseQuery(key: String) -> [String: Any] {
    [
      kSecClass as String: kSecClassGenericPassword,
      kSecAttrService as String: service,
      kSecAttrAccount as String: key,
    ]
  }

  private func upsert(key: String, data: Data) -> OSStatus {
    let updated = SecItemUpdate(
      baseQuery(key: key) as CFDictionary,
      [kSecValueData as String: data] as CFDictionary
    )
    if updated != errSecItemNotFound {
      return updated
    }
    var insert = baseQuery(key: key)
    insert[kSecValueData as String] = data
    insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    return SecItemAdd(insert as CFDictionary, nil)
  }
}

private enum CardVaultError: Error, LocalizedError {
  case keychain(OSStatus)
  case notUtf8

  var errorDescription: String? {
    switch self {
    case .keychain(let status):
      let explanation = SecCopyErrorMessageString(status, nil) as String? ?? "unknown"
      return "Keychain error \(status): \(explanation)"
    case .notUtf8:
      return "value is not representable as UTF-8"
    }
  }
}
