import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "synology-health" / "src"
TOOL_YAML = ROOT / "tools" / "synology-health" / "tool.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "synology-health.walk.txt"
PARDO_TOOL = (
    ROOT / "tools" / "synology-health" / "bin" / "pardo-tool"
)

sys.path.insert(0, str(SRC))

from synology_health.cli import main
from synology_health.manifest import load_tool_manifest


USERNAME = "test-monitor"
AUTH = "TEST_AUTH_SECRET"
PRIV = "TEST_PRIV_SECRET"
DOC_ADDRESS = "192.0.2.47"


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

        config_dir = Path(kwargs["env"]["SNMPCONFPATH"])
        config_file = config_dir / "snmp.conf"

        self.config_snapshots.append(
            {
                "directory": config_dir,
                "config": config_file.read_text(),
                "mode": stat.S_IMODE(config_file.stat().st_mode),
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


class SynologyCliContractTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="pardo-cli.")
        self.root = Path(self.tmpdir.name)
        self.secret_path = self.root / "secret.json"
        self.bindings_path = self.root / "bindings.json"
        self.manifest = load_tool_manifest(TOOL_YAML)
        self._write_secret()
        self._write_bindings()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_secret(self, payload=None, mode=0o600):
        document = payload or {
            "username": USERNAME,
            "authPassphrase": AUTH,
            "privacyPassphrase": PRIV,
        }
        self.secret_path.write_text(json.dumps(document))
        self.secret_path.chmod(mode)

    def _write_bindings(self, document=None):
        payload = document or {
            "apiVersion": "pardo.ai/v0",
            "kind": "ToolBindings",
            "implementationId": "synology-health",
            "targets": {
                "synology-primary": {
                    "address": DOC_ADDRESS,
                    "port": 161,
                    "protocol": "udp",
                }
            },
            "secrets": {
                "synology-primary-snmpv3": {
                    "type": "snmpv3-authpriv",
                    "path": str(self.secret_path),
                }
            },
        }
        self.bindings_path.write_text(json.dumps(payload))

    def _env(self, include_bindings=True):
        env = {"PATH": os.environ.get("PATH", "")}
        if include_bindings:
            env["PARDO_TOOL_BINDINGS_PATH"] = str(self.bindings_path)
        return env

    def _request(self, op, input_obj=None, **overrides):
        payload = {
            "apiVersion": "pardo.ai/v0",
            "op": op,
            "capability": "storage.health",
            "capabilityVersion": "0.1.0",
            "input": input_obj if input_obj is not None else {},
        }
        payload.update(overrides)
        return payload

    def _run(
        self,
        request=None,
        raw=None,
        env=None,
        runner=None,
        argv=None,
    ):
        stdin = io.StringIO(
            raw if raw is not None else json.dumps(request)
        )
        stdout = io.StringIO()
        stderr = io.StringIO()

        code = main(
            argv=[] if argv is None else argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env if env is not None else self._env(),
            runner=runner,
            manifest_path=TOOL_YAML,
        )

        stdout_text = stdout.getvalue()
        stderr_text = stderr.getvalue()
        payload = json.loads(stdout_text) if stdout_text.strip() else None
        return code, payload, stdout_text, stderr_text

    def test_describe_response_schema(self):
        code, payload, stdout_text, stderr_text = self._run(
            self._request("describe"),
            env={},
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["apiVersion"], "pardo.ai/v0")
        self.assertEqual(payload["op"], "describe")
        self.assertEqual(payload["capability"], "storage.health")
        self.assertEqual(payload["capabilityVersion"], "0.1.0")
        self.assertIsNone(payload["error"])

        result = payload["result"]
        self.assertEqual(result["implementationId"], "synology-health")
        self.assertEqual(result["implementationVersion"], "0.1.0")
        self.assertEqual(
            result["operations"],
            ["check-deps", "describe", "invoke", "probe"],
        )
        self.assertEqual(
            result["requiredExecutables"],
            ["/usr/bin/snmpget", "/usr/bin/snmpwalk"],
        )
        self.assertEqual(
            result["contract"]["kind"],
            "ToolRuntimeContract",
        )

        meta = payload["meta"]
        self.assertEqual(meta["implementationId"], "synology-health")
        self.assertEqual(meta["implementationVersion"], "0.1.0")
        self.assertIsInstance(meta["durationMs"], int)
        self.assertEqual(stderr_text, "")
        self.assertTrue(stdout_text.endswith("\n"))

    def test_check_deps_success_and_failure(self):
        original_lstat = Path.lstat
        original_access = os.access

        def fake_lstat(path_self):
            text = str(path_self)
            if text in {"/usr/bin/snmpget", "/usr/bin/snmpwalk"}:
                return os.stat_result(
                    (
                        0o100755,
                        0,
                        0,
                        1,
                        os.getuid(),
                        0,
                        0,
                        0,
                        0,
                        0,
                    )
                )
            return original_lstat(path_self)

        def fake_access(path, mode):
            text = str(path)
            if text in {"/usr/bin/snmpget", "/usr/bin/snmpwalk"}:
                return True
            return original_access(path, mode)

        with mock.patch.object(Path, "lstat", fake_lstat), mock.patch(
            "os.access",
            fake_access,
        ):
            code, payload, _, _ = self._run(
                self._request("check-deps"),
                env={},
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "PASS")
        self.assertTrue(payload["result"]["ok"])

        def missing_lstat(path_self):
            text = str(path_self)
            if text in {"/usr/bin/snmpget", "/usr/bin/snmpwalk"}:
                raise FileNotFoundError(text)
            return original_lstat(path_self)

        with mock.patch.object(Path, "lstat", missing_lstat):
            code, payload, _, _ = self._run(
                self._request("check-deps"),
                env={},
            )

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "FAIL")
        self.assertFalse(payload["result"]["ok"])
        self.assertEqual(
            payload["error"]["code"],
            "check_deps_failed",
        )

    def test_check_deps_rejects_non_executable_regular_file(self):
        non_exec = self.root / "present-but-not-exec"
        non_exec.write_text("#!/bin/sh\necho no\n")
        non_exec.chmod(0o644)

        tool_yaml = self.root / "tool-check-deps.yaml"
        tool_yaml.write_text(
            TOOL_YAML.read_text().replace(
                "      - /usr/bin/snmpget\n"
                "      - /usr/bin/snmpwalk\n",
                f"      - {non_exec}\n",
            )
        )

        stdin = io.StringIO(json.dumps(self._request("check-deps")))
        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(
            argv=[],
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env={},
            manifest_path=tool_yaml,
        )
        payload = json.loads(stdout.getvalue())

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "FAIL")
        self.assertFalse(payload["result"]["ok"])
        entry = payload["result"]["executables"][0]
        self.assertTrue(entry["present"])
        self.assertTrue(entry["regularFile"])
        self.assertFalse(entry["executable"])
        self.assertFalse(entry["ok"])

    def test_malformed_stdin_json_fails_closed(self):
        code, payload, _, _ = self._run(raw="{not-json", env={})

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["error"]["code"], "malformed_request")

    def test_unknown_operation_fails_closed(self):
        code, payload, _, _ = self._run(
            self._request("explode"),
            env={},
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "unknown_op")

    def test_capability_mismatch_fails_closed(self):
        code, payload, _, _ = self._run(
            self._request(
                "describe",
                capability="storage.destroy",
            ),
            env={},
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "capability_mismatch",
        )

    def test_version_mismatch_fails_closed(self):
        code, payload, _, _ = self._run(
            self._request(
                "describe",
                capabilityVersion="9.9.9",
            ),
            env={},
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "version_mismatch",
        )

    def test_missing_bindings_env_fails_closed(self):
        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
            env={},
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "invalid_bindings",
        )

    def test_invalid_bindings_schema_fails_closed(self):
        self.bindings_path.write_text('{"kind":"nope"}')

        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "invalid_bindings",
        )

    def test_unknown_target_id_fails_closed(self):
        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "attacker-controlled"},
            ),
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "unauthorized_target",
        )

    def test_manifest_target_is_accepted(self):
        runner = RecordingRunner()
        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
            runner=runner,
        )

        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(len(runner.calls), 3)

    def test_extra_bindings_target_cannot_expand_authority(self):
        document = {
            "apiVersion": "pardo.ai/v0",
            "kind": "ToolBindings",
            "implementationId": "synology-health",
            "targets": {
                "synology-primary": {
                    "address": DOC_ADDRESS,
                    "port": 161,
                    "protocol": "udp",
                },
                "attacker-target": {
                    "address": "198.51.100.66",
                    "port": 161,
                    "protocol": "udp",
                },
            },
            "secrets": {
                "synology-primary-snmpv3": {
                    "type": "snmpv3-authpriv",
                    "path": str(self.secret_path),
                }
            },
        }
        self._write_bindings(document)
        runner = RecordingRunner()

        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "attacker-target"},
            ),
            runner=runner,
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "unauthorized_target",
        )
        self.assertEqual(runner.calls, [])

    def test_request_input_allowlist_rejects_unknown_keys(self):
        cases = (
            "host",
            "address",
            "secretRef",
            "path",
            "oid",
            "executable",
            "argv",
        )

        for key in cases:
            with self.subTest(key=key):
                runner = RecordingRunner()
                code, payload, _, _ = self._run(
                    self._request(
                        "probe",
                        {
                            "targetId": "synology-primary",
                            key: "attacker-value",
                        },
                    ),
                    runner=runner,
                )

                self.assertEqual(code, 1)
                self.assertEqual(
                    payload["error"]["code"],
                    "invalid_input",
                )
                self.assertEqual(runner.calls, [])

    def test_target_cannot_be_overridden_through_request(self):
        runner = RecordingRunner()

        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {
                    "targetId": "synology-primary",
                    "address": "198.51.100.66",
                    "host": "evil.example",
                },
            ),
            runner=runner,
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "invalid_input")
        self.assertEqual(runner.calls, [])

    def test_secret_path_missing_fails_closed(self):
        self.secret_path.unlink()

        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "invalid_secret",
        )

    def test_secret_path_wrong_mode_fails_closed(self):
        self.secret_path.chmod(0o644)

        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "invalid_secret",
        )
        self.assertNotIn(AUTH, json.dumps(payload))

    def test_secret_json_malformed_fails_closed(self):
        self.secret_path.write_text("{bad")
        self.secret_path.chmod(0o600)

        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "invalid_secret",
        )

    def test_secret_required_fields_missing_fails_closed(self):
        self._write_secret(
            {
                "username": USERNAME,
                "authPassphrase": AUTH,
            }
        )

        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            payload["error"]["code"],
            "invalid_secret",
        )

    def test_probe_and_invoke_return_sanitized_health(self):
        for op in ("probe", "invoke"):
            with self.subTest(op=op):
                runner = RecordingRunner()
                code, payload, stdout_text, stderr_text = self._run(
                    self._request(
                        op,
                        {"targetId": "synology-primary"},
                    ),
                    runner=runner,
                )

                self.assertEqual(code, 0)
                self.assertEqual(payload["status"], "PASS")
                self.assertEqual(payload["op"], op)

                result = payload["result"]
                self.assertTrue(result["target"]["reachable"])
                self.assertEqual(result["target"]["model"], "DS220j")
                self.assertTrue(result["overall"]["healthy"])

                blob = stdout_text + stderr_text + json.dumps(payload)
                self.assertNotIn(USERNAME, blob)
                self.assertNotIn(AUTH, blob)
                self.assertNotIn(PRIV, blob)
                self.assertNotIn(DOC_ADDRESS, json.dumps(payload))
                self.assertNotIn(
                    str(self.secret_path),
                    json.dumps(payload),
                )
                self.assertNotIn(
                    "1.3.6.1.4.1.6574.1.5.2.0",
                    blob,
                )
                self.assertNotIn("serial", blob.lower())

                for argv, kwargs in runner.calls:
                    flat = " ".join(argv)
                    self.assertNotIn(USERNAME, flat)
                    self.assertNotIn(AUTH, flat)
                    self.assertNotIn(PRIV, flat)
                    self.assertIn(DOC_ADDRESS, argv)

                    env_values = "\n".join(
                        str(value) for value in kwargs["env"].values()
                    )
                    self.assertNotIn(AUTH, env_values)
                    self.assertNotIn(PRIV, env_values)

                for snapshot in runner.config_snapshots:
                    self.assertEqual(snapshot["mode"], 0o600)
                    self.assertFalse(snapshot["directory"].exists())

    def test_credentials_absent_from_adapter_error_paths(self):
        def failing_runner(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv,
                1,
                stdout="",
                stderr=(
                    f"user={USERNAME} auth={AUTH} priv={PRIV}"
                ),
            )

        code, payload, stdout_text, stderr_text = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
            runner=failing_runner,
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "FAIL")

        blob = stdout_text + stderr_text + json.dumps(payload)
        self.assertNotIn(USERNAME, blob)
        self.assertNotIn(AUTH, blob)
        self.assertNotIn(PRIV, blob)
        self.assertIn("<redacted>", payload["error"]["message"])

    def test_unexpected_exception_after_secret_load_is_sanitized(self):
        seen_dirs = []

        def boom_runner(argv, **kwargs):
            config_dir = Path(kwargs["env"]["SNMPCONFPATH"])
            seen_dirs.append(config_dir)
            raise RuntimeError(
                f"boom {USERNAME} {AUTH} {PRIV}"
            )

        code, payload, stdout_text, stderr_text = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
            runner=boom_runner,
        )

        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["error"]["code"], "internal_error")
        self.assertEqual(payload["error"]["message"], "internal error")

        blob = stdout_text + stderr_text
        self.assertNotIn(USERNAME, blob)
        self.assertNotIn(AUTH, blob)
        self.assertNotIn(PRIV, blob)
        self.assertEqual(len(seen_dirs), 1)
        self.assertFalse(seen_dirs[0].exists())

    def test_probe_response_omits_address_and_secret_path(self):
        runner = RecordingRunner()
        code, payload, _, _ = self._run(
            self._request(
                "probe",
                {"targetId": "synology-primary"},
            ),
            runner=runner,
        )

        self.assertEqual(code, 0)
        serialized = json.dumps(payload)
        self.assertNotIn(DOC_ADDRESS, serialized)
        self.assertNotIn(str(self.secret_path), serialized)
        self.assertNotIn("198.51.100.", serialized)

    def test_pardo_tool_entrypoint_describe(self):
        completed = subprocess.run(
            [str(PARDO_TOOL)],
            input=json.dumps(self._request("describe")),
            text=True,
            capture_output=True,
            check=False,
            env={},
        )

        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(
            payload["result"]["implementationId"],
            "synology-health",
        )
        self.assertEqual(completed.stderr, "")


if __name__ == "__main__":
    unittest.main()
