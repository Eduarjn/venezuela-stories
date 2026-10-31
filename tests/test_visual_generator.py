import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from visual_generator import (  # noqa: E402
    BACKOFF_SECONDS,
    DEFAULT_STYLE_PREFIX,
    HEIGHT,
    IMAGE_PROMPT_SUFFIX,
    MOTION_TYPES,
    WIDTH,
    GeminiImageClient,
    NoImageReturned,
    QuotaExhausted,
    VisualGenerator,
    VisualGeneratorError,
    build_image_prompt,
    classify_rate_limit,
    estimate_duration,
    fit_vertical,
    motion_filter,
    render_clip,
)

DAY = date(2026, 11, 1)


def png_bytes(size=(768, 1344), color=(120, 40, 40)):
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class ApiError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.code = code


class FakeImageClient:
    """Responde em sequência: bytes de imagem ou uma exceção a lançar."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else png_bytes()
        if isinstance(reply, Exception):
            raise reply
        return reply


def gemini_429(limit="0", quota_id="GenerateRequestsPerDayPerProjectPerModel-FreeTier", delay="19s"):
    """Erro 429 real do SDK, no formato que a API do Gemini devolve."""
    from google.genai import errors

    return errors.ClientError(429, {"error": {
        "code": 429, "status": "RESOURCE_EXHAUSTED",
        "message": ("You exceeded your current quota. Quota exceeded for metric: "
                    "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                    f"limit: {limit}, model: gemini-3.1-flash-image\nPlease retry in 19.8s."),
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{"quotaId": quota_id}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
        ]}})


def fake_render(image, motion, duration, output):
    output.write_bytes(b"clip")
    return output


class VisualGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "output"
        self.episodes = Path(self.tmp.name) / "assets" / "episodes"
        self.waits = []

    def tearDown(self):
        self.tmp.cleanup()

    def generator(self, client, render=fake_render):
        return VisualGenerator(image_client=client, output_dir=self.out, episodes_dir=self.episodes,
                               sleep=self.waits.append, render=render)

    def write_episode(self, n_scenes=3):
        folder = self.out / DAY.isoformat()
        folder.mkdir(parents=True)
        scenes = [{"scene_id": i, "narration_text": "Uma frase curta de teste para a cena.",
                   "image_prompt": f"Lake at night, scene {i}", "motion_type": MOTION_TYPES[i % 5],
                   "sfx_cue": "whoosh"} for i in range(1, n_scenes + 1)]
        (folder / "current_episode.json").write_text(
            json.dumps({"topic_id": 7, "scenes": scenes}), encoding="utf-8")
        return folder

    # -- imagem --------------------------------------------------------------

    def test_image_is_cropped_to_vertical_1080x1920(self):
        img = fit_vertical(png_bytes(size=(1024, 1024)))
        self.assertEqual(img.size, (WIDTH, HEIGHT))

    def test_prompt_gets_style_prefix_and_safety_suffix(self):
        client = FakeImageClient(png_bytes())
        self.generator(client).generate_image("A lake.", self.out / "a.png")
        self.assertTrue(client.prompts[0].startswith(DEFAULT_STYLE_PREFIX))
        self.assertIn("Scene: A lake.", client.prompts[0])
        self.assertTrue(client.prompts[0].endswith(IMAGE_PROMPT_SUFFIX))

    def test_story_engine_style_anchor_is_not_duplicated(self):
        prompt = build_image_prompt(
            "Fishermen at dusk, cinematic historical illustration, dramatic lighting, "
            "rich textures, film grain, 9:16 vertical composition")
        self.assertEqual(prompt.lower().count("film grain"), 1)
        self.assertIn("Scene: Fishermen at dusk.", prompt)

    def test_style_prefix_can_come_from_env(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"VISUAL_STYLE_PREFIX": "Oil painting."}):
            self.assertTrue(build_image_prompt("A lake").startswith("Oil painting. Scene: A lake."))

    def test_retries_with_backoff_then_succeeds(self):
        client = FakeImageClient(ApiError(503), NoImageReturned("vazio"), png_bytes())
        path = self.generator(client).generate_image("A lake", self.out / "a.png")
        self.assertTrue(path.exists())
        self.assertEqual(self.waits, [BACKOFF_SECONDS[0], BACKOFF_SECONDS[1]])

    def test_gives_up_after_one_try_plus_three_retries(self):
        client = FakeImageClient(*[TimeoutError("lento")] * 4)
        with self.assertRaises(VisualGeneratorError):
            self.generator(client).generate_image("A lake", self.out / "a.png")
        self.assertEqual(len(client.prompts), 4)
        self.assertEqual(self.waits, list(BACKOFF_SECONDS))

    def test_free_tier_without_image_quota_fails_fast_without_waiting(self):
        client = FakeImageClient(gemini_429(limit="0"))
        with self.assertRaisesRegex(QuotaExhausted, "faturamento"):
            self.generator(client).generate_image("A lake", self.out / "a.png")
        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(self.waits, [])
        self.assertFalse(QuotaExhausted.retryable)

    def test_daily_quota_used_up_fails_fast(self):
        client = FakeImageClient(gemini_429(limit="100", quota_id="GenerateRequestsPerDayPerProjectPerModel"))
        with self.assertRaises(QuotaExhausted):
            self.generator(client).generate_image("A lake", self.out / "a.png")
        self.assertEqual(len(client.prompts), 1)

    def test_per_minute_limit_waits_what_google_asks_then_succeeds(self):
        client = FakeImageClient(
            gemini_429(limit="10", quota_id="GenerateRequestsPerMinutePerProjectPerModel", delay="37s"),
            png_bytes())
        self.generator(client).generate_image("A lake", self.out / "a.png")
        self.assertEqual(self.waits, [37.0])

    def test_rate_limit_classification(self):
        self.assertEqual(classify_rate_limit(gemini_429(limit="0")), ("hard", None))
        kind, delay = classify_rate_limit(
            gemini_429(limit="10", quota_id="GenerateRequestsPerMinutePerProjectPerModel", delay="12s"))
        self.assertEqual((kind, delay), ("soft", 12.0))

    def test_configuration_error_fails_fast(self):
        client = FakeImageClient(ApiError(404))
        with self.assertRaisesRegex(VisualGeneratorError, "configuração"):
            self.generator(client).generate_image("A lake", self.out / "a.png")
        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(self.waits, [])

    # -- episódio ------------------------------------------------------------

    def test_episode_creates_images_clips_and_manifest(self):
        self.write_episode(3)
        manifest = self.generator(FakeImageClient()).process_episode(DAY)

        visuals = self.episodes / "7" / "visuals"
        self.assertEqual(manifest, visuals / "manifest.json")
        self.assertEqual(sorted(p.name for p in visuals.glob("scene_*.png")),
                         ["scene_01.png", "scene_02.png", "scene_03.png"])
        self.assertEqual(len(list(visuals.glob("scene_*.mp4"))), 3)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(data["episode_id"], 7)
        self.assertTrue(data["durations_are_estimates"])
        self.assertEqual([s["scene_id"] for s in data["scenes"]], [1, 2, 3])

    def test_rerun_does_not_regenerate_existing_images(self):
        self.write_episode(3)
        self.generator(FakeImageClient()).process_episode(DAY)
        client = FakeImageClient()
        self.generator(client).process_episode(DAY)
        self.assertEqual(client.prompts, [])

    def test_missing_episode_explains_what_to_run(self):
        with self.assertRaisesRegex(VisualGeneratorError, "story_engine.py"):
            self.generator(FakeImageClient()).process_episode(DAY)

    def test_missing_model_is_explained(self):
        import os
        from unittest import mock

        with mock.patch.dict(os.environ, {"GEMINI_IMAGE_MODEL": "", "IMAGE_PROVIDER": "gemini"}):
            with self.assertRaisesRegex(VisualGeneratorError, "--list-models"):
                VisualGenerator(output_dir=self.out)

    # -- movimento -----------------------------------------------------------

    def test_estimate_duration_has_minimum_and_tail(self):
        self.assertEqual(estimate_duration("Curta."), 3.0)
        self.assertAlmostEqual(estimate_duration(" ".join(["palavra"] * 13)), 5.5)

    def test_every_motion_builds_a_subpixel_filter(self):
        for motion in MOTION_TYPES:
            vf = motion_filter(motion, 4.0)
            self.assertIn("perspective=", vf)
            self.assertIn("interpolation=cubic:eval=frame", vf)  # subpixel, quadro a quadro
            self.assertNotIn("zoompan", vf)  # arredonda para pixel inteiro e treme (bug #4298)
            self.assertIn("scale=1080:1920", vf)
        self.assertIn("eq=brightness", motion_filter("pulse_glow", 4.0))

    def test_zoom_in_fast_is_fast_for_half_a_second_then_slow(self):
        vf = motion_filter("zoom_in_fast", 4.0)
        self.assertIn("if(lt(on,15),1+0.25*on/15,1.25+0.08*(on-15)/105)", vf)

    def test_finish_has_dynamic_vignette_and_grain(self):
        vf = motion_filter("pan_left_to_right", 2.0)
        self.assertIn("vignette=angle='PI/5+0.05*sin(2*PI*t/2.5)':eval=frame", vf)
        self.assertIn("noise=alls=7:allf=t+u", vf)

    def test_episode_without_topic_id_is_rejected(self):
        folder = self.out / DAY.isoformat()
        folder.mkdir(parents=True)
        (folder / "current_episode.json").write_text('{"scenes": []}', encoding="utf-8")
        with self.assertRaisesRegex(VisualGeneratorError, "topic_id"):
            self.generator(FakeImageClient()).process_episode(DAY)

    def test_unknown_motion_is_rejected(self):
        with self.assertRaises(ValueError):
            motion_filter("spin_360", 3.0)

    @unittest.skipIf(shutil.which("ffmpeg") is None, "FFmpeg não instalado")
    def test_pan_moves_the_same_amount_every_frame(self):
        # Regressão do "vídeo travado": com zoompan a panorâmica andava 1,1 px e 2,3 px em
        # quadros alternados (desvio ~1,2 px). Com perspective o passo fica constante.
        import numpy as np

        rng = np.random.default_rng(7)
        texture = (rng.random((240, 135)) * 255).astype("uint8")
        image = Path(self.tmp.name) / "textura.png"
        from PIL import Image
        Image.fromarray(texture).resize((1080, 1920), Image.BICUBIC).convert("RGB").save(image)
        # 2,5 s: passo fracionário por quadro, como nos vídeos reais (1 s daria passo inteiro).
        clip = render_clip(image, "pan_left_to_right", 2.5, Path(self.tmp.name) / "pan.mp4")
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(clip), "-vf", "crop=256:256:412:832",
                              "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True, check=True).stdout
        frames = np.frombuffer(raw, np.uint8).reshape(-1, 256, 256).astype(np.float64)
        window = np.outer(np.hanning(256), np.hanning(256))

        def dx(a, b):
            spec = np.fft.fft2(a * window) * np.conj(np.fft.fft2(b * window))
            r = np.fft.ifft2(spec / (np.abs(spec) + 1e-9)).real
            py, px = np.unravel_index(np.argmax(r), r.shape)
            left, mid, right = r[py, (px - 1) % 256], r[py, px], r[py, (px + 1) % 256]
            v = px + (left - right) / (2 * (left - 2 * mid + right))
            return v - 256 if v > 128 else v

        steps = np.array([dx(frames[i], frames[i + 1]) for i in range(3, len(frames) - 4)])
        self.assertGreater(abs(steps.mean()), 1.0)  # está andando
        self.assertLess(np.std(np.diff(steps)), 0.2, steps.round(2))  # e sempre no mesmo ritmo

    @unittest.skipIf(shutil.which("ffmpeg") is None, "FFmpeg não instalado")
    def test_ffmpeg_renders_every_motion_with_exact_size_and_frames(self):
        image = Path(self.tmp.name) / "img.png"
        fit_vertical(png_bytes()).save(image)
        for motion in MOTION_TYPES:
            clip = render_clip(image, motion, 1.0, Path(self.tmp.name) / f"{motion}.mp4")
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                 "-show_entries", "stream=width,height,nb_read_frames", "-of", "csv=p=0", str(clip)],
                capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(probe, "1080,1920,30", motion)


class GeminiImageClientTest(unittest.TestCase):
    def test_gemini_model_uses_generate_content(self):
        data = png_bytes()
        part = SimpleNamespace(inline_data=SimpleNamespace(data=data))
        reply = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
        calls = {}

        def generate_content(**kwargs):
            calls.update(kwargs)
            return reply

        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
        self.assertEqual(GeminiImageClient("gemini-x-image", client).generate("p"), data)
        self.assertEqual(calls["config"].image_config.aspect_ratio, "9:16")
        self.assertEqual(calls["config"].response_modalities, ["IMAGE"])

    def test_imagen_model_uses_generate_images(self):
        data = png_bytes()
        generated = SimpleNamespace(image=SimpleNamespace(image_bytes=data), rai_filtered_reason=None)
        calls = {}

        def generate_images(**kwargs):
            calls.update(kwargs)
            return SimpleNamespace(generated_images=[generated])

        client = SimpleNamespace(models=SimpleNamespace(generate_images=generate_images))
        self.assertEqual(GeminiImageClient("imagen-x", client).generate("p"), data)
        self.assertEqual(calls["config"].aspect_ratio, "9:16")

    def test_response_without_image_raises(self):
        reply = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[]))])
        client = SimpleNamespace(models=SimpleNamespace(generate_content=lambda **k: reply))
        with self.assertRaises(NoImageReturned):
            GeminiImageClient("gemini-x-image", client).generate("p")


if __name__ == "__main__":
    unittest.main()
