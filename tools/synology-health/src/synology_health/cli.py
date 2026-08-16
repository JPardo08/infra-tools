"""Language-agnostic ToolRuntimeContract CLI for synology-health.

DeploymentFactory may invoke this process with:
- stdin JSON request;
- stdout JSON response;
- PARDO_TOOL_BINDINGS_PATH pointing at non-secret bindings;
- process exit status (0 only on PASS).

This module must not expose adapter internals, OIDs, or credentials
on stdout.
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
    AdapterError,
    Runner,
    SynologyHealthAdapter,
    SynologyTarget,
    UnknownTargetError,
)
from .bindings import (
    BINDINGS_PATH_ENV,
    BindingsError,
    ToolBindings,
    bindings_path_from_env,
    load_bindings,
    resolve_secret_binding,
    resolve_target,
)
from .manifest import (
    ManifestError,
    ToolManifest,
    default_manifest_path,
    load_tool_manifest,
    resolve_secret_ref,
)
from .secrets import LoadedSecret, SecretError, load_snmpv3_secret


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

# storage.health V0: request.input may contain only targetId.
ALLOWED_INPUT_KEYS = frozenset({"targetId"})


class ContractError(Exception):
    """Fail-closed contract violation with a stable machine code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now_ms() -> int:
    return int(time.time() * 1000)


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    redacted = text

    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")

    return redacted


def _sanitize_error_message(
    message: str,
    secrets: tuple[str, ...],
) -> str:
    return _redact(message, secrets).replace("\n", " ").strip()


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
    target_id: str | None,
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
            "targetId": target_id,
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
            "Request input may contain only targetId",
        )

    if op in OPS_REQUIRING_TARGET:
        target_id = raw_input.get("targetId")
        if not isinstance(target_id, str) or not target_id.strip():
            raise ContractError(
                "missing_target_id",
                "input.targetId is required",
            )
    else:
        target_id = raw_input.get("targetId")
        if target_id is not None and (
            not isinstance(target_id, str) or not target_id.strip()
        ):
            raise ContractError(
                "invalid_input",
                "input.targetId must be a non-empty string when present",
            )

    return (
        op,
        str(capability),
        str(capability_version),
        raw_input,
    )


def _check_deps(manifest: ToolManifest) -> dict[str, Any]:
    executables: list[dict[str, Any]] = []
    all_ok = True

    for path_text in manifest.required_executables:
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
        all_ok = all_ok and ok
        executables.append(
            {
                "path": path_text,
                "present": present,
                "regularFile": is_regular,
                "executable": executable,
                "ok": ok,
            }
        )

    return {
        "ok": all_ok,
        "executables": executables,
    }


def _describe(manifest: ToolManifest) -> dict[str, Any]:
    return {
        "implementationId": manifest.implementation_id,
        "implementationVersion": manifest.implementation_version,
        "capability": manifest.capability,
        "capabilityVersion": manifest.capability_version,
        "risk": manifest.risk,
        "operations": sorted(SUPPORTED_OPS),
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
                "secretPathKey": "path",
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


def _prepare_target_runtime(
    *,
    manifest: ToolManifest,
    env: Mapping[str, str],
    target_id: str,
) -> tuple[ToolBindings, LoadedSecret, str]:
    bindings = _load_runtime_bindings(env, manifest)

    try:
        target = resolve_target(bindings, target_id)
    except BindingsError as exc:
        raise ContractError(
            "unknown_target",
            f"Unknown targetId: {target_id}",
        ) from exc

    secret_ref = resolve_secret_ref(manifest, target_id)

    try:
        secret_binding = resolve_secret_binding(bindings, secret_ref)
    except BindingsError as exc:
        raise ContractError(
            "invalid_bindings",
            f"Unknown secretRef: {secret_ref}",
        ) from exc

    if secret_binding.type != "snmpv3-authpriv":
        raise SecretError(
            f"Unsupported secret type: {secret_binding.type}"
        )

    loaded = load_snmpv3_secret(secret_binding.path)
    return (bindings, loaded, target.snmp_host())


def _run_health(
    *,
    target_id: str,
    host: str,
    loaded: LoadedSecret,
    runner: Runner | None,
) -> dict[str, Any]:
    adapter = SynologyHealthAdapter(
        SynologyTarget(
            target_id=target_id,
            host=host,
        ),
        loaded.credentials,
        runner=runner,
    )

    try:
        health = adapter.get_health(target_id)
    except UnknownTargetError as exc:
        raise ContractError(
            "unknown_target",
            "Target is not authorized",
        ) from exc
    except AdapterError as exc:
        raise ContractError(
            "adapter_error",
            _sanitize_error_message(
                str(exc),
                loaded.redaction_values(),
            ),
        ) from exc

    return health.to_dict()


def handle_request(
    request: dict[str, Any],
    *,
    manifest: ToolManifest,
    env: Mapping[str, str],
    runner: Runner | None = None,
    started_ms: int | None = None,
) -> tuple[dict[str, Any], int]:
    started = started_ms if started_ms is not None else _now_ms()
    secrets: tuple[str, ...] = ()
    op: str | None = None
    capability: str | None = manifest.capability
    capability_version: str | None = manifest.capability_version
    target_id: str | None = None

    try:
        op, capability, capability_version, raw_input = validate_request(
            request,
            manifest,
        )

        if isinstance(raw_input.get("targetId"), str):
            target_id = raw_input["targetId"].strip() or None

        if op == "describe":
            result = _describe(manifest)
            status = "PASS"
            error = None
        elif op == "check-deps":
            result = _check_deps(manifest)
            status = "PASS" if result["ok"] else "FAIL"
            error = (
                None
                if status == "PASS"
                else {
                    "code": "check_deps_failed",
                    "message": (
                        "One or more required executables are missing"
                    ),
                }
            )
        elif op in OPS_REQUIRING_TARGET:
            assert target_id is not None

            if not manifest.target_ref:
                raise ContractError(
                    "invalid_manifest",
                    "Tool manifest declares no authoritative target",
                )

            if target_id != manifest.target_ref:
                raise ContractError(
                    "unauthorized_target",
                    "targetId is not authorized by this implementation",
                )

            _, loaded, host = _prepare_target_runtime(
                manifest=manifest,
                env=env,
                target_id=target_id,
            )
            secrets = loaded.redaction_values()

            try:
                result = _run_health(
                    target_id=target_id,
                    host=host,
                    loaded=loaded,
                    runner=runner,
                )
            except ContractError:
                raise
            except Exception:
                # Never copy unexpected exception text: it may contain
                # secret material. Known AdapterError paths are already
                # converted to sanitized ContractError in _run_health.
                raise ContractError(
                    "internal_error",
                    "internal error",
                ) from None

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
            target_id=target_id,
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
                "message": _sanitize_error_message(
                    exc.message,
                    secrets,
                ),
            },
            implementation_id=manifest.implementation_id,
            implementation_version=manifest.implementation_version,
            target_id=target_id,
            duration_ms=max(0, _now_ms() - started),
        )
        return (payload, 1)

    except (BindingsError, SecretError, ManifestError) as exc:
        code_by_type = {
            BindingsError: "invalid_bindings",
            SecretError: "invalid_secret",
            ManifestError: "invalid_manifest",
        }
        payload = _response(
            op=op,
            status="FAIL",
            capability=capability,
            capability_version=capability_version,
            result={},
            error={
                "code": code_by_type[type(exc)],
                "message": _sanitize_error_message(
                    str(exc),
                    secrets,
                ),
            },
            implementation_id=manifest.implementation_id,
            implementation_version=manifest.implementation_version,
            target_id=target_id,
            duration_ms=max(0, _now_ms() - started),
        )
        return (payload, 1)

    except Exception:
        # Fail closed with a generic envelope; never echo exception text.
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
            target_id=target_id,
            duration_ms=max(0, _now_ms() - started),
        )
        return (payload, 1)


def main(
    argv: list[str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    env: Mapping[str, str] | None = None,
    runner: Runner | None = None,
    manifest_path: Path | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    env = os.environ if env is None else env

    if argv:
        # Contract is stdin/stdout only; refuse argv secret smuggling.
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
                implementation_id="synology-health",
                implementation_version="0.1.0",
                target_id=None,
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
                implementation_id="synology-health",
                implementation_version="0.1.0",
                target_id=None,
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
            runner=runner,
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
                target_id=None,
                duration_ms=max(0, _now_ms() - started),
            ),
        )
        return 1
    except Exception:
        # No exception text / traceback on stderr: may contain secrets.
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
                target_id=None,
                duration_ms=max(0, _now_ms() - started),
            ),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
