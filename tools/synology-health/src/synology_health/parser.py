"""Parse a restricted Net-SNMP result into storage.health."""

import json
import re

from . import oids
from .models import (
    DiskHealth,
    Issue,
    OverallHealth,
    StorageHealth,
    StorageItemHealth,
    SystemHealth,
    TargetHealth,
)


_LINE_RE = re.compile(
    r"^\.(?P<oid>\d+(?:\.\d+)*) = "
    r"(?P<type>INTEGER|STRING|Counter64): "
    r"(?P<value>.*)$"
)


class ParseError(ValueError):
    """Raised when an expected SNMP health response cannot be parsed."""


def _parse_value(value_type: str, raw: str):
    if value_type in {"INTEGER", "Counter64"}:
        try:
            return int(raw)
        except ValueError as exc:
            raise ParseError(
                f"Invalid integer value: {raw!r}"
            ) from exc

    if value_type == "STRING":
        if raw.startswith('"') and raw.endswith('"'):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw[1:-1]
        return raw

    raise ParseError(f"Unsupported SNMP type: {value_type}")


def parse_varbinds(text: str) -> dict[str, object]:
    values: dict[str, object] = {}

    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        match = _LINE_RE.match(line)
        if not match:
            raise ParseError(
                f"Malformed SNMP line {line_number}: {line!r}"
            )

        oid = match.group("oid")
        values[oid] = _parse_value(
            match.group("type"),
            match.group("value"),
        )

    return values


def _required(values: dict[str, object], oid: str):
    if oid not in values:
        raise ParseError(f"Required OID missing: {oid}")
    return values[oid]


def _enum(mapping: dict[int, str], raw: object) -> str:
    if not isinstance(raw, int):
        raise ParseError(f"Expected integer enum, got {raw!r}")
    return mapping.get(raw, f"unknown:{raw}")


def _table_rows(
    values: dict[str, object],
    prefix: str,
    fields: dict[int, str],
) -> dict[int, dict[str, object]]:
    rows: dict[int, dict[str, object]] = {}
    table_prefix = prefix + "."

    for oid, value in values.items():
        if not oid.startswith(table_prefix):
            continue

        suffix = oid[len(table_prefix):]
        parts = suffix.split(".")

        if len(parts) != 2:
            continue

        try:
            field_id = int(parts[0])
            row_id = int(parts[1])
        except ValueError:
            continue

        field_name = fields.get(field_id)
        if field_name is None:
            continue

        rows.setdefault(row_id, {})[field_name] = value

    return rows


def _row_required(
    row: dict[str, object],
    field: str,
    row_description: str,
):
    if field not in row:
        raise ParseError(
            f"{row_description} missing required field: {field}"
        )
    return row[field]


def _storage_kind(name: str) -> str:
    """Best-effort label only.

    Synology RAID MIB identifies all rows as storage items but does not
    expose a volume/pool type field. Known DSM English names are
    classified for readability; all other names remain unknown.
    """
    normalized = name.casefold()

    if normalized.startswith("volume "):
        return "volume"

    if normalized.startswith("storage pool "):
        return "pool"

    return "unknown"


def _status_issue(
    issues: list[Issue],
    resource: str,
    field: str,
    status: str,
):
    if status == "normal":
        return

    issues.append(
        Issue(
            code=f"{field}_abnormal",
            severity="critical",
            resource=resource,
            message=f"{field} is {status}",
        )
    )


def parse_health_walk(text: str) -> StorageHealth:
    values = parse_varbinds(text)

    system_status = _enum(
        oids.NORMAL_FAILED,
        _required(values, oids.SYSTEM["status"]),
    )
    power_status = _enum(
        oids.NORMAL_FAILED,
        _required(values, oids.SYSTEM["power_status"]),
    )
    system_fan_status = _enum(
        oids.NORMAL_FAILED,
        _required(values, oids.SYSTEM["system_fan_status"]),
    )
    cpu_fan_status = _enum(
        oids.NORMAL_FAILED,
        _required(values, oids.SYSTEM["cpu_fan_status"]),
    )
    thermal_status = _enum(
        oids.NORMAL_FAILED,
        _required(values, oids.SYSTEM["thermal_status"]),
    )

    target = TargetHealth(
        reachable=True,
        model=str(
            _required(values, oids.SYSTEM["model"])
        ),
        dsm_version=str(
            _required(values, oids.SYSTEM["dsm_version"])
        ),
    )

    system = SystemHealth(
        status=system_status,
        temperature_c=int(
            _required(values, oids.SYSTEM["temperature_c"])
        ),
        power_status=power_status,
        system_fan_status=system_fan_status,
        cpu_fan_status=cpu_fan_status,
        thermal_status=thermal_status,
        cpu_utilization_pct=int(
            _required(values, oids.SYSTEM["cpu_utilization_pct"])
        ),
        memory_utilization_pct=int(
            _required(values, oids.SYSTEM["memory_utilization_pct"])
        ),
    )

    disk_rows = _table_rows(
        values,
        oids.DISK_TABLE,
        oids.DISK_FIELDS,
    )

    disks: list[DiskHealth] = []

    for row_id in sorted(disk_rows):
        row = disk_rows[row_id]
        description = f"disk row {row_id}"

        # diskName (.12) is authoritative for DSM 7+.
        # diskID (.2) is deprecated and intentionally not exposed.
        name = str(
            _row_required(row, "name", description)
        )

        disks.append(
            DiskHealth(
                name=name,
                model=str(
                    _row_required(row, "model", description)
                ),
                type=str(
                    _row_required(row, "type", description)
                ),
                role=str(
                    _row_required(row, "role", description)
                ),
                status=_enum(
                    oids.DISK_STATUS,
                    _row_required(row, "status", description),
                ),
                health_status=_enum(
                    oids.DISK_HEALTH_STATUS,
                    _row_required(
                        row,
                        "health_status",
                        description,
                    ),
                ),
                temperature_c=int(
                    _row_required(
                        row,
                        "temperature_c",
                        description,
                    )
                ),
                bad_sectors=int(
                    _row_required(
                        row,
                        "bad_sectors",
                        description,
                    )
                ),
                retry_count=int(
                    _row_required(
                        row,
                        "retry_count",
                        description,
                    )
                ),
                identify_fail_count=int(
                    _row_required(
                        row,
                        "identify_fail_count",
                        description,
                    )
                ),
            )
        )

    storage_rows = _table_rows(
        values,
        oids.STORAGE_TABLE,
        oids.STORAGE_FIELDS,
    )

    storage_items: list[StorageItemHealth] = []

    for row_id in sorted(storage_rows):
        row = storage_rows[row_id]
        description = f"storage row {row_id}"

        name = str(
            _row_required(row, "name", description)
        )

        storage_items.append(
            StorageItemHealth(
                name=name,
                kind=_storage_kind(name),
                status=_enum(
                    oids.STORAGE_STATUS,
                    _row_required(
                        row,
                        "status",
                        description,
                    ),
                ),
                free_bytes=int(
                    _row_required(
                        row,
                        "free_bytes",
                        description,
                    )
                ),
                total_bytes=int(
                    _row_required(
                        row,
                        "total_bytes",
                        description,
                    )
                ),
            )
        )

    issues: list[Issue] = []

    _status_issue(
        issues,
        "system",
        "system_status",
        system.status,
    )
    _status_issue(
        issues,
        "system",
        "power_status",
        system.power_status,
    )
    _status_issue(
        issues,
        "system",
        "system_fan_status",
        system.system_fan_status,
    )
    _status_issue(
        issues,
        "system",
        "cpu_fan_status",
        system.cpu_fan_status,
    )
    _status_issue(
        issues,
        "system",
        "thermal_status",
        system.thermal_status,
    )

    for disk in disks:
        _status_issue(
            issues,
            f"disk:{disk.name}",
            "disk_status",
            disk.status,
        )

        if disk.health_status != "normal":
            severity = (
                "warning"
                if disk.health_status == "warning"
                else "critical"
            )

            issues.append(
                Issue(
                    code="disk_health_abnormal",
                    severity=severity,
                    resource=f"disk:{disk.name}",
                    message=(
                        "disk health is "
                        f"{disk.health_status}"
                    ),
                )
            )

        if disk.bad_sectors > 0:
            issues.append(
                Issue(
                    code="disk_bad_sectors",
                    severity="warning",
                    resource=f"disk:{disk.name}",
                    message=(
                        f"{disk.bad_sectors} bad sectors reported"
                    ),
                )
            )

    storage_critical = {
        "degraded",
        "crashed",
        "unknown_status",
    }

    for item in storage_items:
        if item.status == "normal":
            continue

        severity = (
            "critical"
            if (
                item.status in storage_critical
                or item.status.startswith("unknown:")
            )
            else "warning"
        )

        issues.append(
            Issue(
                code="storage_status_abnormal",
                severity=severity,
                resource=f"storage:{item.name}",
                message=f"storage status is {item.status}",
            )
        )

    healthy = not any(
        issue.severity in {"critical", "error"}
        for issue in issues
    )

    return StorageHealth(
        target=target,
        system=system,
        disks=tuple(disks),
        storage_items=tuple(storage_items),
        overall=OverallHealth(
            healthy=healthy,
            issues=tuple(issues),
        ),
    )
