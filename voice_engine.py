"""Módulo 3 – Narração com timestamps por palavra.

Para cada cena de output/AAAA-MM-DD/current_episode.json:
1. gera a fala com o provedor do .env (TTS_PROVIDER = edge | elevenlabs | openai);
2. obtém o início/fim de cada palavra:
   - edge-tts: evento WordBoundary (precisa de boundary="WordBoundary");
   - ElevenLabs: tempos por caractere, agrupados em palavras;
   - OpenAI: não devolve tempos -> alinhamento local com faster-whisper;
3. corta silêncios: começo/fim da cena e pausas internas maiores que 0,25 s;
4. salva assets/episodes/{episode_id}/audio/scene_XX.wav e audio/timeline.json
   com a duração exata (medida com ffprobe) e as palavras já com o tempo corrigido.

Uso:
    python voice_engine.py
    python voice_engine.py --date 2026-11-05
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Protocol

log = logging.getLogger("voice_engine")

PROJECT_DIR = Path(__file__).parent
SAMPLE_RATE = 48000

MAX_GAP_SECONDS = 0.25   # pausa máxima permitida dentro e entre cenas
LEAD_SECONDS = 0.04      # ar antes da primeira palavra da cena
TAIL_SECONDS = 0.12      # ar depois da última palavra (somado ao LEAD da próxima: 0,16 s)

MAX_ATTEMPTS = 4
BACKOFF_SECONDS = (2, 4, 8)
TICKS_PER_SECOND = 10_000_000  # unidade de tempo do edge-tts

DEFAULT_EDGE_VOICES = {"pt-BR": "pt-BR-AntonioNeural", "es-419": "es-VE-SebastianNeural"}


class VoiceEngineError(RuntimeError):
    pass


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class Speech:
    """Áudio cru do TTS + palavras com tempo relativo ao início desse áudio."""

    audio: bytes
    extension: str  # "mp3", "wav"...
    words: list[Word]


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, language: str) -> Speech: ...


# --------------------------------------------------------------------------- provedores


class EdgeTTSProvider:
    """Grátis. Os tempos vêm do evento WordBoundary do próprio serviço."""

    name = "edge"

    def __init__(self, voices: dict[str, str] | None = None, rate: str | None = None) -> None:
        self.voices = voices or {
            "pt-BR": os.getenv("EDGE_TTS_VOICE_PT") or DEFAULT_EDGE_VOICES["pt-BR"],
            "es-419": os.getenv("EDGE_TTS_VOICE_ES") or DEFAULT_EDGE_VOICES["es-419"],
        }
        self.rate = rate or os.getenv("EDGE_TTS_RATE") or "+5%"

    def synthesize(self, text: str, language: str) -> Speech:
        return asyncio.run(self._synthesize(text, self.voices[language]))

    async def _synthesize(self, text: str, voice: str) -> Speech:
        import edge_tts

        # Sem boundary="WordBoundary" o edge-tts 7.x manda só SentenceBoundary.
        communicate = edge_tts.Communicate(text, voice, rate=self.rate, boundary="WordBoundary",
                                           proxy=https_proxy_for(EDGE_TTS_HOST))
        audio = bytearray()
        words: list[Word] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / TICKS_PER_SECOND
                words.append(Word(chunk["text"], start, start + chunk["duration"] / TICKS_PER_SECOND))
        return Speech(bytes(audio), "mp3", words)


EDGE_TTS_HOST = "speech.platform.bing.com"


def https_proxy_for(host: str) -> str | None:
    """Proxy HTTPS das variáveis de ambiente (HTTPS_PROXY), se houver e o host não estiver
    em NO_PROXY. O edge-tts usa aiohttp, que ignora essas variáveis: em rede que só sai
    por proxy (ex.: Claude Code na nuvem) a voz falhava com "Name or service not known".
    Sem HTTPS_PROXY (o normal no PC) devolve None e a conexão segue direta."""
    from urllib.request import getproxies_environment, proxy_bypass_environment

    proxy = getproxies_environment().get("https")
    if proxy and not proxy_bypass_environment(host):
        return proxy
    return None


def words_from_characters(chars: list[str], starts: list[float], ends: list[float]) -> list[Word]:
    """Agrupa tempos por caractere (ElevenLabs) em palavras."""
    words: list[Word] = []
    current, w_start, w_end = "", 0.0, 0.0
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if current:
                words.append(Word(current, w_start, w_end))
                current = ""
            continue
        if not current:
            w_start = s
        current += ch
        w_end = e
    if current:
        words.append(Word(current, w_start, w_end))
    return words


class ElevenLabsProvider:
    """Pago. Devolve tempo de cada caractere junto com o áudio."""

    name = "elevenlabs"

    def __init__(self, client=None, voice_id: str | None = None, model_id: str | None = None) -> None:
        self.voice_id = voice_id or os.getenv("ELEVENLABS_VOICE_ID")
        self.model_id = model_id or os.getenv("ELEVENLABS_MODEL_ID") or None
        if not self.voice_id:
            raise VoiceEngineError("Defina ELEVENLABS_VOICE_ID no .env")
        if client is None:
            from elevenlabs.client import ElevenLabs

            if not os.getenv("ELEVENLABS_API_KEY"):
                raise VoiceEngineError("Defina ELEVENLABS_API_KEY no .env")
            client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
        self.client = client

    def synthesize(self, text: str, language: str) -> Speech:
        kwargs = {"text": text, "output_format": "mp3_44100_128"}
        if self.model_id:
            kwargs["model_id"] = self.model_id
        response = self.client.text_to_speech.convert_with_timestamps(self.voice_id, **kwargs)
        alignment = response.alignment
        if alignment is None:
            raise VoiceEngineError("ElevenLabs respondeu sem alinhamento de tempo")
        words = words_from_characters(
            alignment.characters,
            alignment.character_start_times_seconds,
            alignment.character_end_times_seconds,
        )
        return Speech(base64.b64decode(response.audio_base_64), "mp3", words)


class WhisperAligner:
    """Encontra o tempo de cada palavra num áudio já gerado (faster-whisper, local)."""

    def __init__(self, model_size: str | None = None) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceEngineError(
                "Este provedor não devolve tempos por palavra. Instale o alinhador local: "
                "pip install faster-whisper"
            ) from exc
        size = model_size or os.getenv("WHISPER_MODEL_SIZE") or "small"
        self.model = WhisperModel(size, device="cpu", compute_type="int8")

    def align(self, audio_path: Path, language: str) -> list[Word]:
        segments, _ = self.model.transcribe(
            str(audio_path), language=language.split("-")[0], word_timestamps=True
        )
        return [Word(w.word.strip(), w.start, w.end)
                for segment in segments for w in (segment.words or []) if w.word.strip()]


class OpenAITTSProvider:
    """Pago. Não devolve tempos: usa WhisperAligner depois de gerar o áudio."""

    name = "openai"

    def __init__(self, client=None, aligner: WhisperAligner | None = None) -> None:
        self.model = os.getenv("OPENAI_TTS_MODEL")
        self.voice = os.getenv("OPENAI_TTS_VOICE")
        if not self.model or not self.voice:
            raise VoiceEngineError("Defina OPENAI_TTS_MODEL e OPENAI_TTS_VOICE no .env")
        if client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise VoiceEngineError("Instale o SDK: pip install openai") from exc
            client = OpenAI()
        self.client = client
        self.aligner = aligner or WhisperAligner()

    def synthesize(self, text: str, language: str) -> Speech:
        response = self.client.audio.speech.create(
            model=self.model, voice=self.voice, input=text, response_format="mp3"
        )
        audio = response.read() if hasattr(response, "read") else response.content
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "speech.mp3"
            path.write_bytes(audio)
            words = self.aligner.align(path, language)
        return Speech(audio, "mp3", words)


def make_provider(name: str | None = None) -> TTSProvider:
    name = (name or os.getenv("TTS_PROVIDER") or "edge").strip().lower()
    if name == "edge":
        return EdgeTTSProvider()
    if name == "elevenlabs":
        return ElevenLabsProvider()
    if name == "openai":
        return OpenAITTSProvider()
    raise VoiceEngineError(f"TTS_PROVIDER inválido: {name!r} (use edge, elevenlabs ou openai)")


# --------------------------------------------------------------------------- áudio


def _run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise VoiceEngineError(f"{cmd[0]} falhou: {result.stderr.strip()[-500:]}")
    return result.stdout


def probe_duration(path: Path) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(out.strip())


def plan_segments(words: list[Word], audio_duration: float,
                  max_gap: float = MAX_GAP_SECONDS, lead: float = LEAD_SECONDS,
                  tail: float = TAIL_SECONDS) -> list[tuple[float, float]]:
    """Trechos do áudio original a manter, de modo que nenhuma pausa passe de max_gap."""
    if not words:
        raise VoiceEngineError("o TTS não devolveu nenhuma palavra com tempo")
    half = max_gap / 2
    segments: list[tuple[float, float]] = []
    seg_start = max(0.0, words[0].start - lead)
    for current, nxt in zip(words, words[1:]):
        if nxt.start - current.end > max_gap:
            segments.append((seg_start, current.end + half))
            seg_start = nxt.start - half
    segments.append((seg_start, min(audio_duration, words[-1].end + tail)))
    return segments


def remap_words(words: list[Word], segments: list[tuple[float, float]]) -> list[Word]:
    """Tempo de cada palavra depois de juntar os trechos."""
    remapped: list[Word] = []
    offset = 0.0
    bounds = []
    for start, end in segments:
        bounds.append((start, end, offset))
        offset += end - start
    for w in words:
        for start, end, base in bounds:
            if start - 1e-6 <= w.start <= end + 1e-6:
                remapped.append(Word(w.text, round(base + w.start - start, 3),
                                     round(base + min(w.end, end) - start, 3)))
                break
    return remapped


def tighten_audio(source: Path, words: list[Word], output: Path) -> tuple[list[Word], float]:
    """Corta silêncios longos e converte para WAV 48 kHz mono. Devolve palavras e duração."""
    segments = plan_segments(words, probe_duration(source))
    parts = [f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]"
             for i, (s, e) in enumerate(segments)]
    joined = "".join(f"[a{i}]" for i in range(len(segments)))
    graph = ";".join(parts) + f";{joined}concat=n={len(segments)}:v=0:a=1[out]"
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
          "-filter_complex", graph, "-map", "[out]",
          "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le", str(output)])
    return remap_words(words, segments), probe_duration(output)


# --------------------------------------------------------------------------- orquestração


@dataclass
class SceneAudio:
    scene_id: int
    audio: str
    duration: float
    words: list[Word]


class VoiceEngine:
    def __init__(
        self,
        provider: TTSProvider | None = None,
        output_dir: Path | None = None,
        episodes_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise VoiceEngineError("FFmpeg/ffprobe não encontrados. Confira com: ffmpeg -version")
        self.provider = provider or make_provider()
        self.output_dir = output_dir or PROJECT_DIR / os.getenv("OUTPUT_DIR", "output")
        self.episodes_dir = episodes_dir or PROJECT_DIR / "assets" / "episodes"
        self.sleep = sleep

    def _synthesize(self, text: str, language: str) -> Speech:
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                speech = self.provider.synthesize(text, language)
                if not speech.audio or not speech.words:
                    raise VoiceEngineError("áudio ou tempos vazios")
                return speech
            except Exception as exc:
                last = exc
                if attempt < MAX_ATTEMPTS:
                    wait = BACKOFF_SECONDS[attempt - 1]
                    log.warning("TTS tentativa %s falhou (%s); nova tentativa em %ss", attempt, exc, wait)
                    self.sleep(wait)
        raise VoiceEngineError(f"TTS falhou após {MAX_ATTEMPTS} tentativas: {last}") from last

    def process_episode(self, day: date) -> Path:
        episode_path = self.output_dir / day.isoformat() / "current_episode.json"
        if not episode_path.exists():
            raise VoiceEngineError(f"{episode_path} não existe. Rode antes: python story_engine.py")
        episode = json.loads(episode_path.read_text(encoding="utf-8"))
        episode_id = episode.get("topic_id")
        if episode_id is None:
            raise VoiceEngineError(f"{episode_path} não tem topic_id")
        language = episode.get("language", "pt-BR")
        audio_dir = self.episodes_dir / str(episode_id) / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)

        scenes: list[SceneAudio] = []
        for scene in episode["scenes"]:
            sid = scene["scene_id"]
            wav = audio_dir / f"scene_{sid:02d}.wav"
            words_file = audio_dir / f"scene_{sid:02d}.words.json"
            if wav.exists() and words_file.exists():
                words = [Word(**w) for w in json.loads(words_file.read_text(encoding="utf-8"))]
                duration = probe_duration(wav)
                log.info("Cena %s: áudio já existe (%.2fs)", sid, duration)
            else:
                speech = self._synthesize(scene["narration_text"], language)
                raw = audio_dir / f"scene_{sid:02d}.raw.{speech.extension}"
                raw.write_bytes(speech.audio)
                words, duration = tighten_audio(raw, speech.words, wav)
                raw.unlink()
                words_file.write_text(json.dumps([asdict(w) for w in words], ensure_ascii=False),
                                      encoding="utf-8")
                log.info("Cena %s: %.2fs, %s palavras", sid, duration, len(words))
            scenes.append(SceneAudio(sid, wav.name, round(duration, 3), words))

        timeline = audio_dir / "timeline.json"
        timeline.write_text(json.dumps({
            "episode_id": episode_id,
            "language": language,
            "provider": self.provider.name,
            "max_gap_seconds": MAX_GAP_SECONDS,
            "total_seconds": round(sum(s.duration for s in scenes), 3),
            "scenes": [{**asdict(s), "words": [asdict(w) for w in s.words]} for s in scenes],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Narração pronta: %.1fs em %s", sum(s.duration for s in scenes), timeline)
        return timeline


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_DIR / ".env")
    except ImportError:
        pass
    parser = argparse.ArgumentParser(description="Gera a narração com tempo por palavra.")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        print(VoiceEngine().process_episode(args.date))
    except VoiceEngineError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
