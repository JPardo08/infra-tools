"""Normalize a Synology MCP health_summary into storage.health@1.0.0."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .manifest import UPSTREAM_PIN
from .models import (
    OVERALL_ATTENTION,
    OVERALL_CRITICAL,
    OVERALL_HEALTHY,
    OVERALL_UNKNOWN,
    Capacity,
    DiskHealth,
    Evidence,
    ImplementationInfo,
    Issue,
    OverallHealth,
    PoolHealth,
    StorageHealth,
    SystemHealth,
    Temperatures,
    VolumeHealth,
)


_CRITICAL_STATUSES = frozenset(
    {
        "critical",
        "crashed",
        "crash",
        "error",
        "fail",
        "failed",
        "failure",
        "offline",
        "dead",
    }
)

_ATTENTION_STATUSES = frozenset(
    {
        "warning",
        "attention",
        "degraded",
        "crashed_degraded",
        "repairing",
        "rebuilding",
        "background",
        "sys_background",
        "warning_background",
        "data_scrubbing",
    }
)

_HEALTHY_STATUSES = frozenset(
    {
        "normal",
        "healthy",
        "ok",
        "good",
        "ready",
    }
)

_REBUILDING_STATUSES = frozenset(
    {
        "repairing",
        "rebuilding",
        "background",
        "sys_background",
    }
)

_DEGRADED_STATUSES = frozenset(
    {
        "degraded",
        "crashed_degraded",
        "warning",
    }
)


def _utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _as_mapping(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                return int(float(text))
            except ValueError:
                return None
    if isinstance(value, dict):
        return _as_int(
            _first_present(value, "total", "used", "avail", "available")
        )
    return None


def _size_parts(value: Any) -> tuple[int | None, int | None, int | None]:
    if isinstance(value, dict):
        total = _as_int(_first_present(value, "total", "total_byte", "totalBytes"))
        used = _as_int(_first_present(value, "used", "used_byte", "usedBytes"))
        available = _as_int(
            _first_present(
                value,
                "available",
                "avail",
                "free",
                "free_byte",
                "availableBytes",
            )
        )
        return (total, used, available)
    total = _as_int(value)
    return (total, None, None)


def _utilization_pct(
    total: int | None,
    used: int | None,
    available: int | None,
    reported: Any = None,
) -> int | None:
    reported_pct = _as_int(reported)
    if reported_pct is not None:
        return reported_pct
    if total is not None and total > 0 and used is not None:
        return int(round((used / total) * 100))
    if total is not None and total > 0 and available is not None:
        used_est = total - available
        return int(round((used_est / total) * 100))
    return None


def _complete_capacity(
    total: int | None,
    used: int | None,
    available: int | None,
) -> tuple[int | None, int | None, int | None]:
    if total is not None and used is not None and available is None:
        available = max(total - used, 0)
    elif total is not None and available is not None and used is None:
        used = max(total - available, 0)
    elif used is not None and available is not None and total is None:
        total = used + available
    return (total, used, available)


def _norm_status(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lower()


def _status_rank(status: str | None) -> str | None:
    if status is None:
        return None
    if status in _CRITICAL_STATUSES:
        return OVERALL_CRITICAL
    if status in _ATTENTION_STATUSES:
        return OVERALL_ATTENTION
    if status in _HEALTHY_STATUSES:
        return OVERALL_HEALTHY
    return OVERALL_ATTENTION


def _section(summary: dict[str, Any], *names: str) -> dict[str, Any] | None:
    for name in names:
        value = summary.get(name)
        mapping = _as_mapping(value)
        if mapping is not None:
            return mapping
    return None


def _named_list(section: dict[str, Any] | None, *keys: str) -> list[Any]:
    if section is None:
        return []
    for key in keys:
        value = section.get(key)
        if isinstance(value, list):
            return value
    # Some DSM payloads are already a list stored one level up.
    return []


def _issue(
    code: str,
    severity: str,
    resource: str,
    message: str,
) -> Issue:
    return Issue(
        code=code,
        severity=severity,
        resource=resource,
        message=message,
    )


def normalize_health_summary(
    *,
    target_ref: str,
    matched_nas_name: str,
    summary: Any,
    implementation_id: str,
    implementation_version: str,
    collected_at: str | None = None,
) -> StorageHealth:
    unavailable: list[str] = []
    issues: list[Issue] = []
    timestamp = collected_at or _utcnow()

    root = _as_mapping(summary) or {}
    data = _as_mapping(root.get("data")) if "data" in root else root
    if data is None:
        data = {}
        unavailable.append("summary")

    if root.get("success") is False:
        issues.append(
            _issue(
                "backend_unsuccessful",
                "critical",
                "backend",
                "health summary reported failure",
            )
        )

    system_raw = _section(data, "system")
    util_raw = _section(data, "utilization")
    disks_section = _section(data, "disks")
    volumes_section = _section(data, "volumes")
    pools_section = _section(data, "storage_pools", "pools")

    if system_raw is None:
        unavailable.append("system")
    if util_raw is None:
        unavailable.append("utilization")
    if disks_section is None:
        unavailable.append("disks")
    if volumes_section is None:
        unavailable.append("volumes")
    if pools_section is None:
        unavailable.append("pools")

    model = None
    firmware = None
    system_temp = None
    if system_raw is not None:
        model = _first_present(system_raw, "model", "model_name")
        if model is not None:
            model = str(model)
        firmware = _first_present(
            system_raw,
            "firmware_ver",
            "firmware",
            "version_string",
            "version",
        )
        if firmware is not None:
            firmware = str(firmware)
        system_temp = _as_int(
            _first_present(system_raw, "sys_temp", "temperature", "temp")
        )
        nas_time = _first_present(system_raw, "time", "date_time")
        if isinstance(nas_time, str) and nas_time.strip():
            timestamp = nas_time.strip()

    disks: list[DiskHealth] = []
    disk_temps: list[dict[str, Any]] = []
    for raw_disk in _named_list(disks_section, "disks", "hdd_info", "hddinfo"):
        if not isinstance(raw_disk, dict):
            continue
        name = _first_present(
            raw_disk,
            "name",
            "disk",
            "id",
            "device",
        )
        if name is None:
            continue
        name_text = str(name)
        status = _norm_status(_first_present(raw_disk, "status"))
        smart = _first_present(
            raw_disk,
            "smart_status",
            "smartStatus",
            "smart",
        )
        smart_status = None
        if isinstance(smart, dict):
            smart_status = _norm_status(
                _first_present(smart, "status", "overall")
            )
        elif isinstance(smart, str):
            smart_status = _norm_status(smart)
        temperature = _as_int(
            _first_present(raw_disk, "temp", "temperature", "temperatureC")
        )
        disk = DiskHealth(
            name=name_text,
            model=(
                str(value)
                if (value := _first_present(raw_disk, "model", "vendor"))
                is not None
                else None
            ),
            type=(
                str(value)
                if (
                    value := _first_present(
                        raw_disk,
                        "diskType",
                        "type",
                        "interface",
                    )
                )
                is not None
                else None
            ),
            status=status,
            smart_status=smart_status,
            temperature_c=temperature,
            size_bytes=_as_int(
                _first_present(
                    raw_disk,
                    "size_total",
                    "size",
                    "total",
                    "capacity",
                )
            ),
        )
        disks.append(disk)
        if temperature is not None:
            disk_temps.append({"name": name_text, "celsius": temperature})

        rank = _status_rank(status)
        if rank == OVERALL_CRITICAL:
            issues.append(
                _issue(
                    "disk_status_abnormal",
                    "critical",
                    f"disk:{name_text}",
                    f"disk status is {status}",
                )
            )
        elif rank == OVERALL_ATTENTION:
            issues.append(
                _issue(
                    "disk_status_abnormal",
                    "warning",
                    f"disk:{name_text}",
                    f"disk status is {status}",
                )
            )

        smart_rank = _status_rank(smart_status)
        if smart_rank == OVERALL_CRITICAL:
            issues.append(
                _issue(
                    "disk_smart_abnormal",
                    "critical",
                    f"disk:{name_text}",
                    f"SMART status is {smart_status}",
                )
            )
        elif smart_rank == OVERALL_ATTENTION:
            issues.append(
                _issue(
                    "disk_smart_abnormal",
                    "warning",
                    f"disk:{name_text}",
                    f"SMART status is {smart_status}",
                )
            )

    volumes: list[VolumeHealth] = []
    for raw_volume in _named_list(volumes_section, "volumes"):
        if not isinstance(raw_volume, dict):
            continue
        name = _first_present(
            raw_volume,
            "volume_path",
            "id",
            "name",
            "desc",
        )
        if name is None:
            continue
        name_text = str(name)
        status = _norm_status(_first_present(raw_volume, "status"))
        size_value = _first_present(raw_volume, "size")
        total, used, available = _size_parts(size_value)
        if total is None:
            total = _as_int(_first_present(raw_volume, "total", "size_total"))
        if used is None:
            used = _as_int(_first_present(raw_volume, "used", "used_size"))
        total, used, available = _complete_capacity(total, used, available)
        volume = VolumeHealth(
            name=name_text,
            status=status,
            filesystem=(
                str(value)
                if (
                    value := _first_present(
                        raw_volume,
                        "fs_type",
                        "fstype",
                        "filesystem",
                    )
                )
                is not None
                else None
            ),
            total_bytes=total,
            used_bytes=used,
            available_bytes=available,
            utilization_pct=_utilization_pct(
                total,
                used,
                available,
                _first_present(
                    raw_volume,
                    "used_size_pct",
                    "used_pct",
                    "utilization",
                ),
            ),
        )
        volumes.append(volume)
        rank = _status_rank(status)
        if rank == OVERALL_CRITICAL:
            issues.append(
                _issue(
                    "volume_status_abnormal",
                    "critical",
                    f"volume:{name_text}",
                    f"volume status is {status}",
                )
            )
        elif rank == OVERALL_ATTENTION:
            issues.append(
                _issue(
                    "volume_status_abnormal",
                    "warning",
                    f"volume:{name_text}",
                    f"volume status is {status}",
                )
            )

    pools: list[PoolHealth] = []
    for raw_pool in _named_list(
        pools_section,
        "pools",
        "storagePools",
        "storage_pools",
    ):
        if not isinstance(raw_pool, dict):
            continue
        name = _first_present(raw_pool, "id", "name", "desc", "pool_path")
        if name is None:
            continue
        name_text = str(name)
        status = _norm_status(_first_present(raw_pool, "status"))
        raid = _first_present(
            raw_pool,
            "raid_type",
            "device_type",
            "raid",
            "pool_type",
        )
        total, used, available = _size_parts(
            _first_present(raw_pool, "size")
        )
        total, used, _available = _complete_capacity(total, used, available)
        pools.append(
            PoolHealth(
                name=name_text,
                status=status,
                raid=str(raid) if raid is not None else None,
                total_bytes=total,
                used_bytes=used,
                rebuilding=(
                    status in _REBUILDING_STATUSES
                    if status is not None
                    else None
                ),
                degraded=(
                    status in _DEGRADED_STATUSES
                    if status is not None
                    else None
                ),
            )
        )
        rank = _status_rank(status)
        if rank == OVERALL_CRITICAL:
            issues.append(
                _issue(
                    "pool_status_abnormal",
                    "critical",
                    f"pool:{name_text}",
                    f"pool status is {status}",
                )
            )
        elif rank == OVERALL_ATTENTION:
            issues.append(
                _issue(
                    "pool_status_abnormal",
                    "warning",
                    f"pool:{name_text}",
                    f"pool status is {status}",
                )
            )

    cap_total = 0
    cap_used = 0
    have_capacity = False
    for volume in volumes:
        if volume.total_bytes is not None:
            cap_total += volume.total_bytes
            have_capacity = True
        if volume.used_bytes is not None:
            cap_used += volume.used_bytes
            have_capacity = True

    if not have_capacity:
        for pool in pools:
            if pool.total_bytes is not None:
                cap_total += pool.total_bytes
                have_capacity = True
            if pool.used_bytes is not None:
                cap_used += pool.used_bytes
                have_capacity = True

    if have_capacity:
        total_bytes = cap_total if cap_total else None
        used_bytes = cap_used if volumes or pools else None
        if used_bytes == 0 and not any(
            item.used_bytes is not None for item in volumes
        ) and not any(item.used_bytes is not None for item in pools):
            used_bytes = None
        total_bytes, used_bytes, available_bytes = _complete_capacity(
            total_bytes,
            used_bytes,
            None,
        )
        capacity = Capacity(
            total_bytes=total_bytes,
            used_bytes=used_bytes,
            available_bytes=available_bytes,
            utilization_pct=_utilization_pct(
                total_bytes,
                used_bytes,
                available_bytes,
            ),
        )
    else:
        capacity = Capacity(None, None, None, None)
        unavailable.append("capacity")

    if disks_section is not None and not disks:
        unavailable.append("disks.items")
    if volumes_section is not None and not volumes:
        unavailable.append("volumes.items")
    if pools_section is not None and not pools:
        unavailable.append("pools.items")

    if any(disk.smart_status is None for disk in disks):
        unavailable.append("disks.smartStatus")

    overall_status = OVERALL_HEALTHY
    if any(issue.severity == "critical" for issue in issues):
        overall_status = OVERALL_CRITICAL
    elif any(issue.severity in {"warning", "error"} for issue in issues):
        overall_status = OVERALL_ATTENTION

    storage_unreadable = (
        disks_section is None
        and volumes_section is None
        and pools_section is None
    )
    if storage_unreadable or "summary" in unavailable:
        if overall_status == OVERALL_HEALTHY:
            overall_status = OVERALL_UNKNOWN
        issues.append(
            _issue(
                "health_unverifiable",
                "warning",
                "backend",
                "storage health could not be fully verified",
            )
        )

    warnings = tuple(
        issue
        for issue in issues
        if issue.severity in {"warning", "critical", "error"}
    )

    evidence = Evidence(
        collected_at=timestamp,
        backend="mcp",
        sources=("list_nas", "health_summary"),
        matched_target=matched_nas_name,
        unavailable_fields=tuple(unavailable),
    )

    return StorageHealth(
        target_ref=target_ref,
        timestamp=timestamp,
        overall=OverallHealth(
            status=overall_status,
            issues=tuple(issues),
        ),
        capacity=capacity,
        system=SystemHealth(
            reachable=True,
            model=model,
            firmware_version=firmware,
            status="reachable",
            temperature_c=system_temp,
        ),
        pools=tuple(pools),
        volumes=tuple(volumes),
        disks=tuple(disks),
        temperatures=Temperatures(
            system_c=system_temp,
            disks=tuple(disk_temps),
        ),
        warnings=warnings,
        unavailable_fields=tuple(unavailable),
        evidence=evidence,
        implementation=ImplementationInfo(
            id=implementation_id,
            version=implementation_version,
            upstream=UPSTREAM_PIN,
        ),
    )
