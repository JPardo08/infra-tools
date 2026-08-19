"""Load the ToolImplementation manifest (tool.yaml) for synology-mcp."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    """Raised when the tool manifest is missing or invalid."""


UPSTREAM_PIN = (
    "atom2ueki/mcp-server-synology@"
    "6afdaa3407e07c786d79644b92930152751af223"
)

ALLOWED_MCP_TOOLS = (
    "synology_list_nas",
    "synology_health_summary",
)


@dataclass(frozen=True)
class ToolManifest:
    implementation_id: str
    implementation_version: str
    capability: str
    capability_version: str
    risk: str
    required_executables: tuple[str, ...]
    allowed_mcp_tools: tuple[str, ...]
    upstream: str
    target_resolution: str
    path: Path


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()

    if not text:
        return ""

    if text == "{}":
        return {}
    if text == "[]":
        return []

    if text.startswith('"') and text.endswith('"'):
        return text[1:-1]
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1]

    if text in {"true", "True"}:
        return True
    if text in {"false", "False"}:
        return False
    if text in {"null", "Null", "~"}:
        return None

    if text.isdigit() or (
        text.startswith("-") and text[1:].isdigit()
    ):
        return int(text)

    return text


def _tokenize(line: str) -> tuple[int, str]:
    stripped = line.rstrip("\n")

    if not stripped.strip() or stripped.lstrip().startswith("#"):
        return (-1, "")

    indent = len(stripped) - len(stripped.lstrip(" "))

    if "\t" in stripped[:indent]:
        raise ManifestError("Tabs are not allowed in tool.yaml")

    return (indent, stripped.lstrip(" "))


def load_yaml_subset(text: str) -> Any:
    """Parse a minimal indentation-based YAML subset (maps/lists/scalars)."""
    lines: list[tuple[int, str]] = []

    for line in text.splitlines():
        indent, content = _tokenize(line)

        if indent < 0:
            continue

        lines.append((indent, content))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return ({}, index)

        current_indent, content = lines[index]

        if current_indent != indent:
            raise ManifestError("Invalid YAML indentation")

        if content.startswith("- "):
            items: list[Any] = []

            while index < len(lines) and lines[index][0] == indent:
                _, item_content = lines[index]

                if not item_content.startswith("- "):
                    raise ManifestError("Expected YAML list item")

                value_text = item_content[2:].strip()
                index += 1

                if value_text == "" or value_text.endswith(":"):
                    if value_text.endswith(":"):
                        key = value_text[:-1].strip()
                        child, index = parse_block(index, indent + 2)
                        items.append({key: child})
                    else:
                        child, index = parse_block(index, indent + 2)
                        items.append(child)
                elif ":" in value_text and not (
                    value_text.startswith("'")
                    or value_text.startswith('"')
                ):
                    key, raw = value_text.split(":", 1)
                    key = key.strip()
                    raw = raw.strip()

                    if raw == "":
                        child, index = parse_block(index, indent + 2)
                        items.append({key: child})
                    else:
                        item: dict[str, Any] = {
                            key: _parse_scalar(raw)
                        }

                        while (
                            index < len(lines)
                            and lines[index][0] > indent
                            and not lines[index][1].startswith("- ")
                        ):
                            child_indent, child_content = lines[index]

                            if ":" not in child_content:
                                raise ManifestError(
                                    "Invalid YAML mapping entry"
                                )

                            child_key, child_raw = child_content.split(
                                ":",
                                1,
                            )
                            child_key = child_key.strip()
                            child_raw = child_raw.strip()
                            index += 1

                            if child_raw == "":
                                nested, index = parse_block(
                                    index,
                                    child_indent + 2,
                                )
                                item[child_key] = nested
                            else:
                                item[child_key] = _parse_scalar(
                                    child_raw
                                )

                        items.append(item)
                else:
                    items.append(_parse_scalar(value_text))

            return (items, index)

        mapping: dict[str, Any] = {}

        while index < len(lines) and lines[index][0] == indent:
            _, entry = lines[index]

            if entry.startswith("- "):
                raise ManifestError("Unexpected YAML list item")

            if ":" not in entry:
                raise ManifestError("Expected YAML mapping entry")

            key, raw = entry.split(":", 1)
            key = key.strip()
            raw = raw.strip()
            index += 1

            if raw == "":
                if index < len(lines) and lines[index][0] > indent:
                    child, index = parse_block(
                        index,
                        lines[index][0],
                    )
                    mapping[key] = child
                else:
                    mapping[key] = None
            else:
                mapping[key] = _parse_scalar(raw)

        return (mapping, index)

    if not lines:
        return {}

    root, next_index = parse_block(0, lines[0][0])

    if next_index != len(lines):
        raise ManifestError("Trailing YAML content was not consumed")

    return root


def default_manifest_path() -> Path:
    return Path(__file__).resolve().parents[2] / "tool.yaml"


def _string_list(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestError(f"{label} must be a list")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ManifestError(
                f"{label} entries must be non-empty strings"
            )
        items.append(item)
    return tuple(items)


def load_tool_manifest(path: Path | None = None) -> ToolManifest:
    manifest_path = path or default_manifest_path()

    if not manifest_path.is_file():
        raise ManifestError(
            f"Tool manifest not found: {manifest_path}"
        )

    try:
        document = load_yaml_subset(manifest_path.read_text())
    except ManifestError:
        raise
    except OSError as exc:
        raise ManifestError(
            f"Unable to read tool manifest: {manifest_path}"
        ) from exc

    if not isinstance(document, dict):
        raise ManifestError("Tool manifest root must be a mapping")

    if document.get("kind") != "ToolImplementation":
        raise ManifestError(
            "Tool manifest kind must be ToolImplementation"
        )

    metadata = document.get("metadata")
    spec = document.get("spec")

    if not isinstance(metadata, dict) or not isinstance(spec, dict):
        raise ManifestError(
            "Tool manifest requires metadata and spec mappings"
        )

    implementation_id = metadata.get("id")
    implementation_version = metadata.get("version")

    if not isinstance(implementation_id, str) or not implementation_id:
        raise ManifestError("metadata.id must be a non-empty string")

    if (
        not isinstance(implementation_version, str)
        or not implementation_version
    ):
        raise ManifestError(
            "metadata.version must be a non-empty string"
        )

    provides = spec.get("provides")

    if not isinstance(provides, list) or not provides:
        raise ManifestError("spec.provides must be a non-empty list")

    first = provides[0]

    if not isinstance(first, str) or "@" not in first:
        raise ManifestError(
            "spec.provides entries must look like capability@version"
        )

    capability, capability_version = first.split("@", 1)

    if not capability or not capability_version:
        raise ManifestError("Invalid capability@version in provides")

    security = spec.get("security")
    if not isinstance(security, dict):
        raise ManifestError("spec.security must be a mapping")

    risk = security.get("risk")
    if not isinstance(risk, str) or not risk:
        raise ManifestError("spec.security.risk must be a string")

    runtime = spec.get("runtime")
    if not isinstance(runtime, dict):
        raise ManifestError("spec.runtime must be a mapping")

    required = _string_list(
        runtime.get("requiredExecutables"),
        "spec.runtime.requiredExecutables",
    )

    mcp = spec.get("mcp")
    if not isinstance(mcp, dict):
        raise ManifestError("spec.mcp must be a mapping")

    allowed = _string_list(mcp.get("allowedTools"), "spec.mcp.allowedTools")
    if allowed != ALLOWED_MCP_TOOLS:
        raise ManifestError(
            "spec.mcp.allowedTools must be exactly "
            "synology_list_nas and synology_health_summary"
        )

    upstream = spec.get("upstream")
    if not isinstance(upstream, dict):
        raise ManifestError("spec.upstream must be a mapping")

    repository = upstream.get("repository")
    revision = upstream.get("revision")
    if not isinstance(repository, str) or not isinstance(revision, str):
        raise ManifestError(
            "spec.upstream.repository and revision are required"
        )

    pinned = f"{repository}@{revision}"
    if pinned != UPSTREAM_PIN:
        raise ManifestError(
            "spec.upstream revision is not the pinned SHA"
        )

    target = spec.get("target")
    if not isinstance(target, dict):
        raise ManifestError("spec.target must be a mapping")

    resolution = target.get("resolution")
    if resolution != "bindings":
        raise ManifestError(
            "spec.target.resolution must be bindings"
        )

    return ToolManifest(
        implementation_id=implementation_id,
        implementation_version=implementation_version,
        capability=capability,
        capability_version=capability_version,
        risk=risk,
        required_executables=required,
        allowed_mcp_tools=allowed,
        upstream=pinned,
        target_resolution=resolution,
        path=manifest_path,
    )
