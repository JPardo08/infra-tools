"""Non-secret ToolBindings loading and validation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BindingsError(ValueError):
    """Raised when bindings are missing or fail closed validation."""


ALLOWED_PROTOCOLS = frozenset({"udp"})
BINDINGS_PATH_ENV = "PARDO_TOOL_BINDINGS_PATH"
ALLOWED_SECRET_BINDING_KEYS = frozenset({"type", "path"})


@dataclass(frozen=True)
class TargetBinding:
    target_id: str
    address: str
    port: int
    protocol: str

    def snmp_host(self) -> str:
        if self.port == 161:
            return self.address
        return f"{self.address}:{self.port}"


@dataclass(frozen=True)
class SecretBinding:
    secret_ref: str
    type: str
    path: Path


@dataclass(frozen=True)
class ToolBindings:
    implementation_id: str
    targets: dict[str, TargetBinding]
    secrets: dict[str, SecretBinding]
    path: Path


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BindingsError(f"{label} must be a JSON object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BindingsError(f"{label} must be a non-empty string")
    return value.strip()


def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BindingsError(f"{label} must be an integer")
    return value


def load_bindings(path: Path) -> ToolBindings:
    if not path.is_file():
        raise BindingsError(f"Bindings file not found: {path}")

    try:
        raw = path.read_text()
    except OSError as exc:
        raise BindingsError(
            f"Unable to read bindings file: {path}"
        ) from exc

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BindingsError(
            "Bindings file is not valid JSON"
        ) from exc

    document = _require_mapping(document, "bindings")

    api_version = document.get("apiVersion")
    kind = document.get("kind")

    if api_version != "pardo.ai/v0":
        raise BindingsError(
            "bindings.apiVersion must be pardo.ai/v0"
        )

    if kind != "ToolBindings":
        raise BindingsError(
            "bindings.kind must be ToolBindings"
        )

    implementation_id = _require_string(
        document.get("implementationId"),
        "bindings.implementationId",
    )

    targets_raw = _require_mapping(
        document.get("targets"),
        "bindings.targets",
    )
    secrets_raw = _require_mapping(
        document.get("secrets"),
        "bindings.secrets",
    )

    if not targets_raw:
        raise BindingsError(
            "bindings.targets must declare at least one target"
        )

    targets: dict[str, TargetBinding] = {}

    for target_id, target_value in targets_raw.items():
        if not isinstance(target_id, str) or not target_id:
            raise BindingsError(
                "bindings.targets keys must be non-empty strings"
            )

        target_obj = _require_mapping(
            target_value,
            f"bindings.targets.{target_id}",
        )

        # Reject attempt to smuggle alternate identifiers.
        forbidden = {
            "host",
            "hostname",
            "ip",
            "oid",
            "oids",
            "community",
        }
        unexpected = forbidden.intersection(target_obj)
        if unexpected:
            raise BindingsError(
                f"bindings.targets.{target_id} contains "
                f"unsupported fields"
            )

        address = _require_string(
            target_obj.get("address"),
            f"bindings.targets.{target_id}.address",
        )
        port = _require_int(
            target_obj.get("port"),
            f"bindings.targets.{target_id}.port",
        )
        protocol = _require_string(
            target_obj.get("protocol"),
            f"bindings.targets.{target_id}.protocol",
        ).lower()

        if port < 1 or port > 65535:
            raise BindingsError(
                f"bindings.targets.{target_id}.port is out of range"
            )

        if protocol not in ALLOWED_PROTOCOLS:
            raise BindingsError(
                f"bindings.targets.{target_id}.protocol "
                f"must be udp"
            )

        targets[target_id] = TargetBinding(
            target_id=target_id,
            address=address,
            port=port,
            protocol=protocol,
        )

    secrets: dict[str, SecretBinding] = {}

    for secret_ref, secret_value in secrets_raw.items():
        if not isinstance(secret_ref, str) or not secret_ref:
            raise BindingsError(
                "bindings.secrets keys must be non-empty strings"
            )

        secret_obj = _require_mapping(
            secret_value,
            f"bindings.secrets.{secret_ref}",
        )

        unexpected = set(secret_obj) - ALLOWED_SECRET_BINDING_KEYS
        if unexpected:
            raise BindingsError(
                f"bindings.secrets.{secret_ref} may contain only "
                f"type and path"
            )

        missing = ALLOWED_SECRET_BINDING_KEYS - set(secret_obj)
        if missing:
            raise BindingsError(
                f"bindings.secrets.{secret_ref} requires type and path"
            )

        secret_type = _require_string(
            secret_obj.get("type"),
            f"bindings.secrets.{secret_ref}.type",
        )
        secret_path = _require_string(
            secret_obj.get("path"),
            f"bindings.secrets.{secret_ref}.path",
        )

        secrets[secret_ref] = SecretBinding(
            secret_ref=secret_ref,
            type=secret_type,
            path=Path(secret_path),
        )

    return ToolBindings(
        implementation_id=implementation_id,
        targets=targets,
        secrets=secrets,
        path=path,
    )


def resolve_target(
    bindings: ToolBindings,
    target_id: str,
) -> TargetBinding:
    try:
        return bindings.targets[target_id]
    except KeyError as exc:
        raise BindingsError(
            f"Unknown targetId: {target_id}"
        ) from exc


def resolve_secret_binding(
    bindings: ToolBindings,
    secret_ref: str,
) -> SecretBinding:
    try:
        return bindings.secrets[secret_ref]
    except KeyError as exc:
        raise BindingsError(
            f"Unknown secretRef: {secret_ref}"
        ) from exc


def bindings_path_from_env(
    env: dict[str, str] | None = None,
    env_var: str = BINDINGS_PATH_ENV,
) -> Path:
    source = env if env is not None else os.environ
    raw = source.get(env_var)

    if raw is None or not str(raw).strip():
        raise BindingsError(
            f"Missing required environment variable: {env_var}"
        )

    return Path(str(raw).strip())
