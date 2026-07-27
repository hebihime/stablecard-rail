package expo.modules.cardvault

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Secure storage on Android, backed by the hardware-backed Keystore (SPEC.md §9).
 *
 * Android has no Keychain equivalent that stores arbitrary values, so this is the
 * two-part shape the platform actually offers: an AES-256-GCM key that lives inside
 * the Keystore and never leaves it, and ciphertext in ordinary SharedPreferences.
 * An attacker with the preferences file has ciphertext and no key; the key is not
 * extractable even by this app, which is the point of generating it there.
 *
 * `androidx.security:security-crypto` (EncryptedSharedPreferences) would be fewer
 * lines and is the usual answer. It is deliberately not used: the library has been
 * unmaintained since 1.1.0-alpha06, and the whole reason this module exists is that
 * SPEC.md §9 asks for native work that is demonstrably ours. Talking to the Keystore
 * directly is about forty lines and is the thing that library does.
 *
 * Three details that are easy to get wrong and silent when wrong:
 *
 * - **The IV must be the one the cipher chose.** GCM derives it at `init` time, and
 *   supplying your own — or reusing one across two encryptions under the same key —
 *   breaks GCM completely. So it is read back off the cipher and stored beside the
 *   ciphertext rather than being generated here.
 * - **`setRandomizedEncryptionRequired` stays on** (its default), which is what
 *   forbids passing an IV in on encrypt. That is a feature, and this code is built
 *   around it rather than turning it off to make the API tidier.
 * - **A key can vanish.** A device that changes its lock screen, or a restore onto
 *   new hardware, can leave the Keystore entry unusable while the ciphertext stays
 *   behind. Decryption then throws, and the honest answer is `null` — "there is
 *   nothing readable here" — rather than an error a caller cannot act on.
 */
class CardVaultModule : Module() {
  override fun definition() = ModuleDefinition {
    Name("CardVault")

    Function("describe") {
      mapOf("backend" to "keystore", "protection" to "device-keystore")
    }

    AsyncFunction("setItem") { key: String, value: String ->
      val cipher = Cipher.getInstance(TRANSFORMATION).apply {
        init(Cipher.ENCRYPT_MODE, secretKey())
      }
      val ciphertext = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
      // The IV is whatever the cipher chose. Storing it beside the ciphertext is
      // correct and not a leak: GCM's IV is public, it must simply never repeat.
      val packed = cipher.iv + ciphertext
      preferences()
        .edit()
        .putString(key, Base64.encodeToString(packed, Base64.NO_WRAP))
        .apply()
    }

    AsyncFunction("getItem") { key: String ->
      val stored = preferences().getString(key, null) ?: return@AsyncFunction null
      val packed = Base64.decode(stored, Base64.NO_WRAP)
      if (packed.size <= GCM_IV_BYTES) {
        throw CardVaultException("stored value for '$key' is too short to be valid")
      }
      try {
        val cipher = Cipher.getInstance(TRANSFORMATION).apply {
          init(
            Cipher.DECRYPT_MODE,
            secretKey(),
            GCMParameterSpec(GCM_TAG_BITS, packed, 0, GCM_IV_BYTES),
          )
        }
        String(
          cipher.doFinal(packed, GCM_IV_BYTES, packed.size - GCM_IV_BYTES),
          Charsets.UTF_8,
        )
      } catch (invalidated: java.security.GeneralSecurityException) {
        // The key is gone or no longer usable — a lock-screen change, a restore
        // onto new hardware. The ciphertext left behind can never be read, so the
        // truthful answer is that there is nothing here. Clear it rather than
        // failing this call and every future one identically.
        preferences().edit().remove(key).apply()
        null
      }
    }

    AsyncFunction("deleteItem") { key: String ->
      // Deleting something absent is a no-op: callers delete on sign-out and on
      // token expiry, and neither knows what is actually stored.
      preferences().edit().remove(key).apply()
    }
  }

  private val context: Context
    get() = requireNotNull(appContext.reactContext) { "no Android context available" }

  private fun preferences() =
    context.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)

  /**
   * The Keystore entry, created on first use.
   *
   * `getKey` returning null is the normal first-run path, not an error.
   */
  private fun secretKey(): SecretKey {
    val keyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
    (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }

    val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
    generator.init(
      KeyGenParameterSpec.Builder(
        KEY_ALIAS,
        KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
      )
        .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
        .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
        .setKeySize(AES_KEY_BITS)
        // Left at its default of `true`, which is what forbids supplying an IV on
        // encrypt — the property this whole implementation is built around.
        .setRandomizedEncryptionRequired(true)
        // Deliberately NOT `setUserAuthenticationRequired(true)`. It would be
        // stronger and would break the app the same way `kSecAttrAccessibleWhenUnlocked`
        // does on iOS: a background poll for a 3DS challenge cannot prompt for a
        // fingerprint, and the read it needs would fail exactly when it matters.
        .build()
    )
    return generator.generateKey()
  }

  private companion object {
    const val ANDROID_KEYSTORE = "AndroidKeyStore"
    const val KEY_ALIAS = "test.stablecard.rail.card-vault"
    const val PREFERENCES_NAME = "card-vault"
    const val TRANSFORMATION = "AES/GCM/NoPadding"
    const val AES_KEY_BITS = 256
    const val GCM_IV_BYTES = 12
    const val GCM_TAG_BITS = 128
  }
}

private class CardVaultException(message: String) : CodedException(message)
