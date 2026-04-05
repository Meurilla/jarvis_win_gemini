"""
JARVIS Core — Secret Management

Single gate for all sensitive values in JARVIS. Nothing else in the codebase
should call os.getenv() for secrets or read .env directly for sensitive keys.

Storage hierarchy (tried in order on read, first non-empty value wins):
  1. OS Credential Store via keyring (Windows Credential Manager / macOS Keychain / libsecret)
  2. Fernet-encrypted file at data/jarvis_secrets.enc  (fallback if keyring unavailable)
  3. Environment variable / .env file                   (migration + CI fallback only)

On first run the user enters keys through the settings UI. JARVIS stores them
in the credential store. The .env file is used only to migrate existing keys
into the secure store and is then blanked — values are never read from it again
once migrated.

Encryption key derivation (Fernet fallback):
  - Salt: stored alongside the encrypted file (data/jarvis_secrets.salt)
  - Input: machine-specific hardware ID + optional user passphrase
  - KDF: PBKDF2-HMAC-SHA256, 480 000 iterations (OWASP 2023 recommendation)

Windows-compatible: no platform-specific calls beyond what keyring handles.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import platform
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("jarvis.secrets")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent          # project root
_DATA_DIR = _ROOT / "data"
_ENC_FILE = _DATA_DIR / "jarvis_secrets.enc"
_SALT_FILE = _DATA_DIR / "jarvis_secrets.salt"
_ENV_FILE = _ROOT / ".env"

# ---------------------------------------------------------------------------
# Keyring service name — all JARVIS secrets live under this namespace
# ---------------------------------------------------------------------------

_SERVICE = "jarvis-ai"

# ---------------------------------------------------------------------------
# Known secret keys — the canonical list of every secret JARVIS manages.
# Adding a key here makes it available everywhere without touching any other file.
# ---------------------------------------------------------------------------

KNOWN_SECRETS: dict[str, str] = {
    # AI providers
    "GEMINI_API_KEY": "Gemini API Key",
    "ANTHROPIC_API_KEY": "Anthropic API Key",
    "OPENAI_API_KEY": "OpenAI API Key",

    # TTS
    # "FISH_API_KEY": "Fish Audio API Key",
    # "FISH_VOICE_ID": "Fish Audio Voice ID",

    # Google OAuth (entered manually from the popup, never from a JSON file)
    "GOOGLE_CLIENT_ID": "Google OAuth Client ID",
    "GOOGLE_CLIENT_SECRET": "Google OAuth Client Secret",
    "GOOGLE_REFRESH_TOKEN": "Google OAuth Refresh Token",  # written by JARVIS after auth
}


# ---------------------------------------------------------------------------
# Optional dependency guards
# ---------------------------------------------------------------------------

def _keyring_available() -> bool:
    try:
        import keyring  # noqa: F401
        return True
    except ImportError:
        return False


def _cryptography_available() -> bool:
    try:
        from cryptography.fernet import Fernet  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Machine ID (used as one ingredient for Fernet key derivation)
# ---------------------------------------------------------------------------

def _machine_id() -> str:
    """Return a stable machine-specific string.

    Uses UUID from MAC address on all platforms. Not secret on its own —
    it's combined with a random salt and optionally a passphrase.
    """
    try:
        return str(uuid.UUID(int=uuid.getnode()))
    except Exception:
        return platform.node() or "jarvis-fallback-machine-id"


# ---------------------------------------------------------------------------
# Fernet key derivation + encryption helpers
# ---------------------------------------------------------------------------

def _load_or_create_salt() -> bytes:
    """Load existing salt or create a new random one."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _SALT_FILE.exists():
        return _SALT_FILE.read_bytes()
    salt = os.urandom(32)
    _SALT_FILE.write_bytes(salt)
    log.debug("Created new Fernet salt")
    return salt


def _derive_fernet_key(passphrase: str = "") -> bytes:
    """Derive a 32-byte Fernet-compatible key from machine ID + passphrase."""
    salt = _load_or_create_salt()
    material = (_machine_id() + passphrase).encode("utf-8")
    dk = hashlib.pbkdf2_hmac("sha256", material, salt, iterations=480_000)
    return base64.urlsafe_b64encode(dk)


def _fernet(passphrase: str = ""):
    """Return a Fernet instance. Raises if cryptography is not installed."""
    from cryptography.fernet import Fernet
    return Fernet(_derive_fernet_key(passphrase))


def _load_enc_store(passphrase: str = "") -> dict[str, str]:
    """Decrypt and deserialise the encrypted secrets file."""
    if not _ENC_FILE.exists():
        return {}
    try:
        raw = _fernet(passphrase).decrypt(_ENC_FILE.read_bytes())
        import json
        return json.loads(raw.decode("utf-8"))
    except Exception as e:
        log.warning(f"Could not decrypt secrets file: {e}")
        return {}


def _save_enc_store(store: dict[str, str], passphrase: str = "") -> None:
    """Serialise and encrypt the secrets store to disk."""
    import json
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(store).encode("utf-8")
    _ENC_FILE.write_bytes(_fernet(passphrase).encrypt(raw))
    log.debug(f"Encrypted secrets store updated ({len(store)} keys)")


# ---------------------------------------------------------------------------
# Core read / write
# ---------------------------------------------------------------------------

class SecretsManager:
    """
    Single interface for reading and writing JARVIS secrets.

    Usage:
        secrets = SecretsManager()
        key = secrets.get("GEMINI_API_KEY")
        secrets.set("GEMINI_API_KEY", "AIz-...")
        secrets.delete("GEMINI_API_KEY")

    The passphrase is optional. If provided it strengthens the Fernet fallback
    but is NOT required for the keyring path (keyring uses OS-level protection).
    """

    def __init__(self, passphrase: str = ""):
        self._passphrase = passphrase
        self._use_keyring = _keyring_available()
        self._use_fernet = _cryptography_available()

        if not self._use_keyring and not self._use_fernet:
            log.warning(
                "Neither keyring nor cryptography is installed. "
                "Secrets will fall back to environment variables only. "
                "Run: pip install keyring cryptography"
            )
        elif not self._use_keyring:
            log.info("keyring not available — using Fernet encrypted file fallback")
        else:
            log.info("Using OS credential store via keyring")

    # -- Public API -----------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        """Read a secret. Returns None if not found in any store."""
        # 1. Keyring (preferred)
        if self._use_keyring:
            try:
                import keyring
                value = keyring.get_password(_SERVICE, key)
                if value:
                    return value
            except Exception as e:
                log.debug(f"keyring read failed for {key}: {e}")

        # 2. Fernet encrypted file
        if self._use_fernet:
            try:
                store = _load_enc_store(self._passphrase)
                value = store.get(key)
                if value:
                    return value
            except Exception as e:
                log.debug(f"Fernet read failed for {key}: {e}")

        # 3. Environment variable / .env fallback (migration + CI only)
        value = os.getenv(key, "").strip()
        if value:
            log.debug(f"Secret {key} read from environment (consider migrating to secure store)")
            return value

        return None

    def set(self, key: str, value: str) -> bool:
        """Write a secret to the best available store. Returns True on success."""
        if not value or not value.strip():
            log.warning(f"Attempted to store empty value for {key}")
            return False

        # 1. Keyring (preferred)
        if self._use_keyring:
            try:
                import keyring
                keyring.set_password(_SERVICE, key, value)
                log.info(f"Stored {key} in OS credential store")
                # Also remove from Fernet store if it was there before
                self._remove_from_fernet(key)
                return True
            except Exception as e:
                log.warning(f"keyring write failed for {key}: {e} — falling back to Fernet")

        # 2. Fernet encrypted file
        if self._use_fernet:
            try:
                store = _load_enc_store(self._passphrase)
                store[key] = value
                _save_enc_store(store, self._passphrase)
                log.info(f"Stored {key} in encrypted file")
                return True
            except Exception as e:
                log.error(f"Fernet write failed for {key}: {e}")

        log.error(f"Could not store {key} — no secure store available")
        return False

    def delete(self, key: str) -> bool:
        """Remove a secret from all stores. Returns True if found anywhere."""
        found = False

        if self._use_keyring:
            try:
                import keyring
                keyring.delete_password(_SERVICE, key)
                found = True
                log.info(f"Deleted {key} from OS credential store")
            except Exception:
                pass  # Not in keyring — that's fine

        found = self._remove_from_fernet(key) or found
        return found

    def exists(self, key: str) -> bool:
        """Check if a secret is stored (without returning its value)."""
        return self.get(key) is not None

    def status(self) -> dict[str, dict]:
        """
        Return status of all known secrets — whether stored and where.

        Used by the settings panel to show which keys are configured.
        Returns: {key: {"label": str, "stored": bool, "store": str}}
        """
        result = {}
        for key, label in KNOWN_SECRETS.items():
            store_name = "none"
            stored = False

            if self._use_keyring:
                try:
                    import keyring
                    if keyring.get_password(_SERVICE, key):
                        stored = True
                        store_name = "credential_store"
                except Exception:
                    pass

            if not stored and self._use_fernet:
                try:
                    enc_store = _load_enc_store(self._passphrase)
                    if key in enc_store:
                        stored = True
                        store_name = "encrypted_file"
                except Exception:
                    pass

            if not stored and os.getenv(key, "").strip():
                stored = True
                store_name = "environment"

            result[key] = {
                "label": label,
                "stored": stored,
                "store": store_name,
            }
        return result

    def migrate_from_env(self) -> dict[str, bool]:
        """
        One-time migration: read secrets from .env and move them to the
        secure store. After migration, blank the values in .env so they
        are never read again in production.

        Returns: {key: True if migrated, False if not found in .env}
        """
        results = {}
        env_values: dict[str, str] = {}

        # Parse .env file
        if _ENV_FILE.exists():
            for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if v:
                        env_values[k] = v

        for key in KNOWN_SECRETS:
            value = env_values.get(key, "").strip()
            if not value:
                results[key] = False
                continue
            success = self.set(key, value)
            results[key] = success
            if success:
                log.info(f"Migrated {key} from .env to secure store")

        # Blank migrated values in .env (preserve comments and structure)
        if any(results.values()) and _ENV_FILE.exists():
            lines = _ENV_FILE.read_text(encoding="utf-8").splitlines()
            new_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, _, _ = stripped.partition("=")
                    if k.strip() in KNOWN_SECRETS and results.get(k.strip()):
                        new_lines.append(f"# {k.strip()}=  # migrated to secure store")
                        continue
                new_lines.append(line)
            _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            log.info(".env values blanked after migration")

        return results

    # -- Private helpers ------------------------------------------------------

    def _remove_from_fernet(self, key: str) -> bool:
        """Remove a key from the Fernet encrypted store if present."""
        if not self._use_fernet:
            return False
        try:
            store = _load_enc_store(self._passphrase)
            if key in store:
                del store[key]
                _save_enc_store(store, self._passphrase)
                return True
        except Exception as e:
            log.debug(f"Fernet removal failed for {key}: {e}")
        return False


# ---------------------------------------------------------------------------
# Module-level singleton — import and use directly
# ---------------------------------------------------------------------------

_instance: Optional[SecretsManager] = None


def get_secrets(passphrase: str = "") -> SecretsManager:
    """
    Return the module-level SecretsManager singleton.

    The passphrase only matters for the Fernet fallback path — if keyring
    is available the passphrase is ignored. Pass it once at startup if needed.
    """
    global _instance
    if _instance is None:
        _instance = SecretsManager(passphrase)
    return _instance


def get_secret(key: str) -> Optional[str]:
    """Convenience shortcut: get a single secret from the singleton."""
    return get_secrets().get(key)


def set_secret(key: str, value: str) -> bool:
    """Convenience shortcut: store a single secret via the singleton."""
    return get_secrets().set(key, value)


# ---------------------------------------------------------------------------
# Google OAuth helpers
# ---------------------------------------------------------------------------

def store_google_credentials(client_id: str, client_secret: str) -> bool:
    """
    Store Google OAuth client credentials entered manually by the user.

    The user should copy these from the Google Cloud Console OAuth popup
    before closing it. JARVIS never reads from a downloaded JSON file.

    Returns True if both values were stored successfully.
    """
    secrets = get_secrets()
    ok1 = secrets.set("GOOGLE_CLIENT_ID", client_id.strip())
    ok2 = secrets.set("GOOGLE_CLIENT_SECRET", client_secret.strip())
    if ok1 and ok2:
        log.info("Google OAuth credentials stored successfully")
    else:
        log.error("Failed to store one or both Google OAuth credentials")
    return ok1 and ok2


def store_google_token(refresh_token: str) -> bool:
    """
    Store the Google OAuth refresh token after the user completes the
    consent flow. Called internally by the Google auth module.
    """
    return get_secrets().set("GOOGLE_REFRESH_TOKEN", refresh_token.strip())


def get_google_credentials() -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Return (client_id, client_secret, refresh_token).
    Any value may be None if not yet stored.
    """
    secrets = get_secrets()
    return (
        secrets.get("GOOGLE_CLIENT_ID"),
        secrets.get("GOOGLE_CLIENT_SECRET"),
        secrets.get("GOOGLE_REFRESH_TOKEN"),
    )


# ---------------------------------------------------------------------------
# Diagnostic
# ---------------------------------------------------------------------------

def secrets_health_report() -> str:
    """Return a human-readable summary for logging / settings display."""
    secrets = get_secrets()
    status = secrets.status()
    lines = ["JARVIS Secrets Health Report", "=" * 40]

    backend = "OS Credential Store" if secrets._use_keyring else (
        "Fernet Encrypted File" if secrets._use_fernet else "Environment Variables ONLY (insecure)"
    )
    lines.append(f"Active backend: {backend}")
    lines.append("")

    for key, info in status.items():
        mark = "✓" if info["stored"] else "✗"
        store_label = {
            "credential_store": "keyring",
            "encrypted_file": "encrypted",
            "environment": "env (migrate!)",
            "none": "NOT SET",
        }.get(info["store"], info["store"])
        lines.append(f"  {mark} {info['label']:<30} [{store_label}]")

    return "\n".join(lines)


__all__ = [
    "SecretsManager",
    "get_secrets",
    "get_secret",
    "set_secret",
    "store_google_credentials",
    "store_google_token",
    "get_google_credentials",
    "secrets_health_report",
    "KNOWN_SECRETS",
]
