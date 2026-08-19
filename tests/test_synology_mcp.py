import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "synology-mcp" / "src"
TOOL_YAML = ROOT / "tools" / "synology-mcp" / "tool.yaml"
FIXTURE_LIST = ROOT / "tests" / "fixtures" / "synology-mcp-list-nas.json"
FIXTURE_HEALTH = ROOT / "tests" / "fixtures" / "synology-mcp-health-summary.json"
FIXTURE_PARTIAL = (
    ROOT / "tests" / "fixtures" / "synology-mcp-health-partial-system.json"
)
FIXTURE_DEGRADED = (
    ROOT / "tests" / "fixtures" / "synology-mcp-health-degraded.json"
)
PARDO_TOOL = ROOT / "tools" / "synology-mcp" / "bin" / "pardo-tool"

sys.path.insert(0, str(SRC))

from synology_mcp.adapter import SynologyMcpAdapter
from synology_mcp.cli import handle_request, main
from synology_mcp.errors import AdapterError
from synology_mcp.manifest import ALLOWED_MCP_TOOLS, UPSTREAM_PIN, load_tool_manifest
from synology_mcp.mcp_client import FakeMcpClient
from synology_mcp.normalize import normalize_health_summary
from synology_mcp.resolve import is_local_filesystem_ref, resolve_nas_name


def _load_json(path: Path):
    return json.loads(path.read_text())


class SynologyMcpCliTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="pardo-mcp.")
        self.root = Path(self.tmpdir.name)
        self.command_path = self.root / "synology-mcp"
        self.command_path.write_text("#!/bin/sh\nexit 0\n")
        self.command_path.chmod(0o755)
        self.bindings_path = self.root / "bindings.json"
        self.manifest = load_tool_manifest(TOOL_YAML)
        self.nas_list = _load_json(FIXTURE_LIST)
        self.health = _load_json(FIXTURE_HEALTH)
        self.degraded = _load_json(FIXTURE_DEGRADED)
        self._write_bindings()

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_bindings(self, document=None):
        payload = document or {
            "apiVersion": "pardo.ai/v0",
            "kind": "ToolBindings",
            "implementationId": "synology-mcp",
            "targets": {
                "nas-primary": {
                    "nasName": "nas1",
                }
            },
            "mcp": {
                "transport": "stdio",
                "command": [str(self.command_path)],
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
            "capabilityVersion": "1.0.0",
            "input": input_obj if input_obj is not None else {},
        }
        payload.update(overrides)
        return payload

    def _handle(self, request, client=None, env=None):
        return handle_request(
            request,
            manifest=self.manifest,
            env=env if env is not None else self._env(),
            client=client or FakeMcpClient(self.nas_list, self.health),
        )

    def test_describe(self):
        payload, code = self._handle(self._request("describe"))
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "PASS")
        self.assertEqual(payload["result"]["implementationId"], "synology-mcp")
        self.assertEqual(payload["result"]["upstream"], UPSTREAM_PIN)
        self.assertNotIn("allowedTools", payload["result"])

    def test_invoke_healthy_path(self):
        client = FakeMcpClient(self.nas_list, self.health)
        payload, code = self._handle(
            self._request("invoke", {"targetRef": "nas-primary"}),
            client=client,
        )
        self.assertEqual(code, 0)
        result = payload["result"]
        self.assertEqual(result["targetRef"], "nas-primary")
        self.assertEqual(result["overall"]["status"], "HEALTHY")
        self.assertTrue(result["overall"]["healthy"])
        self.assertEqual(result["system"]["model"], "DS220j")
        self.assertEqual(result["capacity"]["totalBytes"], 3937328955392)
        self.assertEqual(result["capacity"]["usedBytes"], 1200000000000)
        self.assertEqual(len(result["disks"]), 2)
        self.assertEqual(len(result["volumes"]), 1)
        self.assertEqual(len(result["pools"]), 1)
        self.assertEqual(result["implementation"]["id"], "synology-mcp")
        self.assertEqual(result["implementation"]["upstream"], UPSTREAM_PIN)
        self.assertEqual(
            client.calls,
            [
                ("synology_list_nas", {}),
                ("synology_health_summary", {"nas_name": "nas1"}),
            ],
        )
        blob = json.dumps(payload)
        self.assertNotIn("synology_list_nas", blob)
        self.assertNotIn("synology_health_summary", blob)
        self.assertNotIn("SECRET-SERIAL-DO-NOT-EMIT", blob)
        self.assertNotIn("pardo-auditor", blob)

    def test_invoke_partial_system_monitoring(self):
        client = FakeMcpClient(
            self.nas_list,
            _load_json(FIXTURE_PARTIAL),
        )
        payload, code = self._handle(
            self._request("invoke", {"targetRef": "nas-primary"}),
            client=client,
        )
        self.assertEqual(code, 0)
        result = payload["result"]
        self.assertEqual(result["overall"]["status"], "UNKNOWN")
        self.assertFalse(result["overall"]["healthy"])
        self.assertEqual(result["system"]["model"], "DS220j")
        self.assertEqual(result["system"]["firmwareVersion"], "DSM 7.4.1-90080")
        self.assertEqual(result["system"]["temperatureC"], 40)
        self.assertIsNone(result["capacity"]["totalBytes"])
        self.assertEqual(result["disks"], [])
        self.assertEqual(result["volumes"], [])
        self.assertEqual(result["pools"], [])
        for field in ("disks", "volumes", "pools", "capacity"):
            self.assertIn(field, result["unavailableFields"])
        blob = json.dumps(result)
        self.assertNotIn("105", blob)
        self.assertNotIn("synology_list_nas", blob)
        self.assertNotIn("synology_health_summary", blob)
        self.assertNotIn("SECRET-SERIAL-DO-NOT-EMIT", blob)

    def test_invoke_degraded(self):
        client = FakeMcpClient(self.nas_list, self.degraded)
        payload, code = self._handle(
            self._request("invoke", {"targetRef": "nas-primary"}),
            client=client,
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["result"]["overall"]["status"], "ATTENTION")
        self.assertFalse(payload["result"]["overall"]["healthy"])
        self.assertTrue(payload["result"]["warnings"])
        self.assertTrue(payload["result"]["pools"][0]["degraded"])

    def test_target_not_found_in_bindings(self):
        payload, code = self._handle(
            self._request("invoke", {"targetRef": "nas-other"}),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "TARGET_NOT_FOUND")

    def test_target_not_found_in_backend(self):
        payload, code = self._handle(
            self._request("invoke", {"targetRef": "nas-primary"}),
            client=FakeMcpClient([], self.health),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "TARGET_NOT_FOUND")

    def test_target_ambiguous(self):
        payload, code = self._handle(
            self._request("invoke", {"targetRef": "nas-primary"}),
            client=FakeMcpClient(
                [
                    {"nas_name": "nas1"},
                    {"nas_name": "nas1", "note": "duplicate"},
                ],
                self.health,
            ),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "TARGET_AMBIGUOUS")

    def test_local_filesystem_refs_are_rejected(self):
        for target in (
            "/workspace",
            "/dev/mmcblk0",
            "/Users/jpardo",
            "./relative",
            "../escape",
            "C:\\Windows",
        ):
            with self.subTest(target=target):
                self.assertTrue(is_local_filesystem_ref(target))
                self._write_bindings(
                    {
                        "apiVersion": "pardo.ai/v0",
                        "kind": "ToolBindings",
                        "implementationId": "synology-mcp",
                        "targets": {
                            target: {"nasName": "nas1"},
                        },
                        "mcp": {
                            "transport": "stdio",
                            "command": [str(self.command_path)],
                        },
                    }
                )
                payload, code = self._handle(
                    self._request("invoke", {"targetRef": target}),
                )
                self.assertEqual(code, 1)
                self.assertEqual(payload["error"]["code"], "TARGET_NOT_FOUND")

    def test_never_calls_unlisted_mcp_tools(self):
        client = FakeMcpClient(self.nas_list, self.health)
        self._handle(
            self._request("invoke", {"targetRef": "nas-primary"}),
            client=client,
        )
        called = {name for name, _arguments in client.calls}
        self.assertEqual(
            called,
            {"synology_list_nas", "synology_health_summary"},
        )

    def test_target_id_is_not_accepted(self):
        payload, code = self._handle(
            self._request("invoke", {"targetId": "nas-primary"}),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "invalid_input")

    def test_missing_target_ref(self):
        payload, code = self._handle(self._request("invoke", {}))
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "missing_target_ref")

    def test_argv_rejected(self):
        stdout = io.StringIO()
        code = main(
            argv=["--help"],
            stdin=io.StringIO(""),
            stdout=stdout,
            stderr=io.StringIO(),
            env=self._env(),
        )
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["error"]["code"], "argv_not_supported")

    def test_entrypoint_invoke(self):
        client = FakeMcpClient(self.nas_list, self.health)
        stdout = io.StringIO()
        code = main(
            argv=[],
            stdin=io.StringIO(
                json.dumps(
                    self._request("invoke", {"targetRef": "nas-primary"})
                )
            ),
            stdout=stdout,
            stderr=io.StringIO(),
            env=self._env(),
            client=client,
            manifest_path=TOOL_YAML,
        )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["result"]["overall"]["status"], "HEALTHY")

    def test_check_deps_with_bindings(self):
        payload, code = self._handle(self._request("check-deps"))
        self.assertEqual(code, 0)
        self.assertTrue(payload["result"]["ok"])
        self.assertTrue(payload["result"]["mcp"]["command"]["ok"])

    def test_check_deps_without_bindings(self):
        payload, code = handle_request(
            self._request("check-deps"),
            manifest=self.manifest,
            env={"PATH": os.environ.get("PATH", "")},
            client=FakeMcpClient(self.nas_list, self.health),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "check_deps_failed")

    def test_adapter_error_is_sanitized(self):
        client = FakeMcpClient(
            self.nas_list,
            self.health,
            errors={
                "synology_health_summary": AdapterError(
                    "adapter_error",
                    "password=super-secret sid=abc",
                )
            },
        )
        payload, code = self._handle(
            self._request("invoke", {"targetRef": "nas-primary"}),
            client=client,
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "adapter_error")
        self.assertNotIn("super-secret", payload["error"]["message"])
        self.assertNotIn("password=", payload["error"]["message"])

    def test_bindings_reject_host_smuggling(self):
        self._write_bindings(
            {
                "apiVersion": "pardo.ai/v0",
                "kind": "ToolBindings",
                "implementationId": "synology-mcp",
                "targets": {
                    "nas-primary": {
                        "nasName": "nas1",
                        "address": "192.168.1.47",
                    }
                },
                "mcp": {
                    "transport": "stdio",
                    "command": [str(self.command_path)],
                },
            }
        )
        payload, code = self._handle(
            self._request("invoke", {"targetRef": "nas-primary"}),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "invalid_bindings")

    def test_relative_mcp_command_rejected(self):
        self._write_bindings(
            {
                "apiVersion": "pardo.ai/v0",
                "kind": "ToolBindings",
                "implementationId": "synology-mcp",
                "targets": {
                    "nas-primary": {"nasName": "nas1"},
                },
                "mcp": {
                    "transport": "stdio",
                    "command": ["synology-mcp"],
                },
            }
        )
        payload, code = self._handle(
            self._request("invoke", {"targetRef": "nas-primary"}),
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"]["code"], "invalid_bindings")

    def test_pardo_tool_file_mode(self):
        mode = stat.S_IMODE(PARDO_TOOL.stat().st_mode)
        self.assertTrue(mode & stat.S_IXUSR)


class SynologyMcpNormalizeTests(unittest.TestCase):

    def test_healthy_fixture(self):
        health = normalize_health_summary(
            target_ref="nas-primary",
            matched_nas_name="nas1",
            summary=_load_json(FIXTURE_HEALTH),
            implementation_id="synology-mcp",
            implementation_version="1.0.0",
        )
        payload = health.to_dict()
        self.assertEqual(payload["overall"]["status"], "HEALTHY")
        self.assertEqual(payload["disks"][0]["smartStatus"], "normal")
        self.assertEqual(payload["temperatures"]["systemC"], 42)
        self.assertNotIn("serial", json.dumps(payload))

    def test_missing_sections_are_unknown(self):
        health = normalize_health_summary(
            target_ref="nas-primary",
            matched_nas_name="nas1",
            summary={"success": True, "data": {}},
            implementation_id="synology-mcp",
            implementation_version="1.0.0",
        )
        payload = health.to_dict()
        self.assertEqual(payload["overall"]["status"], "UNKNOWN")
        self.assertNotEqual(payload["overall"]["status"], "HEALTHY")
        self.assertIn("disks", payload["unavailableFields"])
        self.assertIn("volumes", payload["unavailableFields"])
        self.assertIn("pools", payload["unavailableFields"])

    def test_system_without_storage_is_unknown(self):
        health = normalize_health_summary(
            target_ref="nas-primary",
            matched_nas_name="synology-primary",
            summary=_load_json(FIXTURE_PARTIAL),
            implementation_id="synology-mcp",
            implementation_version="1.0.0",
        )
        payload = health.to_dict()
        self.assertEqual(payload["overall"]["status"], "UNKNOWN")
        self.assertFalse(payload["overall"]["healthy"])
        self.assertEqual(payload["system"]["model"], "DS220j")
        self.assertEqual(payload["temperatures"]["systemC"], 40)
        self.assertEqual(payload["disks"], [])
        self.assertEqual(payload["volumes"], [])
        self.assertEqual(payload["pools"], [])
        self.assertIsNone(payload["capacity"]["totalBytes"])
        for field in ("disks", "volumes", "pools", "capacity"):
            self.assertIn(field, payload["unavailableFields"])
        self.assertNotIn("system", payload["unavailableFields"])
        blob = json.dumps(payload)
        self.assertNotIn("SECRET-SERIAL-DO-NOT-EMIT", blob)
        self.assertNotIn("105", blob)
        self.assertNotIn("synology_list_nas", blob)

    def test_critical_volume(self):
        health = normalize_health_summary(
            target_ref="nas-primary",
            matched_nas_name="nas1",
            summary={
                "success": True,
                "data": {
                    "volumes": {
                        "volumes": [
                            {
                                "volume_path": "/volume1",
                                "status": "crashed",
                            }
                        ]
                    }
                },
            },
            implementation_id="synology-mcp",
            implementation_version="1.0.0",
        )
        self.assertEqual(health.overall.status, "CRITICAL")


class SynologyMcpResolveTests(unittest.TestCase):

    def test_exact_match(self):
        matched = resolve_nas_name(
            target_ref="nas-primary",
            mapped_nas_name="nas1",
            nas_list=_load_json(FIXTURE_LIST),
        )
        self.assertEqual(matched.nas_name, "nas1")

    def test_does_not_match_note_or_url(self):
        with self.assertRaises(Exception) as ctx:
            resolve_nas_name(
                target_ref="nas-primary",
                mapped_nas_name="primary",
                nas_list=_load_json(FIXTURE_LIST),
            )
        self.assertEqual(ctx.exception.code, "TARGET_NOT_FOUND")

    def test_ignores_message_only_entries(self):
        matched = resolve_nas_name(
            target_ref="nas-primary",
            mapped_nas_name="nas1",
            nas_list=[
                {"message": "No multi-NAS configured"},
                {"nas_name": "nas1"},
            ],
        )
        self.assertEqual(matched.nas_name, "nas1")


class PermissiveMcpClient:
    """Client that would call any tool. The adapter must still refuse."""

    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return {"ok": True}


class SynologyMcpAllowlistTests(unittest.TestCase):

    def test_adapter_cannot_request_arbitrary_mcp_tools(self):
        manifest = load_tool_manifest(TOOL_YAML)
        client = PermissiveMcpClient()
        adapter = SynologyMcpAdapter(
            manifest=manifest,
            bindings=None,  # unused for _call
            client=client,
        )

        with self.assertRaises(AdapterError) as ctx:
            adapter._call("synology_delete_user", {"name": "x"})

        self.assertEqual(ctx.exception.code, "mcp_tool_not_permitted")
        self.assertEqual(client.calls, [])

    def test_adapter_cannot_request_disk_smart(self):
        manifest = load_tool_manifest(TOOL_YAML)
        client = PermissiveMcpClient()
        adapter = SynologyMcpAdapter(
            manifest=manifest,
            bindings=None,
            client=client,
        )

        with self.assertRaises(AdapterError) as ctx:
            adapter._call("synology_disk_smart", {"disk_id": "sata1"})

        self.assertEqual(ctx.exception.code, "mcp_tool_not_permitted")
        self.assertEqual(client.calls, [])
        self.assertEqual(
            ALLOWED_MCP_TOOLS,
            ("synology_list_nas", "synology_health_summary"),
        )


class SynologyMcpPinTests(unittest.TestCase):

    def test_upstream_lock_matches_manifest_and_tool_yaml(self):
        lock_path = (
            ROOT / "tools" / "synology-mcp" / "UPSTREAM.lock"
        )
        lock = {}
        for line in lock_path.read_text().splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            lock[key] = value

        self.assertEqual(
            lock["revision"],
            "6afdaa3407e07c786d79644b92930152751af223",
        )
        self.assertEqual(
            UPSTREAM_PIN,
            f"{lock['repository']}@{lock['revision']}",
        )

        manifest = load_tool_manifest(TOOL_YAML)
        self.assertEqual(manifest.upstream, UPSTREAM_PIN)
        yaml_text = TOOL_YAML.read_text()
        self.assertIn(lock["revision"], yaml_text)
        self.assertIn(lock["repository"], yaml_text)
        notices = (ROOT / "THIRD_PARTY_NOTICES.md").read_text()
        self.assertIn(lock["revision"], notices)
        # Pin must live in ToolFactory, not only in _legacy checkout.
        self.assertTrue(lock_path.is_file())


if __name__ == "__main__":
    unittest.main()
