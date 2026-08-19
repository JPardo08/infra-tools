"""Typed public model for storage.health@1.0.0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


OVERALL_HEALTHY = "HEALTHY"
OVERALL_ATTENTION = "ATTENTION"
OVERALL_CRITICAL = "CRITICAL"
OVERALL_UNKNOWN = "UNKNOWN"

OVERALL_STATUSES = frozenset(
    {
        OVERALL_HEALTHY,
        OVERALL_ATTENTION,
        OVERALL_CRITICAL,
        OVERALL_UNKNOWN,
    }
)


def _omit_nulls(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    resource: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "resource": self.resource,
            "message": self.message,
        }


@dataclass(frozen=True)
class Capacity:
    total_bytes: int | None
    used_bytes: int | None
    available_bytes: int | None
    utilization_pct: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "totalBytes": self.total_bytes,
            "usedBytes": self.used_bytes,
            "availableBytes": self.available_bytes,
            "utilizationPct": self.utilization_pct,
        }


@dataclass(frozen=True)
class SystemHealth:
    reachable: bool
    model: str | None
    firmware_version: str | None
    status: str | None
    temperature_c: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "model": self.model,
            "firmwareVersion": self.firmware_version,
            "status": self.status,
            "temperatureC": self.temperature_c,
        }


@dataclass(frozen=True)
class PoolHealth:
    name: str
    status: str | None
    raid: str | None
    total_bytes: int | None
    used_bytes: int | None
    rebuilding: bool | None
    degraded: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "raid": self.raid,
            "totalBytes": self.total_bytes,
            "usedBytes": self.used_bytes,
            "rebuilding": self.rebuilding,
            "degraded": self.degraded,
        }


@dataclass(frozen=True)
class VolumeHealth:
    name: str
    status: str | None
    filesystem: str | None
    total_bytes: int | None
    used_bytes: int | None
    available_bytes: int | None
    utilization_pct: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "filesystem": self.filesystem,
            "totalBytes": self.total_bytes,
            "usedBytes": self.used_bytes,
            "availableBytes": self.available_bytes,
            "utilizationPct": self.utilization_pct,
        }


@dataclass(frozen=True)
class DiskHealth:
    name: str
    model: str | None
    type: str | None
    status: str | None
    smart_status: str | None
    temperature_c: int | None
    size_bytes: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "type": self.type,
            "status": self.status,
            "smartStatus": self.smart_status,
            "temperatureC": self.temperature_c,
            "sizeBytes": self.size_bytes,
        }


@dataclass(frozen=True)
class Temperatures:
    system_c: int | None
    disks: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "systemC": self.system_c,
            "disks": list(self.disks),
        }


@dataclass(frozen=True)
class OverallHealth:
    status: str
    issues: tuple[Issue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "healthy": self.status == OVERALL_HEALTHY,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class ImplementationInfo:
    id: str
    version: str
    upstream: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "upstream": self.upstream,
        }


@dataclass(frozen=True)
class Evidence:
    collected_at: str
    backend: str
    sources: tuple[str, ...]
    matched_target: str | None
    unavailable_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "collectedAt": self.collected_at,
            "backend": self.backend,
            "sources": list(self.sources),
            "matchedTarget": self.matched_target,
            "unavailableFields": list(self.unavailable_fields),
        }


@dataclass(frozen=True)
class StorageHealth:
    target_ref: str
    timestamp: str
    overall: OverallHealth
    capacity: Capacity
    system: SystemHealth
    pools: tuple[PoolHealth, ...]
    volumes: tuple[VolumeHealth, ...]
    disks: tuple[DiskHealth, ...]
    temperatures: Temperatures
    warnings: tuple[Issue, ...]
    unavailable_fields: tuple[str, ...]
    evidence: Evidence
    implementation: ImplementationInfo

    def to_dict(self) -> dict[str, Any]:
        return _omit_nulls(
            {
                "targetRef": self.target_ref,
                "timestamp": self.timestamp,
                "overall": self.overall.to_dict(),
                "capacity": self.capacity.to_dict(),
                "system": self.system.to_dict(),
                "pools": [item.to_dict() for item in self.pools],
                "volumes": [item.to_dict() for item in self.volumes],
                "disks": [item.to_dict() for item in self.disks],
                "temperatures": self.temperatures.to_dict(),
                "warnings": [item.to_dict() for item in self.warnings],
                "unavailableFields": list(self.unavailable_fields),
                "evidence": self.evidence.to_dict(),
                "implementation": self.implementation.to_dict(),
            }
        )
