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
)
from voice_engine import (  # noqa: E402
    LEAD_SECONDS,
    MAX_GAP_SECONDS,
    TAIL_SECONDS,
    Speech,
    VoiceEngine,
    VoiceEngineError,
    Word,
    plan_segments,
    pronunciation_text,
    remap_words,
    restore_spelling,
    words_from_characters,
)

def pixels(img):
    # Pillow >= 12.1 tem get_flattened_data; getdata sai no Pillow 14.
    return getattr(img, "get_flattened_data", img.getdata)()


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
        self.assertEqual(segments, [(0.5 - LEAD_SECONDS, 0.9 + MAX_GAP_SECONDS / 2),
                                    (1.7 - MAX_GAP_SECONDS / 2, 2.6 + TAIL_SECONDS)])
        remapped = remap_words(words, segments)
        gap = remapped[1].start - remapped[0].end
        self.assertAlmostEqual(gap, MAX_GAP_SECONDS, places=3)
        self.assertAlmostEqual(remapped[0].start, LEAD_SECONDS, places=3)

    def test_dramatic_pause_up_to_half_a_second_is_kept(self):
        # Reticências viram pausa de ~0,45 s no TTS: antes eram cortadas para 0,25 s.
        words = [Word("lago...", 0.0, 0.4), Word("e", 0.85, 1.0)]
        self.assertEqual(len(plan_segments(words, 1.2)), 1)
        self.assertGreaterEqual(MAX_GAP_SECONDS, 0.45)

    def test_spanish_names_are_respelled_for_the_portuguese_voice_only(self):
        spoken, restore = pronunciation_text(
            "Conta-se que o almirante Padilla viu Los Llanos. Rómulo Betancourt e Hollywood.", "pt-BR")
        self.assertEqual(spoken, "Conta-se que o almirante Padiya viu Los Yanos. Rómulo Betancur e Hollywood.")
        self.assertEqual(restore, {"padiya": "Padilla", "yanos": "Llanos", "betancur": "Betancourt"})
        self.assertEqual(pronunciation_text("El almirante Padilla", "es-419"), ("El almirante Padilla", {}))

    def test_subtitle_words_get_the_original_spelling_back(self):
        words = [Word("almirante", 0.0, 0.4), Word("Padiya", 0.4, 0.9)]
        back = restore_spelling(words, {"padiya": "Padilla"})
        self.assertEqual([w.text for w in back], ["almirante", "Padilla"])
        self.assertEqual((back[1].start, back[1].end), (0.4, 0.9))

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
        self.assertEqual(texts, [["Em", "1999"], ["o", "céu", "caiu."],
                                 ["Ninguém", "esperava"], ["aquilo", "naquela", "noite"]])

    def test_chunks_break_where_speech_breathes(self):
        # Vírgula do roteiro, artigo/preposição pendurado e nome próprio guiam a quebra.
        from video_editor import mark_sentence_ends

        def chunks(tts_words, narration):
            marked = mark_sentence_ends(self.words(tts_words), narration)
            return [" ".join(w.text for w in c) for c in chunk_words(marked)]

        self.assertEqual(chunks("Neste lago o céu explode quase 300 noites por ano".split(),
                                "Neste lago, o céu explode quase 300 noites por ano."),
                         ["Neste lago", "o céu explode quase", "300 noites por ano"])
        # O edge-tts devolve "em 2010" como uma palavra só; a vírgula depois de 2010 vale.
        self.assertEqual(chunks(["Mas", "em 2010", "uma", "seca", "ligada", "ao", "El", "Niño",
                                 "apagou", "o", "céu"],
                                "Mas em 2010, uma seca ligada ao El Niño apagou o céu."),
                         ["Mas em 2010", "uma seca ligada", "ao El Niño", "apagou o céu"])
        self.assertIn("Lago de Maracaibo",
                      chunks("Estamos no Lago de Maracaibo no noroeste da Venezuela".split(),
                             "Estamos no Lago de Maracaibo, no noroeste da Venezuela."))

    def test_mark_sentence_ends_also_marks_commas_and_dashes(self):
        from video_editor import mark_sentence_ends

        words = mark_sentence_ends(self.words(["Mas", "em 2010", "uma", "seca", "voltou", "e", "segue"]),
                                   "Mas em 2010, uma seca voltou — e segue.")
        self.assertEqual([w.ends_clause for w in words], [False, True, False, False, True, False, False])
        self.assertEqual([w.ends_sentence for w in words], [False] * 6 + [True])

    def test_tts_words_without_punctuation_still_break_at_sentence_and_scene(self):
        # O edge-tts devolve "Auyán-tepui" sem o ponto; o fim da frase vem do narration_text.
        from video_editor import mark_sentence_ends

        cena12 = mark_sentence_ends(
            self.words("Estão no topo do Auyán-tepui Cercados de abismos".split()),
            "Estão no topo do Auyán-tepui. Cercados de abismos.")
        cena13 = mark_sentence_ends(
            self.words("Lá em cima isolados restava uma única opção".split(), start=2.4),
            "Lá em cima, isolados... restava uma única opção.")
        texts = [[w.text for w in c] for c in chunk_words(cena12 + cena13)]
        self.assertIn(["Cercados", "de", "abismos"], texts)
        self.assertIn(["Lá", "em", "cima", "isolados"], texts)
        for chunk in texts:
            self.assertFalse("Auyán-tepui" in chunk and "Cercados" in chunk, texts)
            self.assertFalse("abismos" in chunk and "Lá" in chunk, texts)

    def test_mark_sentence_ends_tolerates_tts_differences(self):
        from video_editor import mark_sentence_ends

        # Ponto depois de número também fecha a frase; a última palavra da cena sempre fecha.
        words = mark_sentence_ends(self.words(["Em", "1937", "ele", "volta", "decidido"]),
                                   "Em 1937. Ele volta, decidido")
        self.assertEqual([w.ends_sentence for w in words], [False, True, False, False, True])

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
            (sfx / "suspense_riser.mp3").write_bytes(b"x")
            (sfx / "crash.wav").write_bytes(b"x")
            self.assertEqual(find_sfx("riser", sfx).name, "suspense_riser.mp3")  # nome antigo ainda vale
            self.assertEqual(find_sfx("impact", sfx).name, "crash.wav")
            self.assertIsNone(find_sfx("thunder", sfx))  # batida não serve de trovão
            self.assertIsNone(find_sfx("crowd", sfx))

    def test_sound_variants_alternate_recordings_and_synthetic_is_only_a_fallback(self):
        from video_editor import sfx_variants

        with tempfile.TemporaryDirectory() as tmp:
            sfx = Path(tmp)
            for name in ("thunder_synth.wav", "thunder_lib_a1b2c3_2.wav", "thunder_lib_a1b2c3_1.wav",
                         "suspense_riser_synth.wav", "riser_synth2.wav"):
                (sfx / name).write_bytes(b"x")
            self.assertEqual([p.name for p in sfx_variants("thunder", sfx)],
                             ["thunder_lib_a1b2c3_1.wav", "thunder_lib_a1b2c3_2.wav"])
            self.assertEqual([p.name for p in sfx_variants("riser", sfx)],
                             ["riser_synth2.wav", "suspense_riser_synth.wav"])
            (sfx / "thunder.mp3").write_bytes(b"meu")  # arquivo seu vem primeiro
            self.assertEqual(find_sfx("thunder", sfx).name, "thunder.mp3")
            self.assertEqual(sfx_variants("plane", sfx), [])

    def test_changed_image_renders_a_new_clip_and_drops_the_old_one(self):
        # --redo-images troca a imagem com a mesma duração: o clipe antigo não pode ser reaproveitado.
        from unittest import mock

        import video_editor

        with tempfile.TemporaryDirectory() as tmp:
            work, image = Path(tmp), Path(tmp) / "scene_01.png"
            image.write_bytes(b"imagem antiga")
            plan = video_editor.ScenePlan(1, image, work / "a.wav", "slow_ken_burns", "whoosh",
                                          None, 30, 0.0, [])
            rendered = []

            def fake_render(img, motion, duration, out, transition=None):
                rendered.append(out.name)
                out.write_bytes(b"clip")
            with mock.patch.object(video_editor.shutil, "which", return_value="ffmpeg"), \
                    mock.patch.object(video_editor, "render_clip", side_effect=fake_render), \
                    mock.patch.object(video_editor, "_run"):
                editor = VideoEditor(output_dir=work, episodes_dir=work, assets_dir=work)
                editor.render_scenes([plan], work)
                editor.render_scenes([plan], work)  # mesma imagem: reaproveita
                image.write_bytes(b"imagem nova")
                editor.render_scenes([plan], work)  # imagem trocada: refaz
            self.assertEqual(len(rendered), 2)
            self.assertNotEqual(rendered[0], rendered[1])
            self.assertEqual([p.name for p in work.glob("clip_*.mp4")], [rendered[1]])

    def test_real_footage_scenes_continue_the_same_video_without_a_cut(self):
        from unittest import mock

        import video_editor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "out" / DAY.isoformat()).mkdir(parents=True)
            falas = {1: "Um dois", 2: "Em 1999 tudo mudou", 3: "Os raios caem"}
            scenes = [{"scene_id": i, "narration_text": falas[i], "motion_type": "slow_ken_burns",
                       "sfx_cue": "thunder"} for i in (1, 2, 3)]
            (root / "out" / DAY.isoformat() / "current_episode.json").write_text(
                json.dumps({"topic_id": 7, "scenes": scenes}), encoding="utf-8")
            base = root / "episodes" / "7"
            (base / "audio").mkdir(parents=True)
            (base / "visuals").mkdir()
            words = [{"text": "Um", "start": 0.0, "end": 0.4}, {"text": "dois", "start": 0.5, "end": 0.9}]
            (base / "audio" / "timeline.json").write_text(json.dumps({"scenes": [
                {"scene_id": i, "audio": f"scene_{i:02d}.wav", "duration": 2.0, "words": words}
                for i in (1, 2, 3)]}), encoding="utf-8")
            for i in (1, 2, 3):
                (base / "visuals" / f"scene_{i:02d}.png").write_bytes(b"png")
            (base / "visuals" / "v.webm").write_bytes(b"filmagem")
            (base / "visuals" / "videos.json").write_text(json.dumps({
                "2": {"file": "v.webm", "start": 1.5}, "3": {"file": "v.webm"}}), encoding="utf-8")
            with mock.patch.object(video_editor.shutil, "which", return_value="ffmpeg"):
                editor = VideoEditor(output_dir=root / "out", episodes_dir=root / "episodes",
                                     assets_dir=root)
                _, plans, work = editor.plan(DAY)
                self.assertIsNone(plans[0].video)
                self.assertEqual((plans[1].video.name, plans[1].video_start, plans[1].transition),
                                 ("v.webm", 1.5, "dip"))
                # A cena 3 continua do ponto onde a 2 parou: sem o clarão do raio no meio da filmagem.
                self.assertEqual((plans[2].video_start, plans[2].transition), (1.5 + plans[1].duration, None))

                rendered = []
                with mock.patch.object(video_editor, "render_video_clip",
                                       side_effect=lambda v, s, d, out, transition=None: (
                                           rendered.append((v.name, s)), out.write_bytes(b"c"))), \
                        mock.patch.object(video_editor, "render_clip",
                                          side_effect=lambda *a, **k: a[3].write_bytes(b"c")), \
                        mock.patch.object(video_editor, "_run"):
                    work.mkdir(parents=True)
                    editor.render_scenes(plans, work)
                self.assertEqual(rendered, [("v.webm", 1.5), ("v.webm", 1.5 + plans[1].duration)])


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
            {"scene_id": 2, "narration_text": "Em 1999 a chuva caiu", "motion_type": "pan_left_to_right",
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

        # boom: efeito curto do gancho; rain: ambiente (em loop sob a cena 2, que dura mais).
        for name, kind in [("whoosh", "anoisesrc=d=0.4:a=0.3"), ("boom", "sine=f=60:d=0.5"),
                           ("rain", "anoisesrc=d=1.0:a=0.2")]:
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
            self.assertAlmostEqual(words[0]["start"], LEAD_SECONDS, places=2)
            gaps = [b["start"] - a["end"] for a, b in zip(words, words[1:])]
            self.assertLessEqual(max(gaps), MAX_GAP_SECONDS + 0.01)
            # 3,2 s de áudio cru: a pausa de 0,8 s cai para MAX_GAP e o silêncio das pontas some
            expected = (0.4 + MAX_GAP_SECONDS / 2 + LEAD_SECONDS) + (0.9 + MAX_GAP_SECONDS / 2 + TAIL_SECONDS)
            self.assertAlmostEqual(scene["duration"], expected, delta=0.03)

    def test_final_video_has_exact_duration_resolution_and_audio(self):
        info = json.loads(self.probe("-show_entries", "stream=codec_type,width,height,r_frame_rate",
                                     "-show_entries", "format=duration"))
        kinds = {s["codec_type"] for s in info["streams"]}
        self.assertEqual(kinds, {"video", "audio"})
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"], video["r_frame_rate"]), (1080, 1920, "30/1"))
        render = json.loads((self.episodes / "42" / "edit" / "render.json").read_text())
        self.assertAlmostEqual(float(info["format"]["duration"]), render["duration_seconds"], delta=0.06)
        # Corte seco no gancho, mergulho no preto no salto no tempo ("Em 1999") e corte seco.
        self.assertEqual([s["transition"] for s in render["scenes"]], [None, "dip", None])
        self.assertEqual([s["sfx_cue"] for s in render["scenes"]], ["boom", "rain", None])
        for scene in render["scenes"]:
            self.assertEqual(scene["frames"], round(scene["duration"] * 30))

    def test_real_footage_clip_is_vertical_with_exact_frames_even_past_the_end(self):
        from visual_generator import render_video_clip

        footage = Path(self.tmp.name) / "filmagem.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc=size=320x240:rate=25:duration=2", str(footage)], check=True)
        for start in (0.5, 1.5):  # 1.5 + 1.0 passa do fim: o último quadro fica parado
            clip = Path(self.tmp.name) / f"real_{start}.mp4"
            render_video_clip(footage, start, 1.0, clip, transition="flash")
            info = json.loads(subprocess.run(
                ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
                 "stream=width,height,nb_read_frames", "-of", "json", str(clip)],
                capture_output=True, text=True, check=True).stdout)["streams"][0]
            self.assertEqual((info["width"], info["height"], int(info["nb_read_frames"])), (1080, 1920, 30))

    def test_subtitle_is_burned_in_yellow_at_eye_level(self):
        timeline = json.loads(self.timeline.read_text(encoding="utf-8"))
        t = timeline["scenes"][0]["words"][0]["start"] + 0.1
        frame = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(self.video),
                                "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True, check=True).stdout
        img = Image.open(io.BytesIO(frame)).convert("RGB")
        band = img.crop((0, 1080, 1080, 1280))
        yellow = sum(1 for r, g, b in pixels(band) if r > 200 and g > 170 and b < 90)
        self.assertGreater(yellow, 500, "nenhuma palavra amarela na altura dos olhos")
        top = img.crop((0, 100, 1080, 300))
        self.assertEqual(sum(1 for r, g, b in pixels(top) if r > 200 and g > 170 and b < 90), 0)


class EdgeProxyTest(unittest.TestCase):
    """edge-tts (aiohttp) ignora HTTPS_PROXY; o projeto passa o proxy explicitamente."""

    def test_proxy_comes_from_env_when_set(self):
        from unittest import mock
        from voice_engine import EDGE_TTS_HOST, https_proxy_for

        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:9"}, clear=True):
            self.assertEqual(https_proxy_for(EDGE_TTS_HOST), "http://127.0.0.1:9")

    def test_no_proxy_env_means_direct_connection(self):
        from unittest import mock
        from voice_engine import EDGE_TTS_HOST, https_proxy_for

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(https_proxy_for(EDGE_TTS_HOST))

    def test_host_in_no_proxy_skips_the_proxy(self):
        from unittest import mock
        from voice_engine import EDGE_TTS_HOST, https_proxy_for

        env = {"HTTPS_PROXY": "http://127.0.0.1:9", "NO_PROXY": "localhost,speech.platform.bing.com"}
        with mock.patch.dict("os.environ", env, clear=True):
            self.assertIsNone(https_proxy_for(EDGE_TTS_HOST))

    def test_edge_tts_receives_the_proxy(self):
        import types
        from unittest import mock
        import voice_engine

        seen = {}

        class FakeCommunicate:
            def __init__(self, text, voice, **kwargs):
                seen.update(kwargs)

            async def stream(self):
                yield {"type": "audio", "data": b"mp3"}
                yield {"type": "WordBoundary", "offset": 0, "duration": 5_000_000, "text": "Olá"}

        fake = types.SimpleNamespace(Communicate=FakeCommunicate)
        with mock.patch.dict("sys.modules", {"edge_tts": fake}), \
                mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://127.0.0.1:9"}, clear=True):
            speech = voice_engine.EdgeTTSProvider(voices={"pt-BR": "v"}, rate="+0%").synthesize("Olá", "pt-BR")
        self.assertEqual(seen["proxy"], "http://127.0.0.1:9")
        self.assertEqual(speech.audio, b"mp3")


if __name__ == "__main__":
    unittest.main()
