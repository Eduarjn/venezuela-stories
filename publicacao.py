"""Kit para publicar: a legenda pronta de cada rede e a capa do vídeo.

postar.txt: TikTok, Instagram Reels e YouTube Shorts, cada seção pronta para colar. Os
créditos obrigatórios das imagens vão sempre. Na legenda (2.200 caracteres) vai o crédito
compacto: autor e licença de cada foto, a fonte e o link de cada licença, o que a CC BY
aceita como "forma razoável para o meio". A lista completa (título e link de cada foto)
vai na descrição do YouTube, que tem 5.000 caracteres, e fica no post_copy.txt.
capa.jpg: a imagem da 1ª cena com o título, para a capa do Reels e a miniatura.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

CAPTION_MAX = 2200          # Instagram; o TikTok aceita 4.000 no app, mas só 2.200 pela API
INSTAGRAM_MAX_HASHTAGS = 5  # o Instagram passou a aceitar no máximo 5 por post/Reel (dez/2025)
TIKTOK_MAX_HASHTAGS = 5
YOUTUBE_TITLE_MAX = 100
YOUTUBE_DESCRIPTION_MAX = 5000
YOUTUBE_HASHTAGS = 3        # o YouTube mostra as 3 primeiras acima do título
CREDIT_NOTE = "Créditos (a licença exige): imagens de acervos livres, recortadas e animadas para o vídeo."
SOURCE_NAMES = {"wikimedia": "do Wikimedia Commons", "pexels": "da Pexels"}

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


def _author(name: str) -> str:
    # O Commons às vezes repete o autor ("Unknown author Unknown author").
    return re.sub(r"^(.+?) \1$", r"\1", name.strip()) or "autor desconhecido"


def full_credits(items: list) -> str:
    """Um crédito por linha: título, autor, licença com link e página de cada foto."""
    return "\n".join([CREDIT_NOTE, *(f"- {c.credit_line()}" for c in items)]) if items else ""


def compact_credits(items: list) -> str:
    """Crédito curto para a legenda: cada autor com a licença, a fonte e o link das licenças."""
    if not items:
        return ""
    authors = list(dict.fromkeys(f"{_author(c.author)} ({c.license})" for c in items))
    sources = list(dict.fromkeys(SOURCE_NAMES.get(c.source, c.source) for c in items))
    links = list(dict.fromkeys(c.license_link() for c in items if c.license_link()))
    text = (f"Créditos (a licença exige): imagens {' e '.join(sources)}, recortadas e animadas para o "
            f"vídeo. Autores: {'; '.join(authors)}.")
    return text + (f" Licenças: {' · '.join(links)}" if links else "")


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


def youtube_description(description: str, items: list, sources: str, tags: list[str]) -> str:
    """Descrição do Shorts: a lista completa de créditos quando cabe; senão a compacta. As
    fontes saem antes dos créditos se faltar espaço."""
    for credits, with_sources in ((full_credits(items), True), (full_credits(items), False),
                                  (compact_credits(items), True), (compact_credits(items), False)):
        text = "\n\n".join(p for p in (description, credits, sources if with_sources else "",
                                       " ".join(tags)) if p)
        if len(text) <= YOUTUBE_DESCRIPTION_MAX:
            return text
    return text


def build_ready_posts(episode: dict, credit_items: list) -> str:
    """Conteúdo do postar.txt: uma seção por rede, pronta para colar. credit_items: as
    imagens que a licença obriga a creditar (image_sources.Candidate)."""
    title = (episode.get("post_title") or "").strip()
    description = (episode.get("post_description") or "").strip()
    credits = compact_credits(credit_items)
    sources = ""
    if episode.get("track") == "fact" and episode.get("sources"):
        sources = "Fontes:\n" + "\n".join(f"- {s.get('title', '')}: {s['url']}" for s in episode["sources"])
    tiktok = caption(title, description, _hashtags(episode, TIKTOK_MAX_HASHTAGS), credits)
    instagram = caption(title, description, _hashtags(episode, INSTAGRAM_MAX_HASHTAGS), credits)
    too_long = [name for name, text in (("TikTok", tiktok), ("Instagram", instagram)) if len(text) > CAPTION_MAX]
    lines = [
        "=== ANTES DE POSTAR ===",
        "- Capa: capa.jpg (nesta pasta). No Instagram: Editar capa > Adicionar da galeria. Onde não der "
        "para enviar a imagem, escolha o quadro de capa no início do vídeo.",
        "- A narração é voz sintética: ative o rótulo de conteúdo gerado por IA se a rede pedir.",
        *([f"- ATENÇÃO: a legenda do {' e do '.join(too_long)} passou de {CAPTION_MAX} caracteres; "
           "corte a descrição (nunca os créditos)."] if too_long else []),
        "",
        "=== TIKTOK (cole tudo na legenda) ===",
        tiktok,
        "",
        f"=== INSTAGRAM REELS (cole tudo na legenda; o Instagram aceita até {INSTAGRAM_MAX_HASHTAGS} hashtags) ===",
        instagram,
        "",
        "=== YOUTUBE SHORTS ===",
        "Título:",
        short_title(title),
        "",
        "Descrição:",
        youtube_description(description, credit_items, sources, _hashtags(episode, YOUTUBE_HASHTAGS)),
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
