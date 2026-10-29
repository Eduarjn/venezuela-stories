import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class WindowsConsoleTest(unittest.TestCase):
    """Saída redirecionada no Windows usa cp1252, que não tem ✓/✗."""

    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, "PYTHONIOENCODING": "cp1252", "OUTPUT_DIR": tmp}
            return subprocess.run([sys.executable, *args], cwd=ROOT, env=env,
                                  capture_output=True, text=True, encoding="utf-8", errors="replace")

    def test_setup_assets_list_does_not_crash(self):
        result = self.run_cli("setup_assets.py", "--list")
        self.assertNotIn("UnicodeEncodeError", result.stderr)

    def test_main_check_does_not_crash(self):
        result = self.run_cli("main.py", "--check")
        self.assertNotIn("UnicodeEncodeError", result.stderr)
        self.assertTrue("✓ internet" in result.stdout or "sem conexão" in result.stdout, result.stdout)


if __name__ == "__main__":
    unittest.main()
