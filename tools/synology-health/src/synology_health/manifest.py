"""Load the ToolImplementation manifest (tool.yaml) for runtime use."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ManifestError(ValueError):
    """Raised when the tool manifest is missing or invalid."""


@dataclass(frozen=True)
class SecretRef:
    ref: str
    type: str


@dataclass(frozen=True)
class ToolManifest:
    implementation_id: str
    implementation_version: str
    capability: str
    capability_version: str
    risk: str
    required_executables: tuple[str, ...]
    secret_refs: tuple[SecretRef, ...]
    target_ref: str | None
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
    return (
        Path(__file__).resolve().parents[2] / "tool.yaml"
    )


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

    required = runtime.get("requiredExecutables")

    if not isinstance(required, list) or not required:
        raise ManifestError(
            "spec.runtime.requiredExecutables must be a non-empty list"
        )

    executables: list[str] = []

    for item in required:
        if not isinstance(item, str) or not item:
            raise ManifestError(
                "requiredExecutables entries must be non-empty strings"
            )
        executables.append(item)

    secret_items = security.get("secrets")
    secret_refs: list[SecretRef] = []

    if secret_items is not None:
        if not isinstance(secret_items, list):
            raise ManifestError("spec.security.secrets must be a list")

        for item in secret_items:
            if not isinstance(item, dict):
                raise ManifestError(
                    "Each secret entry must be a mapping"
                )

            ref = item.get("ref")
            secret_type = item.get("type")

            if not isinstance(ref, str) or not ref:
                raise ManifestError("secret.ref must be a string")

            if not isinstance(secret_type, str) or not secret_type:
                raise ManifestError("secret.type must be a string")

            secret_refs.append(
                SecretRef(ref=ref, type=secret_type)
            )

    target = spec.get("target")
    target_ref = None

    if isinstance(target, dict):
        ref = target.get("ref")
        if isinstance(ref, str) and ref:
            target_ref = ref

    network_intent = security.get("networkIntent")
    network_target_ref = None

    if isinstance(network_intent, dict):
        intent_ref = network_intent.get("targetRef")
        if isinstance(intent_ref, str) and intent_ref:
            network_target_ref = intent_ref

    if (
        target_ref is not None
        and network_target_ref is not None
        and target_ref != network_target_ref
    ):
        raise ManifestError(
            "spec.target.ref and security.networkIntent.targetRef "
            "disagree"
        )

    if target_ref is None and network_target_ref is not None:
        target_ref = network_target_ref

    if target_ref is None:
        raise ManifestError(
            "Tool manifest must declare spec.target.ref"
        )

    return ToolManifest(
        implementation_id=implementation_id,
        implementation_version=implementation_version,
        capability=capability,
        capability_version=capability_version,
        risk=risk,
        required_executables=tuple(executables),
        secret_refs=tuple(secret_refs),
        target_ref=target_ref,
        path=manifest_path,
    )


def resolve_secret_ref(
    manifest: ToolManifest,
    target_id: str,
) -> str:
    """Map targetId + manifest secret declarations to a logical secretRef."""
    if not manifest.secret_refs:
        raise ManifestError(
            "Tool manifest declares no secret references"
        )

    matching = [
        secret.ref
        for secret in manifest.secret_refs
        if secret.ref.startswith(f"{target_id}-")
        or secret.ref == target_id
    ]

    if len(matching) == 1:
        return matching[0]

    if len(manifest.secret_refs) == 1:
        return manifest.secret_refs[0].ref

    raise ManifestError(
        f"Unable to resolve secretRef for targetId={target_id}"
    )
