import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "synology-mcp" / "src"
sys.path.insert(0, str(SRC))

from synology_mcp.errors import AdapterError
from synology_mcp.mcp_client import StdioMcpClient, child_env


FAKE_SERVER = r'''
import json
import sys

def read_message():
    line = sys.stdin.buffer.readline()
    if not line:
        raise SystemExit(0)
    return json.loads(line.decode("utf-8"))

def write_message(payload):
    body = json.dumps(payload).encode("utf-8")
    sys.stdout.buffer.write(body + b"\n")
    sys.stdout.buffer.flush()

while True:
    message = read_message()
    method = message.get("method")
    if method == "initialize":
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake"}},
        })
    elif method == "notifications/initialized":
        continue
    elif method == "tools/call":
        name = message["params"]["name"]
        if name == "synology_list_nas":
            text = json.dumps([{"nas_name": "nas1", "connected": True}])
        elif name == "synology_health_summary":
            text = json.dumps({"success": True, "data": {"system": {"model": "DS220j"}}})
        else:
            write_message({
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {"isError": True, "content": [{"type": "text", "text": "denied"}]},
            })
            continue
        write_message({
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {"content": [{"type": "text", "text": text}]},
        })
    else:
        write_message({
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32601, "message": "unknown"},
        })
'''


class StdioMcpClientTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="pardo-mcp-stdio.")
        self.script = Path(self.tmpdir.name) / "fake_mcp.py"
        self.script.write_text(FAKE_SERVER)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_allowlisted_tools_over_stdio(self):
        client = StdioMcpClient(
            argv=[sys.executable, str(self.script)],
            env=child_env({"PATH": os.environ.get("PATH", "")}),
            timeout_s=5,
        )
        try:
            listed = client.call_tool("synology_list_nas", {})
            summary = client.call_tool(
                "synology_health_summary",
                {"nas_name": "nas1"},
            )
        finally:
            client.close()

        self.assertEqual(listed[0]["nas_name"], "nas1")
        self.assertEqual(summary["data"]["system"]["model"], "DS220j")

    def test_unlisted_tool_is_blocked_before_rpc(self):
        client = StdioMcpClient(
            argv=[sys.executable, str(self.script)],
            env={"PATH": "/usr/bin:/bin"},
            timeout_s=5,
        )
        try:
            with self.assertRaises(AdapterError) as ctx:
                client.call_tool("synology_delete_user", {"name": "x"})
            self.assertEqual(ctx.exception.code, "mcp_tool_not_permitted")
        finally:
            client.close()

    def test_child_env_strips_secrets(self):
        env = child_env(
            {
                "PATH": "/usr/bin",
                "HOME": "/tmp",
                "SYNOLOGY_PASSWORD": "secret",
                "PARDO_TOOL_BINDINGS_PATH": "/tmp/bindings.json",
            }
        )
        self.assertNotIn("SYNOLOGY_PASSWORD", env)
        self.assertNotIn("PARDO_TOOL_BINDINGS_PATH", env)
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_stdio_framing_is_ndjson_not_content_length(self):
        client = StdioMcpClient(argv=["/usr/bin/true"], timeout_s=1)

        class _Proc:
            def __init__(self):
                self.stdin = io.BytesIO()

        proc = _Proc()
        client._proc = proc
        client._write(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
        )
        raw = proc.stdin.getvalue()
        self.assertFalse(raw.startswith(b"Content-Length"))
        self.assertNotIn(b"Content-Length", raw)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertEqual(raw.count(b"\n"), 1)
        payload = json.loads(raw.decode("utf-8"))
        self.assertEqual(payload["method"], "initialize")


if __name__ == "__main__":
    unittest.main()
