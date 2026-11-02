import contextlib
import io
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
        self.assertTrue("[OK] internet" in result.stdout or "sem conexão" in result.stdout, result.stdout)

    def test_functions_called_inside_a_cp1252_process_do_not_crash(self):
        # Caso real: o unittest chama setup_assets.setup()/report() direto, num processo
        # cujo stdout é cp1252 (sem passar por main(), onde fica o utf8_console()).
        sys.path.insert(0, str(ROOT))
        import setup_assets

        cp1252_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(cp1252_stdout):
            sfx, music = Path(tmp) / "sfx", Path(tmp) / "music"
            sfx.mkdir()
            (sfx / "impact.mp3").write_bytes(b"x")  # caminho que imprime "usa seu arquivo"
            setup_assets.setup(sfx, music, library=False)
            setup_assets.report(sfx, music)
            cp1252_stdout.flush()
        self.assertIn(b"[OK]", cp1252_stdout.buffer.getvalue())

    def test_image_search_with_foreign_titles_does_not_crash(self):
        # Títulos do Wikimedia Commons podem ter caracteres que o cp1252 não tem.
        sys.path.insert(0, str(ROOT))
        from unittest import mock

        import image_sources
        import visual_generator

        class Source:
            name = "wikimedia"

            def search(self, query):
                return [image_sources.Candidate("wikimedia", "https://img/x.jpg", "https://commons/x",
                                                "Maracaibo 湖 Ōkami", "Ana", "CC BY 4.0", "",
                                                2000, 1300, True)]

        cp1252_stdout = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch.object(image_sources, "sources_from_env", return_value=[Source()]), \
                mock.patch("logging.basicConfig"), contextlib.redirect_stdout(cp1252_stdout):
            code = visual_generator.main(["--search", "Lake Maracaibo"])
            cp1252_stdout.flush()
        self.assertEqual(code, 0)
        self.assertIn(b"[OK] wikimedia: 1", cp1252_stdout.buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
