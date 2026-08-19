"""Implementation-local target validation for synology-mcp.

Governance resource resolution is out of scope. This module only checks
that a caller-supplied targetRef maps to exactly one NAS in the
configured MCP backend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .errors import TargetResolutionError


TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"

_ABSOLUTE_UNIX = re.compile(r"^/")
_WINDOWS_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")
_UNC = re.compile(r"^\\\\")
_RELATIVE = re.compile(r"^\.\.?(?:/|\\)")
_DEVICE_DISK = re.compile(r"(?:^|/)(?:dev/)?mmcblk\d")

_LOCAL_PREFIXES = (
    "/workspace",
    "/dev/",
    "/proc/",
    "/sys/",
    "/tmp/",
    "/home/",
    "/users/",
    "/var/",
    "/mnt/",
    "/media/",
    "/opt/",
    "/root/",
)


@dataclass(frozen=True)
class NasCandidate:
    nas_name: str
    connected: bool | None = None


def is_local_filesystem_ref(target_ref: str) -> bool:
    """True when target_ref looks like a local path, never a NAS name."""
    value = target_ref.strip()
    if not value:
        return False

    if (
        _ABSOLUTE_UNIX.match(value)
        or _WINDOWS_DRIVE.match(value)
        or _UNC.match(value)
        or _RELATIVE.match(value)
    ):
        return True

    lowered = value.lower()
    if _DEVICE_DISK.search(lowered):
        return True

    return any(lowered.startswith(prefix) for prefix in _LOCAL_PREFIXES)


def parse_nas_list(payload: Any) -> list[NasCandidate]:
    """Extract NAS entries from synology_list_nas JSON.

    Message-only objects from the upstream tool are ignored. Entries
    without a nas_name cannot be matched and are skipped.
    """
    if isinstance(payload, dict):
        for key in ("data", "nas", "items"):
            nested = payload.get(key)
            if isinstance(nested, list):
                payload = nested
                break
        else:
            payload = [payload]

    if not isinstance(payload, list):
        raise TargetResolutionError(
            TARGET_NOT_FOUND,
            "Backend NAS list is not a JSON array",
        )

    candidates: list[NasCandidate] = []

    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("nas_name")
        if not isinstance(name, str) or not name.strip():
            continue
        connected = item.get("connected")
        candidates.append(
            NasCandidate(
                nas_name=name.strip(),
                connected=connected if isinstance(connected, bool) else None,
            )
        )

    return candidates


def resolve_nas_name(
    *,
    target_ref: str,
    mapped_nas_name: str,
    nas_list: Any,
) -> NasCandidate:
    """Exact-match target validation against the configured MCP NAS list."""
    if is_local_filesystem_ref(target_ref):
        raise TargetResolutionError(
            TARGET_NOT_FOUND,
            "targetRef is not a configured storage target",
        )

    if is_local_filesystem_ref(mapped_nas_name):
        raise TargetResolutionError(
            TARGET_NOT_FOUND,
            "targetRef is not a configured storage target",
        )

    candidates = parse_nas_list(nas_list)
    matches = [
        item
        for item in candidates
        if item.nas_name == mapped_nas_name
    ]

    if len(matches) == 1:
        return matches[0]

    if len(matches) == 0:
        raise TargetResolutionError(
            TARGET_NOT_FOUND,
            "targetRef does not match a configured NAS",
        )

    raise TargetResolutionError(
        TARGET_AMBIGUOUS,
        "targetRef matches more than one configured NAS",
    )
