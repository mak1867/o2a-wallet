"""The wallet's configuration: the user's settings file plus a few advanced environment variables.

The user's settings (saving, save notices, claiming, trusted issuers, buyer email) come only from
the settings file. The environment holds only where things live and how keys are found.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from o2a_protocol import parse_jwks, parse_strict_json
from o2a_protocol.strictjson import StrictJSONError

from . import __version__
from .settings import SettingsError, config_path, load_settings
from .texts import render_instructions

PinnedKeys = dict[str, dict[str, Ed25519PublicKey]]

NO_ISSUERS = "no trusted issuers are configured: run `o2a-wallet init`"


class ConfigError(Exception):
    """The configuration is invalid. The server exits with this message on stderr."""


@dataclass(frozen=True)
class Config:
    db_path: Path
    log_path: Path | None  # None: the activity log is off.
    trusted_issuers: tuple[str, ...]
    pinned_keys: PinnedKeys
    fetch_keys: bool
    buyer_email: str | None
    saving: str
    notices: str
    claiming: str
    max_active: int
    config_path: Path | None = None
    sources: Mapping[str, str] | None = None

    def instructions(self) -> str:
        """The server instructions the model receives."""
        return render_instructions(self.saving, self.notices, self.claiming)

    def log_settings(self) -> dict[str, object]:
        """The settings in force, written into every activity-log line. Never the email."""
        return {
            "version": __version__,
            "saving": self.saving,
            "notices": self.notices,
            "claiming": self.claiming,
        }


def load_config(env: Mapping[str, str], *, require_issuers: bool = True) -> Config:
    """Validate the settings file and the environment. Raises ``ConfigError``.

    ``require_issuers=False`` is for CLI commands that never verify an offer.
    """
    try:
        settings = load_settings(env)
    except SettingsError as exc:
        raise ConfigError(str(exc)) from None
    issuers = settings.trusted_issuers
    if require_issuers and not issuers:
        raise ConfigError(NO_ISSUERS)

    fetch = env.get("O2A_FETCH_KEYS", "1")
    if fetch not in {"0", "1"}:
        raise ConfigError("O2A_FETCH_KEYS must be 0 or 1")
    pinned_path = env.get("O2A_PINNED_KEYS") or None
    pinned = _load_pinned(Path(pinned_path).expanduser()) if pinned_path else {}
    if require_issuers and not pinned_path and fetch == "0":
        raise ConfigError("no key source: set O2A_PINNED_KEYS or O2A_FETCH_KEYS=1")

    raw_max = env.get("O2A_MAX_ACTIVE", "200")
    if not (raw_max.isascii() and raw_max.isdigit() and int(raw_max) > 0):
        raise ConfigError("O2A_MAX_ACTIVE must be a positive integer")

    raw_log = env.get("O2A_WALLET_LOG") or "~/.o2a/activity.jsonl"
    return Config(
        db_path=Path(env.get("O2A_WALLET_DB") or "~/.o2a/wallet.db").expanduser(),
        log_path=None if raw_log == "off" else Path(raw_log).expanduser(),
        trusted_issuers=issuers,
        pinned_keys=pinned,
        fetch_keys=fetch == "1",
        buyer_email=settings.buyer_email,
        saving=settings.saving,
        notices=settings.notices,
        claiming=settings.claiming,
        max_active=int(raw_max),
        config_path=config_path(env),
        sources=settings.sources,
    )


def _load_pinned(path: Path) -> PinnedKeys:
    try:
        document = parse_strict_json(path.read_bytes())
    except OSError as exc:
        raise ConfigError(f"O2A_PINNED_KEYS: cannot read {path}: {exc.strerror}") from None
    except StrictJSONError as exc:
        raise ConfigError(f"O2A_PINNED_KEYS: {path} is not valid JSON: {exc.detail}") from None
    if not isinstance(document, dict):
        raise ConfigError(f"O2A_PINNED_KEYS: {path} must be an object of issuer to JWKS")
    pinned: PinnedKeys = {}
    for iss, jwks in document.items():
        if not isinstance(jwks, dict):
            raise ConfigError(f"O2A_PINNED_KEYS: the entry for {iss} must be a JWKS object")
        try:
            pinned[iss] = parse_jwks(jwks)
        except ValueError as exc:
            raise ConfigError(f"O2A_PINNED_KEYS: the entry for {iss}: {exc}") from None
    return pinned
