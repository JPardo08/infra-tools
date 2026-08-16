"""External secret-file loading for SNMPv3 authPriv material."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapter import SnmpV3Credentials


class SecretError(ValueError):
    """Raised when a secret file is missing or fails closed checks."""


REQUIRED_SECRET_MODE = 0o600


@dataclass(frozen=True)
class LoadedSecret:
    credentials: SnmpV3Credentials
    path: Path

    def redaction_values(self) -> tuple[str, ...]:
        values = (
            self.credentials.username,
            self.credentials.auth_passphrase,
            self.credentials.privacy_passphrase,
        )
        return tuple(value for value in values if value)


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise SecretError(f"{label} must be a non-empty string")
    return value


def load_snmpv3_secret(path: Path) -> LoadedSecret:
    """Load a tool-private SNMPv3 secret file.

    Guarantees minimization via permissions and validation only.
    Does not claim Python memory zeroization.
    """
    try:
        st = path.lstat()
    except OSError as exc:
        raise SecretError(
            f"Secret file not found: {path}"
        ) from exc

    if stat.S_ISLNK(st.st_mode):
        raise SecretError(
            "Secret path must be a regular file, not a symlink"
        )

    if not stat.S_ISREG(st.st_mode):
        raise SecretError(
            "Secret path must be a regular file"
        )

    mode = stat.S_IMODE(st.st_mode)

    if mode != REQUIRED_SECRET_MODE:
        raise SecretError(
            f"Secret file mode must be 0600, found {mode:04o}"
        )

    if st.st_uid != os.geteuid():
        raise SecretError(
            "Secret file owner must match the effective user"
        )

    try:
        raw = path.read_text()
    except OSError as exc:
        raise SecretError(
            f"Unable to read secret file: {path}"
        ) from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SecretError(
            "Secret file is not valid JSON"
        ) from exc

    if not isinstance(document, dict):
        raise SecretError(
            "Secret file root must be a JSON object"
        )

    username = _require_string(
        document.get("username"),
        "secret.username",
    )
    auth_passphrase = _require_string(
        document.get("authPassphrase"),
        "secret.authPassphrase",
    )
    privacy_passphrase = _require_string(
        document.get("privacyPassphrase"),
        "secret.privacyPassphrase",
    )

    credentials = SnmpV3Credentials(
        username=username,
        auth_passphrase=auth_passphrase,
        privacy_passphrase=privacy_passphrase,
    )

    return LoadedSecret(credentials=credentials, path=path)
