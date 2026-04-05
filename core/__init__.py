"""
JARVIS Core — Foundation modules.

Import from here for convenience:
    from core import get_secret, get_config, cfg
    from core.secrets import SecretsManager, KNOWN_SECRETS
    from core.config import ConfigManager, active_llm, llm_model
"""

from core.secrets import (
    get_secret,
    set_secret,
    get_secrets,
    secrets_health_report,
    store_google_credentials,
    store_google_token,
    get_google_credentials,
    KNOWN_SECRETS,
)

from core.config import (
    get_config,
    cfg,
    active_llm,
    llm_model,
    DEFAULTS,
)

__all__ = [
    # secrets
    "get_secret",
    "set_secret",
    "get_secrets",
    "secrets_health_report",
    "store_google_credentials",
    "store_google_token",
    "get_google_credentials",
    "KNOWN_SECRETS",
    # config
    "get_config",
    "cfg",
    "active_llm",
    "llm_model",
    "DEFAULTS",
]
