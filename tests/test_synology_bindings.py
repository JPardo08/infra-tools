import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "synology-health" / "src"
sys.path.insert(0, str(SRC))

from synology_health.bindings import (
    BindingsError,
    bindings_path_from_env,
    load_bindings,
    resolve_secret_binding,
    resolve_target,
)
from synology_health.secrets import SecretError, load_snmpv3_secret


class SynologyBindingsTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="pardo-bind.")
        self.root = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_bindings(self, document: dict, name: str = "bindings.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(document))
        return path

    def _valid_document(self) -> dict:
        return {
            "apiVersion": "pardo.ai/v0",
            "kind": "ToolBindings",
            "implementationId": "synology-health",
            "targets": {
                "synology-primary": {
                    "address": "192.0.2.47",
                    "port": 161,
                    "protocol": "udp",
                }
            },
            "secrets": {
                "synology-primary-snmpv3": {
                    "type": "snmpv3-authpriv",
                    "path": str(self.root / "secret.json"),
                }
            },
        }

    def test_loads_valid_bindings(self):
        path = self._write_bindings(self._valid_document())
        bindings = load_bindings(path)

        self.assertEqual(
            bindings.implementation_id,
            "synology-health",
        )
        target = resolve_target(bindings, "synology-primary")
        self.assertEqual(target.address, "192.0.2.47")
        self.assertEqual(target.snmp_host(), "192.0.2.47")

        secret = resolve_secret_binding(
            bindings,
            "synology-primary-snmpv3",
        )
        self.assertEqual(secret.type, "snmpv3-authpriv")

    def test_non_default_port_is_encoded_in_host(self):
        document = self._valid_document()
        document["targets"]["synology-primary"]["port"] = 1161
        bindings = load_bindings(self._write_bindings(document))
        target = resolve_target(bindings, "synology-primary")
        self.assertEqual(target.snmp_host(), "192.0.2.47:1161")

    def test_missing_bindings_env_fails_closed(self):
        with self.assertRaises(BindingsError):
            bindings_path_from_env({})

    def test_invalid_bindings_schema_fails_closed(self):
        document = self._valid_document()
        del document["targets"]

        with self.assertRaises(BindingsError):
            load_bindings(self._write_bindings(document))

    def test_unknown_target_fails_closed(self):
        bindings = load_bindings(
            self._write_bindings(self._valid_document())
        )

        with self.assertRaises(BindingsError):
            resolve_target(bindings, "attacker-target")

    def test_embedded_secret_values_are_rejected(self):
        document = self._valid_document()
        document["secrets"]["synology-primary-snmpv3"][
            "username"
        ] = "should-not-be-here"

        with self.assertRaises(BindingsError):
            load_bindings(self._write_bindings(document))

    def test_privacy_passphrase_in_bindings_is_rejected(self):
        document = self._valid_document()
        document["secrets"]["synology-primary-snmpv3"][
            "privacyPassphrase"
        ] = "TEST_PRIV_SECRET"

        with self.assertRaises(BindingsError) as context:
            load_bindings(self._write_bindings(document))

        self.assertIn("type and path", str(context.exception))
        self.assertNotIn(
            "TEST_PRIV_SECRET",
            str(context.exception),
        )

    def test_secret_symlink_is_rejected(self):
        real = self.root / "real-secret.json"
        real.write_text(
            json.dumps(
                {
                    "username": "test-monitor",
                    "authPassphrase": "TEST_AUTH_SECRET",
                    "privacyPassphrase": "TEST_PRIV_SECRET",
                }
            )
        )
        real.chmod(0o600)
        link = self.root / "link-secret.json"
        link.symlink_to(real)

        with self.assertRaises(SecretError) as context:
            load_snmpv3_secret(link)

        self.assertIn("symlink", str(context.exception))

    def test_secret_wrong_owner_fails_closed(self):
        secret_path = self.root / "secret.json"
        secret_path.write_text(
            json.dumps(
                {
                    "username": "test-monitor",
                    "authPassphrase": "TEST_AUTH_SECRET",
                    "privacyPassphrase": "TEST_PRIV_SECRET",
                }
            )
        )
        secret_path.chmod(0o600)

        with mock.patch(
            "synology_health.secrets.os.geteuid",
            return_value=secret_path.stat().st_uid + 1,
        ):
            with self.assertRaises(SecretError) as context:
                load_snmpv3_secret(secret_path)

        self.assertIn("owner", str(context.exception))

    def test_secret_file_missing_fails_closed(self):
        with self.assertRaises(SecretError):
            load_snmpv3_secret(self.root / "missing.json")

    def test_secret_wrong_mode_fails_closed(self):
        secret_path = self.root / "secret.json"
        secret_path.write_text(
            json.dumps(
                {
                    "username": "test-monitor",
                    "authPassphrase": "TEST_AUTH_SECRET",
                    "privacyPassphrase": "TEST_PRIV_SECRET",
                }
            )
        )
        secret_path.chmod(0o644)

        with self.assertRaises(SecretError) as context:
            load_snmpv3_secret(secret_path)

        self.assertIn("0600", str(context.exception))

    def test_secret_malformed_json_fails_closed(self):
        secret_path = self.root / "secret.json"
        secret_path.write_text("{not-json")
        secret_path.chmod(0o600)

        with self.assertRaises(SecretError):
            load_snmpv3_secret(secret_path)

    def test_secret_missing_required_fields_fails_closed(self):
        secret_path = self.root / "secret.json"
        secret_path.write_text(
            json.dumps(
                {
                    "username": "test-monitor",
                    "authPassphrase": "TEST_AUTH_SECRET",
                }
            )
        )
        secret_path.chmod(0o600)

        with self.assertRaises(SecretError):
            load_snmpv3_secret(secret_path)

    def test_secret_loads_with_0600(self):
        secret_path = self.root / "secret.json"
        secret_path.write_text(
            json.dumps(
                {
                    "username": "test-monitor",
                    "authPassphrase": "TEST_AUTH_SECRET",
                    "privacyPassphrase": "TEST_PRIV_SECRET",
                }
            )
        )
        secret_path.chmod(0o600)

        loaded = load_snmpv3_secret(secret_path)
        self.assertEqual(
            loaded.credentials.username,
            "test-monitor",
        )
        self.assertEqual(
            stat.S_IMODE(secret_path.stat().st_mode),
            0o600,
        )


if __name__ == "__main__":
    unittest.main()
