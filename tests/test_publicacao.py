import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from image_sources import Candidate  # noqa: E402
from publicacao import (  # noqa: E402
    CAPTION_MAX,
    build_ready_posts,
    caption,
    compact_credits,
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
def photo(title, author, license, link):
    return Candidate("wikimedia", "d", f"https://commons/{title}", title, author, license, link, 1000, 800, True)


CREDITS = [photo("Catatumbo", "Fernando Flores", "CC BY 3.0", "https://cc/by/3.0"),
           photo("Ponte", "Ana Souza", "CC BY 4.0", "https://cc/by/4.0")]


def section(text: str, name: str) -> str:
    return text.split(f"=== {name}", 1)[1].split("\n=== ", 1)[0]


class ReadyPostsTest(unittest.TestCase):
    def test_each_network_gets_title_credits_and_its_hashtag_limit(self):
        text = build_ready_posts(EPISODE, CREDITS)
        tiktok, instagram = section(text, "TIKTOK"), section(text, "INSTAGRAM")
        youtube = section(text, "YOUTUBE")
        for part in (tiktok, instagram):  # crédito compacto: autor, licença e link da licença
            self.assertIn("Autores: Fernando Flores (CC BY 3.0); Ana Souza (CC BY 4.0).", part)
            self.assertIn("https://cc/by/3.0 · https://cc/by/4.0", part)
            self.assertIn("Wikimedia Commons", part)
        self.assertIn('Imagem: "Catatumbo" — Fernando Flores — CC BY 3.0', youtube)  # lista completa
        self.assertIn("O lago onde o céu explode", tiktok)
        self.assertEqual(instagram.count("#"), 5)  # o Instagram aceita no máximo 5 hashtags
        self.assertIn("#raios", instagram)          # hashtag sem # no roteiro ganha o #
        self.assertIn("Fontes:\n- NASA: https://nasa.gov/x", youtube)
        self.assertEqual(youtube.split("Descrição:", 1)[1].count("#"), 3)
        self.assertIn("--mark-published 2026-11-01", text)

    def test_long_text_drops_hashtags_then_description_but_never_credits(self):
        credits = compact_credits(CREDITS)
        text = caption("Título", "x" * 3000, ["#a", "#b"], credits)
        self.assertLessEqual(len(text), CAPTION_MAX)
        self.assertTrue(text.endswith(credits))
        self.assertNotIn("#a", text)
        self.assertIn("x…", text)

    def test_twenty_cc_by_photos_still_fit_the_caption_with_the_whole_description(self):
        # Caso real (30/10): 20 fotos CC BY de 11 autores; a lista completa passa de 3.900 caracteres.
        items = [photo(f"Foto {i}", f"Autor {i % 11}", "CC BY 3.0", "https://creativecommons.org/licenses/by/3.0")
                 for i in range(20)]
        text = build_ready_posts(EPISODE, items)
        instagram = section(text, "INSTAGRAM")
        self.assertLessEqual(len(instagram.strip().split("\n", 1)[1]), CAPTION_MAX)
        self.assertIn(EPISODE["post_description"], instagram)
        self.assertEqual(instagram.count("Autor 3 (CC BY 3.0)"), 1)
        self.assertNotIn("ATENÇÃO", text)
        self.assertIn('Imagem: "Foto 19"', section(text, "YOUTUBE"))  # cabe nos 5.000 do YouTube

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
