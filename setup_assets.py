"""Prepara assets/sfx/ e assets/music/ para o vídeo sair com a mixagem completa.

1. Efeitos gravados de verdade (chuva, trovão, buzina de navio, avião, helicóptero, carros
   numa ponte, correnteza, ondas, multidão, relógio, caneta no papel, câmera, explosão):
   baixa do Wikimedia Commons só gravações em domínio público ou CC0 (sem crédito
   obrigatório), corta os melhores trechos (o estrondo do trovão, a passagem do avião),
   iguala o volume e salva como {efeito}_lib_*.wav. Cada gravação rende 1 a 3 versões: o
   editor usa uma diferente a cada vez que o efeito se repete no vídeo.
2. Efeitos "desenhados" (whoosh, sobe-tensão, impacto grave, pane elétrica) são
   sintetizados com FFmpeg em várias versões ({efeito}_synth*.wav): som gerado por você
   não tem direitos de terceiros.
3. Se assets/music/ estiver vazia, gera um drone de tensão (fundo grave contínuo, em loop).

Nunca sobrescreve um arquivo seu: se você colocou "whoosh.mp3" ou "thunder.wav", ele vem
primeiro. Sem internet, os efeitos gravados ficam para a próxima vez (o vídeo sai só com
os sintéticos).

Uso:
    python setup_assets.py            # gera e baixa só o que falta
    python setup_assets.py --list     # mostra o que o editor vai usar
    python setup_assets.py --force    # regenera os sintéticos (*_synth*.wav)
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

from efeitos import BEDS, CUES
from video_editor import AUDIO_EXTENSIONS, SFX_ALIASES, find_sfx, sfx_variants

PROJECT_DIR = Path(__file__).parent
SFX_DIR = PROJECT_DIR / "assets" / "sfx"
MUSIC_DIR = PROJECT_DIR / "assets" / "music"
SAMPLE_RATE = 48000
SYNTH_SUFFIX = "_synth"

# Receitas FFmpeg (filter_complex com saída [out]), por nome de arquivo. Cada uma termina
# normalizada. Os nomes sem número são os de antes (quem já tem os arquivos não refaz).
SFX_RECIPES = {
    # Rajadas de ar: ruído filtrado com subida e descida de volume.
    "whoosh_synth": (0.7, (
        "anoisesrc=color=pink:d=0.7:r={sr}:a=0.9,highpass=f=350,lowpass=f=5000,"
        "afade=t=in:d=0.32:curve=qsin,afade=t=out:st=0.32:d=0.38:curve=exp,"
        "aphaser=type=t:speed=1.8[out]"
    )),
    "whoosh_synth2": (0.9, (
        "anoisesrc=color=white:d=0.9:r={sr}:a=0.8,highpass=f=600,lowpass=f=7000,"
        "afade=t=in:d=0.5:curve=qsin,afade=t=out:st=0.5:d=0.4:curve=exp,aphaser=type=t:speed=0.9[out]"
    )),
    "whoosh_synth3": (0.5, (
        "anoisesrc=color=brown:d=0.5:r={sr}:a=1,highpass=f=200,lowpass=f=3000,"
        "afade=t=in:d=0.18:curve=qsin,afade=t=out:st=0.18:d=0.32:curve=exp[out]"
    )),
    # Trovão sintético: só vale quando não há gravação de trovão (boom + estrondo que decai).
    "thunder_synth": (2.2, (
        "sine=f=48:d=2.2:r={sr},volume='exp(-5*t)':eval=frame[boom];"
        "anoisesrc=color=brown:d=2.2:r={sr}:a=1,lowpass=f=420,"
        "volume='min(1,t/0.015)*exp(-2.2*t)':eval=frame[rumble];"
        "[boom][rumble]amix=inputs=2:normalize=0[out]"
    )),
    # Sobe-tensão: tom que sobe + ar crescente, termina seco.
    "suspense_riser_synth": (2.4, (
        "aevalsrc='0.5*sin(2*PI*(180*t+215*t*t))*(t/2.4)':d=2.4:s={sr}[tone];"
        "anoisesrc=color=white:d=2.4:r={sr}:a=0.25,highpass=f=1500,"
        "afade=t=in:d=2.3:curve=exp[air];"
        "[tone][air]amix=inputs=2:normalize=0,afade=t=out:st=2.32:d=0.08[out]"
    )),
    "riser_synth2": (1.8, (
        "aevalsrc='0.5*sin(2*PI*(140*t+190*t*t))*(t/1.8)':d=1.8:s={sr}[tone];"
        "anoisesrc=color=pink:d=1.8:r={sr}:a=0.3,highpass=f=900,afade=t=in:d=1.7:curve=exp[air];"
        "[tone][air]amix=inputs=2:normalize=0,afade=t=out:st=1.74:d=0.06[out]"
    )),
    # Impacto de cinema para o gancho: grave que desce de 70 para 30 Hz + estalo.
    "boom_synth": (2.0, (
        "aevalsrc='0.9*sin(2*PI*(70*t-10*t*t))*exp(-2.2*t)':d=2.0:s={sr}[sub];"
        "anoisesrc=color=brown:d=2.0:r={sr}:a=1,lowpass=f=900,"
        "volume='min(1,t/0.004)*exp(-9*t)':eval=frame[hit];"
        "[sub][hit]amix=inputs=2:normalize=0[out]"
    )),
    "boom_synth2": (1.6, (
        "aevalsrc='0.9*sin(2*PI*(55*t-8*t*t))*exp(-2.8*t)':d=1.6:s={sr}[sub];"
        "anoisesrc=color=pink:d=1.6:r={sr}:a=0.8,bandpass=f=1800:width_type=h:w=2500,"
        "volume='min(1,t/0.002)*exp(-25*t)':eval=frame[click];"
        "[sub][click]amix=inputs=2:normalize=0[out]"
    )),
    # Batida pesada (colisão, queda): grave curto + ruído seco.
    "impact_synth": (1.2, (
        "aevalsrc='sin(2*PI*(90*t-25*t*t))*exp(-6*t)':d=1.2:s={sr}[low];"
        "anoisesrc=color=brown:d=1.2:r={sr}:a=1,lowpass=f=1200,"
        "volume='min(1,t/0.003)*exp(-12*t)':eval=frame[crunch];"
        "[low][crunch]amix=inputs=2:normalize=0[out]"
    )),
    # Pane elétrica / algo que apaga: tom que desce até sumir.
    "power_down_synth": (1.4, (
        "aevalsrc='(0.6*sin(2*PI*(420*t-135*t*t))+0.25*sin(4*PI*(420*t-135*t*t))"
        "+0.12*sin(6*PI*(420*t-135*t*t)))*(1-t/1.4)':d=1.4:s={sr},afade=t=out:st=1.1:d=0.3[out]"
    )),
    "power_down_synth2": (1.1, (
        "aevalsrc='(0.6*sin(2*PI*(300*t-118*t*t))+0.3*sin(4*PI*(300*t-118*t*t)))*(1-t/1.1)'"
        ":d=1.1:s={sr},afade=t=out:st=0.9:d=0.2[out]"
    )),
    # Sem as gravações (sem internet), cada efeito ainda tem uma versão sintetizada.
    # Ambientes de 7 s (o editor repete em loop se a cena for maior).
    "rain_synth": (7.0, (
        "anoisesrc=color=pink:d=7:r={sr}:a=0.6,highpass=f=400,lowpass=f=9000[hiss];"
        "aevalsrc='(random(0)-0.5)*gt(random(1),0.9985)':d=7:s={sr},highpass=f=1500,lowpass=f=6000,"
        "volume=6[drops];[hiss][drops]amix=inputs=2:normalize=0[out]"
    )),
    "waves_synth": (7.0, (
        "anoisesrc=color=brown:d=7:r={sr}:a=1,lowpass=f=1400,highpass=f=60,"
        "volume='0.25+0.75*pow(sin(PI*(t+0.8)/3.6),2)':eval=frame[out]"
    )),
    "river_synth": (7.0, (
        "anoisesrc=color=pink:d=7:r={sr}:a=0.9,bandpass=f=900:width_type=h:w=2400,"
        "tremolo=f=7.3:d=0.18,tremolo=f=2.1:d=0.15[out]"
    )),
    "traffic_synth": (7.0, (
        "anoisesrc=color=brown:d=7:r={sr}:a=0.8,lowpass=f=260[rumble];"
        "anoisesrc=color=pink:d=7:r={sr}:a=0.9,bandpass=f=850:width_type=h:w=1300,"
        "volume='exp(-pow((t-2.0)/0.8,2))+0.8*exp(-pow((t-5.3)/1.0,2))':eval=frame[cars];"
        "[rumble][cars]amix=inputs=2:normalize=0[out]"
    )),
    "crowd_synth": (7.0, (
        "anoisesrc=color=brown:d=7:r={sr}:a=1,bandpass=f=650:width_type=h:w=800,tremolo=f=3.1:d=0.5[a];"
        "anoisesrc=color=pink:d=7:r={sr}:a=0.6,bandpass=f=1300:width_type=h:w=900,tremolo=f=4.7:d=0.6[b];"
        "[a][b]amix=inputs=2:normalize=0,tremolo=f=0.7:d=0.3[out]"
    )),
    "plane_synth": (7.0, (
        "aevalsrc='(0.5*sin(2*PI*95*t)+0.3*sin(2*PI*190*t)+0.2*sin(2*PI*285*t))*(0.6+0.4*sin(2*PI*21*t))'"
        ":d=7:s={sr}[engine];anoisesrc=color=brown:d=7:r={sr}:a=0.5,lowpass=f=700[air];"
        "[engine][air]amix=inputs=2:normalize=0,volume='0.3+0.7*exp(-pow((t-3.5)/2.2,2))':eval=frame[out]"
    )),
    "helicopter_synth": (7.0, (
        "anoisesrc=color=brown:d=7:r={sr}:a=1,lowpass=f=420,"
        "volume='0.15+0.85*pow(abs(sin(PI*5.6*t)),10)':eval=frame[rotor];"
        "sine=f=1480:d=7:r={sr},volume=0.04[whine];"
        "[rotor][whine]amix=inputs=2:normalize=0,volume='0.4+0.6*exp(-pow((t-3.5)/2.6,2))':eval=frame[out]"
    )),
    # Efeitos curtos.
    "ship_horn_synth": (4.0, (
        "aevalsrc='(sin(2*PI*110*t)+0.5*sin(2*PI*220*t)+0.33*sin(2*PI*330*t)+0.25*sin(2*PI*440*t))"
        "*min(1,t/0.15)*if(lt(t,2.9),1,exp(-4*(t-2.9)))*(1+0.01*sin(2*PI*5*t))':d=4:s={sr},"
        "lowpass=f=1200,aecho=0.6:0.5:140:0.25[out]"
    )),
    "ship_horn_synth2": (4.0, (
        "aevalsrc='(sin(2*PI*92*t)+0.6*sin(2*PI*138*t)+0.4*sin(2*PI*184*t)+0.25*sin(2*PI*276*t))"
        "*min(1,t/0.2)*if(lt(t,2.6),1,exp(-3.5*(t-2.6)))':d=4:s={sr},lowpass=f=1000,"
        "aecho=0.6:0.6:220:0.3[out]"
    )),
    "clock_synth": (3.0, (
        "aevalsrc='lt(mod(t,0.5),0.0015)*if(lt(mod(t,1),0.5),1,0.6)':d=3:s={sr},"
        "bandpass=f=2600:width_type=h:w=700,aecho=0.5:0.3:18:0.25[out]"
    )),
    "camera_synth": (1.0, (
        "anoisesrc=color=white:d=1:r={sr}:a=1,highpass=f=1200,"
        "volume='exp(-120*t)+0.7*gte(t,0.09)*exp(-120*(t-0.09))':eval=frame[out]"
    )),
    "writing_synth": (3.0, (
        "anoisesrc=color=pink:d=3:r={sr}:a=1,bandpass=f=4200:width_type=h:w=3000,"
        "volume='0.15+0.85*pow(abs(sin(PI*2.7*t+sin(PI*1.3*t))),3)':eval=frame,afade=t=out:st=2.6:d=0.4[out]"
    )),
    "explosion_synth": (3.0, (
        "sine=f=42:d=3:r={sr},volume='exp(-3*t)':eval=frame[boom];"
        "anoisesrc=color=brown:d=3:r={sr}:a=1,lowpass=f=1800,"
        "volume='min(1,t/0.006)*exp(-1.6*t)':eval=frame[blast];"
        "[boom][blast]amix=inputs=2:normalize=0[out]"
    )),
    "thunder_synth2": (3.5, (
        "anoisesrc=color=white:d=3.5:r={sr}:a=0.6,highpass=f=1500,volume='exp(-30*t)':eval=frame[crack];"
        "anoisesrc=color=brown:d=3.5:r={sr}:a=1,lowpass=f=380,"
        "volume='min(1,t/0.05)*exp(-1.3*t)*(0.7+0.3*sin(2*PI*1.7*t))':eval=frame[roll];"
        "[crack][roll]amix=inputs=2:normalize=0[out]"
    )),
    # Batida de metal (casco contra pilar): parciais desafinadas que somem + baque grave.
    "impact_synth2": (1.6, (
        "aevalsrc='(0.5*sin(2*PI*220*t)+0.4*sin(2*PI*377*t)+0.3*sin(2*PI*563*t)+0.2*sin(2*PI*911*t))"
        "*exp(-4*t)+sin(2*PI*(80*t-20*t*t))*exp(-9*t)':d=1.6:s={sr}[out]"
    )),
    # Reservas antigas: tique de projetor (relógio) e murmúrio (multidão).
    "old_film_click_synth": (1.2, (
        "aevalsrc='0.9*lt(mod(t,1/24),0.0015)*(random(0)*2-1)':d=1.2:s={sr}[clicks];"
        "anoisesrc=color=white:d=1.2:r={sr}:a=0.04,highpass=f=2000[hiss];"
        "[clicks][hiss]amix=inputs=2:normalize=0,afade=t=out:st=1.0:d=0.2[out]"
    )),
    "crowd_murmur_synth": (2.5, (
        "anoisesrc=color=brown:d=2.5:r={sr}:a=1,bandpass=f=700:width_type=h:w=900,"
        "tremolo=f=3.3:d=0.5,tremolo=f=1.7:d=0.4,"
        "afade=t=in:d=0.4,afade=t=out:st=1.9:d=0.6[out]"
    )),
}

# Gravações reais do Wikimedia Commons, só em domínio público ou CC0 (não pedem crédito).
# (efeito, arquivo, modo, segundos, trechos). "peak": corta em volta dos estrondos mais
# fortes (trovão, buzina, explosão). "steady": pega os trechos contínuos mais presentes
# (a passagem do avião, a chuva forte, o tique do relógio).
SFX_LIBRARY = (
    ("thunder", "Storm thunderbolts.ogg", "peak", 4.0, 3),
    ("thunder", "Rain and thunder (1).ogg", "peak", 4.0, 2),
    ("thunder", "Rain and thunder.ogg", "peak", 4.0, 1),
    ("rain", "Rain (1).ogg", "steady", 7.0, 1),
    ("rain", "Rain against the window.ogg", "steady", 7.0, 1),
    ("ship_horn", "Cruise ship Albatros ship horn.ogg", "peak", 4.0, 2),
    ("ship_horn", "Original foghorn at East Brother Island Lighthouse.ogg", "peak", 4.0, 1),
    ("plane", "Propeller plane and birds singing.ogg", "steady", 7.0, 2),
    ("helicopter", "Chinook helicopter flying over Greenwich in London.ogg", "steady", 7.0, 2),
    ("traffic", "Cars passing over bridge.ogg", "steady", 7.0, 2),
    ("river", "James River Rapids 1.ogg", "steady", 7.0, 2),
    ("waves", "On a pebble beach.ogg", "steady", 7.0, 2),
    ("crowd", "Shopping mall less crowded.ogg", "steady", 7.0, 1),
    ("crowd", "1 minute at the alexa mall in berlin.ogg", "steady", 7.0, 1),
    ("clock", "Clock ticking.ogg", "steady", 3.0, 1),
    ("clock", "LA2 kitchen clock.ogg", "steady", 3.0, 1),
    ("writing", "292934-152 Writing-pen-paper-various marks and lines.wav", "steady", 3.0, 2),
    ("camera", "Holga shuttersound.ogg", "peak", 1.2, 1),
    ("explosion", "Explosions-LS100150.ogg", "peak", 3.0, 1),
    ("explosion", "Explosion 10.ogg", "peak", 2.5, 1),
    ("impact", "Dull thud.ogg", "peak", 0.8, 1),
)
ONE_SHOT_PEAK_DB = -3.0   # efeito curto: pico em -3 dBFS
BED_RMS_DB = -24.0        # ambiente: volume médio baixo, o editor ainda o coloca sob a voz

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


def _render(graph: str, output: Path, duration: float, loudness: bool = False, bed: bool = False) -> None:
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
            gain = PEAK_DB - peak
            if bed:  # ambiente: volume médio igual ao das gravações, sem passar do pico
                mean = float(re.search(r"mean_volume: (-?[\d.]+) dB", stats).group(1))
                gain = min(gain, BED_RMS_DB - mean)
            final = f"volume={gain:.2f}dB"
        _ffmpeg(["-loglevel", "error", "-i", str(raw), "-af",
                 f"{final},atrim=end_sample={round(duration * SAMPLE_RATE)}",
                 "-ac", "1", "-c:a", "pcm_s16le", str(output)], output.name)
    finally:
        raw.unlink(missing_ok=True)


def expected_cues() -> list[str]:
    missing_alias = [c for c in CUES if c not in SFX_ALIASES]
    if missing_alias:
        raise RuntimeError(f"o editor não tem nomes de arquivo para: {missing_alias}")
    return list(CUES)


def music_tracks(music_dir: Path = MUSIC_DIR) -> list[Path]:
    return sorted(p for p in music_dir.glob("*") if p.suffix.lower() in AUDIO_EXTENSIONS)


def _decode(path: Path):
    import numpy as np

    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(SAMPLE_RATE),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(pcm, np.float32)


def pick_segments(samples, mode: str, seconds: float, count: int) -> list[tuple[int, int]]:
    """Trechos (início, fim em amostras) que valem a pena, sem se sobrepor."""
    import numpy as np

    length = int(seconds * SAMPLE_RATE)
    if len(samples) <= length:
        return [(0, len(samples))]
    frame = SAMPLE_RATE // 20  # 50 ms
    frames = len(samples) // frame
    rms = np.sqrt(np.mean(samples[:frames * frame].reshape(frames, frame) ** 2, axis=1) + 1e-12)
    picks: list[tuple[int, int]] = []

    def free(start: int, end: int) -> bool:
        return all(end <= a or start >= b for a, b in picks)

    if mode == "peak":
        loudest = float(rms.max())
        for f in np.argsort(rms)[::-1]:
            if len(picks) >= count or rms[f] < loudest * 0.25:
                break
            onset = int(f)  # volta até o começo do estrondo, para não cortar o ataque
            while onset > 0 and rms[onset - 1] > rms[f] * 0.3 and f - onset < 20:
                onset -= 1
            start = max(0, (onset - 2) * frame)
            end = min(len(samples), start + length)
            if free(start, end):
                picks.append((start, end))
    else:
        width = max(1, length // frame)
        window = np.convolve(rms, np.ones(width) / width, mode="valid")
        for f in np.argsort(window)[::-1]:
            start = int(f) * frame
            end = start + length
            if len(picks) >= count:
                break
            if end <= len(samples) and free(start, end):
                picks.append((start, end))
    return sorted(picks)


def shape(segment, one_shot: bool, bed: bool):
    """Entrada e saída suaves e volume igualado (pico para efeito, média para ambiente)."""
    import numpy as np

    out = np.array(segment, dtype=np.float32)
    n = len(out)
    fade_in = min(n // 4, int((0.01 if one_shot else 0.3) * SAMPLE_RATE))
    fade_out = min(n // 2, int((0.35 * n) if one_shot else 0.3 * SAMPLE_RATE))
    if fade_in:
        out[:fade_in] *= np.linspace(0, 1, fade_in, dtype=np.float32)
    if fade_out:
        out[-fade_out:] *= np.linspace(1, 0, fade_out, dtype=np.float32) ** 2
    peak = float(np.abs(out).max()) or 1e-9
    peak_gain = 10 ** (ONE_SHOT_PEAK_DB / 20) / peak
    if bed:
        rms = float(np.sqrt(np.mean(out ** 2))) or 1e-9
        return out * min(10 ** (BED_RMS_DB / 20) / rms, peak_gain)
    return out * peak_gain


def _write_wav(path: Path, samples) -> None:
    import numpy as np

    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm)


def fetch_library(sfx_dir: Path = SFX_DIR, sources=SFX_LIBRARY, find=None, fetch=None) -> list[Path]:
    """Baixa e corta as gravações que faltam. Falha de rede não para nada: fica para depois."""
    from image_sources import download, wikimedia_media

    sfx_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    for cue, title, mode, seconds, count in sources:
        slug = hashlib.sha1(title.encode("utf-8")).hexdigest()[:6]
        if any(sfx_dir.glob(f"{cue}_lib_{slug}_*.wav")):
            continue
        raw = sfx_dir / f".{slug}.download"
        try:
            candidate, urls = (find or wikimedia_media)(f"File:{title}", False, kind="audio")
            if candidate.attribution_required:
                print(f"  [X] {cue:<11} {title}: a licença pede crédito; só entram domínio público e CC0")
                continue
            data, errors = None, []
            for url in urls[1:] + urls[:1]:  # versão convertida (menor) antes do original
                try:
                    data = (fetch or download)(url)
                    break
                except Exception as exc:  # noqa: BLE001 - tenta a próxima versão
                    errors.append(str(exc)[:80])
            if data is None:
                raise RuntimeError("download falhou: " + "; ".join(errors or ["sem URL"]))
            raw.write_bytes(data)
            samples = _decode(raw)
            segments = pick_segments(samples, mode, seconds, count)
            for k, (a, b) in enumerate(segments, start=1):
                out = sfx_dir / f"{cue}_lib_{slug}_{k}.wav"
                _write_wav(out, shape(samples[a:b], one_shot=mode == "peak", bed=cue in BEDS))
                created.append(out)
            print(f"  + {cue:<11} {title}: {len(segments)} trecho(s)")
        except Exception as exc:  # noqa: BLE001 - sem internet ou arquivo fora do ar
            print(f"  [X] {cue:<11} {title}: {str(exc)[:160]}")
        finally:
            raw.unlink(missing_ok=True)
    return created


def setup(sfx_dir: Path = SFX_DIR, music_dir: Path = MUSIC_DIR, force: bool = False,
          library: bool = True) -> list[Path]:
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("FFmpeg não encontrado. Instale e confira com: ffmpeg -version")
    created = []
    for stem, (duration, graph) in SFX_RECIPES.items():
        out = sfx_dir / f"{stem}.wav"
        if out.exists() and not force:
            continue
        _render(graph.format(sr=SAMPLE_RATE), out, duration, bed=stem.split("_synth")[0] in BEDS)
        created.append(out)
        print(f"  + {stem:<22} gerado ({duration}s)")
    if library:
        created += fetch_library(sfx_dir)

    synth_music = music_dir / f"tension_drone{SYNTH_SUFFIX}.wav"
    others = [t for t in music_tracks(music_dir) if t != synth_music]
    if others:
        print(f"  [OK] música: {len(others)} faixa(s) sua(s), drone sintético não é necessário")
    elif force or not synth_music.exists():
        _render(MUSIC_RECIPE.format(d=MUSIC_SECONDS + 1, sr=SAMPLE_RATE), synth_music, MUSIC_SECONDS,
                loudness=True)
        created.append(synth_music)
        print(f"  + música gerada: {synth_music.name} ({MUSIC_SECONDS}s em loop)")
    return created


def report(sfx_dir: Path = SFX_DIR, music_dir: Path = MUSIC_DIR) -> bool:
    ok = True
    print("Efeitos (efeito -> versões que o editor alterna):")
    for cue in expected_cues():
        files = sfx_variants(cue, sfx_dir)
        recorded = sum("_lib_" in f.name for f in files)
        synth = sum("_synth" in f.name for f in files)
        own = len(files) - recorded - synth
        detail = ", ".join(part for part in (f"{own} sua(s)" if own else "", f"{recorded} gravada(s)" if recorded
                                             else "", f"{synth} sintética(s)" if synth else "") if part)
        print(f"  {'[OK]' if files else '[X]'} {cue:<11} {detail or 'FALTA (rode com internet)'}")
        ok &= bool(files)
    tracks = music_tracks(music_dir)
    print("Música:", ", ".join(t.name for t in tracks) if tracks else "[X] nenhuma em assets/music/")
    return ok and bool(tracks)


def utf8_console() -> None:
    """No Windows, saída redirecionada (pipe, Claude Code, agendador) usa cp1252.
    Força UTF-8 e troca o que não der para escrever, em vez de travar. As mensagens
    também só usam caracteres que existem no cp1252 ([OK]/[X] em vez de símbolos)."""
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
