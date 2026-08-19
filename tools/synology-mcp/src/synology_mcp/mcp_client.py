"""Minimal MCP stdio client used by synology-mcp.

The adapter never vendors the upstream server. It launches an external
process whose argv comes from ToolBindings and speaks JSON-RPC.

The pinned upstream (mcp>=2.0.0) uses newline-delimited JSON on stdio,
not Content-Length framing.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
from typing import Any, Mapping, Protocol

from .errors import AdapterError
from .manifest import ALLOWED_MCP_TOOLS


CHILD_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "TZ",
        "USER",
        "XDG_CONFIG_HOME",
        "XDG_RUNTIME_DIR",
    }
)

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT_S = 20.0


class McpToolClient(Protocol):
    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call one allowlisted MCP tool and return parsed JSON content."""


def child_env(parent: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if parent is None else parent
    env = {
        key: source[key]
        for key in CHILD_ENV_ALLOWLIST
        if key in source and source[key]
    }
    if "PATH" not in env:
        env["PATH"] = "/usr/bin:/bin"
    return env


class FakeMcpClient:
    """Deterministic client for tests. Records allowlisted tool calls."""

    def __init__(
        self,
        nas_list: Any,
        health_summary: Any,
        errors: dict[str, Exception] | None = None,
    ):
        self.nas_list = nas_list
        self.health_summary = health_summary
        self.errors = errors or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in ALLOWED_MCP_TOOLS:
            raise AdapterError(
                "mcp_tool_not_permitted",
                "MCP tool is not permitted",
            )
        self.calls.append((name, dict(arguments)))
        if name in self.errors:
            raise self.errors[name]
        if name == "synology_list_nas":
            return self.nas_list
        if name == "synology_health_summary":
            return self.health_summary
        raise AdapterError(
            "mcp_tool_not_permitted",
            "MCP tool is not permitted",
        )


class StdioMcpClient:
    """JSON-RPC MCP client over a child stdio process."""

    def __init__(
        self,
        argv: list[str],
        env: Mapping[str, str] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        popen: Any = subprocess.Popen,
    ):
        if not argv:
            raise AdapterError(
                "invalid_bindings",
                "MCP command is empty",
            )
        self.argv = list(argv)
        self.env = child_env(env)
        self.timeout_s = timeout_s
        self._popen = popen
        self._proc: subprocess.Popen[bytes] | None = None
        self._stderr_thread: threading.Thread | None = None
        self._next_id = 1
        self._initialized = False

    def close(self) -> None:
        proc = self._proc
        self._proc = None
        self._initialized = False
        if proc is None:
            return
        if proc.stdin and not proc.stdin.closed:
            try:
                proc.stdin.close()
            except OSError:
                pass
        for stream_name in ("stdout", "stderr"):
            stream = getattr(proc, stream_name)
            if stream and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
            self._stderr_thread = None

    def __enter__(self) -> "StdioMcpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in ALLOWED_MCP_TOOLS:
            raise AdapterError(
                "mcp_tool_not_permitted",
                "MCP tool is not permitted",
            )
        self._ensure_initialized()
        response = self._rpc(
            "tools/call",
            {
                "name": name,
                "arguments": arguments,
            },
        )
        return _tool_result_payload(response)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._start()
        self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "pardo-synology-mcp",
                    "version": "1.0.0",
                },
            },
        )
        self._notify("notifications/initialized", {})
        self._initialized = True

    def _start(self) -> None:
        if self._proc is not None:
            return
        try:
            self._proc = self._popen(
                self.argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
                bufsize=0,
            )
        except OSError as exc:
            raise AdapterError(
                "adapter_error",
                "Unable to start MCP server",
            ) from exc

        if self._proc.stdin is None or self._proc.stdout is None:
            raise AdapterError(
                "adapter_error",
                "MCP server stdio is unavailable",
            )
        if self._proc.stderr is not None:
            self._stderr_thread = threading.Thread(
                target=_drain_pipe,
                args=(self._proc.stderr,),
                daemon=True,
            )
            self._stderr_thread.start()

    def _next(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message_id = self._next()
        self._write(
            {
                "jsonrpc": "2.0",
                "id": message_id,
                "method": method,
                "params": params,
            }
        )
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError(
                    "adapter_error",
                    "MCP server timed out",
                )
            payload = self._read(timeout_s=remaining)
            if payload.get("id") != message_id:
                continue
            if "error" in payload:
                raise AdapterError(
                    "adapter_error",
                    "MCP server returned an error",
                )
            result = payload.get("result")
            if not isinstance(result, dict):
                raise AdapterError(
                    "adapter_error",
                    "MCP server returned an invalid result",
                )
            return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._write(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    def _write(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AdapterError(
                "adapter_error",
                "MCP server is not running",
            )
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        try:
            proc.stdin.write(body + b"\n")
            proc.stdin.flush()
        except OSError as exc:
            raise AdapterError(
                "adapter_error",
                "Unable to write to MCP server",
            ) from exc

    def _read(self, timeout_s: float) -> dict[str, Any]:
        proc = self._proc
        if proc is None or proc.stdout is None:
            raise AdapterError(
                "adapter_error",
                "MCP server is not running",
            )
        deadline = time.monotonic() + timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError(
                    "adapter_error",
                    "MCP server timed out",
                )
            raw = _read_until(proc.stdout, b"\n", remaining)
            line = raw.strip()
            if not line:
                continue
            if len(line) > 8_000_000:
                raise AdapterError(
                    "adapter_error",
                    "MCP payload is too large",
                )
            try:
                payload = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise AdapterError(
                    "adapter_error",
                    "MCP server returned invalid JSON",
                ) from exc
            if not isinstance(payload, dict):
                raise AdapterError(
                    "adapter_error",
                    "MCP server returned an invalid message",
                )
            return payload


def _drain_pipe(stream: Any) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
    except OSError:
        return


def _read_until(stream: Any, marker: bytes, timeout_s: float) -> bytes:
    deadline = time.monotonic() + timeout_s
    buf = bytearray()
    while marker not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError(
                "adapter_error",
                "MCP server timed out",
            )
        _wait_readable(stream, remaining)
        chunk = stream.read(1)
        if not chunk:
            raise AdapterError(
                "adapter_error",
                "MCP server closed stdout",
            )
        buf.extend(chunk)
        if len(buf) > 8_000_000:
            raise AdapterError(
                "adapter_error",
                "MCP framing is too large",
            )
    return bytes(buf)


def _wait_readable(stream: Any, timeout_s: float) -> None:
    fileno = getattr(stream, "fileno", None)
    if not callable(fileno):
        return
    ready, _, _ = select.select([stream], [], [], timeout_s)
    if not ready:
        raise AdapterError(
            "adapter_error",
            "MCP server timed out",
        )


def _tool_result_payload(result: dict[str, Any]) -> Any:
    if result.get("isError"):
        raise AdapterError(
            "adapter_error",
            "MCP tool returned an error",
        )

    structured = result.get("structuredContent")
    if structured is not None:
        return structured

    content = result.get("content")
    if not isinstance(content, list) or not content:
        raise AdapterError(
            "adapter_error",
            "MCP tool returned no content",
        )

    first = content[0]
    if not isinstance(first, dict):
        raise AdapterError(
            "adapter_error",
            "MCP tool returned invalid content",
        )
    text = first.get("text")
    if not isinstance(text, str):
        raise AdapterError(
            "adapter_error",
            "MCP tool returned invalid content",
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
