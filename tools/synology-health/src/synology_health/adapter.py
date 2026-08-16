"""Controlled Net-SNMP adapter for storage.health.

This module owns the external authority of the Synology health tool:
- one configured target;
- SNMPv3 authPriv credentials;
- fixed OID allowlist;
- no arbitrary host, OID, or shell execution.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import oids
from .models import StorageHealth
from .parser import ParseError, parse_health_walk


SNMPGET = "/usr/bin/snmpget"
SNMPWALK = "/usr/bin/snmpwalk"

SNMP_TIMEOUT_SECONDS = 2
SNMP_RETRIES = 1
PROCESS_TIMEOUT_SECONDS = 8


class AdapterError(RuntimeError):
    """Base exception for the controlled Synology adapter."""


class UnknownTargetError(AdapterError):
    """Raised when a capability requests an unauthorized target."""


class SnmpTimeoutError(AdapterError):
    """Raised when the Net-SNMP process does not complete in time."""


class SnmpCommandError(AdapterError):
    """Raised when Net-SNMP returns a non-zero result."""


@dataclass(frozen=True)
class SynologyTarget:
    target_id: str
    host: str


@dataclass(frozen=True)
class SnmpV3Credentials:
    username: str = field(repr=False)
    auth_passphrase: str = field(repr=False)
    privacy_passphrase: str = field(repr=False)


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _quote_snmp_config(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _runtime_temp_parent() -> str | None:
    """Prefer a per-user runtime directory when one is available."""
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")

    if xdg_runtime and Path(xdg_runtime).is_dir():
        return xdg_runtime

    run_user = Path("/run/user") / str(os.getuid())

    if run_user.is_dir():
        return str(run_user)

    return None


def _write_snmp_config(
    directory: Path,
    credentials: SnmpV3Credentials,
) -> Path:
    """Create the ephemeral Net-SNMP config with restrictive permissions."""
    config = directory / "snmp.conf"

    content = "\n".join(
        [
            "defVersion 3",
            "defSecurityLevel authPriv",
            "defAuthType SHA",
            "defPrivType AES",
            "defSecurityName "
            + _quote_snmp_config(credentials.username),
            "defAuthPassphrase "
            + _quote_snmp_config(credentials.auth_passphrase),
            "defPrivPassphrase "
            + _quote_snmp_config(credentials.privacy_passphrase),
        ]
    ) + "\n"

    config.write_text(content)
    config.chmod(0o600)

    return config


def _base_command(executable: str, host: str) -> list[str]:
    return [
        executable,
        "-m",
        "",
        "-On",
        "-t",
        str(SNMP_TIMEOUT_SECONDS),
        "-r",
        str(SNMP_RETRIES),
        host,
    ]


class SynologyHealthAdapter:
    """Narrow SNMPv3 implementation of storage.health."""

    def __init__(
        self,
        target: SynologyTarget,
        credentials: SnmpV3Credentials,
        runner: Runner | None = None,
    ):
        self._target = target
        self._credentials = credentials
        self._runner = runner or subprocess.run

    def _redact(self, value: str) -> str:
        redacted = value

        for secret in (
            self._credentials.username,
            self._credentials.auth_passphrase,
            self._credentials.privacy_passphrase,
        ):
            if secret:
                redacted = redacted.replace(secret, "<redacted>")

        return redacted

    def _run(
        self,
        argv: list[str],
        environment: dict[str, str],
    ) -> str:
        try:
            result = self._runner(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=PROCESS_TIMEOUT_SECONDS,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise SnmpTimeoutError(
                "SNMP command exceeded process timeout"
            ) from exc

        if result.returncode != 0:
            stderr = self._redact(result.stderr.strip())

            raise SnmpCommandError(
                f"SNMP command failed with rc={result.returncode}"
                + (f": {stderr}" if stderr else "")
            )

        return result.stdout.strip()

    def _commands(self) -> tuple[list[str], list[str], list[str]]:
        """Return the complete fixed command surface."""
        system_command = _base_command(
            SNMPGET,
            self._target.host,
        ) + list(oids.SYSTEM.values())

        disk_command = _base_command(
            SNMPWALK,
            self._target.host,
        ) + [oids.DISK_TABLE]

        storage_command = _base_command(
            SNMPWALK,
            self._target.host,
        ) + [oids.STORAGE_TABLE]

        return (
            system_command,
            disk_command,
            storage_command,
        )

    def get_health(self, target_id: str) -> StorageHealth:
        """Execute storage.health for the single configured target."""
        if target_id != self._target.target_id:
            raise UnknownTargetError(
                f"Target is not authorized: {target_id}"
            )

        parent = _runtime_temp_parent()

        with tempfile.TemporaryDirectory(
            prefix="pardo-snmp.",
            dir=parent,
        ) as temp_dir:
            temp_path = Path(temp_dir)
            temp_path.chmod(0o700)

            _write_snmp_config(
                temp_path,
                self._credentials,
            )

            environment = os.environ.copy()
            environment["SNMPCONFPATH"] = str(temp_path)

            outputs = [
                self._run(command, environment)
                for command in self._commands()
            ]

        combined = "\n".join(
            output
            for output in outputs
            if output
        )

        try:
            return parse_health_walk(combined)
        except ParseError as exc:
            raise AdapterError(
                "SNMP response failed storage.health validation"
            ) from exc
