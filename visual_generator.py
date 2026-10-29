"""Módulo 2 – Imagens (Gemini) e clipes animados (FFmpeg).

Lê output/AAAA-MM-DD/current_episode.json e, para cada cena:
1. gera a imagem no Gemini em 9:16 (até 3 tentativas com backoff);
2. ajusta para 1080x1920 e salva em visuals/scene_XX.png;
3. aplica o movimento (motion_type) via FFmpeg e salva visuals/scene_XX.mp4.

Imagens e clipes que já existem não são gerados de novo: se o processo cair no
meio, rodar outra vez continua de onde parou sem gastar API.

A duração de cada clipe aqui é estimada pelo número de palavras. O editor
(Módulo 4) chama render_clip() de novo com a duração exata da narração.

Uso:
    python visual_generator.py                         # episódio de hoje
    python visual_generator.py --date 2026-11-05
    python visual_generator.py --list-models           # modelos de imagem da sua conta
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Callable

log = logging.getLogger("visual_generator")

PROJECT_DIR = Path(__file__).parent
WIDTH, HEIGHT = 1080, 1920
FPS = 30
MAX_IMAGE_ATTEMPTS = 3
BACKOFF_SECONDS = (2, 4, 8)
# Erros de configuração: tentar de novo não resolve.
NON_RETRYABLE_CODES = {400, 401, 403, 404}

WORDS_PER_SECOND = 2.6
MIN_CLIP_SECONDS = 2.5
# Sobra no fim de cada clipe para o crossfade do editor.
CLIP_TAIL_SECONDS = 0.5

IMAGE_PROMPT_SUFFIX = (
    "No text, no letters, no captions, no watermark, no logos. "
    "No identifiable face of a real person."
)

MOTION_TYPES = ("zoom_in_fast", "pan_left_to_right", "shake_impact", "slow_ken_burns", "pulse_glow")

# Acabamento comum: vinheta leve + granulação de filme.
FINISH_FILTERS = "vignette=PI/5,noise=alls=7:allf=t+u,format=yuv420p"


class VisualGeneratorError(RuntimeError):
    pass


class NoImageReturned(VisualGeneratorError):
    """O modelo respondeu sem imagem (ex.: filtro de segurança)."""


# --------------------------------------------------------------------------- movimento


def motion_filter(motion: str, duration: float, fps: int = FPS,
                  width: int = WIDTH, height: int = HEIGHT) -> str:
    """Filtro FFmpeg (-vf) que transforma uma imagem parada em clipe com movimento.

    A imagem é ampliada 2x antes do zoompan para o movimento não "tremer" por
    arredondamento de pixel.
    """
    if motion not in MOTION_TYPES:
        raise ValueError(f"motion_type desconhecido: {motion!r}")
    n = max(1, round(duration * fps))
    big_w, big_h = width * 2, height * 2
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"

    if motion == "zoom_in_fast":
        # Aproximação forte com desaceleração (ease-out).
        z = f"1+0.35*(1-pow(1-on/{n},2))"
        x, y = center_x, center_y
    elif motion == "slow_ken_burns":
        z = f"1.04+0.08*on/{n}"
        x = f"(iw-iw/zoom)*(0.3+0.4*on/{n})"
        y = "(ih-ih/zoom)*0.45"
    elif motion == "pan_left_to_right":
        z = "1.2"
        x = f"(iw-iw/zoom)*on/{n}"
        y = "(ih-ih/zoom)/2"
    elif motion == "shake_impact":
        # Tremor forte no primeiro meio segundo, que some aos poucos.
        decay = f"max(0,1-on/{fps * 0.5})"
        z = "1.12"
        # Amplitude em pixels da imagem ampliada 2x; a margem com zoom 1.12 é ~115px.
        x = f"(iw-iw/zoom)/2+70*sin(on*1.9)*{decay}"
        y = f"(ih-ih/zoom)/2+55*cos(on*2.7)*{decay}"
    else:  # pulse_glow
        z = f"1.03+0.05*on/{n}"
        x, y = center_x, center_y

    filters = [
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase",
        f"crop={big_w}:{big_h}",
        f"zoompan=z='{z}':x='{x}':y='{y}':d={n}:s={width}x{height}:fps={fps}",
    ]
    if motion == "pulse_glow":
        filters.append(
            "eq=brightness='0.05*sin(2*PI*t*1.1)':saturation='1.12+0.1*sin(2*PI*t*1.1)':eval=frame"
        )
    filters.append(FINISH_FILTERS)
    return ",".join(filters)


def render_clip(image: Path, motion: str, duration: float, output: Path,
                ffmpeg: str = "ffmpeg") -> Path:
    """Gera o clipe animado (H.264, sem áudio) de uma imagem."""
    if shutil.which(ffmpeg) is None:
        raise VisualGeneratorError("FFmpeg não encontrado. Instale e confira com: ffmpeg -version")
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(duration * FPS))
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-i", str(image),
        "-vf", motion_filter(motion, duration),
        "-frames:v", str(frames),
        "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-an",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise VisualGeneratorError(f"FFmpeg falhou em {image.name}: {result.stderr.strip()[-500:]}")
    return output


def estimate_duration(narration: str) -> float:
    words = len(narration.split())
    return round(max(MIN_CLIP_SECONDS, words / WORDS_PER_SECOND) + CLIP_TAIL_SECONDS, 2)


# --------------------------------------------------------------------------- imagem


def fit_vertical(image_bytes: bytes, width: int = WIDTH, height: int = HEIGHT) -> "Image.Image":
    """Corta no centro para 9:16 e redimensiona para width x height."""
    from PIL import Image, ImageOps

    with Image.open(BytesIO(image_bytes)) as img:
        img = img.convert("RGB")
        return ImageOps.fit(img, (width, height), method=Image.LANCZOS, centering=(0.5, 0.5))


def _is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code not in NON_RETRYABLE_CODES


class GeminiImageClient:
    """Gera uma imagem 9:16 com o SDK google-genai.

    Modelos "imagen-*" usam generate_images; modelos Gemini com saída de imagem
    usam generate_content com response_modalities=["IMAGE"].
    """

    def __init__(self, model: str, client=None) -> None:
        self.model = model
        if client is None:
            from google import genai

            if not os.getenv("GEMINI_API_KEY"):
                raise VisualGeneratorError("Defina GEMINI_API_KEY no .env")
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self.client = client

    def generate(self, prompt: str) -> bytes:
        from google.genai import types

        if self.model.startswith("imagen"):
            response = self.client.models.generate_images(
                model=self.model,
                prompt=prompt,
                config=types.GenerateImagesConfig(number_of_images=1, aspect_ratio="9:16"),
            )
            for generated in response.generated_images or []:
                if generated.image and generated.image.image_bytes:
                    return generated.image.image_bytes
            reasons = [g.rai_filtered_reason for g in response.generated_images or []
                       if g.rai_filtered_reason]
            raise NoImageReturned(f"nenhuma imagem retornada {reasons or ''}".strip())

        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
                image_config=types.ImageConfig(aspect_ratio="9:16"),
            ),
        )
        for candidate in response.candidates or []:
            for part in (candidate.content.parts if candidate.content else None) or []:
                if part.inline_data and part.inline_data.data:
                    return part.inline_data.data
        raise NoImageReturned("o modelo respondeu sem imagem (possível filtro de segurança)")


# --------------------------------------------------------------------------- orquestração


@dataclass
class SceneVisual:
    scene_id: int
    image: str
    clip: str
    motion_type: str
    duration: float


class VisualGenerator:
    def __init__(
        self,
        image_client: GeminiImageClient | None = None,
        output_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        render: Callable[[Path, str, float, Path], Path] = render_clip,
    ) -> None:
        if image_client is None:
            model = os.getenv("GEMINI_IMAGE_MODEL")
            if not model:
                raise VisualGeneratorError(
                    "Defina GEMINI_IMAGE_MODEL no .env "
                    "(veja os disponíveis com: python visual_generator.py --list-models)"
                )
            image_client = GeminiImageClient(model)
        self.image_client = image_client
        self.output_dir = output_dir or PROJECT_DIR / os.getenv("OUTPUT_DIR", "output")
        self.sleep = sleep
        self.render = render

    def generate_image(self, prompt: str, path: Path) -> Path:
        """Gera e salva a imagem, com até MAX_IMAGE_ATTEMPTS tentativas e backoff."""
        full_prompt = f"{prompt.rstrip('. ')}. {IMAGE_PROMPT_SUFFIX}"
        last_error: Exception | None = None
        for attempt in range(1, MAX_IMAGE_ATTEMPTS + 1):
            try:
                image = fit_vertical(self.image_client.generate(full_prompt))
                path.parent.mkdir(parents=True, exist_ok=True)
                image.save(path, "PNG")
                return path
            except Exception as exc:
                last_error = exc
                if not _is_retryable(exc):
                    raise VisualGeneratorError(
                        f"{path.name}: erro de configuração/permissão ({exc}); "
                        "confira GEMINI_API_KEY e GEMINI_IMAGE_MODEL"
                    ) from exc
                if attempt < MAX_IMAGE_ATTEMPTS:
                    wait = BACKOFF_SECONDS[attempt - 1]
                    log.warning("%s: tentativa %s falhou (%s); nova tentativa em %ss",
                                path.name, attempt, exc, wait)
                    self.sleep(wait)
        raise VisualGeneratorError(
            f"{path.name}: falhou após {MAX_IMAGE_ATTEMPTS} tentativas ({last_error})"
        ) from last_error

    def process_episode(self, day: date) -> Path:
        folder = self.output_dir / day.isoformat()
        episode_path = folder / "current_episode.json"
        if not episode_path.exists():
            raise VisualGeneratorError(
                f"{episode_path} não existe. Rode antes: python story_engine.py --date {day}"
            )
        episode = json.loads(episode_path.read_text(encoding="utf-8"))
        visuals_dir = folder / "visuals"
        visuals_dir.mkdir(parents=True, exist_ok=True)

        results: list[SceneVisual] = []
        for scene in episode["scenes"]:
            sid = scene["scene_id"]
            image_path = visuals_dir / f"scene_{sid:02d}.png"
            clip_path = visuals_dir / f"scene_{sid:02d}.mp4"
            duration = estimate_duration(scene["narration_text"])

            if image_path.exists():
                log.info("Cena %s: imagem já existe, pulando geração", sid)
            else:
                log.info("Cena %s: gerando imagem", sid)
                self.generate_image(scene["image_prompt"], image_path)

            if not clip_path.exists():
                log.info("Cena %s: animando (%s, %.1fs)", sid, scene["motion_type"], duration)
                self.render(image_path, scene["motion_type"], duration, clip_path)

            results.append(SceneVisual(sid, image_path.name, clip_path.name,
                                       scene["motion_type"], duration))

        manifest = visuals_dir / "manifest.json"
        manifest.write_text(json.dumps({
            "topic_id": episode.get("topic_id"),
            "date": day.isoformat(),
            "resolution": f"{WIDTH}x{HEIGHT}",
            "fps": FPS,
            "durations_are_estimates": True,
            "scenes": [vars(r) for r in results],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Visuais prontos: %s cenas em %s", len(results), visuals_dir)
        return manifest


def list_image_models() -> list[str]:
    from google import genai

    if not os.getenv("GEMINI_API_KEY"):
        raise VisualGeneratorError("Defina GEMINI_API_KEY no .env")
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    names = []
    for model in client.models.list():
        name = (model.name or "").removeprefix("models/")
        if "imagen" in name or "image" in name:
            names.append(name)
    return sorted(names)


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_DIR / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Gera imagens e clipes animados do episódio.")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--list-models", action="store_true",
                        help="lista os modelos de imagem disponíveis na sua conta Gemini")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        if args.list_models:
            for name in list_image_models():
                print(name)
            return 0
        print(VisualGenerator().process_episode(args.date))
    except VisualGeneratorError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
