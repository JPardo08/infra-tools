"""Typed public model for the storage.health capability."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    resource: str
    message: str

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "resource": self.resource,
            "message": self.message,
        }


@dataclass(frozen=True)
class TargetHealth:
    reachable: bool
    model: str
    dsm_version: str

    def to_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "model": self.model,
            "dsmVersion": self.dsm_version,
        }


@dataclass(frozen=True)
class SystemHealth:
    status: str
    temperature_c: int
    power_status: str
    system_fan_status: str
    cpu_fan_status: str
    thermal_status: str
    cpu_utilization_pct: int
    memory_utilization_pct: int

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "temperatureC": self.temperature_c,
            "powerStatus": self.power_status,
            "systemFanStatus": self.system_fan_status,
            "cpuFanStatus": self.cpu_fan_status,
            "thermalStatus": self.thermal_status,
            "cpuUtilizationPct": self.cpu_utilization_pct,
            "memoryUtilizationPct": self.memory_utilization_pct,
        }


@dataclass(frozen=True)
class DiskHealth:
    name: str
    model: str
    type: str
    role: str
    status: str
    health_status: str
    temperature_c: int
    bad_sectors: int
    retry_count: int
    identify_fail_count: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "model": self.model,
            "type": self.type,
            "role": self.role,
            "status": self.status,
            "healthStatus": self.health_status,
            "temperatureC": self.temperature_c,
            "badSectors": self.bad_sectors,
            "retryCount": self.retry_count,
            "identifyFailCount": self.identify_fail_count,
        }


@dataclass(frozen=True)
class StorageItemHealth:
    name: str
    kind: str
    status: str
    free_bytes: int
    total_bytes: int

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "freeBytes": self.free_bytes,
            "totalBytes": self.total_bytes,
        }


@dataclass(frozen=True)
class OverallHealth:
    healthy: bool
    issues: tuple[Issue, ...]

    def to_dict(self) -> dict:
        return {
            "healthy": self.healthy,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class StorageHealth:
    target: TargetHealth
    system: SystemHealth
    disks: tuple[DiskHealth, ...]
    storage_items: tuple[StorageItemHealth, ...]
    overall: OverallHealth

    def to_dict(self) -> dict:
        return {
            "target": self.target.to_dict(),
            "system": self.system.to_dict(),
            "disks": [disk.to_dict() for disk in self.disks],
            "storageItems": [
                item.to_dict()
                for item in self.storage_items
            ],
            "overall": self.overall.to_dict(),
        }
