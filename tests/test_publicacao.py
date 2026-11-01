import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from publicacao import (  # noqa: E402
    CAPTION_MAX,
    build_ready_posts,
    caption,
    cover_text,
    find_font,
    make_cover,
    short_title,
)

EPISODE = {
    "date": "2026-11-01", "track": "fact",
    "post_title": "O lago onde o céu explode quase toda noite ⚡",
    "post_description": "No Lago de Maracaibo cai mais raio do que em qualquer lugar da Terra.",
    "hashtags": ["#venezuela", "#catatumbo", "raios", "#curiosidades", "#historia", "#lago", "#ciencia", "#natureza"],
    "sources": [{"title": "NASA", "url": "https://nasa.gov/x"}],
}
CREDITS = ['Imagem: "Catatumbo" — Fernando Flores — CC BY 3.0 (https://cc/by/3.0) — https://commons/x',
           'Imagem: "Ponte" — Autor — CC BY 4.0 (https://cc/by/4.0) — https://commons/y']


def section(text: str, name: str) -> str:
    return text.split(f"=== {name}", 1)[1].split("\n=== ", 1)[0]


class ReadyPostsTest(unittest.TestCase):
    def test_each_network_gets_title_credits_and_its_hashtag_limit(self):
        text = build_ready_posts(EPISODE, CREDITS)
        tiktok, instagram = section(text, "TIKTOK"), section(text, "INSTAGRAM")
        youtube = section(text, "YOUTUBE")
        for part in (tiktok, instagram, youtube):
            self.assertIn("Fernando Flores", part)
            self.assertIn("CC BY 4.0", part)
        self.assertIn("O lago onde o céu explode", tiktok)
        self.assertEqual(instagram.count("#"), 5)  # o Instagram aceita no máximo 5 hashtags
        self.assertIn("#raios", instagram)          # hashtag sem # no roteiro ganha o #
        self.assertIn("Fontes:\n- NASA: https://nasa.gov/x", youtube)
        self.assertEqual(youtube.split("Descrição:", 1)[1].count("#"), 3)
        self.assertIn("--mark-published 2026-11-01", text)

    def test_long_text_drops_hashtags_then_description_but_never_credits(self):
        text = caption("Título", "x" * 3000, ["#a", "#b"], "\n".join(CREDITS))
        self.assertLessEqual(len(text), CAPTION_MAX)
        self.assertTrue(text.endswith(CREDITS[1]))
        self.assertNotIn("#a", text)
        self.assertIn("x…", text)

    def test_youtube_title_is_cut_at_a_word(self):
        title = short_title("palavra " * 20)
        self.assertLessEqual(len(title), 100)
        self.assertTrue(title.endswith("palavra…"))


class CoverTest(unittest.TestCase):
    def test_cover_is_vertical_jpeg_without_emoji(self):
        self.assertEqual(cover_text("O lago onde o céu explode ⚡️ US$ 5"), "O lago onde o céu explode US$ 5")
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "scene_01.png"
            Image.new("RGB", (1080, 1920), (20, 40, 90)).save(image)
            out = make_cover(image, EPISODE["post_title"], Path(tmp) / "capa.jpg", find_font())
            with Image.open(out) as cover:
                self.assertEqual((cover.format, cover.size), ("JPEG", (1080, 1920)))
                # Texto claro no centro (título) e o fundo original nas bordas.
                center = cover.crop((100, 800, 980, 1000)).convert("L")
                self.assertGreater(center.getextrema()[1], 200)
                for got, want in zip(cover.getpixel((540, 20)), (20, 40, 90)):  # JPEG: quase igual
                    self.assertLessEqual(abs(got - want), 4)


if __name__ == "__main__":
    unittest.main()
