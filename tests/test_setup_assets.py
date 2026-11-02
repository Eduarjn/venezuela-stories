import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

import setup_assets  # noqa: E402
from image_sources import Candidate  # noqa: E402
from video_editor import find_sfx, pick_music, sfx_variants  # noqa: E402


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
        (cls.sfx / "crash.mp3").write_bytes(b"meu arquivo")  # arquivo do usuário para impact
        cls.created = setup_assets.setup(cls.sfx, cls.music, library=False)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_every_effect_has_a_sound_even_without_internet(self):
        # Sem internet não há gravações: cada efeito ainda tem ao menos uma versão sintetizada.
        for cue in setup_assets.expected_cues():
            files = sfx_variants(cue, self.sfx)
            self.assertTrue(files, cue)
            if cue != "impact":  # para impact, o arquivo do usuário vem antes (teste abaixo)
                self.assertTrue(all("_synth" in f.name for f in files), (cue, files))
        self.assertEqual(len(sfx_variants("whoosh", self.sfx)), 3)
        self.assertIsNotNone(pick_music(self.music, 0))

    def test_user_file_wins_and_is_never_overwritten(self):
        self.assertEqual([p.name for p in sfx_variants("impact", self.sfx)], ["crash.mp3"])
        self.assertEqual((self.sfx / "crash.mp3").read_bytes(), b"meu arquivo")

    def test_durations_are_exact(self):
        for stem, (seconds, _) in setup_assets.SFX_RECIPES.items():
            self.assertAlmostEqual(duration(self.sfx / f"{stem}.wav"), seconds, places=3)
        self.assertAlmostEqual(duration(self.music / "tension_drone_synth.wav"),
                               setup_assets.MUSIC_SECONDS, places=3)

    def test_running_again_creates_nothing(self):
        self.assertEqual(setup_assets.setup(self.sfx, self.music, library=False), [])

    def test_user_music_makes_drone_unnecessary(self):
        with tempfile.TemporaryDirectory() as tmp:
            music = Path(tmp) / "music"
            music.mkdir()
            (music / "minha_trilha.mp3").write_bytes(b"x")
            setup_assets.setup(self.sfx, music, library=False)
            self.assertEqual([p.name for p in music.iterdir()], ["minha_trilha.mp3"])

    def test_thunder_decays(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "t.wav"
            seconds, graph = setup_assets.SFX_RECIPES["thunder_synth"]
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


class LibraryTest(unittest.TestCase):
    """Gravações do Commons: corta os trechos certos e respeita a licença (sem rede)."""

    @staticmethod
    def recording(seconds=12.0, bursts=(3.0, 8.0)):
        t = np.arange(int(seconds * setup_assets.SAMPLE_RATE)) / setup_assets.SAMPLE_RATE
        x = 0.01 * np.sin(2 * np.pi * 220 * t)
        for b in bursts:  # dois estrondos
            x += np.where((t >= b) & (t < b + 0.6), 0.8 * np.sin(2 * np.pi * 60 * t), 0)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = Path(f.name)
        setup_assets._write_wav(path, x.astype(np.float32))
        data = path.read_bytes()
        path.unlink()
        return data

    def candidate(self, attribution=False):
        return Candidate("wikimedia", "u", "p", "Trovão", "Autor", "Public domain", "", 0, 0, attribution, kind="Som")

    def test_peaks_are_cut_around_each_burst_and_beds_have_exact_length(self):
        data = self.recording()
        with tempfile.TemporaryDirectory() as tmp:
            sfx = Path(tmp)
            sources = (("thunder", "Trovao.ogg", "peak", 2.0, 2), ("rain", "Chuva.ogg", "steady", 3.0, 2))
            created = setup_assets.fetch_library(sfx, sources, find=lambda ref, sa, kind: (self.candidate(), ["u"]),
                                                 fetch=lambda url: data)
            self.assertEqual(len(created), 4)
            thunders = sorted(sfx.glob("thunder_lib_*.wav"))
            self.assertEqual(len(thunders), 2)
            for path in thunders:  # cada trecho começa logo antes de um estrondo e o contém
                self.assertAlmostEqual(duration(path), 2.0, places=2)
            for path in sfx.glob("rain_lib_*.wav"):
                self.assertAlmostEqual(duration(path), 3.0, places=2)
            # Rodar de novo não baixa nada.
            self.assertEqual(setup_assets.fetch_library(sfx, sources, find=lambda *a, **k: 1 / 0), [])

    def test_segments_start_right_before_the_burst(self):
        samples = np.frombuffer(self.recording(bursts=(5.0,))[44:], "<i2").astype(np.float32) / 32767
        (start, end), = setup_assets.pick_segments(samples, "peak", 2.0, 1)
        self.assertTrue(4.7 <= start / setup_assets.SAMPLE_RATE <= 5.0, start / setup_assets.SAMPLE_RATE)

    def test_sound_that_requires_credit_or_fails_to_download_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            sfx = Path(tmp)
            sources = (("thunder", "Trovao.ogg", "peak", 2.0, 1),)
            self.assertEqual(setup_assets.fetch_library(
                sfx, sources, find=lambda ref, sa, kind: (self.candidate(attribution=True), ["u"]),
                fetch=lambda url: b""), [])
            self.assertEqual(setup_assets.fetch_library(
                sfx, sources, find=lambda ref, sa, kind: (self.candidate(), ["u"]),
                fetch=lambda url: (_ for _ in ()).throw(ConnectionError("429"))), [])
            self.assertEqual(list(sfx.iterdir()), [])  # nem o arquivo temporário fica
