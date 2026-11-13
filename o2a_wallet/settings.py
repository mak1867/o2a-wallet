"""The user's wallet settings, in ``~/.o2a/config.toml`` (or the path in ``O2A_CONFIG``).

Values come from the file, or defaults for anything it doesn't set. The server reads them once at
startup; the CLI (``init``, ``config``) is the only thing that writes the file.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from o2a_protocol import StaticKeyResolver, Verifier

SAVING_MODES = ("auto", "relevant", "ask", "off")
NOTICE_MODES = ("every", "when_relevant", "off")
CLAIMING_MODES = ("with_purchase", "never")
DEFAULT_ISSUER = "o2a.exchange"
DEFAULT_PATH = "~/.o2a/config.toml"

_EMAIL_FORBIDDEN = re.compile(r"[\s\x00-\x1f\x7f-\x9f]")
_LOOKS_LIKE_EMAIL = "at most 254 characters, exactly one @, no whitespace"

_DEFAULTS: dict[str, object] = {
    "saving": "auto",
    "notices": "every",
    "claiming": "with_purchase",
    "issuers": (),
    "email": None,
}

# Section -> key -> setting name.
_FILE_KEYS: dict[str, dict[str, str]] = {
    "saving": {"mode": "saving", "notices": "notices"},
    "claiming": {"mode": "claiming"},
    "issuers": {"trusted": "issuers"},
    "buyer": {"email": "email"},
}


class SettingsError(Exception):
    """The settings are invalid. The server exits with this message on stderr."""


@dataclass(frozen=True)
class Settings:
    saving: str = "auto"
    notices: str = "every"
    claiming: str = "with_purchase"
    trusted_issuers: tuple[str, ...] = ()
    buyer_email: str | None = None
    # Where each value came from: "default", "file", or "unset".
    sources: Mapping[str, str] = field(default_factory=dict)


def config_path(env: Mapping[str, str]) -> Path:
    return Path(env.get("O2A_CONFIG") or DEFAULT_PATH).expanduser()


def check_choice(name: str, value: object, choices: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise SettingsError(f"{name} must be one of {', '.join(choices)}")
    return value


def check_issuers(name: str, issuers: Sequence[str]) -> tuple[str, ...]:
    try:
        Verifier(tuple(issuers), StaticKeyResolver({}))
    except ValueError as exc:
        raise SettingsError(f"{name}: {exc}") from None
    return tuple(issuers)


def check_email(name: str, email: str) -> str:
    if len(email) > 254 or email.count("@") != 1 or _EMAIL_FORBIDDEN.search(email):
        raise SettingsError(f"{name} is not a plausible email ({_LOOKS_LIKE_EMAIL})")
    return email


def split_issuers(text: str) -> list[str]:
    return [i.strip() for i in text.split(",") if i.strip()]


def read_file(path: Path) -> dict[str, object]:
    """The file's settings by name (saving, notices, claiming, issuers, email), validated.

    A missing file is empty. Unknown sections or keys are errors, so a typo never goes unnoticed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) else "not UTF-8"
        raise SettingsError(f"cannot read {path}: {reason}") from None
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(f"{path} is not valid TOML: {exc}") from None
    found: dict[str, object] = {}
    for section, table in document.items():
        keys = _FILE_KEYS.get(section)
        if keys is None or not isinstance(table, dict):
            raise SettingsError(f"{path}: unknown section [{section}]")
        for key, value in table.items():
            if key not in keys:
                raise SettingsError(f"{path}: unknown setting {section}.{key}")
            name, label = keys[key], f"{path}: {section}.{key}"
            if name == "saving":
                found[name] = check_choice(label, value, SAVING_MODES)
            elif name == "notices":
                found[name] = check_choice(label, value, NOTICE_MODES)
            elif name == "claiming":
                found[name] = check_choice(label, value, CLAIMING_MODES)
            elif name == "issuers":
                if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
                    raise SettingsError(f"{label} must be a list of issuer hostnames")
                found[name] = check_issuers(label, value)
            else:
                if not isinstance(value, str):
                    raise SettingsError(f"{label} must be a string")
                found[name] = check_email(label, value)
    return found


def load_settings(env: Mapping[str, str]) -> Settings:
    """The settings file's values, with defaults for anything it doesn't set. Raises
    ``SettingsError``. Trusted issuers may be empty; callers decide if that's fatal.

    ``env`` is consulted only for ``O2A_CONFIG``, the file's location.
    """
    file = read_file(config_path(env))
    sources = {name: "file" if name in file else "default" for name in _DEFAULTS}
    values = {name: file.get(name, default) for name, default in _DEFAULTS.items()}
    if not values["issuers"]:
        sources["issuers"] = "unset"
    if values["email"] is None:
        sources["email"] = "unset"
    return Settings(
        saving=str(values["saving"]),
        notices=str(values["notices"]),
        claiming=str(values["claiming"]),
        trusted_issuers=tuple(cast(Sequence[str], values["issuers"])),
        buyer_email=cast(str | None, values["email"]),
        sources=sources,
    )


# Writing -------------------------------------------------------------------------------------


def render_file(
    *,
    saving: str,
    notices: str,
    claiming: str,
    issuers: Sequence[str],
    email: str | None,
) -> str:
    # JSON string escapes are valid TOML basic-string escapes.
    trusted = ", ".join(json.dumps(i) for i in issuers)
    buyer = (
        f"[buyer]\nemail = {json.dumps(email)}  # sent with claims; only if an issuer requires it\n"
        if email
        else '# [buyer]\n# email = "you@example.com"  # optional; only if an issuer requires it\n'
    )
    return (
        "# O2A wallet settings, managed by `o2a-wallet config set`. Comments you add by hand\n"
        "# aren't kept. Changes take effect in the agent's next session. The wallet's tools let\n"
        "# the agent read these settings, never change them.\n"
        "\n"
        "[saving]\n"
        f"mode = {json.dumps(saving):<24} # auto | relevant | ask | off\n"
        f"notices = {json.dumps(notices):<21} # every | when_relevant | off\n"
        "\n"
        "[claiming]\n"
        f"mode = {json.dumps(claiming):<24} # with_purchase | never\n"
        "\n"
        "[issuers]\n"
        f"trusted = [{trusted}]\n"
        "\n" + buyer
    )


def write_file(path: Path, text: str) -> None:
    """Writes atomically, private (0600) from its first moment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
