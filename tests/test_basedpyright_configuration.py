import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BasedpyrightConfigurationTests(unittest.TestCase):
    def test_config_scopes_analysis_and_tracks_a_baseline(self) -> None:
        config = json.loads((ROOT / "pyrightconfig.json").read_text())

        self.assertEqual(config["include"], ["scripts"])
        self.assertEqual(config["extraPaths"], ["scripts"])
        self.assertEqual(config["typeCheckingMode"], "basic")
        self.assertEqual(config["reportMissingImports"], "error")
        self.assertEqual(config["baselineFile"], ".basedpyright/baseline.json")
        self.assertTrue((ROOT / ".basedpyright/baseline.json").is_file())

    def test_documentation_names_configuration_and_command(self) -> None:
        documentation = (ROOT / "docs/configuration.md").read_text()

        self.assertIn("`pyrightconfig.json`", documentation)
        self.assertIn("`.basedpyright/baseline.json`", documentation)
        self.assertIn("`basedpyright`", documentation)
        self.assertIn("`basedpyright --writebaseline`", documentation)


if __name__ == "__main__":
    unittest.main()
