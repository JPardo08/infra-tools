import tempfile
import unittest
from pathlib import Path

import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "tools" / "synology-health" / "src"
TOOL_YAML = ROOT / "tools" / "synology-health" / "tool.yaml"

sys.path.insert(0, str(SRC))

from synology_health.manifest import (
    ManifestError,
    load_tool_manifest,
)


class SynologyManifestAuthorityTests(unittest.TestCase):

    def test_real_tool_yaml_target_refs_agree(self):
        manifest = load_tool_manifest(TOOL_YAML)
        self.assertEqual(manifest.target_ref, "synology-primary")

    def test_disagreeing_target_refs_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="pardo-man.") as tmp:
            path = Path(tmp) / "tool.yaml"
            text = TOOL_YAML.read_text().replace(
                "      targetRef: synology-primary\n",
                "      targetRef: other-target\n",
            )
            path.write_text(text)

            with self.assertRaises(ManifestError) as context:
                load_tool_manifest(path)

            self.assertIn("disagree", str(context.exception))


if __name__ == "__main__":
    unittest.main()
