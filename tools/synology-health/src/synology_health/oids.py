"""Numeric Synology OIDs used by storage.health V0.

Only explicitly approved OIDs are exposed here. The adapter must not
provide arbitrary OID access.
"""

SYSTEM_ROOT = "1.3.6.1.4.1.6574.1"
DISK_TABLE = "1.3.6.1.4.1.6574.2.1.1"
STORAGE_TABLE = "1.3.6.1.4.1.6574.3.1.1"


SYSTEM = {
    "status": f"{SYSTEM_ROOT}.1.0",
    "temperature_c": f"{SYSTEM_ROOT}.2.0",
    "power_status": f"{SYSTEM_ROOT}.3.0",
    "system_fan_status": f"{SYSTEM_ROOT}.4.1.0",
    "cpu_fan_status": f"{SYSTEM_ROOT}.4.2.0",
    "model": f"{SYSTEM_ROOT}.5.1.0",
    # .5.2.0 is the NAS serial number and is intentionally excluded.
    "dsm_version": f"{SYSTEM_ROOT}.5.3.0",
    "cpu_utilization_pct": f"{SYSTEM_ROOT}.7.1.0",
    "memory_utilization_pct": f"{SYSTEM_ROOT}.7.2.0",
    "thermal_status": f"{SYSTEM_ROOT}.8.0",
}


DISK_FIELDS = {
    2: "id",
    3: "model",
    4: "type",
    5: "status",
    6: "temperature_c",
    7: "role",
    8: "retry_count",
    9: "bad_sectors",
    10: "identify_fail_count",
    11: "remaining_life",
    12: "name",
    13: "health_status",
}


STORAGE_FIELDS = {
    2: "name",
    3: "status",
    4: "free_bytes",
    5: "total_bytes",
    6: "hotspare_count",
}


NORMAL_FAILED = {
    1: "normal",
    2: "failed",
}


DISK_STATUS = {
    1: "normal",
    2: "initialized",
    3: "not_initialized",
    4: "system_partition_failed",
    5: "crashed",
    6: "disconnected",
}


DISK_HEALTH_STATUS = {
    1: "normal",
    2: "warning",
    3: "critical",
    4: "failing",
}


STORAGE_STATUS = {
    1: "normal",
    2: "repairing",
    3: "migrating",
    4: "expanding",
    5: "deleting",
    6: "creating",
    7: "raid_syncing",
    8: "raid_parity_checking",
    9: "raid_assembling",
    10: "canceling",
    11: "degraded",
    12: "crashed",
    13: "data_scrubbing",
    14: "raid_deploying",
    15: "raid_undeploying",
    16: "raid_mount_cache",
    17: "raid_unmount_cache",
    18: "raid_expanding_unfinished_shr",
    19: "raid_convert_shr_to_pool",
    20: "raid_migrate_shr1_to_shr2",
    21: "unknown_status",
}
