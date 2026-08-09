#!/usr/bin/env python3
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

WIKI = Path(__file__).with_name("wiki")


class WikiWrapperTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.vault = root / "vault"
        self.vault.mkdir()
        (self.vault / "PURPOSE.md").write_text("# Test\n")
        self.capture = root / "argv.json"
        fake = root / "js"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "open(os.environ['CAPTURE'], 'w').write(json.dumps(sys.argv[1:]))\n"
        )
        fake.chmod(0o755)
        self.env = os.environ | {"WIKI_JS": str(fake), "CAPTURE": str(self.capture)}

    def tearDown(self):
        self.temp.cleanup()

    def run_wiki(self, *args):
        result = subprocess.run([str(WIKI), *args], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.capture.read_text())

    def test_query_routes_agent_and_passes_js_options(self):
        argv = self.run_wiki(
            "query", str(self.vault), "what changed?", "--", "--model", "cheap/model", "--debug"
        )
        self.assertEqual(argv[:4], ["-C", str(self.vault), "--agent", "wiki-query"])
        self.assertIn("--model", argv)
        self.assertIn("--prompt", argv)
        self.assertNotIn("--session", argv)
        self.assertNotIn("--no-save", argv)

    def test_direct_agent_passes_arguments_unchanged(self):
        argv = self.run_wiki("agent", "wiki-query", "-C", str(self.vault), "--prompt", "hello")
        self.assertEqual(argv, ["--agent", "wiki-query", "-C", str(self.vault), "--prompt", "hello"])


if __name__ == "__main__":
    unittest.main()
