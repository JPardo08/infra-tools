import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "synology-health" / "src"
sys.path.insert(0, str(SRC))

from synology_health import oids
from synology_health.parser import ParseError, parse_health_walk


FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "synology-health.walk.txt"
)


class SynologyHealthContractTests(unittest.TestCase):

    def test_serial_number_oid_is_not_allowed(self):
        self.assertNotIn(
            "1.3.6.1.4.1.6574.1.5.2.0",
            oids.SYSTEM.values(),
        )

    def test_only_expected_synology_roots_are_declared(self):
        roots = {
            oids.SYSTEM_ROOT,
            oids.DISK_TABLE,
            oids.STORAGE_TABLE,
        }

        self.assertEqual(
            roots,
            {
                "1.3.6.1.4.1.6574.1",
                "1.3.6.1.4.1.6574.2.1.1",
                "1.3.6.1.4.1.6574.3.1.1",
            },
        )

    def test_fixture_contains_no_nas_serial_oid(self):
        fixture = FIXTURE.read_text()

        self.assertNotIn(
            "1.3.6.1.4.1.6574.1.5.2.0",
            fixture,
        )


class SynologyHealthParserTests(unittest.TestCase):

    def test_real_ds220j_fixture_parses_as_healthy(self):
        result = parse_health_walk(FIXTURE.read_text())
        payload = result.to_dict()

        self.assertTrue(payload["target"]["reachable"])
        self.assertEqual(
            payload["target"]["model"],
            "DS220j",
        )
        self.assertEqual(
            payload["target"]["dsmVersion"],
            "DSM 7.3-86009",
        )

        self.assertEqual(
            payload["system"]["status"],
            "normal",
        )
        self.assertEqual(
            payload["system"]["temperatureC"],
            40,
        )
        self.assertEqual(
            payload["system"]["cpuUtilizationPct"],
            0,
        )
        self.assertEqual(
            payload["system"]["memoryUtilizationPct"],
            49,
        )

        self.assertEqual(len(payload["disks"]), 1)

        disk = payload["disks"][0]

        self.assertEqual(disk["name"], "Drive 1")
        self.assertEqual(
            disk["model"],
            "WD40EFAX-68JH4N1",
        )
        self.assertEqual(disk["status"], "normal")
        self.assertEqual(
            disk["healthStatus"],
            "normal",
        )
        self.assertEqual(disk["temperatureC"], 33)
        self.assertEqual(disk["badSectors"], 0)
        self.assertEqual(disk["identifyFailCount"], 2)

        self.assertEqual(
            len(payload["storageItems"]),
            2,
        )

        volume = payload["storageItems"][0]
        pool = payload["storageItems"][1]

        self.assertEqual(volume["name"], "Volume 1")
        self.assertEqual(volume["kind"], "volume")
        self.assertEqual(volume["status"], "normal")
        self.assertEqual(
            volume["freeBytes"],
            2990802006016,
        )
        self.assertEqual(
            volume["totalBytes"],
            3931605622784,
        )

        self.assertEqual(
            pool["name"],
            "Storage Pool 1",
        )
        self.assertEqual(pool["kind"], "pool")
        self.assertEqual(pool["status"], "normal")

        self.assertTrue(payload["overall"]["healthy"])
        self.assertEqual(
            payload["overall"]["issues"],
            [],
        )

    def test_degraded_volume_is_unhealthy(self):
        fixture = FIXTURE.read_text()

        degraded = fixture.replace(
            ".1.3.6.1.4.1.6574.3.1.1.3.0 = INTEGER: 1",
            ".1.3.6.1.4.1.6574.3.1.1.3.0 = INTEGER: 11",
        )

        payload = parse_health_walk(
            degraded
        ).to_dict()

        self.assertFalse(
            payload["overall"]["healthy"]
        )

        self.assertEqual(
            payload["storageItems"][0]["status"],
            "degraded",
        )

        self.assertTrue(
            any(
                issue["code"]
                == "storage_status_abnormal"
                and issue["severity"]
                == "critical"
                for issue in payload["overall"]["issues"]
            )
        )


    def test_missing_required_system_oid_fails_closed(self):
        fixture = FIXTURE.read_text()

        broken = fixture.replace(
            ".1.3.6.1.4.1.6574.1.1.0 = INTEGER: 1\n",
            "",
        )

        with self.assertRaises(ParseError):
            parse_health_walk(broken)

    def test_malformed_snmp_line_fails_closed(self):
        fixture = FIXTURE.read_text()

        broken = fixture + "\nTHIS IS NOT A VARBIND\n"

        with self.assertRaises(ParseError):
            parse_health_walk(broken)

    def test_unknown_storage_status_is_unhealthy(self):
        fixture = FIXTURE.read_text()

        unknown = fixture.replace(
            ".1.3.6.1.4.1.6574.3.1.1.3.0 = INTEGER: 1",
            ".1.3.6.1.4.1.6574.3.1.1.3.0 = INTEGER: 999",
        )

        payload = parse_health_walk(
            unknown
        ).to_dict()

        self.assertEqual(
            payload["storageItems"][0]["status"],
            "unknown:999",
        )

        self.assertFalse(
            payload["overall"]["healthy"]
        )

        self.assertTrue(
            any(
                issue["severity"] == "critical"
                for issue in payload["overall"]["issues"]
            )
        )


if __name__ == "__main__":
    unittest.main()
