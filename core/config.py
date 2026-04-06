"""
JARVIS Core — Configuration Management

Manages all non-sensitive configuration for JARVIS. Secrets (API keys, OAuth
tokens) live in core/secrets.py — this file handles everything else.

Storage: data/jarvis_config.json (plaintext, safe to inspect and commit-ignore)

Config is organised into sections:
  - user        Personal preferences (name, honorific, timezone)
  - voice       TTS voice selection, speech recognition language
  - llm         Active provider, selected model per provider, parameters
  - ui          Theme, orb colours, status display preferences
  - integrations Feature flags for each integration (calendar, mail, etc.)
  - system      Internal settings (log level, cache TTLs, port)

All values have typed defaults. Reading a missing key always returns the
default rather than raising — JARVIS should degrade gracefully if config
is partially missing or corrupted.

LLM model handling:
  The `llm` section stores the SELECTED model per provider. The available
  models are fetched at runtime by core/models_registry.py and are NOT
  persisted here (they change as providers update their offerings).

  Structure:
    config["llm"]["provider"]               = "gemini"          # active provider
    config["llm"]["providers"]["gemini"]["model"]  = "gemini-2.5-flash-lite"
    config["llm"]["providers"]["gemini"]["params"]["max_tokens"] = 600
    config["llm"]["providers"]["openai"]["model"]  = "gpt-4o-mini"
    ...

  JARVIS reads the active provider + model in one call:
    provider, model, params = config_manager.active_llm()
"""

from __future__ import annotations

import json
import logging
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("jarvis.config")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).parent.parent
_DATA_DIR = _ROOT / "data"
_CONFIG_FILE = _DATA_DIR / "jarvis_config.json"

# ---------------------------------------------------------------------------
# Default configuration
# All keys must have a default. Nested dicts are supported.
# ---------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "user": {
        "name": "sir",                    # How JARVIS addresses the user in voice
        "honorific": "sir",               # sir | ma'am | none
        "timezone": "UTC",                # IANA timezone string
        "locale": "en-GB",               # Locale for date/number formatting
    },

    "voice": {
        "tts_provider": "edge_tts",       # edge_tts | fish_audio (future)
        "tts_voice": "en-GB-RyanNeural",  # edge-tts voice identifier
        "stt_language": "en-US",          # Web Speech API language tag
        "speech_rate": 1.0,               # Playback speed multiplier (future)
    },

    "llm": {
        "provider": "gemini",             # Active provider key
        "providers": {
            "gemini": {
                "model": "gemma-4-31b-it",
                "model_research": "gemma-4-31b-it",   # used for deep research
                "model_fast": "gemma-4-31b-it",       # used for intent/summary
                "params": {
                    "max_tokens": 600,
                    "thinking_budget": 0,         # 0 = off, -1 = dynamic
                    "research_max_tokens": 3000,
                    "research_thinking_budget": -1,
                },
            },
            "anthropic": {
                "model": "claude-haiku-4-5-20251001",
                "model_research": "claude-opus-4-6",
                "model_fast": "claude-haiku-4-5-20251001",
                "params": {
                    "max_tokens": 600,
                    "research_max_tokens": 3000,
                },
            },
            "openai": {
                "model": "gpt-4o-mini",
                "model_research": "gpt-4o",
                "model_fast": "gpt-4o-mini",
                "params": {
                    "max_tokens": 600,
                    "research_max_tokens": 3000,
                },
            },
        },
        "fallback_to_env": True,          # If True, read model names from env as last resort
    },

    "ui": {
        "orb_color_idle": "#4ca8e8",
        "orb_color_listening": "#4ca8e8",
        "orb_color_thinking": "#6ec4ff",
        "orb_color_speaking": "#5ab8f0",
        "status_label_idle": "",
        "status_label_listening": "listening...",
        "status_label_thinking": "thinking...",
        "status_label_speaking": "",
        "show_jarvis_label": True,
        "show_status_text": True,
    },

    "integrations": {
        "google": {
            "enabled": False,             # Master switch — requires OAuth setup
            "calendar_enabled": False,     # Create + read calendar events
            "mail_enabled": False,         # Read + label mail
            "tasks_enabled": False,        # Create + read Google Tasks
            "calendar_refresh_interval": 300,   # seconds between cache refreshes
            "mail_refresh_interval": 120,
        },
        "browser_automation": {
            "enabled": True,
            "headless": False,
        },
        "screen_awareness": {
            "enabled": True,
            "refresh_interval": 30,       # seconds
        },
        "memory": {
            "enabled": True,
            "auto_extract": True,         # Extract memories from conversations
        },
    },

    "system": {
        "port": 8340,
        "host": "0.0.0.0",
        "ssl": True,                      # Use HTTPS / WSS
        "log_level": "INFO",
        "projects_dir": "",               # Overrides ~/Desktop if set
        "agent_cli": "gemini",            # Which CLI agent to use for builds
        "max_concurrent_tasks": 3,
        "context_refresh_interval": 30,   # seconds for background context thread
        "weather_lat": None,              # User location for weather
        "weather_lon": None,
    },
}


# ---------------------------------------------------------------------------
# Config Manager
# ---------------------------------------------------------------------------

class ConfigManager:
    """
    Thread-safe configuration manager for JARVIS.

    Reads from data/jarvis_config.json, falls back to DEFAULTS for any
    missing key. Writes are atomic (write to temp file, then rename).

    Usage:
        cfg = ConfigManager()
        name = cfg.get("user.name")              # dot-path notation
        cfg.set("user.name", "Tony")
        provider, model, params = cfg.active_llm()
    """

    def __init__(self):
        self._data: dict[str, Any] = {}
        self._load()

    # -- Load / Save ----------------------------------------------------------

    def _load(self):
        """Load config from disk, merging with defaults for any missing keys."""
        _DATA_DIR.mkdir(parents=True, exist_ok=True)

        if _CONFIG_FILE.exists():
            try:
                raw = _CONFIG_FILE.read_text(encoding="utf-8")
                loaded = json.loads(raw)
                self._data = _deep_merge(deepcopy(DEFAULTS), loaded)
                log.debug(f"Config loaded from {_CONFIG_FILE}")
                return
            except Exception as e:
                log.warning(f"Could not read config file: {e} — using defaults")

        # First run or corrupt file — start from defaults
        self._data = deepcopy(DEFAULTS)
        self._save()
        log.info("Created default config file")

    def _save(self):
        """Atomically write config to disk."""
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(_CONFIG_FILE)
            log.debug("Config saved")
        except Exception as e:
            log.error(f"Failed to save config: {e}")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def reload(self):
        """Re-read config from disk (picks up external edits)."""
        self._load()

    # -- Read -----------------------------------------------------------------

    def get(self, path: str, default: Any = None) -> Any:
        """
        Read a config value using dot-path notation.

        Examples:
            cfg.get("user.name")                    → "sir"
            cfg.get("llm.provider")                 → "gemini"
            cfg.get("llm.providers.gemini.model")   → "gemini-2.5-flash-lite"
            cfg.get("does.not.exist", "fallback")   → "fallback"
        """
        parts = path.split(".")
        node: Any = self._data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                # Try defaults before returning the passed-in default
                default_val = _get_nested(DEFAULTS, parts)
                return default_val if default_val is not None else default
            node = node[part]
        return node

    def get_section(self, section: str) -> dict[str, Any]:
        """Return an entire top-level section as a dict (copy)."""
        return deepcopy(self._data.get(section, DEFAULTS.get(section, {})))

    def all(self) -> dict[str, Any]:
        """Return a full copy of the current config."""
        return deepcopy(self._data)

    # -- Write ----------------------------------------------------------------

    def set(self, path: str, value: Any) -> None:
        """
        Write a value using dot-path notation and persist to disk.

        Examples:
            cfg.set("user.name", "Tony")
            cfg.set("llm.provider", "openai")
            cfg.set("llm.providers.openai.model", "gpt-4o")
        """
        parts = path.split(".")
        node = self._data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        self._save()
        log.debug(f"Config set: {path} = {value!r}")

    def set_many(self, updates: dict[str, Any]) -> None:
        """Set multiple dot-path values in a single save operation."""
        for path, value in updates.items():
            parts = path.split(".")
            node = self._data
            for part in parts[:-1]:
                if part not in node or not isinstance(node[part], dict):
                    node[part] = {}
                node = node[part]
            node[parts[-1]] = value
        self._save()
        log.debug(f"Config batch update: {list(updates.keys())}")

    def reset_section(self, section: str) -> None:
        """Reset a top-level section to its defaults and save."""
        if section in DEFAULTS:
            self._data[section] = deepcopy(DEFAULTS[section])
            self._save()
            log.info(f"Config section '{section}' reset to defaults")

    def reset_all(self) -> None:
        """Reset entire config to defaults (nuclear option)."""
        self._data = deepcopy(DEFAULTS)
        self._save()
        log.info("Config fully reset to defaults")

    # -- LLM helpers ----------------------------------------------------------

    def active_llm(self) -> tuple[str, str, dict[str, Any]]:
        """
        Return (provider, model, params) for the currently active LLM.

        This is the single call point in server.py — nothing should
        hardcode provider or model names anywhere else.

        Example return:
            ("gemini", "gemini-2.5-flash-lite", {"max_tokens": 600, ...})
        """
        provider = self.get("llm.provider", "gemini")
        model = self.get(f"llm.providers.{provider}.model", "gemini-2.5-flash-lite")
        params = self.get(f"llm.providers.{provider}.params", {})
        return provider, model, dict(params)

    def llm_model(self, role: str = "default") -> str:
        """
        Return the model name for a specific role within the active provider.

        Roles:
            "default"  — standard voice loop model
            "fast"     — intent classification, summaries, quick calls
            "research" — deep research, planning, complex tasks

        Falls back to the default model if the role-specific one isn't set.
        """
        provider = self.get("llm.provider", "gemini")
        key_map = {
            "default": "model",
            "fast": "model_fast",
            "research": "model_research",
        }
        key = key_map.get(role, "model")
        model = self.get(f"llm.providers.{provider}.{key}")
        if not model:
            model = self.get(f"llm.providers.{provider}.model", "gemini-2.5-flash-lite")
        return model

    def set_model(self, provider: str, model: str, role: str = "default") -> None:
        """
        Update the selected model for a provider + role.

        Called from the settings UI after the user picks a model from the
        dropdown populated by models_registry.py.
        """
        key_map = {
            "default": "model",
            "fast": "model_fast",
            "research": "model_research",
        }
        key = key_map.get(role, "model")
        self.set(f"llm.providers.{provider}.{key}", model)
        log.info(f"Model updated: {provider}/{role} → {model}")

    # -- Provider helpers -----------------------------------------------------

    def active_provider(self) -> str:
        return self.get("llm.provider", "gemini")

    def set_provider(self, provider: str) -> None:
        """Switch the active LLM provider."""
        self.set("llm.provider", provider)
        log.info(f"Active LLM provider set to: {provider}")

    def configured_providers(self) -> list[str]:
        """Return list of provider keys that have config entries."""
        providers = self.get("llm.providers", {})
        return list(providers.keys()) if isinstance(providers, dict) else []

    # -- User helpers ---------------------------------------------------------

    def user_name(self) -> str:
        return self.get("user.name", "sir")

    def honorific(self) -> str:
        return self.get("user.honorific", "sir")

    def tts_voice(self) -> str:
        return self.get("voice.tts_voice", "en-GB-RyanNeural")

    # -- Integration flags ----------------------------------------------------

    def integration_enabled(self, name: str) -> bool:
        """Check if an integration is enabled. Example: cfg.integration_enabled('google')"""
        return bool(self.get(f"integrations.{name}.enabled", False))

    # -- System helpers -------------------------------------------------------

    def port(self) -> int:
        return int(self.get("system.port", 8340))

    def projects_dir(self) -> Optional[Path]:
        """Return configured projects directory, or None to use ~/Desktop."""
        d = self.get("system.projects_dir", "").strip()
        if d:
            return Path(d)
        env_override = os.getenv("PROJECTS_DIR", "").strip()
        if env_override:
            return Path(env_override)
        return None

    def log_level(self) -> str:
        return self.get("system.log_level", "INFO").upper()

    # -- Settings panel serialisation -----------------------------------------

    def for_settings_panel(self) -> dict[str, Any]:
        """
        Return a flattened, sanitised view of config for the settings UI.

        Excludes internal/advanced fields that shouldn't be user-editable
        through the basic settings panel.
        """
        return {
            "user": self.get_section("user"),
            "voice": self.get_section("voice"),
            "llm": {
                "provider": self.active_provider(),
                "providers": {
                    p: {
                        "model": self.get(f"llm.providers.{p}.model"),
                        "model_fast": self.get(f"llm.providers.{p}.model_fast"),
                        "model_research": self.get(f"llm.providers.{p}.model_research"),
                    }
                    for p in self.configured_providers()
                },
            },
            "integrations": self.get_section("integrations"),
            "system": {
                "port": self.port(),
                "ssl": self.get("system.ssl", True),
                "log_level": self.log_level(),
                "projects_dir": str(self.projects_dir() or ""),
                "agent_cli": self.get("system.agent_cli", "gemini"),
            },
        }

    def update_from_settings_panel(self, data: dict[str, Any]) -> None:
        """
        Apply a settings panel payload to config.

        Only whitelisted keys are accepted — the UI cannot write arbitrary
        paths into the config. Provider models are handled via set_model().
        """
        updates: dict[str, Any] = {}

        allowed_user = {"name", "honorific", "timezone", "locale"}
        allowed_voice = {"tts_provider", "tts_voice", "stt_language"}
        allowed_system = {"port", "ssl", "log_level", "projects_dir", "agent_cli"}

        for key in allowed_user:
            if "user" in data and key in data["user"]:
                updates[f"user.{key}"] = data["user"][key]

        for key in allowed_voice:
            if "voice" in data and key in data["voice"]:
                updates[f"voice.{key}"] = data["voice"][key]

        for key in allowed_system:
            if "system" in data and key in data["system"]:
                updates[f"system.{key}"] = data["system"][key]

        # LLM provider switch
        if "llm" in data and "provider" in data["llm"]:
            updates["llm.provider"] = data["llm"]["provider"]

        # Integration toggles
        if "integrations" in data:
            for integration, values in data["integrations"].items():
                if isinstance(values, dict) and "enabled" in values:
                    updates[f"integrations.{integration}.enabled"] = values["enabled"]

        if updates:
            self.set_many(updates)
            log.info(f"Settings panel update applied: {list(updates.keys())}")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """
    Recursively merge override into base. Returns base modified in place.
    Existing keys in base that are not in override are preserved.
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _get_nested(d: dict, parts: list[str]) -> Any:
    """Traverse a nested dict using a list of keys. Returns None if any key is missing."""
    node: Any = d
    for part in parts:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: Optional[ConfigManager] = None


def get_config() -> ConfigManager:
    """Return the module-level ConfigManager singleton."""
    global _instance
    if _instance is None:
        _instance = ConfigManager()
    return _instance


# ---------------------------------------------------------------------------
# Convenience shortcuts (mirrors get_secret pattern from secrets.py)
# ---------------------------------------------------------------------------

def cfg(path: str, default: Any = None) -> Any:
    """Read a single config value. Shortcut for get_config().get(path)."""
    return get_config().get(path, default)


def active_llm() -> tuple[str, str, dict[str, Any]]:
    """Shortcut for get_config().active_llm()."""
    return get_config().active_llm()


def llm_model(role: str = "default") -> str:
    """Shortcut for get_config().llm_model(role)."""
    return get_config().llm_model(role)


__all__ = [
    "ConfigManager",
    "get_config",
    "cfg",
    "active_llm",
    "llm_model",
    "DEFAULTS",
]
