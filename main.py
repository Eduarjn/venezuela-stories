"""Orquestrador: gera o vídeo do dia do começo ao fim, com checkpoint.

Passos:
    1. story    – tema inédito + roteiro JSON           (story_engine.py)
    2. visuals  – imagens Gemini + clipes animados      (visual_generator.py)
    3. voice    – narração + tempo de cada palavra      (voice_engine.py)
    4. edit     – vídeo final + post_copy.txt           (video_editor.py)

Anti-falhas:
- Health check antes de começar: FFmpeg, chaves do .env, espaço em disco e internet
  (espera a conexão voltar por até HEALTH_WAIT_MINUTES).
- Checkpoint em output/AAAA-MM-DD/checkpoint.json: um passo concluído nunca roda de
  novo. Dentro de cada passo, imagens/áudios/clipes já gerados também são reaproveitados,
  então uma queda no meio não gasta créditos de novo.
- Cada passo tem até STEP_ATTEMPTS tentativas, com espera crescente.
- Trava (logs/pipeline.lock) impede duas execuções ao mesmo tempo.
- Log do dia em logs/AAAA-MM-DD.log.

Uso:
    python main.py                          # vídeo de hoje (retoma se já começou)
    python main.py --date 2026-11-05
    python main.py --check                  # só o health check
    python main.py --status                 # mostra o checkpoint de hoje
    python main.py --mark-published 2026-11-05
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import socket
import sys
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

PROJECT_DIR = Path(__file__).parent
STEPS = ("story", "visuals", "voice", "edit")
STEP_ATTEMPTS = 3
STEP_BACKOFF_SECONDS = (60, 300)        # espera antes da 2ª e da 3ª tentativa
HEALTH_WAIT_MINUTES = 30
MIN_FREE_DISK_GB = 2.0
RUN_LOCK_PORT = 47622                    # o agendador usa a 47621
API_HOSTS = ("api.anthropic.com", "generativelanguage.googleapis.com")

log = logging.getLogger("main")


class PipelineError(RuntimeError):
    pass


# --------------------------------------------------------------------------- caminhos


def output_dir() -> Path:
    return PROJECT_DIR / os.getenv("OUTPUT_DIR", "output")


def day_dir(day: date) -> Path:
    return output_dir() / day.isoformat()


def episodes_dir() -> Path:
    return PROJECT_DIR / "assets" / "episodes"


def checkpoint_path(day: date) -> Path:
    return day_dir(day) / "checkpoint.json"


# --------------------------------------------------------------------------- checkpoint


@dataclass
class Checkpoint:
    day: str
    steps: dict[str, str] = field(default_factory=lambda: {s: "pending" for s in STEPS})
    attempts: dict[str, int] = field(default_factory=lambda: {s: 0 for s in STEPS})
    errors: list[dict] = field(default_factory=list)
    final_video: str | None = None
    post_copy: str | None = None
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None

    @classmethod
    def load(cls, day: date) -> "Checkpoint":
        path = checkpoint_path(day)
        if path.exists():
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        return cls(day=day.isoformat())

    def save(self) -> None:
        path = checkpoint_path(date.fromisoformat(self.day))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)  # escrita atômica: queda de energia não corrompe o checkpoint

    @property
    def done(self) -> bool:
        return all(v == "done" for v in self.steps.values())


# --------------------------------------------------------------------------- health check


def internet_ok(hosts=API_HOSTS, timeout: float = 5) -> bool:
    for host in hosts:
        try:
            with socket.create_connection((host, 443), timeout=timeout):
                pass
        except OSError:
            return False
    return True


def health_problems() -> list[str]:
    """Problemas que impedem rodar (sem contar a internet, tratada à parte)."""
    problems = []
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            problems.append(f"{tool} não encontrado (instale o FFmpeg)")
    from story_engine import StoryEngineError, find_claude_binary, resolve_backend

    # Imagens: "stock" (padrão, grátis) não precisa de chave; "gemini" (pago) precisa.
    required = []
    if (os.getenv("IMAGE_PROVIDER") or "stock").strip().lower() == "gemini":
        required = ["GEMINI_API_KEY", "GEMINI_IMAGE_MODEL"]
    try:
        backend = resolve_backend()
    except StoryEngineError as exc:
        problems.append(str(exc))
        backend = None
    if backend == "api":
        required = ["ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", *required]
    elif backend == "claude_code" and find_claude_binary() is None:
        problems.append("Claude Code (claude) não encontrado no PATH "
                        "(ANTHROPIC_BACKEND=claude_code usa o Claude Code logado neste PC)")
    provider = (os.getenv("TTS_PROVIDER") or "edge").lower()
    if provider == "elevenlabs":
        required += ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"]
    elif provider == "openai":
        required += ["OPENAI_API_KEY", "OPENAI_TTS_MODEL", "OPENAI_TTS_VOICE"]
    problems += [f"{key} vazio no .env" for key in required if not os.getenv(key)]
    free_gb = shutil.disk_usage(PROJECT_DIR).free / 1024**3
    if free_gb < MIN_FREE_DISK_GB:
        problems.append(f"pouco espaço em disco: {free_gb:.1f} GB livres (mínimo {MIN_FREE_DISK_GB})")
    return problems


def wait_for_internet(sleep: Callable[[float], None] = time.sleep,
                      check: Callable[[], bool] = internet_ok,
                      minutes: int = HEALTH_WAIT_MINUTES) -> None:
    for minute in range(minutes + 1):
        if check():
            return
        if minute == 0:
            log.warning("Sem conexão com as APIs; aguardando até %s min...", minutes)
        sleep(60)
    raise PipelineError(f"sem internet há {minutes} minutos")


# --------------------------------------------------------------------------- trava


class RunLock:
    """Impede que o agendador e uma execução manual rodem ao mesmo tempo.

    Ocupa uma porta local, como o agendador: o sistema a libera sozinho quando o processo
    termina, mesmo se ele for morto (falta de energia, timeout do terminal). A trava antiga,
    em arquivo, ficava presa por horas depois de um processo morto."""

    def __init__(self, port: int = RUN_LOCK_PORT) -> None:
        self.port = port
        self.sock: socket.socket | None = None

    def __enter__(self) -> "RunLock":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError:
            sock.close()
            raise PipelineError(f"outra execução do pipeline está rodando agora (porta local "
                                f"{self.port} ocupada); espere ela terminar") from None
        self.sock = sock
        (PROJECT_DIR / "logs" / "pipeline.lock").unlink(missing_ok=True)  # trava antiga, em arquivo
        return self

    def __exit__(self, *exc) -> None:
        if self.sock is not None:
            self.sock.close()
            self.sock = None


# --------------------------------------------------------------------------- publicação


def slugify_title(title: str, max_len: int = 60) -> str:
    from database import normalize

    slug = normalize(title)[:max_len].rstrip("-")
    return slug or "video"


@dataclass
class ImageCredits:
    required: list[str] = field(default_factory=list)  # CC BY / BY-SA: a licença exige o crédito
    optional: list[str] = field(default_factory=list)  # domínio público, CC0, Pexels: cortesia
    reused_scenes: int = 0                              # cenas que repetem a imagem de outra
    by_scene: dict[int, str] = field(default_factory=dict)  # cena -> imagem, para a revisão


def build_post_copy(episode: dict, credits: ImageCredits | None = None) -> str:
    """Texto pronto para colar na publicação.

    credits: créditos das imagens reais (Wikimedia/Pexels) do episódio.
    """
    credits = credits or ImageCredits()
    hashtags = " ".join(h if h.startswith("#") else f"#{h}" for h in episode.get("hashtags", []))
    lines = [
        "=== TÍTULO ===",
        episode.get("post_title", ""),
        "",
        "=== DESCRIÇÃO ===",
        episode.get("post_description", ""),
        "",
        hashtags,
        "",
        "=== HASHTAGS ===",
        hashtags,
    ]
    if episode.get("track") == "fact" and episode.get("sources"):
        lines += ["", "=== FONTES (para comentário fixado ou descrição longa) ==="]
        lines += [f"- {s.get('title', '')}: {s['url']}" for s in episode["sources"]]
    if credits.required:
        lines += ["", "=== CRÉDITOS DAS IMAGENS (obrigatório: cole na descrição; a licença exige) ===",
                  # A licença CC BY exige indicar que houve modificação.
                  "Imagens de acervos livres, recortadas e animadas para o vídeo; algumas podem ser ilustrativas."]
        lines += [f"- {c}" for c in credits.required]
    if credits.optional:
        lines += ["", "=== Créditos opcionais (domínio público / Pexels: não é obrigatório) ==="]
        lines += [f"- {c}" for c in credits.optional]
    ai_note = ("- A narração é voz sintética e as imagens vêm de acervos livres: ative o rótulo de "
               "conteúdo gerado/alterado por IA se a plataforma pedir."
               ) if credits.required or credits.optional else (
               "- Imagens e voz são geradas por IA: ative o rótulo de conteúdo gerado por IA da plataforma.")
    warning = []
    if credits.reused_scenes:
        warning = [f"- ATENÇÃO: {credits.reused_scenes} de {len(episode.get('scenes') or [])} cenas repetem "
                   "a imagem de outra cena (faltou imagem livre). Se ficar repetitivo: coloque "
                   "PEXELS_API_KEY no .env e rode python main.py --date "
                   f"{episode.get('date', '')} --redo-images"]
    lines += [
        "",
        "=== ANTES DE PUBLICAR ===",
        *warning,
        ai_note,
        "- Assista ao vídeo inteiro uma vez (fatos, pronúncia de nomes, legenda).",
        f"- Depois de publicar: python main.py --mark-published {episode.get('date', '')}",
    ]
    if credits.by_scene:
        lines += ["", "=== REVISÃO: IMAGEM DE CADA CENA (não cole; confira se a narração cita um lugar "
                      "e a imagem é de outro) ==="]
        for scene in episode.get("scenes") or []:
            narration = scene.get("narration_text", "")
            narration = narration if len(narration) <= 70 else narration[:67] + "..."
            image = credits.by_scene.get(scene.get("scene_id"), "?")
            lines.append(f"Cena {scene.get('scene_id')}: {narration} -> {image}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- passos


def _language() -> str:
    return (os.getenv("VIDEO_LANGUAGES") or "pt-BR").split(",")[0].strip()


def step_story(day: date) -> None:
    from database import StoryDatabase
    from story_engine import StoryEngine

    if (day_dir(day) / "current_episode.json").exists():
        log.info("Roteiro já existe; reaproveitando")
        return
    db = StoryDatabase()
    existing = db.topic_for_date(day)
    if existing:
        raise PipelineError(
            f"o banco já tem o tema #{existing.id} para {day}, mas o JSON sumiu. "
            f"Marque-o como failed para gerar outro: python -c \"from database import "
            f"StoryDatabase; StoryDatabase().set_status({existing.id}, 'failed', error='json perdido')\""
        )
    StoryEngine(db).create_episode(day, _language())


def step_visuals(day: date) -> None:
    from visual_generator import VisualGenerator

    VisualGenerator().process_episode(day)


def step_voice(day: date) -> None:
    from voice_engine import VoiceEngine

    VoiceEngine().process_episode(day)


def load_image_credits(episode: dict) -> ImageCredits:
    """Créditos únicos das imagens reais usadas no episódio (visuals/credits.json)."""
    from image_sources import Candidate

    credits = ImageCredits()
    path = episodes_dir() / str(episode.get("topic_id")) / "visuals" / "credits.json"
    if not path.exists():
        return credits
    for sid, entry in json.loads(path.read_text(encoding="utf-8")).items():
        if "source" not in entry:  # cena que reaproveitou a imagem de outra
            credits.reused_scenes += 1
            credits.by_scene[int(sid)] = f"(repete a imagem da cena {entry.get('reused_from_scene')})"
            continue
        candidate = Candidate(**entry)
        credits.by_scene[int(sid)] = f"{candidate.title} ({candidate.source})"
        target = credits.required if candidate.attribution_required else credits.optional
        if candidate.credit_line() not in target:
            target.append(candidate.credit_line())
    return credits


def _episode_to_redo(day: date, scenes: list[int] | None) -> dict:
    episode_path = day_dir(day) / "current_episode.json"
    if not episode_path.exists():
        raise PipelineError(f"{episode_path} não existe: ainda não há episódio em {day}")
    episode = json.loads(episode_path.read_text(encoding="utf-8"))
    unknown = sorted(set(scenes or []) - {s["scene_id"] for s in episode.get("scenes", [])})
    if unknown:
        raise PipelineError(f"cena(s) {unknown} não existem no episódio de {day}")
    return episode


def _reset_steps(day: date, steps: tuple[str, ...]) -> None:
    cp = Checkpoint.load(day)
    for step in steps:
        cp.steps[step] = "pending"
        cp.attempts[step] = 0
    cp.final_video = cp.post_copy = cp.finished_at = None
    cp.save()


def redo_images(day: date, scenes: list[int] | None = None) -> None:
    """Apaga as imagens do dia (ou só das cenas indicadas, sem repetir a imagem recusada) e
    marca visuals/edit para rodar de novo. Roteiro e narração ficam."""
    from visual_generator import clear_episode_visuals, reject_scene_images

    episode = _episode_to_redo(day, scenes)
    visuals = episodes_dir() / str(episode.get("topic_id")) / "visuals"
    if visuals.exists():
        if scenes:
            reject_scene_images(visuals, scenes)
        else:
            clear_episode_visuals(visuals)
    _reset_steps(day, ("visuals", "edit"))
    log.info("Imagens de %s apagadas; visuals e edit vão rodar de novo", day)


def redo_voice(day: date, scenes: list[int] | None = None) -> None:
    """Apaga a narração do dia (ou só das cenas indicadas) e marca voice/edit para rodar de
    novo. Use depois de mudar o narration_text no current_episode.json: o áudio pronto é
    reaproveitado sem olhar o texto."""
    episode = _episode_to_redo(day, scenes)
    audio = episodes_dir() / str(episode.get("topic_id")) / "audio"
    for sid in scenes or [s["scene_id"] for s in episode["scenes"]]:
        for name in (f"scene_{sid:02d}.wav", f"scene_{sid:02d}.words.json"):
            (audio / name).unlink(missing_ok=True)
    _reset_steps(day, ("voice", "edit"))
    log.info("Narração de %s apagada; voice e edit vão rodar de novo", day)


def write_post_copy(day: date, episode: dict) -> Path:
    post_copy = day_dir(day) / "post_copy.txt"
    post_copy.write_text(build_post_copy({**episode, "date": day.isoformat()},
                                         load_image_credits(episode)), encoding="utf-8")
    return post_copy


def copy_final_video(video: Path, final: Path) -> Path:
    """Copia o vídeo pronto. No Windows, um player com o vídeo antigo aberto bloqueia a cópia
    (refazendo imagens ou voz do mesmo dia): salva com outro nome em vez de falhar o passo."""
    try:
        shutil.copy2(video, final)
        return final
    except PermissionError:
        alt = final.with_name(f"{final.stem}_{datetime.now():%H%M%S}{final.suffix}")
        log.warning("%s está aberto em outro programa; vídeo novo salvo como %s", final.name, alt.name)
        shutil.copy2(video, alt)
        return alt


def step_edit(day: date) -> tuple[Path, Path]:
    from video_editor import VideoEditor, mark_rendered

    video = VideoEditor().edit(day)
    episode = json.loads((day_dir(day) / "current_episode.json").read_text(encoding="utf-8"))
    final = copy_final_video(
        video, output_dir() / f"{day.isoformat()}_{slugify_title(episode['topic_title'])}.mp4")
    post_copy = write_post_copy(day, episode)
    if episode.get("topic_id") is not None:
        mark_rendered(int(episode["topic_id"]), final)
    return final, post_copy


DEFAULT_STEPS: dict[str, Callable] = {
    "story": step_story,
    "visuals": step_visuals,
    "voice": step_voice,
    "edit": step_edit,
}


# --------------------------------------------------------------------------- pipeline


def run_pipeline(
    day: date,
    steps: dict[str, Callable] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    connectivity: Callable[[], bool] = internet_ok,
    check_health: bool = True,
) -> Checkpoint:
    steps = steps or DEFAULT_STEPS
    cp = Checkpoint.load(day)
    if cp.done:
        log.info("Vídeo de %s já está pronto: %s", day, cp.final_video)
        return cp

    if check_health:
        problems = health_problems()
        if problems:
            raise PipelineError("health check falhou:\n- " + "\n- ".join(problems))

    for name in STEPS:
        if cp.steps[name] == "done":
            log.info("[%s] já concluído (checkpoint)", name)
            continue
        for attempt in range(1, STEP_ATTEMPTS + 1):
            if name != "edit":  # edit é local (FFmpeg); os outros chamam APIs
                wait_for_internet(sleep, connectivity)
            cp.steps[name] = "running"
            cp.attempts[name] += 1
            cp.save()
            log.info("[%s] tentativa %s/%s", name, attempt, STEP_ATTEMPTS)
            try:
                result = steps[name](day)
            except Exception as exc:
                cp.steps[name] = "failed"
                cp.errors.append({
                    "step": name, "attempt": attempt, "at": datetime.now().isoformat(timespec="seconds"),
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
                cp.save()
                log.error("[%s] falhou: %s", name, exc)
                log.debug("%s", traceback.format_exc())
                # Erros marcados retryable=False (ex.: cota do Gemini esgotada) não se
                # resolvem esperando: falha já, sem as esperas de 1 e 5 minutos.
                if (attempt == STEP_ATTEMPTS or isinstance(exc, PipelineError)
                        or getattr(exc, "retryable", True) is False):
                    raise PipelineError(
                        f"passo '{name}' falhou {attempt}x. Corrija e rode de novo: "
                        f"python main.py --date {day} (retoma deste passo)"
                    ) from exc
                wait = STEP_BACKOFF_SECONDS[attempt - 1]
                log.info("[%s] nova tentativa em %ss", name, wait)
                sleep(wait)
                continue
            if name == "edit" and result:
                cp.final_video, cp.post_copy = (str(p) for p in result)
            cp.steps[name] = "done"
            cp.save()
            log.info("[%s] concluído", name)
            break

    cp.finished_at = datetime.now().isoformat(timespec="seconds")
    cp.save()
    log.info("Pronto! Vídeo: %s | Texto do post: %s", cp.final_video, cp.post_copy)
    return cp


# --------------------------------------------------------------------------- CLI


def utf8_console() -> None:
    """No Windows, saída redirecionada (pipe, Claude Code, agendador) usa cp1252.
    Força UTF-8 e troca o que não der para escrever, em vez de travar. As mensagens
    também só usam caracteres que existem no cp1252 ([OK]/[X] em vez de símbolos)."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def setup_logging(day: date) -> None:
    logs = PROJECT_DIR / "logs"
    logs.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    handlers: list[logging.Handler] = [logging.FileHandler(logs / f"{day}.log", encoding="utf-8")]
    if sys.stderr is not None:  # pythonw (Windows, sem janela) não tem console
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(fmt)
        root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_DIR / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description="Gera o vídeo do dia (com checkpoint).")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--check", action="store_true", help="só roda o health check")
    parser.add_argument("--status", action="store_true", help="mostra o checkpoint do dia")
    parser.add_argument("--mark-published", type=date.fromisoformat, metavar="AAAA-MM-DD")
    parser.add_argument("--redo-images", nargs="*", type=int, metavar="CENA",
                        help="busca imagens novas e remonta o vídeo (roteiro e narração ficam). "
                             "Sem números: todas as cenas. Com números (ex.: 3 7): só essas, "
                             "sem repetir a imagem recusada")
    parser.add_argument("--redo-voice", nargs="*", type=int, metavar="CENA",
                        help="refaz a narração e remonta o vídeo, depois de mudar o narration_text "
                             "no current_episode.json. Sem números: todas as cenas. Com números: só essas")
    parser.add_argument("--post-copy", action="store_true",
                        help="só refaz o post_copy.txt do dia (sem renderizar o vídeo)")
    args = parser.parse_args(argv)
    setup_logging(args.date)

    if args.check:
        problems = health_problems()
        net = internet_ok()
        for p in problems:
            print(f"[X] {p}")
        print("[OK] internet" if net else "[X] sem conexão com as APIs")
        if not problems and net:
            print("[OK] tudo pronto para rodar")
        return 0 if not problems and net else 1

    if args.status:
        print(json.dumps(Checkpoint.load(args.date).__dict__, ensure_ascii=False, indent=2))
        return 0

    if args.post_copy:
        episode_path = day_dir(args.date) / "current_episode.json"
        if not episode_path.exists():
            print(f"[X] {episode_path} não existe")
            return 1
        print(write_post_copy(args.date, json.loads(episode_path.read_text(encoding="utf-8"))))
        return 0

    if args.mark_published:
        from database import StoryDatabase

        db = StoryDatabase()
        topic = db.topic_for_date(args.mark_published)
        if topic is None:
            print(f"Nenhum tema em {args.mark_published}")
            return 1
        db.set_status(topic.id, "published")
        print(f"Tema #{topic.id} '{topic.title}' marcado como publicado")
        return 0

    try:
        with RunLock():
            if args.redo_images is not None:
                redo_images(args.date, args.redo_images or None)
            if args.redo_voice is not None:
                redo_voice(args.date, args.redo_voice or None)
            cp = run_pipeline(args.date)
    except PipelineError as exc:
        log.error("%s", exc)
        return 1
    print(cp.final_video)
    return 0


if __name__ == "__main__":
    sys.exit(main())
