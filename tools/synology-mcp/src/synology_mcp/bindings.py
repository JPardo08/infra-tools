"""Non-secret ToolBindings for the synology-mcp implementation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class BindingsError(ValueError):
    """Raised when bindings are missing or fail closed validation."""


BINDINGS_PATH_ENV = "PARDO_TOOL_BINDINGS_PATH"
ALLOWED_TARGET_KEYS = frozenset({"nasName"})
ALLOWED_MCP_KEYS = frozenset({"transport", "command", "args"})


@dataclass(frozen=True)
class TargetBinding:
    target_ref: str
    nas_name: str


@dataclass(frozen=True)
class McpBinding:
    transport: str
    command: tuple[str, ...]
    args: tuple[str, ...]

    def argv(self) -> list[str]:
        return [*self.command, *self.args]


@dataclass(frozen=True)
class ToolBindings:
    implementation_id: str
    targets: dict[str, TargetBinding]
    mcp: McpBinding
    path: Path


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BindingsError(f"{label} must be a JSON object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BindingsError(f"{label} must be a non-empty string")
    return value.strip()


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

    if document.get("apiVersion") != "pardo.ai/v0":
        raise BindingsError(
            "bindings.apiVersion must be pardo.ai/v0"
        )

    if document.get("kind") != "ToolBindings":
        raise BindingsError(
            "bindings.kind must be ToolBindings"
        )

    if document.get("secrets"):
        raise BindingsError(
            "bindings.secrets are not used by synology-mcp; "
            "DSM credentials stay with the MCP server"
        )

    implementation_id = _require_string(
        document.get("implementationId"),
        "bindings.implementationId",
    )

    targets_raw = _require_mapping(
        document.get("targets"),
        "bindings.targets",
    )

    if not targets_raw:
        raise BindingsError(
            "bindings.targets must declare at least one target"
        )

    targets: dict[str, TargetBinding] = {}

    for target_ref, target_value in targets_raw.items():
        if not isinstance(target_ref, str) or not target_ref.strip():
            raise BindingsError(
                "bindings.targets keys must be non-empty strings"
            )

        target_obj = _require_mapping(
            target_value,
            f"bindings.targets.{target_ref}",
        )

        forbidden = {
            "host",
            "hostname",
            "address",
            "ip",
            "url",
            "base_url",
            "username",
            "password",
            "path",
        }
        if forbidden.intersection(target_obj):
            raise BindingsError(
                f"bindings.targets.{target_ref} contains "
                f"unsupported fields"
            )

        unexpected = set(target_obj) - ALLOWED_TARGET_KEYS
        if unexpected:
            raise BindingsError(
                f"bindings.targets.{target_ref} may contain only nasName"
            )

        nas_name = _require_string(
            target_obj.get("nasName"),
            f"bindings.targets.{target_ref}.nasName",
        )
        targets[target_ref] = TargetBinding(
            target_ref=target_ref,
            nas_name=nas_name,
        )

    mcp_raw = _require_mapping(document.get("mcp"), "bindings.mcp")
    unexpected_mcp = set(mcp_raw) - ALLOWED_MCP_KEYS
    if unexpected_mcp:
        raise BindingsError(
            "bindings.mcp may contain only transport, command, and args"
        )

    transport = _require_string(
        mcp_raw.get("transport"),
        "bindings.mcp.transport",
    ).lower()
    if transport != "stdio":
        raise BindingsError(
            "bindings.mcp.transport must be stdio"
        )

    command_raw = mcp_raw.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise BindingsError(
            "bindings.mcp.command must be a non-empty argv array"
        )

    command: list[str] = []
    for index, item in enumerate(command_raw):
        if not isinstance(item, str) or not item:
            raise BindingsError(
                f"bindings.mcp.command[{index}] must be a non-empty string"
            )
        command.append(item)

    if not Path(command[0]).is_absolute():
        raise BindingsError(
            "bindings.mcp.command[0] must be an absolute path"
        )

    args_raw = mcp_raw.get("args", [])
    if args_raw is None:
        args_raw = []
    if not isinstance(args_raw, list):
        raise BindingsError("bindings.mcp.args must be an array")

    args: list[str] = []
    for index, item in enumerate(args_raw):
        if not isinstance(item, str):
            raise BindingsError(
                f"bindings.mcp.args[{index}] must be a string"
            )
        args.append(item)

    return ToolBindings(
        implementation_id=implementation_id,
        targets=targets,
        mcp=McpBinding(
            transport=transport,
            command=tuple(command),
            args=tuple(args),
        ),
        path=path,
    )


def resolve_target(
    bindings: ToolBindings,
    target_ref: str,
) -> TargetBinding:
    try:
        return bindings.targets[target_ref]
    except KeyError as exc:
        raise BindingsError(
            f"Unknown targetRef: {target_ref}"
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
