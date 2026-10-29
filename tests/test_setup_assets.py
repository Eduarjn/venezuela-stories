import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import setup_assets  # noqa: E402
from story_engine import SfxCue  # noqa: E402
from video_editor import find_sfx, pick_music  # noqa: E402


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=duration_ts,sample_rate",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout
    rate, samples = (int(x) for x in out.strip().split(","))
    return samples / rate


@unittest.skipIf(shutil.which("ffmpeg") is None, "FFmpeg não instalado")
class SetupAssetsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.sfx, cls.music = root / "sfx", root / "music"
        cls.sfx.mkdir()
        (cls.sfx / "impact.mp3").write_bytes(b"meu arquivo")  # arquivo do usuário para thunder
        cls.created = setup_assets.setup(cls.sfx, cls.music)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_cue_the_script_can_ask_for_has_a_file(self):
        for cue in get_args(SfxCue):
            self.assertIsNotNone(find_sfx(cue, self.sfx), cue)
        self.assertIsNotNone(pick_music(self.music, 0))

    def test_user_file_wins_and_is_never_overwritten(self):
        self.assertEqual(find_sfx("thunder", self.sfx).name, "impact.mp3")
        self.assertFalse((self.sfx / "thunder_synth.wav").exists())
        self.assertEqual((self.sfx / "impact.mp3").read_bytes(), b"meu arquivo")

    def test_durations_are_exact(self):
        for cue, (seconds, _) in setup_assets.SFX_RECIPES.items():
            path = self.sfx / f"{cue}_synth.wav"
            if path.exists():
                self.assertAlmostEqual(duration(path), seconds, places=3)
        self.assertAlmostEqual(duration(self.music / "tension_drone_synth.wav"),
                               setup_assets.MUSIC_SECONDS, places=3)

    def test_running_again_creates_nothing(self):
        self.assertEqual(setup_assets.setup(self.sfx, self.music), [])

    def test_user_music_makes_drone_unnecessary(self):
        with tempfile.TemporaryDirectory() as tmp:
            music = Path(tmp) / "music"
            music.mkdir()
            (music / "minha_trilha.mp3").write_bytes(b"x")
            setup_assets.setup(self.sfx, music)
            self.assertEqual([p.name for p in music.iterdir()], ["minha_trilha.mp3"])

    def test_thunder_decays(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "t.wav"
            seconds, graph = setup_assets.SFX_RECIPES["thunder"]
            setup_assets._render(graph.format(sr=48000), out, seconds)
            levels = subprocess.run(
                ["ffmpeg", "-hide_banner", "-i", str(out), "-af",
                 "asetnsamples=n=24000,astats=metadata=1:reset=1,"
                 "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"],
                capture_output=True, text=True).stdout
            rms = [float(line.split("=")[1]) for line in levels.splitlines() if "RMS_level" in line]
            self.assertGreater(rms[0] - rms[-1], 20, rms)  # cai mais de 20 dB


class FindSfxPriorityTest(unittest.TestCase):
    def test_synthetic_is_only_the_last_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            sfx = Path(tmp)
            (sfx / "whoosh_synth.wav").write_bytes(b"x")
            self.assertEqual(find_sfx("whoosh", sfx).name, "whoosh_synth.wav")
            (sfx / "whoosh.mp3").write_bytes(b"x")
            self.assertEqual(find_sfx("whoosh", sfx).name, "whoosh.mp3")


if __name__ == "__main__":
    unittest.main()
