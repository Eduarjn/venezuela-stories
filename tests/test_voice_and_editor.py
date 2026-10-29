import io
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from datetime import date
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video_editor import (  # noqa: E402
    DUCK_DB,
    MUSIC_BASE_DB,
    TimedWord,
    VideoEditor,
    _ass_time,
    build_ass,
    chunk_words,
    db_to_gain,
    ducking_expression,
    find_sfx,
    speech_regions,
    transition_for,
)
from voice_engine import (  # noqa: E402
    MAX_GAP_SECONDS,
    Speech,
    VoiceEngine,
    VoiceEngineError,
    Word,
    plan_segments,
    remap_words,
    words_from_characters,
)

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
DAY = date(2026, 11, 1)


def synth_wav(pieces, rate=24000):
    """pieces = [("silence"|"tone", segundos)] -> bytes WAV."""
    frames = bytearray()
    for kind, seconds in pieces:
        for i in range(int(seconds * rate)):
            value = int(8000 * math.sin(2 * math.pi * 330 * i / rate)) if kind == "tone" else 0
            frames += struct.pack("<h", value)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return buf.getvalue()


class FakeTTS:
    """0,5 s de silêncio, palavra, 0,8 s de pausa, palavra, palavra, 0,6 s de silêncio."""

    name = "fake"

    def __init__(self, fail_first=0):
        self.calls = 0
        self.fail_first = fail_first

    def synthesize(self, text, language):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise ConnectionError("rede")
        texts = text.split()[:3]
        audio = synth_wav([("silence", 0.5), ("tone", 0.4), ("silence", 0.8),
                           ("tone", 0.4), ("silence", 0.1), ("tone", 0.4), ("silence", 0.6)])
        words = [Word(texts[0], 0.5, 0.9), Word(texts[1], 1.7, 2.1), Word(texts[2], 2.2, 2.6)]
        return Speech(audio, "wav", words)


# --------------------------------------------------------------------------- unidade


class VoiceUnitTest(unittest.TestCase):
    def test_long_pauses_become_max_gap(self):
        words = [Word("a", 0.5, 0.9), Word("b", 1.7, 2.1), Word("c", 2.2, 2.6)]
        segments = plan_segments(words, 3.2)
        self.assertEqual(segments, [(0.46, 0.9 + MAX_GAP_SECONDS / 2), (1.7 - MAX_GAP_SECONDS / 2, 2.72)])
        remapped = remap_words(words, segments)
        gap = remapped[1].start - remapped[0].end
        self.assertAlmostEqual(gap, MAX_GAP_SECONDS, places=3)
        self.assertAlmostEqual(remapped[0].start, 0.04, places=3)

    def test_short_pauses_are_kept(self):
        words = [Word("a", 0.0, 0.4), Word("b", 0.5, 0.9)]
        self.assertEqual(len(plan_segments(words, 1.0)), 1)

    def test_elevenlabs_characters_become_words(self):
        chars = list("Hola mundo")
        starts = [i * 0.1 for i in range(len(chars))]
        ends = [s + 0.1 for s in starts]
        words = words_from_characters(chars, starts, ends)
        self.assertEqual([w.text for w in words], ["Hola", "mundo"])
        self.assertAlmostEqual(words[1].start, 0.5)
        self.assertAlmostEqual(words[1].end, 1.0)

    def test_no_words_is_an_error(self):
        with self.assertRaises(VoiceEngineError):
            plan_segments([], 1.0)


class EditorUnitTest(unittest.TestCase):
    def words(self, texts, start=0.0, step=0.3):
        return [TimedWord(t, start + i * step, start + i * step + 0.25) for i, t in enumerate(texts)]

    def test_chunks_have_2_to_4_words_and_never_cross_a_sentence(self):
        chunks = chunk_words(self.words("Em 1999 o céu caiu. Ninguém esperava aquilo naquela noite".split()))
        texts = [[w.text for w in c] for c in chunks]
        self.assertTrue(all(2 <= len(c) <= 4 for c in texts), texts)
        self.assertEqual(texts, [["Em", "1999", "o"], ["céu", "caiu."],
                                 ["Ninguém", "esperava"], ["aquilo", "naquela", "noite"]])

    def test_long_pause_also_breaks_the_group(self):
        words = self.words(["Ali", "estava"]) + [TimedWord("ele", 2.0, 2.3), TimedWord("parado", 2.3, 2.6)]
        self.assertEqual([[w.text for w in c] for c in chunk_words(words)],
                         [["Ali", "estava"], ["ele", "parado"]])

    def test_long_words_never_exceed_two_lines(self):
        chunks = chunk_words(self.words("Extraordinariamente impressionante desenvolvimento econômico".split()))
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 2)

    def test_ass_highlights_current_word_and_numbers_in_red(self):
        ass = build_ass(self.words(["O", "ano", "1999", "mudou"]), 5.0, "Arial Black")
        lines = [l for l in ass.splitlines() if l.startswith("Dialogue")]
        self.assertEqual(len(lines), 4)
        self.assertIn("{\\c&H0000D7FF&}O{\\c&H00FFFFFF&}", lines[0])
        self.assertIn("{\\c&H00303BFF&}1999", lines[2])
        self.assertIn("\\pos(540,1180)", lines[0])
        self.assertIn("\\fscx112", lines[0])
        self.assertNotIn("\\fscx112", lines[1])

    def test_ass_time_format(self):
        self.assertEqual(_ass_time(3725.456), "1:02:05.46")

    def test_ducking_is_18db_during_speech_and_base_outside(self):
        regions = speech_regions([TimedWord("a", 1.0, 1.4), TimedWord("b", 1.6, 2.0),
                                  TimedWord("c", 4.0, 4.5)])
        self.assertEqual(regions, [(1.0, 2.0), (4.0, 4.5)])
        expr = ducking_expression(regions)

        def gain(t):
            safe = expr.replace("clip", "_clip")
            return eval(safe, {"_clip": lambda x, a, b: max(a, min(b, x)), "max": max, "min": min, "t": t})

        base = db_to_gain(MUSIC_BASE_DB)
        self.assertAlmostEqual(gain(1.5), base * db_to_gain(DUCK_DB), places=4)
        self.assertAlmostEqual(gain(3.0), base, places=4)
        self.assertLess(gain(2.05), base)  # rampa saindo da fala

    def test_sfx_aliases_find_user_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            sfx = Path(tmp)
            (sfx / "riser.mp3").write_bytes(b"x")
            (sfx / "impact.wav").write_bytes(b"x")
            self.assertEqual(find_sfx("suspense_riser", sfx).name, "riser.mp3")
            self.assertEqual(find_sfx("thunder", sfx).name, "impact.wav")
            self.assertIsNone(find_sfx("crowd_murmur", sfx))

    def test_transitions(self):
        self.assertIsNone(transition_for(0, "whoosh"))
        self.assertEqual(transition_for(3, "whoosh"), "whip")
        self.assertEqual(transition_for(3, "thunder"), "flash")


# --------------------------------------------------------------------------- ponta a ponta


@unittest.skipUnless(HAS_FFMPEG, "FFmpeg não instalado")
class EndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.out, cls.assets = root / "output", root / "assets"
        cls.episodes = cls.assets / "episodes"
        folder = cls.out / DAY.isoformat()
        folder.mkdir(parents=True)
        scenes = [
            {"scene_id": 1, "narration_text": "Um lago acende", "motion_type": "zoom_in_fast",
             "sfx_cue": "thunder", "image_prompt": "x"},
            {"scene_id": 2, "narration_text": "Em 1999 tudo", "motion_type": "pan_left_to_right",
             "sfx_cue": "whoosh", "image_prompt": "x"},
            {"scene_id": 3, "narration_text": "Ninguém sabia porquê", "motion_type": "shake_impact",
             "sfx_cue": "suspense_riser", "image_prompt": "x"},
        ]
        (folder / "current_episode.json").write_text(json.dumps(
            {"topic_id": 42, "language": "pt-BR", "scenes": scenes}), encoding="utf-8")

        visuals = cls.episodes / "42" / "visuals"
        visuals.mkdir(parents=True)
        for i, color in enumerate([(60, 30, 20), (20, 40, 70), (30, 60, 30)], start=1):
            Image.new("RGB", (1080, 1920), color).save(visuals / f"scene_{i:02d}.png")

        for name, kind in [("whoosh", "anoisesrc=d=0.4:a=0.3"), ("impact", "sine=f=60:d=0.5"),
                           ("riser", "sine=f=500:d=0.8")]:
            (cls.assets / "sfx").mkdir(parents=True, exist_ok=True)
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", kind,
                            str(cls.assets / "sfx" / f"{name}.mp3")], check=True)
        (cls.assets / "music").mkdir()
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "sine=f=220:d=2", str(cls.assets / "music" / "tension.mp3")], check=True)

        cls.tts = FakeTTS(fail_first=1)
        cls.timeline = VoiceEngine(provider=cls.tts, output_dir=cls.out, episodes_dir=cls.episodes,
                                   sleep=lambda s: None).process_episode(DAY)
        cls.video = VideoEditor(output_dir=cls.out, episodes_dir=cls.episodes,
                                assets_dir=cls.assets).edit(DAY)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def probe(self, *entries):
        return subprocess.run(["ffprobe", "-v", "error", *entries, "-of", "json", str(self.video)],
                              capture_output=True, text=True, check=True).stdout

    def test_tts_retried_after_network_error(self):
        self.assertEqual(self.tts.calls, 4)  # 1 falha + 3 cenas

    def test_silences_were_tightened(self):
        timeline = json.loads(self.timeline.read_text(encoding="utf-8"))
        for scene in timeline["scenes"]:
            words = scene["words"]
            self.assertAlmostEqual(words[0]["start"], 0.04, places=2)
            gaps = [b["start"] - a["end"] for a, b in zip(words, words[1:])]
            self.assertLessEqual(max(gaps), MAX_GAP_SECONDS + 0.01)
            # 3,2 s de áudio cru viram ~1,86 s
            self.assertLess(scene["duration"], 2.0)

    def test_final_video_has_exact_duration_resolution_and_audio(self):
        info = json.loads(self.probe("-show_entries", "stream=codec_type,width,height,r_frame_rate",
                                     "-show_entries", "format=duration"))
        kinds = {s["codec_type"] for s in info["streams"]}
        self.assertEqual(kinds, {"video", "audio"})
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"], video["r_frame_rate"]), (1080, 1920, "30/1"))
        render = json.loads((self.episodes / "42" / "edit" / "render.json").read_text())
        self.assertAlmostEqual(float(info["format"]["duration"]), render["duration_seconds"], delta=0.06)
        self.assertEqual([s["transition"] for s in render["scenes"]], [None, "whip", "flash"])
        for scene in render["scenes"]:
            self.assertEqual(scene["frames"], round(scene["duration"] * 30))

    def test_subtitle_is_burned_in_yellow_at_eye_level(self):
        timeline = json.loads(self.timeline.read_text(encoding="utf-8"))
        t = timeline["scenes"][0]["words"][0]["start"] + 0.1
        frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(self.video),
                                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True, check=True).stdout
        img = Image.open(io.BytesIO(frame)).convert("RGB")
        band = img.crop((0, 1080, 1080, 1280))
        yellow = sum(1 for r, g, b in band.getdata() if r > 200 and g > 170 and b < 90)
        self.assertGreater(yellow, 500, "nenhuma palavra amarela na altura dos olhos")
        top = img.crop((0, 100, 1080, 300))
        self.assertEqual(sum(1 for r, g, b in top.getdata() if r > 200 and g > 170 and b < 90), 0)


if __name__ == "__main__":
    unittest.main()
