"""Módulo 2 – Imagens (Gemini) e clipes animados (FFmpeg).

Lê output/AAAA-MM-DD/current_episode.json e, para cada cena:
1. monta o prompt com o prefixo fixo de estilo (mesmo "diretor de arte" em todas);
2. gera a imagem no Gemini em 9:16 (1 tentativa + até 3 novas, com backoff e timeout);
3. ajusta para 1080x1920 e salva em assets/episodes/{episode_id}/visuals/scene_XX.png;
4. aplica o movimento (motion_type) via FFmpeg e salva visuals/scene_XX.mp4.

episode_id é o topic_id do banco (gravado no JSON pelo story_engine).

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
import re
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
# 1 tentativa + até 3 novas tentativas.
MAX_IMAGE_ATTEMPTS = 4
BACKOFF_SECONDS = (2, 4, 8)
# Uma chamada travada conta como falha e entra no retry.
API_TIMEOUT_SECONDS = 120
# Erros de configuração: tentar de novo não resolve.
NON_RETRYABLE_CODES = {400, 401, 403, 404}

WORDS_PER_SECOND = 2.6
MIN_CLIP_SECONDS = 2.5
# Sobra no fim de cada clipe para o crossfade do editor.
CLIP_TAIL_SECONDS = 0.5

# Prefixo fixo: todas as cenas parecem do mesmo diretor de arte.
# Pode ser trocado no .env (VISUAL_STYLE_PREFIX) sem mexer no código.
DEFAULT_STYLE_PREFIX = (
    "Cinematic historical illustration in the style of a prestige documentary. "
    "Dramatic chiaroscuro lighting, rich painterly textures, muted earthy palette with "
    "warm amber highlights and deep shadows, subtle film grain, 9:16 vertical composition."
)
# Âncora de estilo que o story_engine acrescenta; sai do prompt para não duplicar.
STORY_ENGINE_STYLE_ANCHOR = (
    "cinematic historical illustration, dramatic lighting, rich textures, "
    "film grain, 9:16 vertical composition"
)

IMAGE_PROMPT_SUFFIX = (
    "No text, no letters, no captions, no watermark, no logos. "
    "No identifiable face of a real person."
)

MOTION_TYPES = ("zoom_in_fast", "pan_left_to_right", "shake_impact", "slow_ken_burns", "pulse_glow",
                "tilt_down", "pan_right_to_left", "zoom_out_slow")

# Acabamento de documentário: vinheta que "respira", leve tremulação de brilho de
# projetor antigo e granulação de filme que muda a cada frame.
FINISH_FILTERS = ",".join([
    "vignette=angle='PI/5+0.05*sin(2*PI*t/2.5)':eval=frame",
    "eq=brightness='0.012*sin(2*PI*t*7)':eval=frame",
    "noise=alls=7:allf=t+u",
    "format=yuv420p",
])


# Transições de entrada (aplicadas no começo do clipe pelo editor, Módulo 4).
TRANSITIONS = {
    # Flash sutil: brilho sobe e volta ao normal em 0,12 s.
    "flash": "eq=brightness='0.35*max(0,1-t/0.12)':eval=frame",
    # Whip pan: borrão horizontal forte nos primeiros 0,14 s.
    "whip": "gblur=sigma=45:sigmaV=0.01:enable='lt(t,0.14)'",
    # Mergulho no preto: a cena nasce do preto em 0,3 s (salto no tempo, notícia triste).
    "dip": "fade=t=in:st=0:d=0.3",
    # Soco: zoom que entra forte e assenta em 0,22 s (no motion_filter) + brilho curto.
    "punch": "eq=brightness='0.15*max(0,1-t/0.08)':eval=frame",
}


def build_image_prompt(scene_prompt: str, style_prefix: str | None = None) -> str:
    """Prefixo de estilo + descrição da cena + restrições."""
    prefix = style_prefix or os.getenv("VISUAL_STYLE_PREFIX") or DEFAULT_STYLE_PREFIX
    scene = scene_prompt.strip()
    lowered = scene.lower()
    anchor_at = lowered.find(STORY_ENGINE_STYLE_ANCHOR)
    if anchor_at != -1:
        scene = scene[:anchor_at] + scene[anchor_at + len(STORY_ENGINE_STYLE_ANCHOR):]
    scene = scene.strip(" ,.")
    return f"{prefix.strip()} Scene: {scene}. {IMAGE_PROMPT_SUFFIX}"


class VisualGeneratorError(RuntimeError):
    pass


class NoImageReturned(VisualGeneratorError):
    """O modelo respondeu sem imagem (ex.: filtro de segurança)."""


class QuotaExhausted(VisualGeneratorError):
    """Cota do Gemini sem saída hoje: tentar de novo só gasta tempo."""

    retryable = False  # main.py não repete o passo


# --------------------------------------------------------------------------- movimento


def motion_filter(motion: str, duration: float, fps: int = FPS,
                  width: int = WIDTH, height: int = HEIGHT, transition: str | None = None) -> str:
    """Filtro FFmpeg (-vf) que transforma uma imagem parada em clipe com movimento.

    O movimento usa o filtro perspective com interpolação cúbica, que posiciona a imagem
    com precisão de subpixel. O zoompan arredonda a posição para pixels inteiros (bug
    #4298 do FFmpeg): numa panorâmica a imagem andava 1,1 px e 2,3 px em quadros
    alternados, e o vídeo parecia "travado". Entrada: a imagem em loop (-loop 1).
    """
    if motion not in MOTION_TYPES:
        raise ValueError(f"motion_type desconhecido: {motion!r}")
    if transition is not None and transition not in TRANSITIONS:
        raise ValueError(f"transição desconhecida: {transition!r}")
    n = max(1, round(duration * fps))
    center_x = "(W-W/({z}))/2"
    center_y = "(H-H/({z}))/2"

    if motion == "zoom_in_fast":
        # Zoom rápido até 1.25x nos primeiros 0.5s, depois aproximação lenta até 1.33x.
        fast = max(1, round(fps * 0.5))
        z = f"if(lt(on,{fast}),1+0.25*on/{fast},1.25+0.08*(on-{fast})/{max(1, n - fast)})"
        x, y = center_x, center_y
    elif motion == "slow_ken_burns":
        z = f"1.04+0.08*on/{n}"
        x = f"(W-W/({{z}}))*(0.3+0.4*on/{n})"
        y = "(H-H/({z}))*0.45"
    elif motion == "pan_left_to_right":
        z = "1.2"
        x = f"(W-W/({{z}}))*on/{n}"
        y = center_y
    elif motion == "shake_impact":
        # Tremor forte no primeiro meio segundo, que some aos poucos. Com zoom 1.12 sobra
        # ~58 px de margem na horizontal e ~103 px na vertical.
        decay = f"max(0,1-on/{fps * 0.5})"
        z = "1.12"
        x = f"(W-W/({{z}}))/2+35*sin(on*1.9)*{decay}"
        y = f"(H-H/({{z}}))/2+27.5*cos(on*2.7)*{decay}"
    elif motion == "tilt_down":
        # A câmera desce devagar (cachoeira, queda, algo alto).
        z = "1.18"
        x = center_x
        y = f"(H-H/({{z}}))*on/{n}"
    elif motion == "pan_right_to_left":
        z = "1.2"
        x = f"(W-W/({{z}}))*(1-on/{n})"
        y = center_y
    elif motion == "zoom_out_slow":
        z = f"1.22-0.16*on/{n}"
        x, y = center_x, center_y
    else:  # pulse_glow
        z = f"1.03+0.05*on/{n}"
        x, y = center_x, center_y
    if transition == "punch":
        z = f"({z})*(1+0.14*max(0,1-on/{max(1, round(fps * 0.22))}))"

    x0, y0 = x.format(z=z), y.format(z=z)
    x1, y1 = f"{x0}+W/({z})", f"{y0}+H/({z})"
    # Cantos do recorte na imagem de origem -> cantos do quadro (sense=source, o padrão).
    corners = (f"x0='{x0}':y0='{y0}':x1='{x1}':y1='{y0}':"
               f"x2='{x0}':y2='{y1}':x3='{x1}':y3='{y1}'")
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=increase",
        f"crop={width}:{height}",
        f"perspective={corners}:interpolation=cubic:eval=frame",
    ]
    if motion == "pulse_glow":
        filters.append(
            "eq=brightness='0.05*sin(2*PI*t*1.1)':saturation='1.12+0.1*sin(2*PI*t*1.1)':eval=frame"
        )
    if transition:
        filters.append(TRANSITIONS[transition])
    filters.append(FINISH_FILTERS)
    return ",".join(filters)


def render_clip(image: Path, motion: str, duration: float, output: Path,
                ffmpeg: str = "ffmpeg", transition: str | None = None) -> Path:
    """Gera o clipe animado (H.264, sem áudio) de uma imagem.

    duration é arredondada para frames inteiros (30 fps), o mesmo cálculo que o
    editor usa para o áudio, para os dois baterem frame a frame.
    """
    if shutil.which(ffmpeg) is None:
        raise VisualGeneratorError("FFmpeg não encontrado. Instale e confira com: ffmpeg -version")
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(duration * FPS))
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", str(FPS), "-i", str(image),
        "-vf", motion_filter(motion, duration, transition=transition),
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


def video_clip_filter(start: float, duration: float, transition: str | None = None,
                      width: int = WIDTH, height: int = HEIGHT) -> str:
    """Filtro (-filter_complex, saída [v]) para um trecho de filmagem real numa cena.

    Mesmo enquadramento das fotos horizontais (fit_vertical): o vídeo inteiro no terço
    superior-central, sobre ele mesmo desfocado e escurecido, deixando a faixa das legendas
    sobre o fundo. Sem movimento de câmera: a própria filmagem já se mexe. Se o vídeo acabar
    antes da cena, o último quadro fica parado (o áudio nunca perde a sincronia).
    """
    if transition is not None and transition not in TRANSITIONS:
        raise ValueError(f"transição desconhecida: {transition!r}")
    chain = (
        f"[0:v]trim=start={start:.3f},setpts=PTS-STARTPTS,fps={FPS},"
        f"tpad=stop_mode=clone:stop_duration={duration:.3f},split[a][b];"
        f"[a]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        "gblur=sigma=36,colorchannelmixer=rr=0.55:gg=0.55:bb=0.55[bg];"
        f"[b]scale={width}:-2:flags=lanczos[fg];"
        "[bg][fg]overlay=x=(W-w)/2:y='max(0,H*0.40-h/2)'"
    )
    if transition:
        chain += "," + TRANSITIONS[transition]
    return chain + "," + FINISH_FILTERS + "[v]"


def render_video_clip(video: Path, start: float, duration: float, output: Path,
                      ffmpeg: str = "ffmpeg", transition: str | None = None) -> Path:
    """Clipe da cena feito de filmagem real (o trecho que começa em start), com a duração
    exata da narração em frames inteiros, como render_clip."""
    if shutil.which(ffmpeg) is None:
        raise VisualGeneratorError("FFmpeg não encontrado. Instale e confira com: ffmpeg -version")
    output.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, round(duration * FPS))
    cmd = [
        ffmpeg, "-y", "-loglevel", "error", "-i", str(video),
        "-filter_complex", video_clip_filter(start, duration, transition), "-map", "[v]",
        "-frames:v", str(frames), "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-an",
        str(output),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise VisualGeneratorError(f"FFmpeg falhou em {video.name}: {result.stderr.strip()[-500:]}")
    return output


def estimate_duration(narration: str) -> float:
    words = len(narration.split())
    return round(max(MIN_CLIP_SECONDS, words / WORDS_PER_SECOND) + CLIP_TAIL_SECONDS, 2)


# --------------------------------------------------------------------------- imagem


# Acima desta proporção (largura/altura) a imagem não é cortada: vai inteira sobre um
# fundo desfocado dela mesma, o padrão dos vídeos verticais com fotos horizontais.
CROP_MAX_RATIO = 0.75


def fit_vertical(image_bytes: bytes, width: int = WIDTH, height: int = HEIGHT) -> "Image.Image":
    """Ajusta qualquer imagem para width x height (9:16).

    Quase vertical: corta no centro. Horizontal/quadrada (fotos históricas, pinturas):
    imagem inteira no terço superior-central, sobre a própria imagem desfocada e escurecida,
    deixando a faixa das legendas (altura dos olhos) sobre o fundo.
    """
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    with Image.open(BytesIO(image_bytes)) as img:
        img = img.convert("RGB")
        if img.width / img.height <= CROP_MAX_RATIO:
            return ImageOps.fit(img, (width, height), method=Image.LANCZOS, centering=(0.5, 0.5))
        background = ImageOps.fit(img, (width, height), method=Image.LANCZOS)
        background = background.filter(ImageFilter.GaussianBlur(36))
        background = ImageEnhance.Brightness(background).enhance(0.55)
        fg_w, fg_h = width, round(img.height * width / img.width)
        max_h = int(height * 0.62)
        if fg_h > max_h:
            fg_w, fg_h = round(img.width * max_h / img.height), max_h
        foreground = img.resize((fg_w, fg_h), Image.LANCZOS)
        top = max(0, int(height * 0.40 - fg_h / 2))
        background.paste(foreground, ((width - fg_w) // 2, top))
        return background


def _is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return code not in NON_RETRYABLE_CODES


MAX_RATE_LIMIT_WAIT_SECONDS = 90


def classify_rate_limit(exc: Exception) -> tuple[str, float | None]:
    """Para um erro 429, diz se é cota esgotada ("hard") ou limite por minuto ("soft").

    - "limit: 0": o modelo não tem cota no seu plano (ex.: imagem no plano gratuito);
    - cota "PerDay": acabou a do dia;
    nos dois casos esperar alguns segundos não resolve. Fora isso é limite por minuto,
    e o Google informa quanto esperar (retryDelay).
    """
    text = str(exc)
    if re.search(r"limit:\s*0\b", text) or "PerDay" in text:
        return "hard", None
    match = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", text) or \
        re.search(r"retry in (\d+(?:\.\d+)?)\s*s", text, re.IGNORECASE)
    delay = float(match.group(1)) if match else None
    return "soft", delay


class GeminiImageClient:
    """Gera uma imagem 9:16 com o SDK google-genai.

    Modelos "imagen-*" usam generate_images; modelos Gemini com saída de imagem
    usam generate_content com response_modalities=["IMAGE"].
    """

    def __init__(self, model: str, client=None) -> None:
        self.model = model
        if client is None:
            from google import genai
            from google.genai import types

            if not os.getenv("GEMINI_API_KEY"):
                raise VisualGeneratorError("Defina GEMINI_API_KEY no .env")
            client = genai.Client(
                api_key=os.environ["GEMINI_API_KEY"],
                http_options=types.HttpOptions(timeout=API_TIMEOUT_SECONDS * 1000),
            )
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


def clear_episode_visuals(visuals_dir: Path) -> None:
    """Apaga imagens, clipes e créditos de um episódio, para buscar (ou gerar) de novo."""
    for pattern in ("scene_*.png", "scene_*.mp4", "credits.json", "manifest.json"):
        for path in visuals_dir.glob(pattern):
            path.unlink()


def reject_scene_images(visuals_dir: Path, scene_ids: list[int]) -> None:
    """Troca só estas cenas: apaga imagem e clipe, e a imagem recusada não volta no episódio.
    Cenas que copiavam a imagem de uma cena trocada também são refeitas."""
    credits_path = visuals_dir / "credits.json"
    credits = json.loads(credits_path.read_text(encoding="utf-8")) if credits_path.exists() else {}
    rejected_path = visuals_dir / "rejected.json"
    rejected = json.loads(rejected_path.read_text(encoding="utf-8")) if rejected_path.exists() else []
    redo = set(scene_ids) | {int(k) for k, v in credits.items() if v.get("reused_from_scene") in scene_ids}
    for sid in sorted(redo):
        entry = credits.pop(str(sid), {})
        if sid in scene_ids and entry.get("page_url"):
            rejected.append(entry["page_url"])
        for suffix in (".png", ".mp4"):
            (visuals_dir / f"scene_{sid:02d}{suffix}").unlink(missing_ok=True)
    credits_path.write_text(json.dumps(credits, ensure_ascii=False, indent=2), encoding="utf-8")
    rejected_path.write_text(json.dumps(rejected, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_image_provider() -> str:
    """stock = imagens reais grátis (Wikimedia/Pexels); gemini = imagens geradas (pago)."""
    provider = (os.getenv("IMAGE_PROVIDER") or "stock").strip().lower()
    if provider not in ("stock", "gemini"):
        raise VisualGeneratorError(f"IMAGE_PROVIDER inválido: {provider!r} (use stock ou gemini)")
    return provider


class VisualGenerator:
    def __init__(
        self,
        image_client: GeminiImageClient | None = None,
        output_dir: Path | None = None,
        episodes_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        render: Callable[[Path, str, float, Path], Path] = render_clip,
        stock_finder=None,
    ) -> None:
        self.output_dir = output_dir or PROJECT_DIR / os.getenv("OUTPUT_DIR", "output")
        self.episodes_dir = episodes_dir or PROJECT_DIR / "assets" / "episodes"
        self.sleep = sleep
        self.render = render
        self.stock = None
        self.image_client = None
        if stock_finder is not None or (image_client is None and resolve_image_provider() == "stock"):
            from image_sources import StockImageFinder, region_from_env, sources_from_env

            self.stock = stock_finder or StockImageFinder(sources_from_env(), region=region_from_env())
            return
        if image_client is None:
            model = os.getenv("GEMINI_IMAGE_MODEL")
            if not model:
                raise VisualGeneratorError(
                    "Defina GEMINI_IMAGE_MODEL no .env "
                    "(veja os disponíveis com: python visual_generator.py --list-models)"
                )
            image_client = GeminiImageClient(model)
        self.image_client = image_client

    def generate_image(self, prompt: str, path: Path) -> Path:
        """Gera e salva a imagem: 1 tentativa + até 3 novas, com backoff de 2, 4 e 8 s."""
        full_prompt = build_image_prompt(prompt)
        last_error: Exception | None = None
        for attempt in range(1, MAX_IMAGE_ATTEMPTS + 1):
            try:
                image = fit_vertical(self.image_client.generate(full_prompt))
                path.parent.mkdir(parents=True, exist_ok=True)
                image.save(path, "PNG")
                return path
            except Exception as exc:
                last_error = exc
                code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
                rate_wait = None
                if code == 429:
                    kind, rate_wait = classify_rate_limit(exc)
                    if kind == "hard":
                        raise QuotaExhausted(
                            "Cota de imagens do Gemini esgotada ou inexistente para "
                            f"'{getattr(self.image_client, 'model', '?')}' (erro 429). "
                            "No plano gratuito a geração de imagem não tem cota: ative o "
                            "faturamento do projeto da sua chave no Google AI Studio "
                            "(aistudio.google.com > Get API key > Set up billing) ou troque "
                            "GEMINI_IMAGE_MODEL. Depois rode de novo: o roteiro já gerado é "
                            "reaproveitado."
                        ) from exc
                if not _is_retryable(exc):
                    raise VisualGeneratorError(
                        f"{path.name}: erro de configuração/permissão ({exc}); "
                        "confira GEMINI_API_KEY e GEMINI_IMAGE_MODEL"
                    ) from exc
                if attempt < MAX_IMAGE_ATTEMPTS:
                    wait = BACKOFF_SECONDS[attempt - 1]
                    if rate_wait is not None:  # limite por minuto: espera o que o Google pediu
                        wait = min(MAX_RATE_LIMIT_WAIT_SECONDS, max(wait, rate_wait))
                    log.warning("%s: tentativa %s falhou (%s); nova tentativa em %ss",
                                path.name, attempt, exc, wait)
                    self.sleep(wait)
        raise VisualGeneratorError(
            f"{path.name}: falhou após {MAX_IMAGE_ATTEMPTS} tentativas ({last_error})"
        ) from last_error

    def _save_stock_image(self, sid: int, queries: list[str], visuals_dir: Path,
                          used: set[str], credits: dict) -> bool:
        found = self.stock.find(queries, used)
        if found is None:
            return False
        candidate, data = found
        used.add(candidate.page_url)  # inválida ou não, não tenta a mesma de novo
        try:
            fit_vertical(data).save(visuals_dir / f"scene_{sid:02d}.png", "PNG")
        except Exception as exc:  # arquivo corrompido ou formato estranho
            log.warning("Cena %s: imagem inválida (%s)", sid, exc)
            return False
        credits[str(sid)] = candidate.to_dict()
        (visuals_dir / "credits.json").write_text(json.dumps(credits, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
        return True

    def _ensure_stock_images(self, episode: dict, visuals_dir: Path) -> None:
        """Baixa uma imagem real para cada cena; sem resultado, reaproveita a mais próxima."""
        from image_sources import scene_queries

        credits_path = visuals_dir / "credits.json"
        credits: dict = json.loads(credits_path.read_text(encoding="utf-8")) if credits_path.exists() else {}
        used = {c["page_url"] for c in credits.values() if c.get("page_url")}
        rejected_path = visuals_dir / "rejected.json"
        if rejected_path.exists():  # imagens recusadas na revisão nunca voltam neste episódio
            used |= set(json.loads(rejected_path.read_text(encoding="utf-8")))
        missing = []
        for scene in episode["scenes"]:
            sid = scene["scene_id"]
            if (visuals_dir / f"scene_{sid:02d}.png").exists():
                continue
            if not self._save_stock_image(sid, scene_queries(scene, episode), visuals_dir, used, credits):
                missing.append(sid)

        # 2ª chance, antes de repetir uma imagem: as buscas das outras cenas (mesmo tema),
        # da mais próxima para a mais distante, ainda podem ter imagens não usadas.
        for sid in list(missing):
            neighbours = sorted((s for s in episode["scenes"] if s["scene_id"] != sid),
                                key=lambda s: (abs(s["scene_id"] - sid), s["scene_id"] > sid))
            queries = list(dict.fromkeys(q for s in neighbours for q in scene_queries(s, episode)))
            if self._save_stock_image(sid, queries, visuals_dir, used, credits):
                missing.remove(sid)

        if not missing:
            return
        available = sorted(int(p.stem.split("_")[1]) for p in visuals_dir.glob("scene_*.png"))
        if not available:
            raise VisualGeneratorError(
                "Nenhuma imagem livre encontrada para este episódio. Opções: coloque uma chave "
                "grátis da Pexels em PEXELS_API_KEY (pexels.com/api), ative ALLOW_SHARE_ALIKE=1 "
                "ou use IMAGE_PROVIDER=gemini (pago)."
            )
        for sid in missing:
            # A cena mais próxima (de preferência a anterior); o movimento diferente varia o visual.
            source_sid = min(available, key=lambda other: (abs(other - sid), other > sid))
            shutil.copyfile(visuals_dir / f"scene_{source_sid:02d}.png",
                            visuals_dir / f"scene_{sid:02d}.png")
            credits[str(sid)] = {"reused_from_scene": source_sid}
            log.info("Cena %s: sem imagem nova; reaproveitando a da cena %s", sid, source_sid)
        credits_path.write_text(json.dumps(credits, ensure_ascii=False, indent=2), encoding="utf-8")
        log.warning("%s de %s cenas repetem a imagem de outra cena (faltou imagem livre). "
                    "Mais opções: PEXELS_API_KEY no .env ou ALLOW_SHARE_ALIKE=1",
                    len(missing), len(episode["scenes"]))

    def process_episode(self, day: date, refresh: bool = False) -> Path:
        folder = self.output_dir / day.isoformat()
        episode_path = folder / "current_episode.json"
        if not episode_path.exists():
            raise VisualGeneratorError(
                f"{episode_path} não existe. Rode antes: python story_engine.py --date {day}"
            )
        episode = json.loads(episode_path.read_text(encoding="utf-8"))
        episode_id = episode.get("topic_id")
        if episode_id is None:
            raise VisualGeneratorError(f"{episode_path} não tem topic_id (gere de novo com story_engine.py)")
        visuals_dir = self.episodes_dir / str(episode_id) / "visuals"
        visuals_dir.mkdir(parents=True, exist_ok=True)
        if refresh:
            clear_episode_visuals(visuals_dir)
        if self.stock is not None:
            region = self.stock.region
            if episode.get("image_region") is not None:  # o país da história vence o IMAGE_REGION
                self.stock.region = str(episode["image_region"]).strip()
            try:
                self._ensure_stock_images(episode, visuals_dir)
            finally:
                self.stock.region = region

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
            "episode_id": episode_id,
            "date": day.isoformat(),
            "episode_json": str(episode_path),
            "image_provider": "stock" if self.stock is not None else "gemini",
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


def utf8_console() -> None:
    """No Windows, saída redirecionada usa cp1252; títulos do Wikimedia podem ter
    caracteres que não existem nele. Força UTF-8 e troca o resto, em vez de travar."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_DIR / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Gera imagens e clipes animados do episódio.")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--list-models", action="store_true",
                        help="lista os modelos de imagem disponíveis na sua conta Gemini")
    parser.add_argument("--search", metavar="TERMOS",
                        help="testa a busca de imagens grátis (Wikimedia/Pexels) sem baixar nada")
    parser.add_argument("--refresh", action="store_true",
                        help="apaga as imagens do episódio e busca (ou gera) de novo")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        if args.search:
            from image_sources import StockImageFinder, region_from_env, sources_from_env

            finder = StockImageFinder([], region=region_from_env())
            for source in sources_from_env():
                query = finder.query_for(source, args.search)
                if query != args.search:
                    print(f"[i] {source.name}: busca genérica vira \"{query}\"")
                try:
                    found = source.search(query)
                except Exception as exc:
                    print(f"[X] {source.name}: {exc}")
                    continue
                print(f"[OK] {source.name}: {len(found)} imagem(ns) com licença aceita")
                for c in found[:5]:
                    print(f"     {c.width}x{c.height}  {c.license:<16} {c.title[:60]}  {c.page_url}")
            return 0
        if args.list_models:
            for name in list_image_models():
                print(name)
            return 0
        print(VisualGenerator().process_episode(args.date, refresh=args.refresh))
    except VisualGeneratorError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
