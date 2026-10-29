"""Prepara assets/sfx/ e assets/music/ para o primeiro vídeo sair com a mixagem completa.

1. Lê do próprio código quais efeitos o roteiro pode pedir (story_engine.SfxCue) e quais
   nomes de arquivo o editor aceita para cada um (video_editor.SFX_ALIASES).
2. Para cada efeito sem arquivo, sintetiza um com FFmpeg (lavfi). Som gerado por você não
   tem direitos de terceiros, então não há licença nem atribuição a resolver.
3. Se assets/music/ estiver vazia, gera um drone de tensão (fundo grave contínuo, em loop).

Nunca sobrescreve um arquivo seu: se você já colocou "whoosh.mp3" ou "impact.wav", ele é
mantido. Os sintéticos servem para testar; troque a música por uma trilha real depois
(ex.: YouTube Audio Library, ou faixas com licença comercial sem atribuição).

Uso:
    python setup_assets.py            # gera só o que falta
    python setup_assets.py --list     # mostra o que o editor vai usar
    python setup_assets.py --force    # regenera os sintéticos (*_synth.wav)
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import get_args

from story_engine import SfxCue
from video_editor import AUDIO_EXTENSIONS, SFX_ALIASES, find_sfx

PROJECT_DIR = Path(__file__).parent
SFX_DIR = PROJECT_DIR / "assets" / "sfx"
MUSIC_DIR = PROJECT_DIR / "assets" / "music"
SAMPLE_RATE = 48000
SYNTH_SUFFIX = "_synth"

# Receitas FFmpeg (filter_complex com saída [out]). Cada uma termina normalizada.
SFX_RECIPES = {
    # Rajada de ar: ruído rosa com subida e descida de volume e filtro de banda.
    "whoosh": (0.7, (
        "anoisesrc=color=pink:d=0.7:r={sr}:a=0.9,highpass=f=350,lowpass=f=5000,"
        "afade=t=in:d=0.32:curve=qsin,afade=t=out:st=0.32:d=0.38:curve=exp,"
        "aphaser=type=t:speed=1.8[out]"
    )),
    # Impacto/trovão: boom grave curto + estrondo de ruído marrom com decaimento exponencial
    # (cai ~19 dB por segundo, como um estrondo real).
    "thunder": (2.2, (
        "sine=f=48:d=2.2:r={sr},volume='exp(-5*t)':eval=frame[boom];"
        "anoisesrc=color=brown:d=2.2:r={sr}:a=1,lowpass=f=420,"
        "volume='min(1,t/0.015)*exp(-2.2*t)':eval=frame[rumble];"
        "[boom][rumble]amix=inputs=2:normalize=0[out]"
    )),
    # Riser: tom que sobe de 180 Hz para ~1,2 kHz + ruído crescente, termina seco.
    "suspense_riser": (2.4, (
        "aevalsrc='0.5*sin(2*PI*(180*t+215*t*t))*(t/2.4)':d=2.4:s={sr}[tone];"
        "anoisesrc=color=white:d=2.4:r={sr}:a=0.25,highpass=f=1500,"
        "afade=t=in:d=2.3:curve=exp[air];"
        "[tone][air]amix=inputs=2:normalize=0,afade=t=out:st=2.32:d=0.08[out]"
    )),
    # Projetor antigo: estalos a 24 quadros/s sobre chiado de fita.
    "old_film_click": (1.2, (
        "aevalsrc='0.9*lt(mod(t,1/24),0.0015)*(random(0)*2-1)':d=1.2:s={sr}[clicks];"
        "anoisesrc=color=white:d=1.2:r={sr}:a=0.04,highpass=f=2000[hiss];"
        "[clicks][hiss]amix=inputs=2:normalize=0,afade=t=out:st=1.0:d=0.2[out]"
    )),
    # Murmúrio de multidão: ruído na faixa da voz com oscilação lenta de volume.
    "crowd_murmur": (2.5, (
        "anoisesrc=color=brown:d=2.5:r={sr}:a=1,bandpass=f=700:width_type=h:w=900,"
        "tremolo=f=3.3:d=0.5,tremolo=f=1.7:d=0.4,"
        "afade=t=in:d=0.4,afade=t=out:st=1.9:d=0.6[out]"
    )),
}

# Drone de tensão de 64 s. Frequências com ciclos inteiros em 64 s: o loop não dá "clique".
MUSIC_SECONDS = 64
MUSIC_RECIPE = (
    "aevalsrc='0.30*sin(2*PI*55*t)+0.18*sin(2*PI*82.5*t)+0.10*sin(2*PI*110*t)"
    "+0.06*sin(2*PI*116.5*t)':d={d}:s={sr},"
    "tremolo=f=0.25:d=0.35[pad];"
    "aevalsrc='0.55*sin(2*PI*50*t)*exp(-9*mod(t,1.0))*lt(mod(t,4),2)':d={d}:s={sr}[pulse];"
    "anoisesrc=color=pink:d={d}:r={sr}:a=0.05,lowpass=f=600[air];"
    "[pad][pulse][air]amix=inputs=3:normalize=0,aecho=0.8:0.6:180:0.3[out]"
)


PEAK_DB = -3.0


def _ffmpeg(args: list[str], name: str) -> str:
    result = subprocess.run(["ffmpeg", "-y", "-hide_banner", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg falhou em {name}: {result.stderr.strip()[-400:]}")
    return result.stderr


def _render(graph: str, output: Path, duration: float, loudness: bool = False) -> None:
    """Gera o som e normaliza.

    Efeitos curtos: normalização de PICO (ganho fixo), que preserva o envelope; o loudnorm
    age como compressor e achataria o decaimento de um trovão. Música: loudnorm.
    O atrim garante a duração exata (importante para o loop da música fechar sem clique).
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = output.with_suffix(".raw.wav")
    _ffmpeg(["-loglevel", "error", "-filter_complex",
             f"{graph};[out]aresample={SAMPLE_RATE},atrim=end_sample={round(duration * SAMPLE_RATE)}[r]",
             "-map", "[r]", "-ac", "1", "-c:a", "pcm_s16le", str(raw)], output.name)
    try:
        if loudness:
            final = f"loudnorm=I=-16:TP=-2:LRA=7,aresample={SAMPLE_RATE}"
        else:
            stats = _ffmpeg(["-i", str(raw), "-af", "volumedetect", "-f", "null", "-"], output.name)
            peak = float(re.search(r"max_volume: (-?[\d.]+) dB", stats).group(1))
            final = f"volume={PEAK_DB - peak:.2f}dB"
        _ffmpeg(["-loglevel", "error", "-i", str(raw), "-af",
                 f"{final},atrim=end_sample={round(duration * SAMPLE_RATE)}",
                 "-ac", "1", "-c:a", "pcm_s16le", str(output)], output.name)
    finally:
        raw.unlink(missing_ok=True)


def expected_cues() -> list[str]:
    cues = list(get_args(SfxCue))
    missing_alias = [c for c in cues if c not in SFX_ALIASES]
    if missing_alias:
        raise RuntimeError(f"o editor não tem nomes de arquivo para: {missing_alias}")
    return cues


def music_tracks(music_dir: Path = MUSIC_DIR) -> list[Path]:
    return sorted(p for p in music_dir.glob("*") if p.suffix.lower() in AUDIO_EXTENSIONS)


def setup(sfx_dir: Path = SFX_DIR, music_dir: Path = MUSIC_DIR, force: bool = False) -> list[Path]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg não encontrado. Instale e confira com: ffmpeg -version")
    created = []
    for cue in expected_cues():
        synth = sfx_dir / f"{cue}{SYNTH_SUFFIX}.wav"
        existing = find_sfx(cue, sfx_dir)
        if existing and existing != synth:
            print(f"  ✓ {cue:<15} usa {existing.name} (seu arquivo, mantido)")
            continue
        if synth.exists() and not force:
            continue
        duration, graph = SFX_RECIPES[cue]
        _render(graph.format(sr=SAMPLE_RATE), synth, duration)
        created.append(synth)
        print(f"  + {cue:<15} gerado: {synth.name} ({duration}s)")

    synth_music = music_dir / f"tension_drone{SYNTH_SUFFIX}.wav"
    others = [t for t in music_tracks(music_dir) if t != synth_music]
    if others:
        print(f"  ✓ música: {len(others)} faixa(s) sua(s), drone sintético não é necessário")
    elif force or not synth_music.exists():
        _render(MUSIC_RECIPE.format(d=MUSIC_SECONDS + 1, sr=SAMPLE_RATE), synth_music, MUSIC_SECONDS,
                loudness=True)
        created.append(synth_music)
        print(f"  + música gerada: {synth_music.name} ({MUSIC_SECONDS}s em loop)")
    return created


def report(sfx_dir: Path = SFX_DIR, music_dir: Path = MUSIC_DIR) -> bool:
    ok = True
    print("Efeitos (sfx_cue -> arquivo que o editor vai usar):")
    for cue in expected_cues():
        found = find_sfx(cue, sfx_dir)
        names = ", ".join(f"{a}.mp3/.wav" for a in SFX_ALIASES[cue])
        print(f"  {'✓' if found else '✗'} {cue:<15} -> {found.name if found else 'FALTA (' + names + ')'}")
        ok &= found is not None
    tracks = music_tracks(music_dir)
    print("Música:", ", ".join(t.name for t in tracks) if tracks else "✗ nenhuma em assets/music/")
    return ok and bool(tracks)


def utf8_console() -> None:
    """No Windows, saída redirecionada (pipe, Claude Code, agendador) usa cp1252 e quebra
    com ✓/✗. Força UTF-8 e troca o que não der para escrever, em vez de travar."""
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    utf8_console()
    parser = argparse.ArgumentParser(description="Gera os SFX e a trilha que faltam.")
    parser.add_argument("--list", action="store_true", help="só mostra o que existe")
    parser.add_argument("--force", action="store_true", help="regenera os arquivos *_synth")
    args = parser.parse_args(argv)
    if not args.list:
        print("Preparando assets...")
        setup(force=args.force)
    print()
    return 0 if report() else 1


if __name__ == "__main__":
    sys.exit(main())
