"""Language-agnostic ToolRuntimeContract CLI for synology-mcp.

DeploymentFactory may invoke this process with:
- stdin JSON request;
- stdout JSON response;
- PARDO_TOOL_BINDINGS_PATH pointing at non-secret bindings;
- process exit status (0 only on PASS).
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Mapping, TextIO

from .adapter import (
    ClientFactory,
    SynologyMcpAdapter,
    default_client_factory,
)
from .bindings import (
    BINDINGS_PATH_ENV,
    BindingsError,
    ToolBindings,
    bindings_path_from_env,
    load_bindings,
)
from .errors import AdapterError, TargetResolutionError
from .manifest import (
    ManifestError,
    ToolManifest,
    default_manifest_path,
    load_tool_manifest,
)
from .mcp_client import McpToolClient


API_VERSION = "pardo.ai/v0"
SUPPORTED_OPS = frozenset(
    {
        "describe",
        "check-deps",
        "probe",
        "invoke",
    }
)
OPS_REQUIRING_TARGET = frozenset({"probe", "invoke"})
ALLOWED_INPUT_KEYS = frozenset({"targetRef", "options"})
ALLOWED_OPTION_KEYS: frozenset[str] = frozenset()


class ContractError(Exception):
    """Fail-closed contract violation with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now_ms() -> int:
    return int(time.time() * 1000)


def _write_json(stream: TextIO, payload: Mapping[str, Any]) -> None:
    stream.write(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )
    stream.write("\n")
    stream.flush()


def _response(
    *,
    op: str | None,
    status: str,
    capability: str | None,
    capability_version: str | None,
    result: dict[str, Any],
    error: dict[str, str] | None,
    implementation_id: str,
    implementation_version: str,
    target_ref: str | None,
    duration_ms: int,
) -> dict[str, Any]:
    return {
        "apiVersion": API_VERSION,
        "op": op,
        "status": status,
        "capability": capability,
        "capabilityVersion": capability_version,
        "result": result,
        "error": error,
        "meta": {
            "implementationId": implementation_id,
            "implementationVersion": implementation_version,
            "targetRef": target_ref,
            "durationMs": duration_ms,
        },
    }


def parse_request(raw: str) -> dict[str, Any]:
    if not raw.strip():
        raise ContractError(
            "malformed_request",
            "Request body is empty",
        )

    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractError(
            "malformed_request",
            "Request body is not valid JSON",
        ) from exc

    if not isinstance(document, dict):
        raise ContractError(
            "malformed_request",
            "Request root must be a JSON object",
        )

    return document


def validate_request(
    request: dict[str, Any],
    manifest: ToolManifest,
) -> tuple[str, str, str, dict[str, Any]]:
    api_version = request.get("apiVersion")
    op = request.get("op")
    capability = request.get("capability")
    capability_version = request.get("capabilityVersion")
    raw_input = request.get("input", {})

    if api_version != API_VERSION:
        raise ContractError(
            "api_version_mismatch",
            f"apiVersion must be {API_VERSION}",
        )

    if not isinstance(op, str) or not op:
        raise ContractError(
            "malformed_request",
            "op must be a non-empty string",
        )

    if op not in SUPPORTED_OPS:
        raise ContractError(
            "unknown_op",
            f"Unknown operation: {op}",
        )

    if capability != manifest.capability:
        raise ContractError(
            "capability_mismatch",
            "capability does not match this implementation",
        )

    if capability_version != manifest.capability_version:
        raise ContractError(
            "version_mismatch",
            "capabilityVersion does not match this implementation",
        )

    if raw_input is None:
        raw_input = {}

    if not isinstance(raw_input, dict):
        raise ContractError(
            "malformed_request",
            "input must be a JSON object",
        )

    unexpected = set(raw_input) - ALLOWED_INPUT_KEYS
    if unexpected:
        raise ContractError(
            "invalid_input",
            "Request input may contain only targetRef and options",
        )

    options = raw_input.get("options")
    if options is not None:
        if not isinstance(options, dict):
            raise ContractError(
                "invalid_input",
                "input.options must be a JSON object",
            )
        if set(options) - ALLOWED_OPTION_KEYS:
            raise ContractError(
                "invalid_input",
                "input.options contains unsupported keys",
            )

    target_ref = raw_input.get("targetRef")
    if op in OPS_REQUIRING_TARGET:
        if not isinstance(target_ref, str) or not target_ref.strip():
            raise ContractError(
                "missing_target_ref",
                "input.targetRef is required",
            )
    elif target_ref is not None and (
        not isinstance(target_ref, str) or not target_ref.strip()
    ):
        raise ContractError(
            "invalid_input",
            "input.targetRef must be a non-empty string when present",
        )

    return (
        op,
        str(capability),
        str(capability_version),
        raw_input,
    )


def _executable_ok(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    present = False
    is_regular = False
    executable = False

    try:
        st = path.lstat()
        present = True
        is_regular = stat.S_ISREG(st.st_mode)
        executable = is_regular and os.access(path, os.X_OK)
    except OSError:
        present = False

    ok = present and is_regular and executable
    return {
        "path": path_text,
        "present": present,
        "regularFile": is_regular,
        "executable": executable,
        "ok": ok,
    }


def _check_deps(
    manifest: ToolManifest,
    env: Mapping[str, str],
) -> dict[str, Any]:
    executables = [
        _executable_ok(path_text)
        for path_text in manifest.required_executables
    ]
    mcp_command: dict[str, Any] | None = None
    all_ok = all(item["ok"] for item in executables) if executables else True

    try:
        bindings = _load_runtime_bindings(env, manifest)
        command_path = bindings.mcp.command[0]
        mcp_command = _executable_ok(command_path)
        mcp_command["argv"] = list(bindings.mcp.argv())
        all_ok = all_ok and mcp_command["ok"]
    except BindingsError:
        mcp_command = {
            "verified": False,
            "ok": False,
        }
        all_ok = False

    return {
        "ok": all_ok,
        "executables": executables,
        "mcp": {
            "transport": "stdio",
            "command": mcp_command,
        },
    }


def _describe(manifest: ToolManifest) -> dict[str, Any]:
    return {
        "implementationId": manifest.implementation_id,
        "implementationVersion": manifest.implementation_version,
        "capability": manifest.capability,
        "capabilityVersion": manifest.capability_version,
        "risk": manifest.risk,
        "operations": sorted(SUPPORTED_OPS),
        "upstream": manifest.upstream,
        "targetResolution": manifest.target_resolution,
        "requiredExecutables": list(manifest.required_executables),
        "contract": {
            "apiVersion": API_VERSION,
            "kind": "ToolRuntimeContract",
            "io": {
                "request": "stdin-json",
                "response": "stdout-json",
                "diagnostics": "stderr-text",
            },
            "bindings": {
                "configPathEnv": BINDINGS_PATH_ENV,
            },
        },
    }


def _load_runtime_bindings(
    env: Mapping[str, str],
    manifest: ToolManifest,
) -> ToolBindings:
    path = bindings_path_from_env(dict(env))
    bindings = load_bindings(path)

    if bindings.implementation_id != manifest.implementation_id:
        raise BindingsError(
            "bindings.implementationId does not match this tool"
        )

    return bindings


def _close_client(client: McpToolClient) -> None:
    closer = getattr(client, "close", None)
    if callable(closer):
        closer()


def _run_health(
    *,
    target_ref: str,
    manifest: ToolManifest,
    env: Mapping[str, str],
    client_factory: ClientFactory | None,
    injected_client: McpToolClient | None,
) -> dict[str, Any]:
    bindings = _load_runtime_bindings(env, manifest)
    client = injected_client
    owns_client = False

    if client is None:
        factory = client_factory or default_client_factory
        client = factory(bindings, env)
        owns_client = True

    try:
        adapter = SynologyMcpAdapter(
            manifest=manifest,
            bindings=bindings,
            client=client,
        )
        health = adapter.get_health(target_ref)
    except TargetResolutionError as exc:
        raise ContractError(exc.code, exc.message) from exc
    except AdapterError as exc:
        raise ContractError(
            exc.code if exc.code else "adapter_error",
            "adapter error",
        ) from exc
    finally:
        if owns_client:
            _close_client(client)

    payload = health.to_dict()
    blob = json.dumps(payload)
    if "synology_list_nas" in blob or "synology_health_summary" in blob:
        raise ContractError(
            "internal_error",
            "internal error",
        )
    return payload


def handle_request(
    request: dict[str, Any],
    *,
    manifest: ToolManifest,
    env: Mapping[str, str],
    client_factory: ClientFactory | None = None,
    client: McpToolClient | None = None,
    started_ms: int | None = None,
) -> tuple[dict[str, Any], int]:
    started = started_ms if started_ms is not None else _now_ms()
    op: str | None = None
    capability: str | None = manifest.capability
    capability_version: str | None = manifest.capability_version
    target_ref: str | None = None

    try:
        op, capability, capability_version, raw_input = validate_request(
            request,
            manifest,
        )

        if isinstance(raw_input.get("targetRef"), str):
            target_ref = raw_input["targetRef"].strip() or None

        if op == "describe":
            result = _describe(manifest)
            status = "PASS"
            error = None
        elif op == "check-deps":
            result = _check_deps(manifest, env)
            status = "PASS" if result["ok"] else "FAIL"
            error = (
                None
                if status == "PASS"
                else {
                    "code": "check_deps_failed",
                    "message": "One or more required dependencies are missing",
                }
            )
        elif op in OPS_REQUIRING_TARGET:
            assert target_ref is not None
            result = _run_health(
                target_ref=target_ref,
                manifest=manifest,
                env=env,
                client_factory=client_factory,
                injected_client=client,
            )
            status = "PASS"
            error = None
        else:
            raise ContractError(
                "unknown_op",
                f"Unknown operation: {op}",
            )

        payload = _response(
            op=op,
            status=status,
            capability=capability,
            capability_version=capability_version,
            result=result,
            error=error,
            implementation_id=manifest.implementation_id,
            implementation_version=manifest.implementation_version,
            target_ref=target_ref,
            duration_ms=max(0, _now_ms() - started),
        )
        return (payload, 0 if status == "PASS" else 1)

    except ContractError as exc:
        payload = _response(
            op=op,
            status="FAIL",
            capability=capability,
            capability_version=capability_version,
            result={},
            error={
                "code": exc.code,
                "message": exc.message.replace("\n", " ").strip(),
            },
            implementation_id=manifest.implementation_id,
            implementation_version=manifest.implementation_version,
            target_ref=target_ref,
            duration_ms=max(0, _now_ms() - started),
        )
        return (payload, 1)

    except BindingsError as exc:
        payload = _response(
            op=op,
            status="FAIL",
            capability=capability,
            capability_version=capability_version,
            result={},
            error={
                "code": "invalid_bindings",
                "message": str(exc).replace("\n", " ").strip(),
            },
            implementation_id=manifest.implementation_id,
            implementation_version=manifest.implementation_version,
            target_ref=target_ref,
            duration_ms=max(0, _now_ms() - started),
        )
        return (payload, 1)

    except ManifestError as exc:
        payload = _response(
            op=op,
            status="FAIL",
            capability=capability,
            capability_version=capability_version,
            result={},
            error={
                "code": "invalid_manifest",
                "message": str(exc).replace("\n", " ").strip(),
            },
            implementation_id=manifest.implementation_id,
            implementation_version=manifest.implementation_version,
            target_ref=target_ref,
            duration_ms=max(0, _now_ms() - started),
        )
        return (payload, 1)

    except Exception:
        payload = _response(
            op=op,
            status="FAIL",
            capability=capability,
            capability_version=capability_version,
            result={},
            error={
                "code": "internal_error",
                "message": "internal error",
            },
            implementation_id=manifest.implementation_id,
            implementation_version=manifest.implementation_version,
            target_ref=target_ref,
            duration_ms=max(0, _now_ms() - started),
        )
        return (payload, 1)


def main(
    argv: list[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    env: Mapping[str, str] | None = None,
    client_factory: ClientFactory | None = None,
    client: McpToolClient | None = None,
    manifest_path: Path | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    env = os.environ if env is None else env

    if argv:
        _write_json(
            stdout,
            _response(
                op=None,
                status="FAIL",
                capability=None,
                capability_version=None,
                result={},
                error={
                    "code": "argv_not_supported",
                    "message": (
                        "pardo-tool accepts no argv; "
                        "use stdin JSON only"
                    ),
                },
                implementation_id="synology-mcp",
                implementation_version="1.0.0",
                target_ref=None,
                duration_ms=0,
            ),
        )
        return 1

    started = _now_ms()

    try:
        manifest = load_tool_manifest(
            manifest_path or default_manifest_path()
        )
    except ManifestError as exc:
        _write_json(
            stdout,
            _response(
                op=None,
                status="FAIL",
                capability=None,
                capability_version=None,
                result={},
                error={
                    "code": "invalid_manifest",
                    "message": str(exc),
                },
                implementation_id="synology-mcp",
                implementation_version="1.0.0",
                target_ref=None,
                duration_ms=max(0, _now_ms() - started),
            ),
        )
        return 1

    try:
        raw = stdin.read()
        request = parse_request(raw)
        payload, code = handle_request(
            request,
            manifest=manifest,
            env=env,
            client_factory=client_factory,
            client=client,
            started_ms=started,
        )
        _write_json(stdout, payload)
        return code
    except ContractError as exc:
        _write_json(
            stdout,
            _response(
                op=None,
                status="FAIL",
                capability=manifest.capability,
                capability_version=manifest.capability_version,
                result={},
                error={
                    "code": exc.code,
                    "message": exc.message,
                },
                implementation_id=manifest.implementation_id,
                implementation_version=manifest.implementation_version,
                target_ref=None,
                duration_ms=max(0, _now_ms() - started),
            ),
        )
        return 1
    except Exception:
        print("pardo-tool failed closed", file=stderr)
        _write_json(
            stdout,
            _response(
                op=None,
                status="FAIL",
                capability=manifest.capability,
                capability_version=manifest.capability_version,
                result={},
                error={
                    "code": "internal_error",
                    "message": "internal error",
                },
                implementation_id=manifest.implementation_id,
                implementation_version=manifest.implementation_version,
                target_ref=None,
                duration_ms=max(0, _now_ms() - started),
            ),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
