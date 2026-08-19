"""Read-only adapter from storage.health to a Synology MCP server."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Mapping

from .bindings import ToolBindings
from .errors import AdapterError, TargetResolutionError
from .manifest import ALLOWED_MCP_TOOLS, ToolManifest
from .mcp_client import McpToolClient, StdioMcpClient, child_env
from .models import StorageHealth
from .normalize import normalize_health_summary
from .resolve import resolve_nas_name


ClientFactory = Callable[[ToolBindings, Mapping[str, str]], McpToolClient]


def default_client_factory(
    bindings: ToolBindings,
    env: Mapping[str, str],
) -> McpToolClient:
    return StdioMcpClient(
        argv=bindings.mcp.argv(),
        env=child_env(env),
    )


class SynologyMcpAdapter:
    """Maps a resolved PARDO targetRef onto allowlisted MCP health tools."""

    def __init__(
        self,
        *,
        manifest: ToolManifest,
        bindings: ToolBindings,
        client: McpToolClient,
    ):
        self.manifest = manifest
        self.bindings = bindings
        self.client = client

    def get_health(self, target_ref: str) -> StorageHealth:
        try:
            target = self.bindings.targets[target_ref]
        except KeyError as exc:
            raise TargetResolutionError(
                "TARGET_NOT_FOUND",
                "targetRef is not a configured storage target",
            ) from exc

        nas_list = self._call("synology_list_nas", {})
        matched = resolve_nas_name(
            target_ref=target_ref,
            mapped_nas_name=target.nas_name,
            nas_list=nas_list,
        )
        summary = self._call(
            "synology_health_summary",
            {"nas_name": matched.nas_name},
        )
        return normalize_health_summary(
            target_ref=target_ref,
            matched_nas_name=matched.nas_name,
            summary=summary,
            implementation_id=self.manifest.implementation_id,
            implementation_version=self.manifest.implementation_version,
        )

    def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name not in ALLOWED_MCP_TOOLS:
            raise AdapterError(
                "mcp_tool_not_permitted",
                "MCP tool is not permitted",
            )
        if name not in self.manifest.allowed_mcp_tools:
            raise AdapterError(
                "mcp_tool_not_permitted",
                "MCP tool is not permitted",
            )
        return self.client.call_tool(name, arguments)
