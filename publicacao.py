"""Kit para publicar: a legenda pronta de cada rede e a capa do vídeo.

postar.txt: TikTok, Instagram Reels e YouTube Shorts, cada seção pronta para colar. Os
créditos obrigatórios das imagens vão sempre; quando falta espaço, saem hashtags e depois
o fim da descrição, nunca os créditos.
capa.jpg: a imagem da 1ª cena com o título, para a capa do Reels e a miniatura.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

CAPTION_MAX = 2200          # Instagram; o TikTok aceita 4.000 no app, mas só 2.200 pela API
INSTAGRAM_MAX_HASHTAGS = 5  # o Instagram passou a aceitar no máximo 5 por post/Reel (dez/2025)
TIKTOK_MAX_HASHTAGS = 5
YOUTUBE_TITLE_MAX = 100
YOUTUBE_HASHTAGS = 3        # o YouTube mostra as 3 primeiras acima do título
CREDIT_NOTE = "Créditos (a licença exige): imagens de acervos livres, recortadas e animadas para o vídeo."

COVER_SIZE = (1080, 1920)
COVER_TEXT_WIDTH = 940
YELLOW = (255, 215, 0)      # o mesmo amarelo das legendas
FONT_CANDIDATES = (
    "C:/Windows/Fonts/ariblk.ttf",                         # Arial Black (Windows)
    "/System/Library/Fonts/Supplemental/Arial Black.ttf",  # macOS
    "/usr/share/fonts/opentype/inter/Inter-Black.otf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _hashtags(episode: dict, limit: int) -> list[str]:
    tags = [h if h.startswith("#") else f"#{h}" for h in episode.get("hashtags", []) if h]
    return list(dict.fromkeys(tags))[:limit]


def _credits(required: list[str]) -> str:
    return "\n".join([CREDIT_NOTE, *(f"- {c}" for c in required)]) if required else ""


def caption(title: str, description: str, tags: list[str], credits: str, limit: int = CAPTION_MAX) -> str:
    """Título, descrição, hashtags e créditos dentro do limite de caracteres da rede."""
    def join(desc: str, tags: list[str]) -> str:
        return "\n\n".join(p for p in (title, desc, " ".join(tags), credits) if p)

    text = join(description, tags)
    while len(text) > limit and tags:
        tags = tags[:-1]
        text = join(description, tags)
    if len(text) > limit:
        keep = max(0, len(description) - (len(text) - limit) - 1)
        text = join(description[:keep].rstrip() + "…", tags)
    return text


def short_title(title: str, limit: int = YOUTUBE_TITLE_MAX) -> str:
    if len(title) <= limit:
        return title
    cut = title[:limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:") + "…"


def build_ready_posts(episode: dict, required_credits: list[str]) -> str:
    """Conteúdo do postar.txt: uma seção por rede, pronta para colar."""
    title = (episode.get("post_title") or "").strip()
    description = (episode.get("post_description") or "").strip()
    credits = _credits(required_credits)
    sources = ""
    if episode.get("track") == "fact" and episode.get("sources"):
        sources = "Fontes:\n" + "\n".join(f"- {s.get('title', '')}: {s['url']}" for s in episode["sources"])
    youtube_description = "\n\n".join(p for p in (
        description, credits, sources, " ".join(_hashtags(episode, YOUTUBE_HASHTAGS))) if p)
    lines = [
        "=== ANTES DE POSTAR ===",
        "- Capa: capa.jpg (nesta pasta). No Instagram: Editar capa > Adicionar da galeria. Onde não der "
        "para enviar a imagem, escolha o quadro de capa no início do vídeo.",
        "- A narração é voz sintética: ative o rótulo de conteúdo gerado por IA se a rede pedir.",
        "",
        "=== TIKTOK (cole tudo na legenda) ===",
        caption(title, description, _hashtags(episode, TIKTOK_MAX_HASHTAGS), credits),
        "",
        f"=== INSTAGRAM REELS (cole tudo na legenda; o Instagram aceita até {INSTAGRAM_MAX_HASHTAGS} hashtags) ===",
        caption(title, description, _hashtags(episode, INSTAGRAM_MAX_HASHTAGS), credits),
        "",
        "=== YOUTUBE SHORTS ===",
        "Título:",
        short_title(title),
        "",
        "Descrição:",
        youtube_description,
        "",
        "=== DEPOIS DE PUBLICAR ===",
        f"python main.py --mark-published {episode.get('date', '')}",
    ]
    return "\n".join(lines) + "\n"


def cover_text(title: str) -> str:
    """Título sem emoji (a fonte da capa não desenha emoji) e sem espaços sobrando."""
    kept = "".join(ch for ch in title
                   if unicodedata.category(ch) not in ("So", "Sk") and ch not in "\ufe0f\u200d")
    return " ".join(kept.split())


def find_font(fonts_dir: Path | None = None) -> str | None:
    """Fonte da capa: a 1ª de assets/fonts (a mesma das legendas) ou uma do sistema."""
    if fonts_dir is not None and fonts_dir.exists():
        own = sorted(p for p in fonts_dir.iterdir() if p.suffix.lower() in (".ttf", ".otf"))
        if own:
            return str(own[0])
    return next((f for f in FONT_CANDIDATES if Path(f).exists()), None)


def _wrap(draw, words: list[str], font) -> list[str]:
    from video_editor import SUB_WEAK_ENDINGS

    lines: list[str] = []
    for word in words:
        if lines and draw.textlength(f"{lines[-1]} {word}", font=font) <= COVER_TEXT_WIDTH:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    # Como nas legendas: artigo ou preposição no fim da linha desce para a próxima
    # ("O LAGO ONDE O / CÉU" vira "O LAGO ONDE / O CÉU").
    for i in range(len(lines) - 1):
        head, _, last = lines[i].rpartition(" ")
        moved = f"{last} {lines[i + 1]}"
        if head and last.lower() in SUB_WEAK_ENDINGS and draw.textlength(moved, font=font) <= COVER_TEXT_WIDTH:
            lines[i], lines[i + 1] = head, moved
    return lines


def make_cover(image: Path, title: str, output: Path, font_file: str | None = None) -> Path:
    """Capa 1080x1920: a imagem da cena com o título em letras grandes no centro (área que
    aparece também no recorte 3:4 da grade do perfil); a última linha em amarelo."""
    from PIL import Image, ImageDraw, ImageFont

    base = Image.open(image).convert("RGB").resize(COVER_SIZE)
    width, height = COVER_SIZE
    shade = Image.new("L", COVER_SIZE, 0)  # faixa escura atrás do texto, que some nas bordas
    for y in range(height):
        distance = abs(y - height * 0.47) / (height * 0.22)
        shade.paste(int(125 * max(0.0, 1 - distance ** 2)), (0, y, width, y + 1))
    base = Image.composite(Image.new("RGB", COVER_SIZE, (0, 0, 0)), base, shade)

    words = cover_text(title).upper().split()
    draw = ImageDraw.Draw(base)
    for size in range(130, 50, -6):
        font = ImageFont.truetype(font_file, size) if font_file else ImageFont.load_default(size)
        lines = _wrap(draw, words, font)
        if len(lines) <= 4 and all(draw.textlength(line, font=font) <= COVER_TEXT_WIDTH for line in lines):
            break
    line_height = int(size * 1.18)
    y = int(height * 0.47 - line_height * len(lines) / 2)
    for i, line in enumerate(lines):
        x = (width - draw.textlength(line, font=font)) / 2
        draw.text((x, y), line, font=font, fill=YELLOW if i == len(lines) - 1 and len(lines) > 1 else "white",
                  stroke_width=max(4, size // 14), stroke_fill="black")
        y += line_height
    output.parent.mkdir(parents=True, exist_ok=True)
    base.save(output, "JPEG", quality=92)
    return output
