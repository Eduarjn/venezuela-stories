"""Diretor de efeitos: escolhe, cena a cena, o movimento da imagem, a transição e o efeito
sonoro pelo que a narração conta, sem repetir.

Antes o roteiro escolhia o som entre 5 sons sintéticos e a transição vinha do som: clarão
branco (como relâmpago) em 60 a 70% dos cortes e trovão até no vídeo do avião. Agora:
- som: só quando a cena fala daquilo (trovão só com raio ou tempestade, buzina com navio,
  hélice com avião, carros com ponte...), nunca o mesmo em cenas seguidas, poucas vezes
  cada um, e várias cenas ficam só com a voz e a música. Cada repetição usa outra gravação.
- transição: corte seco é o padrão; mergulho no preto em salto no tempo ("Em 1937...") e
  em notícia de morte; clarão só em raio, relâmpago ou foto; soco de zoom em batida, queda
  ou explosão; chicote (whip) quando algo se desloca.
- movimento: o do roteiro, trocado quando repete o da cena anterior ou aparece demais;
  cachoeira e queda descem a câmera (tilt), batida treme a imagem.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

# Efeito curto, no início da cena.
ONE_SHOTS = ("boom", "impact", "thunder", "explosion", "ship_horn", "camera", "writing", "clock",
             "whoosh", "riser", "power_down")
# Ambiente: toca baixo durante a cena inteira.
BEDS = ("rain", "river", "waves", "traffic", "crowd", "plane", "helicopter")
CUES = ONE_SHOTS + BEDS

MOTIONS = ("zoom_in_fast", "pan_left_to_right", "shake_impact", "slow_ken_burns", "pulse_glow",
           "tilt_down", "pan_right_to_left", "zoom_out_slow")
# Para trocar um movimento repetido: só os tranquilos (tremor e pulso vêm do conteúdo).
CALM_MOTIONS = ("slow_ken_burns", "pan_left_to_right", "zoom_out_slow", "pan_right_to_left",
                "zoom_in_fast", "tilt_down")

# Conteúdo -> efeito sonoro, em ordem de prioridade (texto sem acento, minúsculo). Quando o
# 1º não pode (repetiria a cena anterior ou já apareceu demais), vale o próximo que casar.
SFX_RULES = (
    ("thunder", r"(?<!nenhum )(?<!sem )\b(raios?|relampag\w*|relampej\w*|trov\w+|tempestades?|tormentas?|"
                r"claroes?)\b|\bceu (explode|se acende|acende)"),
    ("rain", r"\b(chuvas?|chove|chovia|chovendo|garoa)\b"),
    ("explosion", r"\b(explos\w*|canh\w+|bombard\w*|batalhas?|guerras?|tiros?)\b"),
    ("impact", r"\b(bat(eu|e|eram)|colid\w*|colisao|choque|despencou|desab\w*|caiu|cairam|tomb\w*|"
               r"afund\w*|derrub\w*|desmoron\w*)\b"),
    ("power_down", r"\b(pane|sem energia|apag(ou|aram|ao)|blecaute|desligou)\b"),
    ("helicopter", r"\bhelicopteros?\b"),
    ("plane", r"\b(aviao|avioes|piloto|sobrevo\w*|decol\w*|pous\w*|aterriss\w*|voo|voou|voar|aeroporto)\b"),
    ("ship_horn", r"\b(navios?|petroleiros?|barcos?|frotas?|porto|cargueiros?)\b"),
    ("traffic", r"\b(pontes?|carros?|estradas?|transito|avenidas?)\b"),
    ("river", r"\b(cachoeiras?|cataratas?|correntezas?|rios?|despencando|queda)\b"),
    ("waves", r"\b(mar|oceanos?|praias?|ondas?|lagos?|lagoas?)\b"),
    ("crowd", r"\b(multidao|torcida|plateia|gente|moradores)\b"),
    ("writing", r"\b(anot\w*|diario|escrev\w*|cartas?|registr\w*|documentos?)\b"),
    ("camera", r"\b(fotos?|fotograf\w*|cameras?)\b"),
)
MONTHS = "janeiro|fevereiro|marco|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro"
# Salto no tempo no começo da fala: "Em 1937", "9 de outubro de 1937", "Novembro de 1933",
# "Só em 1970", "Mas em 2010", "Anos depois", "Hoje".
TIME_JUMP = re.compile(rf"^(mas |e |entao )?(so |ate )?(em )?((\d{{1,2}} de )?({MONTHS}) de \d{{4}}|\d{{4}}\b|"
                       r"(anos|decadas|seculos|meses|dias) depois|hoje\b)")
DEATH = re.compile(r"\b(morreram|morreu|mortos?|mortes?|vitimas?|faleceu|faleceram)\b")
MOVEMENT = re.compile(r"\b(sobrevo\w*|atravess\w*|cruza\w*|entram|saem|viaj\w*|desce\w*|desceu|"
                      r"voltou|rumo|partiu|chegou)\b")
FALLING_WATER = re.compile(r"\b(cachoeiras?|cataratas?|queda|despencando)\b")
CALL_TO_ACTION = re.compile(r"^(siga|inscreva|curta|comente)\b")


@dataclass
class SceneEffects:
    motion: str
    transition: str | None
    sfx: str | None
    sfx_variant: int = 0  # qual gravação daquele efeito (cada repetição usa outra)


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def content_sfx(narration: str) -> list[str]:
    """Efeitos que a fala pede, pelas palavras dela, do mais forte para o mais fraco."""
    text = _plain(narration)
    return [cue for cue, pattern in SFX_RULES if re.search(pattern, text)]


def direct(scenes: list[dict]) -> list[SceneEffects]:
    """Efeitos de cada cena, na ordem das cenas."""
    n = len(scenes)
    texts = [_plain(s.get("narration_text", "")).strip() for s in scenes]
    stormy = sum(bool(re.search(SFX_RULES[0][1], t)) for t in texts) >= max(2, n // 4)
    sfx_cap = max(2, math.ceil(n * 0.2))
    caps = {"thunder": max(sfx_cap, math.ceil(n * 0.3)) if stormy else 2,
            **{bed: 2 for bed in BEDS}}  # ambiente repetido cansa mais que um efeito curto
    flash_cap = max(2, math.ceil(n * 0.25))
    motion_cap = max(3, math.ceil(n * 0.3))
    sounds, transitions, motions = Counter(), Counter(), Counter()
    effects: list[SceneEffects] = []
    for i, (scene, text) in enumerate(zip(scenes, texts)):
        prev = effects[-1] if effects else None
        time_jump = bool(TIME_JUMP.match(text))
        death = bool(DEATH.search(text))

        # Som
        sfx = None
        if not death and not CALL_TO_ACTION.match(text):  # morte e "siga": só voz e música
            wanted = content_sfx(text) + (["clock"] if time_jump else []) + (["riser"] if text.endswith("?") else [])
            if i == 0:
                wanted = [c for c in wanted if c not in BEDS] + ["boom"]  # o gancho entra com peso
            allowed = [c for c in wanted
                       if not (prev and prev.sfx == c) and sounds[c] < caps.get(c, sfx_cap)]
            sfx = allowed[0] if allowed else None

        # Transição
        if i == 0:
            transition = None
        elif death or time_jump:
            transition = "dip"
        elif sfx in ("thunder", "camera"):
            transition = "flash"
        elif sfx in ("impact", "explosion"):
            transition = "punch"
        elif MOVEMENT.search(text):
            transition = "whip"
        else:
            transition = None
        if transition and prev and prev.transition == transition:
            transition = None
        if transition == "flash" and transitions["flash"] >= flash_cap:
            transition = None

        # Movimento
        motion = scene.get("motion_type") if scene.get("motion_type") in MOTIONS else "slow_ken_burns"
        if FALLING_WATER.search(text):
            motion = "tilt_down"
        elif sfx in ("impact", "explosion"):
            motion = "shake_impact"
        if (prev and motion == prev.motion) or motions[motion] >= motion_cap:
            options = [m for m in CALM_MOTIONS
                       if m != (prev.motion if prev else None) and motions[m] < motion_cap]
            motion = options[i % len(options)] if options else "slow_ken_burns"

        effects.append(SceneEffects(motion, transition, sfx, sounds[sfx] if sfx else 0))
        if sfx:
            sounds[sfx] += 1
        if transition:
            transitions[transition] += 1
        motions[motion] += 1
    return effects
