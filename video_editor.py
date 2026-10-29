"""Módulo 4 – Editor: monta o final_video.mp4.

Entrada (gerada pelos módulos anteriores):
    output/AAAA-MM-DD/current_episode.json
    assets/episodes/{id}/visuals/scene_XX.png     (visual_generator.py)
    assets/episodes/{id}/audio/timeline.json      (voice_engine.py)
    assets/sfx/*.mp3|wav   assets/music/*.mp3|wav|m4a   assets/fonts/*.ttf|otf  (seus arquivos)

Saída:
    output/AAAA-MM-DD/final_video.mp4   (1080x1920, 30 fps, H.264 + AAC, -14 LUFS)

O que faz:
1. Sincronia: cada cena dura exatamente o tempo da fala (arredondado para frames
   inteiros); o áudio de cada cena é completado até o mesmo número de frames.
   Os silêncios longos já foram cortados pelo voice_engine.
2. Transições: whip pan (cenas com sfx_cue "whoosh") ou flash sutil, com o efeito
   sonoro da cena entrando no corte.
3. Trilha com ducking: a música cai -18 dB enquanto o narrador fala e volta
   nas pausas maiores, com rampas de 0,15 s.
4. Legendas: 2 a 4 palavras por vez, centralizadas na altura dos olhos, fonte
   grossa, borda preta espessa, palavra falada em amarelo (vermelho se tiver número).

Memória: tudo roda no FFmpeg em fluxo (frame a frame), cena por cena; o uso de
RAM não cresce com a duração do vídeo.

Uso:
    python video_editor.py
    python video_editor.py --date 2026-11-05
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from visual_generator import FPS, HEIGHT, WIDTH, render_clip

log = logging.getLogger("video_editor")

PROJECT_DIR = Path(__file__).parent
SAMPLE_RATE = 48000

# --- áudio
DUCK_DB = -18.0           # quanto a música cai enquanto há fala
MUSIC_BASE_DB = -6.0      # volume da música (sem fala) em relação ao arquivo original
DUCK_RAMP_SECONDS = 0.15
SPEECH_MERGE_GAP = 0.6    # pausas menores que isso não "levantam" a música
SFX_DB = -8.0
SFX_LEAD_SECONDS = 0.08   # o efeito começa um pouco antes do corte
TARGET_LUFS = -14         # padrão de TikTok/Reels/Shorts

# --- legendas
SUB_MAX_WORDS = 4
SUB_MIN_WORDS = 2
SUB_MAX_LINE_CHARS = 14
SUB_MAX_LINES = 2
SUB_POS_Y = 1180          # ~61% da altura: altura dos olhos, longe da UI do TikTok
SUB_FONT_SIZE = 92
YELLOW = "&H0000D7FF&"    # ASS usa BGR: #FFD700
RED = "&H00303BFF&"       # #FF3B30
WHITE = "&H00FFFFFF&"

SFX_ALIASES = {
    "whoosh": ["whoosh"],
    "thunder": ["thunder", "impact"],
    "suspense_riser": ["suspense_riser", "riser"],
    "old_film_click": ["old_film_click", "click", "film_click"],
    "crowd_murmur": ["crowd_murmur", "crowd", "murmur"],
}
AUDIO_EXTENSIONS = (".mp3", ".wav", ".m4a", ".ogg", ".flac")


class VideoEditorError(RuntimeError):
    pass


@dataclass
class TimedWord:
    text: str
    start: float
    end: float


@dataclass
class ScenePlan:
    scene_id: int
    image: Path
    audio: Path
    motion: str
    sfx_cue: str
    transition: str | None
    frames: int
    start: float          # início da cena no vídeo final (s)
    words: list[TimedWord]  # tempo absoluto no vídeo final

    @property
    def duration(self) -> float:
        return self.frames / FPS


# --------------------------------------------------------------------------- utilidades


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    log.debug("%s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise VideoEditorError(f"FFmpeg falhou: {result.stderr.strip()[-800:]}")


def db_to_gain(db: float) -> float:
    return 10 ** (db / 20)


def ffmpeg_threads() -> list[str]:
    threads = os.getenv("FFMPEG_THREADS", "").strip()
    return ["-threads", threads] if threads else []


def transition_for(scene_index: int, sfx_cue: str) -> str | None:
    if scene_index == 0:
        return None  # o hook entra seco
    return "whip" if sfx_cue == "whoosh" else "flash"


def find_sfx(cue: str, sfx_dir: Path) -> Path | None:
    """Arquivo do efeito: primeiro os seus (whoosh.mp3, impact.wav...), por último o
    sintético gerado pelo setup_assets.py ({cue}_synth.wav)."""
    names = SFX_ALIASES.get(cue, [cue])
    for name in [*names, f"{cue}_synth"]:
        for ext in AUDIO_EXTENSIONS:
            candidate = sfx_dir / f"{name}{ext}"
            if candidate.exists():
                return candidate
    return None


def pick_music(music_dir: Path, episode_id: int) -> Path | None:
    tracks = sorted(p for p in music_dir.glob("*") if p.suffix.lower() in AUDIO_EXTENSIONS)
    if not tracks:
        return None
    return tracks[episode_id % len(tracks)]  # varia entre episódios, estável no mesmo episódio


# --------------------------------------------------------------------------- ducking


def speech_regions(words: list[TimedWord], merge_gap: float = SPEECH_MERGE_GAP) -> list[tuple[float, float]]:
    regions: list[list[float]] = []
    for w in sorted(words, key=lambda w: w.start):
        if regions and w.start - regions[-1][1] <= merge_gap:
            regions[-1][1] = max(regions[-1][1], w.end)
        else:
            regions.append([w.start, w.end])
    return [(round(a, 3), round(b, 3)) for a, b in regions]


def ducking_expression(regions: list[tuple[float, float]], base_db: float = MUSIC_BASE_DB,
                       duck_db: float = DUCK_DB, ramp: float = DUCK_RAMP_SECONDS) -> str:
    """Expressão do filtro volume: base_db fora da fala, base_db+duck_db durante a fala."""
    base, duck = db_to_gain(base_db), db_to_gain(duck_db)
    if not regions:
        return f"{base:.4f}"
    # Para cada região: 0 dentro dela, sobe linearmente até 1 a "ramp" segundos de distância.
    terms = [f"clip(max({a:.3f}-t,t-{b:.3f})/{ramp},0,1)" for a, b in regions]
    nearest = terms[0]
    for term in terms[1:]:
        nearest = f"min({nearest},{term})"
    return f"{base:.4f}*({duck:.4f}+{1 - duck:.4f}*{nearest})"


# --------------------------------------------------------------------------- legendas


def _ass_time(seconds: float) -> str:
    cs = max(0, round(seconds * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(text: str) -> str:
    return re.sub(r"[{}\\]", "", text).strip().upper()


def _lines(words: list[str]) -> list[list[int]]:
    """Quebra índices de palavras em linhas de até SUB_MAX_LINE_CHARS caracteres."""
    lines: list[list[int]] = [[]]
    length = 0
    for i, w in enumerate(words):
        extra = len(w) + (1 if lines[-1] else 0)
        if lines[-1] and length + extra > SUB_MAX_LINE_CHARS:
            lines.append([i])
            length = len(w)
        else:
            lines[-1].append(i)
            length += extra
    return lines


def _sentences(words: list[TimedWord]) -> list[list[TimedWord]]:
    """Separa em frases: pontuação final ou pausa longa. Um grupo nunca cruza frases."""
    groups: list[list[TimedWord]] = [[]]
    for i, w in enumerate(words):
        groups[-1].append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt and (re.search(r"[.!?…:;]$", w.text) or nxt.start - w.end > 0.35):
            groups.append([])
    return [g for g in groups if g]


def _fits(chunk: list[TimedWord]) -> bool:
    return len(_lines([_clean(w.text) for w in chunk])) <= SUB_MAX_LINES


def _chunk_cost(size: int) -> float:
    # Cada grupo custa 1; grupo de 1 palavra custa bem mais; 2-3 palavras é o ideal.
    return 1 + (2.5 if size == 1 else 0) + (0.3 if size == SUB_MAX_WORDS else 0)


def _balanced(sentence: list[TimedWord]) -> list[list[TimedWord]]:
    """Melhor divisão da frase (programação dinâmica): poucos grupos, que caibam na tela,
    evitando palavra sozinha. Ex.: 5 palavras -> 3+2 ou 2+3, nunca 4+1."""
    n = len(sentence)
    best = [0.0] + [math.inf] * n
    cut = [0] * (n + 1)
    for end in range(1, n + 1):
        for size in range(1, min(SUB_MAX_WORDS, end) + 1):
            chunk = sentence[end - size:end]
            if size > 1 and not _fits(chunk):
                continue
            cost = best[end - size] + _chunk_cost(size)
            if cost < best[end]:
                best[end], cut[end] = cost, end - size
    chunks, end = [], n
    while end > 0:
        chunks.append(sentence[cut[end]:end])
        end = cut[end]
    return chunks[::-1]


def chunk_words(words: list[TimedWord]) -> list[list[TimedWord]]:
    """Grupos de até 4 palavras (normalmente 2-4) em no máximo 2 linhas, sem cruzar frases.

    Grupo de 1 palavra só acontece em frase de 1 palavra ou palavra longa demais.
    """
    return [chunk for sentence in _sentences(words) for chunk in _balanced(sentence)]


def build_ass(words: list[TimedWord], video_end: float, font: str) -> str:
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {WIDTH}
PlayResY: {HEIGHT}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,{font},{SUB_FONT_SIZE},{WHITE},{WHITE},&H00000000&,&H90000000&,-1,0,0,0,100,100,1,0,1,9,4,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    chunks = chunk_words(words)
    for c_index, chunk in enumerate(chunks):
        texts = [_clean(w.text) for w in chunk]
        lines = _lines(texts)
        chunk_end = chunks[c_index + 1][0].start if c_index + 1 < len(chunks) else min(
            video_end, chunk[-1].end + 0.4)
        for k, word in enumerate(chunk):
            start = word.start
            end = chunk[k + 1].start if k + 1 < len(chunk) else chunk_end
            if end <= start:
                continue
            color = RED if re.search(r"\d", word.text) else YELLOW
            rendered_lines = []
            for line in lines:
                parts = []
                for i in line:
                    if i == k:
                        parts.append(f"{{\\c{color}}}{texts[i]}{{\\c{WHITE}}}")
                    else:
                        parts.append(texts[i])
                rendered_lines.append(" ".join(parts))
            # Entrada do grupo com "pop" (112% -> 100% em 90 ms).
            pop = "\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)" if k == 0 else ""
            text = f"{{\\an5\\pos({WIDTH // 2},{SUB_POS_Y}){pop}}}" + "\\N".join(rendered_lines)
            events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Hook,,0,0,0,,{text}")
    return header + "\n".join(events) + "\n"


# --------------------------------------------------------------------------- editor


class VideoEditor:
    def __init__(self, output_dir: Path | None = None, episodes_dir: Path | None = None,
                 assets_dir: Path | None = None) -> None:
        if shutil.which("ffmpeg") is None:
            raise VideoEditorError("FFmpeg não encontrado. Confira com: ffmpeg -version")
        self.output_dir = output_dir or PROJECT_DIR / os.getenv("OUTPUT_DIR", "output")
        self.assets_dir = assets_dir or PROJECT_DIR / "assets"
        self.episodes_dir = episodes_dir or self.assets_dir / "episodes"
        self.font = os.getenv("SUBTITLE_FONT") or "Arial Black"

    # -- planejamento -------------------------------------------------------

    def plan(self, day: date) -> tuple[dict, list[ScenePlan], Path]:
        episode_path = self.output_dir / day.isoformat() / "current_episode.json"
        if not episode_path.exists():
            raise VideoEditorError(f"{episode_path} não existe. Rode antes: python story_engine.py")
        episode = json.loads(episode_path.read_text(encoding="utf-8"))
        episode_id = episode.get("topic_id")
        base = self.episodes_dir / str(episode_id)
        timeline_path = base / "audio" / "timeline.json"
        if not timeline_path.exists():
            raise VideoEditorError(f"{timeline_path} não existe. Rode antes: python voice_engine.py")
        timeline = json.loads(timeline_path.read_text(encoding="utf-8"))
        audio_by_scene = {s["scene_id"]: s for s in timeline["scenes"]}

        plans: list[ScenePlan] = []
        cursor = 0.0
        for index, scene in enumerate(episode["scenes"]):
            sid = scene["scene_id"]
            image = base / "visuals" / f"scene_{sid:02d}.png"
            if not image.exists():
                raise VideoEditorError(f"{image} não existe. Rode antes: python visual_generator.py")
            voice = audio_by_scene.get(sid)
            if voice is None:
                raise VideoEditorError(f"cena {sid} sem áudio no timeline.json")
            # Duração em frames inteiros: vídeo e áudio terminam no mesmo frame.
            frames = max(1, math.ceil(voice["duration"] * FPS - 1e-6))
            words = [TimedWord(w["text"], cursor + w["start"], cursor + w["end"]) for w in voice["words"]]
            plans.append(ScenePlan(sid, image, base / "audio" / voice["audio"], scene["motion_type"],
                                   scene["sfx_cue"], transition_for(index, scene["sfx_cue"]),
                                   frames, cursor, words))
            cursor += frames / FPS
        return episode, plans, base / "edit"

    # -- etapas -------------------------------------------------------------

    def render_scenes(self, plans: list[ScenePlan], work: Path) -> Path:
        """Clipe de cada cena com a duração exata; depois concatena sem recodificar."""
        clips = []
        for p in plans:
            clip = work / f"clip_{p.scene_id:02d}_{p.frames}f_{p.transition or 'cut'}.mp4"
            if not clip.exists():
                log.info("Cena %s: %.2fs, %s, transição %s", p.scene_id, p.duration, p.motion,
                         p.transition or "corte seco")
                render_clip(p.image, p.motion, p.duration, clip, transition=p.transition)
            clips.append(clip)
        listing = work / "clips.txt"
        listing.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
        video = work / "video_only.mp4"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
              "-i", listing.name, "-c", "copy", video.name], cwd=work)
        return video

    def build_narration(self, plans: list[ScenePlan], work: Path) -> Path:
        """Junta as falas, cada uma completada até a duração exata da cena."""
        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for p in plans:
            cmd += ["-i", str(p.audio)]
        parts = [f"[{i}:a]aresample={SAMPLE_RATE},apad=whole_dur={p.duration:.6f},"
                 f"atrim=end={p.duration:.6f}[n{i}]" for i, p in enumerate(plans)]
        graph = ";".join(parts) + ";" + "".join(f"[n{i}]" for i in range(len(plans)))
        graph += f"concat=n={len(plans)}:v=0:a=1[out]"
        narration = work / "narration.wav"
        _run(cmd + ["-filter_complex", graph, "-map", "[out]", "-ac", "1",
                    "-c:a", "pcm_s16le", str(narration)])
        return narration

    def render_final(self, episode: dict, plans: list[ScenePlan], work: Path, output: Path) -> Path:
        total = sum(p.duration for p in plans)
        video = self.render_scenes(plans, work)
        narration = self.build_narration(plans, work)

        words = [w for p in plans for w in p.words]
        (work / "subtitles.ass").write_text(build_ass(words, total, self.font), encoding="utf-8")

        inputs = ["-i", video.name, "-i", narration.name]
        audio_labels = ["[1:a]"]
        graph = []

        music = pick_music(self.assets_dir / "music", int(episode.get("topic_id") or 0))
        next_input = 2
        if music:
            inputs = inputs + ["-stream_loop", "-1", "-i", str(music.resolve())]
            expr = ducking_expression(speech_regions(words))
            graph.append(f"[{next_input}:a]aresample={SAMPLE_RATE},atrim=end={total:.3f},"
                         f"volume='{expr}':eval=frame[music]")
            audio_labels.append("[music]")
            next_input += 1
        else:
            log.warning("Nenhuma música em assets/music/ – vídeo sem trilha")

        missing_sfx = set()
        for p in plans:
            sfx = find_sfx(p.sfx_cue, self.assets_dir / "sfx")
            if sfx is None:
                missing_sfx.add(p.sfx_cue)
                continue
            delay_ms = max(0, round((p.start - SFX_LEAD_SECONDS) * 1000))
            inputs += ["-i", str(sfx.resolve())]
            label = f"[sfx{p.scene_id}]"
            graph.append(f"[{next_input}:a]aresample={SAMPLE_RATE},volume={SFX_DB}dB,"
                         f"adelay={delay_ms}:all=1{label}")
            audio_labels.append(label)
            next_input += 1
        if missing_sfx:
            log.warning("Sem arquivo em assets/sfx/ para: %s", ", ".join(sorted(missing_sfx)))

        mix_in = "".join(audio_labels)
        graph.append(f"{mix_in}amix=inputs={len(audio_labels)}:duration=first:normalize=0,"
                     f"atrim=end={total:.3f},loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11,"
                     f"aresample={SAMPLE_RATE}[aout]")
        subtitles = "subtitles=subtitles.ass"
        fonts_dir = self.assets_dir / "fonts"
        if any(fonts_dir.glob("*.[ot]tf")):
            try:
                subtitles += f":fontsdir={os.path.relpath(fonts_dir.resolve(), work.resolve())}"
            except ValueError:  # Windows: drives diferentes
                pass
        graph.append(f"[0:v]{subtitles}[vout]")

        output.parent.mkdir(parents=True, exist_ok=True)
        cmd = (["ffmpeg", "-y", "-loglevel", "error"] + inputs + ffmpeg_threads() +
               ["-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", str(SAMPLE_RATE),
                "-t", f"{total:.3f}", "-movflags", "+faststart", str(output.resolve())])
        log.info("Renderizando vídeo final (%.1fs)...", total)
        _run(cmd, cwd=work)
        return output

    def edit(self, day: date) -> Path:
        episode, plans, work = self.plan(day)
        work.mkdir(parents=True, exist_ok=True)
        output = self.output_dir / day.isoformat() / "final_video.mp4"
        self.render_final(episode, plans, work, output)
        (work / "render.json").write_text(json.dumps({
            "episode_id": episode.get("topic_id"),
            "duration_seconds": round(sum(p.duration for p in plans), 3),
            "scenes": [{"scene_id": p.scene_id, "start": round(p.start, 3),
                        "duration": round(p.duration, 3), "frames": p.frames,
                        "transition": p.transition, "sfx_cue": p.sfx_cue} for p in plans],
            "output": str(output),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Vídeo pronto: %s", output)
        return output


def mark_rendered(topic_id: int, video: Path) -> None:
    from database import StoryDatabase

    db = StoryDatabase()
    topic = db.get(topic_id)
    if topic.status == "verified":
        db.set_status(topic_id, "rendered", video_path=str(video))
    else:
        log.info("Tema #%s está em '%s'; status não alterado", topic_id, topic.status)


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_DIR / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description="Monta o vídeo final do episódio.")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        editor = VideoEditor()
        video = editor.edit(args.date)
        episode = json.loads((editor.output_dir / args.date.isoformat() / "current_episode.json")
                             .read_text(encoding="utf-8"))
        if episode.get("topic_id") is not None:
            mark_rendered(int(episode["topic_id"]), video)
    except VideoEditorError as exc:
        log.error("%s", exc)
        return 1
    print(video)
    return 0


if __name__ == "__main__":
    sys.exit(main())
