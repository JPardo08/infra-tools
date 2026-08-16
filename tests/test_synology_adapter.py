import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "synology-health" / "src"
sys.path.insert(0, str(SRC))

from synology_health import oids
from synology_health.adapter import (
    AdapterError,
    SnmpCommandError,
    SnmpTimeoutError,
    SnmpV3Credentials,
    SynologyHealthAdapter,
    SynologyTarget,
    UnknownTargetError,
)


FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "synology-health.walk.txt"
)


def fixture_sections():
    lines = FIXTURE.read_text().splitlines()

    system = []
    disks = []
    storage = []

    for line in lines:
        if line.startswith(".1.3.6.1.4.1.6574.1."):
            system.append(line)
        elif line.startswith(".1.3.6.1.4.1.6574.2."):
            disks.append(line)
        elif line.startswith(".1.3.6.1.4.1.6574.3."):
            storage.append(line)

    return (
        "\n".join(system) + "\n",
        "\n".join(disks) + "\n",
        "\n".join(storage) + "\n",
    )


class RecordingRunner:

    def __init__(self, outputs=None, returncodes=None):
        self.calls = []
        self.config_snapshots = []

        self.outputs = outputs or fixture_sections()
        self.returncodes = returncodes or [0, 0, 0]

    def __call__(self, argv, **kwargs):
        call_number = len(self.calls)
        self.calls.append((list(argv), kwargs))

        config_dir = Path(
            kwargs["env"]["SNMPCONFPATH"]
        )
        config_file = config_dir / "snmp.conf"

        self.config_snapshots.append(
            {
                "directory": config_dir,
                "config": config_file.read_text(),
                "mode": stat.S_IMODE(
                    config_file.stat().st_mode
                ),
            }
        )

        return subprocess.CompletedProcess(
            argv,
            self.returncodes[call_number],
            stdout=self.outputs[call_number],
            stderr=(
                ""
                if self.returncodes[call_number] == 0
                else "synthetic SNMP failure"
            ),
        )


class SynologyHealthAdapterTests(unittest.TestCase):

    def setUp(self):
        self.target = SynologyTarget(
            target_id="synology-primary",
            host="192.0.2.47",
        )

        self.credentials = SnmpV3Credentials(
            username="test-monitor",
            auth_passphrase="TEST_AUTH_SECRET",
            privacy_passphrase="TEST_PRIV_SECRET",
        )

    def test_executes_only_three_fixed_snmp_commands(self):
        runner = RecordingRunner()

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=runner,
        )

        result = adapter.get_health(
            "synology-primary"
        )

        self.assertTrue(result.overall.healthy)
        self.assertEqual(len(runner.calls), 3)

        system_argv = runner.calls[0][0]
        disk_argv = runner.calls[1][0]
        storage_argv = runner.calls[2][0]

        self.assertEqual(
            system_argv[0],
            "/usr/bin/snmpget",
        )
        self.assertEqual(
            disk_argv[0],
            "/usr/bin/snmpwalk",
        )
        self.assertEqual(
            storage_argv[0],
            "/usr/bin/snmpwalk",
        )

        self.assertEqual(
            disk_argv[-1],
            oids.DISK_TABLE,
        )
        self.assertEqual(
            storage_argv[-1],
            oids.STORAGE_TABLE,
        )

    def test_credentials_never_appear_in_argv(self):
        runner = RecordingRunner()

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=runner,
        )

        adapter.get_health("synology-primary")

        flattened = "\n".join(
            " ".join(argv)
            for argv, _ in runner.calls
        )

        self.assertNotIn(
            self.credentials.auth_passphrase,
            flattened,
        )
        self.assertNotIn(
            self.credentials.privacy_passphrase,
            flattened,
        )
        self.assertNotIn(
            self.credentials.username,
            flattened,
        )

    def test_serial_number_oid_is_never_requested(self):
        runner = RecordingRunner()

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=runner,
        )

        adapter.get_health("synology-primary")

        flattened = "\n".join(
            " ".join(argv)
            for argv, _ in runner.calls
        )

        self.assertNotIn(
            "1.3.6.1.4.1.6574.1.5.2.0",
            flattened,
        )

    def test_target_id_cannot_override_configured_host(self):
        runner = RecordingRunner()

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=runner,
        )

        with self.assertRaises(UnknownTargetError):
            adapter.get_health("attacker-controlled")

        self.assertEqual(runner.calls, [])

    def test_all_commands_use_only_configured_host(self):
        runner = RecordingRunner()

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=runner,
        )

        adapter.get_health("synology-primary")

        for argv, _ in runner.calls:
            self.assertIn(
                self.target.host,
                argv,
            )

    def test_ephemeral_config_is_0600_and_removed(self):
        runner = RecordingRunner()

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=runner,
        )

        adapter.get_health("synology-primary")

        for snapshot in runner.config_snapshots:
            self.assertEqual(
                snapshot["mode"],
                0o600,
            )

            self.assertIn(
                "defSecurityLevel authPriv",
                snapshot["config"],
            )
            self.assertIn(
                "defAuthType SHA",
                snapshot["config"],
            )
            self.assertIn(
                "defPrivType AES",
                snapshot["config"],
            )

            self.assertFalse(
                snapshot["directory"].exists()
            )

    def test_snmp_failure_is_typed_and_fails_closed(self):
        runner = RecordingRunner(
            returncodes=[0, 1, 0],
        )

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=runner,
        )

        with self.assertRaises(SnmpCommandError):
            adapter.get_health("synology-primary")

        self.assertEqual(len(runner.calls), 2)

    def test_process_environment_contains_no_credentials(self):
        runner = RecordingRunner()

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=runner,
        )

        adapter.get_health("synology-primary")

        for _, kwargs in runner.calls:
            environment = kwargs["env"]

            values = "\n".join(
                str(value)
                for value in environment.values()
            )

            self.assertNotIn(
                self.credentials.auth_passphrase,
                values,
            )
            self.assertNotIn(
                self.credentials.privacy_passphrase,
                values,
            )


    def test_process_timeout_is_typed_and_fails_closed(self):
        def timeout_runner(argv, **kwargs):
            raise subprocess.TimeoutExpired(
                cmd=argv,
                timeout=kwargs["timeout"],
            )

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=timeout_runner,
        )

        with self.assertRaises(SnmpTimeoutError):
            adapter.get_health("synology-primary")

    def test_invalid_snmp_payload_fails_closed(self):
        system, disks, storage = fixture_sections()

        runner = RecordingRunner(
            outputs=(
                "THIS IS NOT A VARBIND\n",
                disks,
                storage,
            ),
        )

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=runner,
        )

        with self.assertRaises(AdapterError):
            adapter.get_health("synology-primary")

    def test_secrets_are_redacted_from_snmp_errors(self):
        def failing_runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr=(
                    f"user={self.credentials.username} "
                    f"auth={self.credentials.auth_passphrase} "
                    f"priv={self.credentials.privacy_passphrase}"
                ),
            )

        adapter = SynologyHealthAdapter(
            self.target,
            self.credentials,
            runner=failing_runner,
        )

        with self.assertRaises(SnmpCommandError) as context:
            adapter.get_health("synology-primary")

        message = str(context.exception)

        self.assertNotIn(
            self.credentials.username,
            message,
        )
        self.assertNotIn(
            self.credentials.auth_passphrase,
            message,
        )
        self.assertNotIn(
            self.credentials.privacy_passphrase,
            message,
        )
        self.assertIn("<redacted>", message)


if __name__ == "__main__":
    unittest.main()
