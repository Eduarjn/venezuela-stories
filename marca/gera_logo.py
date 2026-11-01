"""Gera a marca "Fato Raro" em SVG: o último O é um eclipse com o "anel de diamante".

Uso: python gera_logo.py <pasta_das_fontes> <pasta_de_saida>
Precisa de: pip install fonttools uharfbuzz, e Outfit-Black.ttf e Outfit-Medium.ttf
(Google Fonts, licença SIL OFL 1.1) na pasta das fontes. O texto vira contorno, então os
SVG gerados não dependem de fonte instalada. Para PNG, abra o SVG no navegador ou rasterize.
"""
import math
import sys
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.recordingPen import RecordingPen
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

FONTS, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
NAME = "FATO RARO"
TAGLINE = "HISTÓRIAS REAIS QUE PARECEM MENTIRA"

NAVY, NAVY_2 = "#0A0F1E", "#1A2445"
INK, INK_ON_LIGHT = "#F4EEE3", "#0A0F1E"
GOLD = ("#FFEDB3", "#F6BC34", "#D27A12")
PAPER = "#F5EFE3"
AMBER_ON_LIGHT = "#A85F06"
BEAD_ANGLE = -42  # graus; o "diamante" fica no alto à direita
OFFSET = 0.34     # quanto o anel engrossa do lado do diamante (fração da espessura)


class Font:
    def __init__(self, path: Path):
        self.tt = TTFont(path)
        self.gs = self.tt.getGlyphSet()
        self.order = self.tt.getGlyphOrder()
        self.hb = hb.Font(hb.Face(hb.Blob.from_file_path(str(path))))
        self.upem = self.tt["head"].unitsPerEm

    def shape(self, text, size, tracking_em=0.0):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb, buf, {"kern": True})
        s, x, out = size / self.upem, 0.0, []
        for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
            out.append((self.order[info.codepoint], x + pos.x_offset * s))
            x += pos.x_advance * s + tracking_em * size
        return out, x - tracking_em * size

    def path(self, glyph, x, baseline, size):
        s = size / self.upem
        pen = SVGPathPen(self.gs)
        self.gs[glyph].draw(TransformPen(pen, (s, 0, 0, -s, x, baseline)))
        return pen.getCommands()

    def contour_bounds(self, glyph):
        rec = RecordingPen()
        self.gs[glyph].draw(rec)
        contours, current = [], []
        for op, args in rec.value:
            current.append((op, args))
            if op in ("closePath", "endPath"):
                contours.append(current)
                current = []
        bounds = []
        for c in contours:
            bp = BoundsPen(self.gs)
            for op, args in c:
                getattr(bp, op)(*args)
            bounds.append(bp.bounds)
        return sorted(bounds, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)


def circle_d(cx, cy, r):
    return (f"M{cx - r:.2f},{cy:.2f}a{r:.2f},{r:.2f} 0 1,0 {2 * r:.2f},0"
            f"a{r:.2f},{r:.2f} 0 1,0 {-2 * r:.2f},0Z")


def eclipse(cx, cy, R, t, uid, *, disc=NAVY, glow=False, bead_glow=True, bead=0.5, halo=3.2):
    """Disco escuro + anel dourado mais grosso de um lado + o ponto de luz ("diamante")."""
    ang = math.radians(BEAD_ANGLE)
    ux, uy = math.cos(ang), math.sin(ang)
    off = t * OFFSET
    r_in = R - t
    icx, icy = cx - ux * off, cy - uy * off
    on_rim = R - (t + off) * 0.3  # perto da borda de fora: o "diamante" vaza para o céu
    bx, by = cx + ux * on_rim, cy + uy * on_rim
    bead_r = (t + off) * bead
    gx1, gy1, gx2, gy2 = cx + ux * R, cy + uy * R, cx - ux * R, cy - uy * R
    defs = (f'<linearGradient id="{uid}g" gradientUnits="userSpaceOnUse" x1="{gx1:.1f}" y1="{gy1:.1f}" '
            f'x2="{gx2:.1f}" y2="{gy2:.1f}"><stop offset="0" stop-color="{GOLD[0]}"/>'
            f'<stop offset="0.45" stop-color="{GOLD[1]}"/><stop offset="1" stop-color="{GOLD[2]}"/></linearGradient>'
            f'<radialGradient id="{uid}b"><stop offset="0" stop-color="#FFFFFF"/>'
            f'<stop offset="0.28" stop-color="#FFFFFF"/><stop offset="0.45" stop-color="#FFF4D6" stop-opacity="0.75"/>'
            f'<stop offset="1" stop-color="#FFE3A0" stop-opacity="0"/></radialGradient>')
    parts = []
    if glow:  # coroa: brilho difuso atrás do anel
        defs += (f'<filter id="{uid}f" x="-50%" y="-50%" width="200%" height="200%">'
                 f'<feGaussianBlur stdDeviation="{t * 0.55:.1f}"/></filter>')
        parts.append(f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{R - t * 0.35:.2f}" fill="none" '
                     f'stroke="{GOLD[1]}" stroke-opacity="0.55" stroke-width="{t * 1.2:.1f}" filter="url(#{uid}f)"/>')
    parts.append(f'<circle cx="{icx:.2f}" cy="{icy:.2f}" r="{r_in:.2f}" fill="{disc}"/>')
    parts.append(f'<path fill="url(#{uid}g)" fill-rule="evenodd" d="{circle_d(cx, cy, R)}{circle_d(icx, icy, r_in)}"/>')
    if bead_glow:
        parts.append(f'<circle cx="{bx:.2f}" cy="{by:.2f}" r="{bead_r * halo:.2f}" fill="url(#{uid}b)"/>')
    if bead:  # no fundo claro o ponto de luz vira "brilho de olho": fica só o anel
        parts.append(f'<circle cx="{bx:.2f}" cy="{by:.2f}" r="{bead_r:.2f}" fill="#FFFFFF"/>')
    return defs, "".join(parts)


def wordmark(font, size, x0, baseline, ink, uid, *, disc=NAVY, tracking=0.015, bead_glow=True):
    """FATO RARO com o último O trocado pelo eclipse. Devolve (defs, svg, largura)."""
    glyphs, width = font.shape(NAME, size, tracking)
    s = size / font.upem
    paths = [font.path(g, x0 + x, baseline, size) for g, x in glyphs[:-1]]
    o_name, o_x = glyphs[-1]
    outer, inner = font.contour_bounds(o_name)[:2]
    cx = x0 + o_x + (outer[0] + outer[2]) / 2 * s
    cy = baseline - (outer[1] + outer[3]) / 2 * s
    R = ((outer[2] - outer[0]) + (outer[3] - outer[1])) / 4 * s
    t = ((outer[2] - outer[0]) - (inner[2] - inner[0])) / 2 * s
    defs, ecl = eclipse(cx, cy, R, t * 0.92, uid, disc=disc, bead=0.4 if bead_glow else 0, halo=2.3,
                        bead_glow=bead_glow)
    svg = f'<path fill="{ink}" d="{"".join(paths)}"/>' + ecl
    return defs, svg, width


def text_paths(font, text, size, x0, baseline, color, tracking):
    glyphs, width = font.shape(text, size, tracking)
    d = "".join(font.path(g, x0 + x, baseline, size) for g, x in glyphs)
    return f'<path fill="{color}" d="{d}"/>', width


def svg_doc(w, h, defs, body, bg=None):
    bg_rect = f'<rect width="{w}" height="{h}" fill="{bg}"/>' if bg else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" width="{w:.0f}" height="{h:.0f}">'
            f'<defs>{defs}</defs>{bg_rect}{body}</svg>')


black = Font(FONTS / "Outfit-Black.ttf")
medium = Font(FONTS / "Outfit-Medium.ttf")
cap = black.tt["OS/2"].sCapHeight / black.upem

# 1) Avatar (perfil do TikTok, Instagram e YouTube): só o eclipse, recorte redondo seguro.
W = 1000
d, e = eclipse(500, 500, 292, 78, "av", glow=True)
bg = (f'<radialGradient id="avbg" cx="0.5" cy="0.46" r="0.62"><stop offset="0" stop-color="{NAVY_2}"/>'
      f'<stop offset="1" stop-color="{NAVY}"/></radialGradient>')
(OUT / "avatar.svg").write_text(svg_doc(W, W, d + bg, f'<rect width="{W}" height="{W}" fill="url(#avbg)"/>' + e),
                                encoding="utf-8")


def lockup(name, *, ink, bg, tag_color, disc, with_tagline=True, size=200, pad=90, shadow=False, bead_glow=True):
    tracking = 0.015
    _, width = black.shape(NAME, size, tracking)
    cap_px = cap * size
    top = pad
    baseline = top + cap_px
    d, body, _ = wordmark(black, size, pad, baseline, ink, name, disc=disc, tracking=tracking, bead_glow=bead_glow)
    h = baseline + pad
    if with_tagline:
        # tagline justificada na largura da marca
        tag_size = 10.0
        for _ in range(30):
            _, tw = medium.shape(TAGLINE, tag_size, 0.24)
            tag_size *= width / tw
        tag_base = baseline + cap_px * 0.62 + cap * tag_size
        tag, _ = text_paths(medium, TAGLINE, tag_size, pad, tag_base, tag_color, 0.24)
        body += tag
        h = tag_base + pad
    w = width + 2 * pad
    if shadow:
        d += ('<filter id="sh" x="-10%" y="-30%" width="120%" height="160%"><feDropShadow dx="0" dy="3" '
              'stdDeviation="6" flood-color="#000" flood-opacity="0.55"/></filter>')
        body = f'<g filter="url(#sh)">{body}</g>'
    (OUT / f"{name}.svg").write_text(svg_doc(w, h, d, body, bg), encoding="utf-8")
    return w, h


print("horizontal_escuro", lockup("horizontal_escuro", ink=INK, bg=NAVY, tag_color=GOLD[1], disc=NAVY))
print("horizontal_claro", lockup("horizontal_claro", ink=INK_ON_LIGHT, bg=PAPER, tag_color=AMBER_ON_LIGHT, disc=NAVY,
                                      bead_glow=False))
print("marca_dagua", lockup("marca_dagua", ink="#FFFFFF", bg=None, tag_color=GOLD[1], disc=NAVY,
                            with_tagline=False, pad=24, shadow=True))

# 2) Empilhado: FATO / RARO, para capa ou fim de vídeo.
size, pad = 260, 110
cap_px = cap * size
line_gap = cap_px * 0.34
_, w1 = black.shape("FATO", size, 0.015)
glyphs, w2 = black.shape("RARO", size, 0.015)
w = max(w1, w2) + 2 * pad
b1 = pad + cap_px
b2 = b1 + line_gap + cap_px
p1, _ = text_paths(black, "FATO", size, pad, b1, INK, 0.015)
s = size / black.upem
p2 = "".join(black.path(g, pad + x, b2, size) for g, x in glyphs[:-1])
o_name, o_x = glyphs[-1]
outer, inner = black.contour_bounds(o_name)[:2]
cx = pad + o_x + (outer[0] + outer[2]) / 2 * s
cy = b2 - (outer[1] + outer[3]) / 2 * s
R = ((outer[2] - outer[0]) + (outer[3] - outer[1])) / 4 * s
t = ((outer[2] - outer[0]) - (inner[2] - inner[0])) / 2 * s
d, ecl = eclipse(cx, cy, R, t * 0.92, "st", bead=0.4, halo=2.3)
h = b2 + pad
(OUT / "empilhado_escuro.svg").write_text(
    svg_doc(w, h, d, p1 + f'<path fill="{INK}" d="{p2}"/>' + ecl, NAVY), encoding="utf-8")
print("empilhado_escuro", (w, h))

# 3) Banner do YouTube (2560x1440; área segura central 1546x423).
BW, BH = 2560, 1440
size = 210
_, width = black.shape(NAME, size, 0.015)
x0 = (BW - width) / 2
baseline = BH / 2 + cap * size / 2 - 40
d, body, _ = wordmark(black, size, x0, baseline, INK, "bn")
tag_size = 44
_, tw = medium.shape(TAGLINE, tag_size, 0.24)
tag, _ = text_paths(medium, TAGLINE, tag_size, (BW - tw) / 2, baseline + 110, GOLD[1], 0.24)
bgb = (f'<radialGradient id="bnbg" cx="0.5" cy="0.5" r="0.7"><stop offset="0" stop-color="{NAVY_2}"/>'
       f'<stop offset="1" stop-color="{NAVY}"/></radialGradient>')
(OUT / "banner_youtube.svg").write_text(
    svg_doc(BW, BH, d + bgb, f'<rect width="{BW}" height="{BH}" fill="url(#bnbg)"/>' + body + tag), encoding="utf-8")
print("ok")
