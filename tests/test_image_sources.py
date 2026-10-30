import json
import os
import sys
import tempfile
import unittest
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from image_sources import (  # noqa: E402
    PexelsSource,
    StockImageFinder,
    WikimediaSource,
    classify_license,
    scene_queries,
)
from visual_generator import VisualGenerator, VisualGeneratorError, fit_vertical  # noqa: E402

DAY = date(2026, 11, 1)


def png(size=(1600, 900), color=(90, 60, 30)):
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()


def commons_page(index, name, license_code, short, mime="image/jpeg", w=2000, h=1300, artist="<a>Ana</a>"):
    return {
        "index": index, "title": f"File:{name}.jpg",
        "imageinfo": [{
            "url": f"https://upload.wikimedia.org/{name}.jpg",
            "thumburl": f"https://upload.wikimedia.org/thumb/{name}.jpg",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}.jpg",
            "width": w, "height": h, "mime": mime,
            "extmetadata": {
                "License": {"value": license_code},
                "LicenseShortName": {"value": short},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0"},
                "Artist": {"value": artist},
            },
        }],
    }


def commons_response(*pages):
    return {"query": {"pages": list(pages)}}


class LicenseTest(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(classify_license("pd", "Public domain"), "pd")
        self.assertEqual(classify_license("cc0", "CC0"), "pd")
        self.assertEqual(classify_license("cc-by-4.0", "CC BY 4.0"), "by")
        self.assertEqual(classify_license("cc-by-sa-4.0", "CC BY-SA 4.0"), "by-sa")
        self.assertIsNone(classify_license("cc-by-nc-4.0", "CC BY-NC 4.0"))
        self.assertIsNone(classify_license("cc-by-nd-4.0", "CC BY-ND 4.0"))
        self.assertIsNone(classify_license("", ""))


class CreditLineTest(unittest.TestCase):
    def test_cc_by_credit_links_the_license(self):
        # CC BY 2.0, 3.0 e 4.0 exigem o link da licença, não só o nome.
        c = cand("Lake")
        self.assertIn("CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/)", c.credit_line())
        c.license, c.license_url = "CC BY 2.0", "https://creativecommons.org/licenses/by/2.0"
        self.assertIn("CC BY 2.0 (https://creativecommons.org/licenses/by/2.0)", c.credit_line())

    def test_public_domain_has_no_license_link_and_repeated_author_is_collapsed(self):
        c = cand("Map")
        c.license, c.attribution_required, c.author = "Public domain", False, "Unknown author Unknown author"
        self.assertEqual(c.credit_line(), 'Imagem: "Map" — Unknown author — Public domain — https://commons/Map')


class WikimediaTest(unittest.TestCase):
    def source(self, response, **kw):
        self.requests = []

        def get_json(url, params, headers):
            self.requests.append(params)
            return response
        return WikimediaSource(get_json=get_json, **kw)

    def test_keeps_free_licenses_in_search_order_and_skips_the_rest(self):
        src = self.source(commons_response(
            commons_page(3, "Bolivar_painting", "pd", "Public domain"),
            commons_page(1, "Maracaibo_photo", "cc-by-4.0", "CC BY 4.0"),
            commons_page(2, "Map", "pd", "Public domain", mime="image/svg+xml"),
            commons_page(4, "Sharealike", "cc-by-sa-3.0", "CC BY-SA 3.0"),
            commons_page(5, "NonCommercial", "cc-by-nc-2.0", "CC BY-NC 2.0"),
        ))
        found = src.search("Lake Maracaibo")
        self.assertEqual([c.title for c in found], ["Maracaibo_photo", "Bolivar_painting"])
        self.assertEqual(found[0].author, "Ana")
        self.assertTrue(found[0].attribution_required)
        self.assertFalse(found[1].attribution_required)
        self.assertEqual(found[0].download_url, "https://upload.wikimedia.org/thumb/Maracaibo_photo.jpg")
        self.assertEqual(self.requests[0]["gsrsearch"], "Lake Maracaibo")
        self.assertEqual(self.requests[0]["gsrnamespace"], "6")

    def test_share_alike_only_when_allowed(self):
        response = commons_response(commons_page(1, "SA", "cc-by-sa-4.0", "CC BY-SA 4.0"))
        self.assertEqual(self.source(response).search("x"), [])
        self.assertEqual(len(self.source(response, allow_share_alike=True).search("x")), 1)


class PexelsTest(unittest.TestCase):
    def test_parses_photos_and_sends_key(self):
        seen = {}

        def get_json(url, params, headers):
            seen.update(headers=headers, params=params)
            return {"photos": [{"url": "https://www.pexels.com/photo/1/", "photographer": "Luis",
                                "alt": "Lightning over lake", "width": 3000, "height": 4500,
                                "src": {"large2x": "https://images.pexels.com/1-large2x.jpg"}}]}
        found = PexelsSource("KEY", get_json=get_json).search("lightning lake")
        self.assertEqual(seen["headers"], {"Authorization": "KEY"})
        self.assertEqual(seen["params"]["orientation"], "portrait")
        self.assertEqual(found[0].credit_line(), "Foto: Luis / Pexels (https://www.pexels.com/photo/1/)")


class FakeSource:
    def __init__(self, name, results_by_query, fail=False):
        self.name = name
        self.results = results_by_query
        self.fail = fail
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        if self.fail:
            raise ConnectionError("fora do ar")
        return self.results.get(query, [])


def cand(page, w=2000, h=1300):
    from image_sources import Candidate

    return Candidate("wikimedia", f"https://img/{page}.jpg", f"https://commons/{page}", page, "Ana",
                     "CC BY 4.0", "", w, h, True)


class FinderTest(unittest.TestCase):
    def test_skips_used_small_and_failing_sources(self):
        broken = FakeSource("pexels", {}, fail=True)
        commons = FakeSource("wikimedia", {"q": [cand("used"), cand("tiny", 300, 200), cand("good")]})
        finder = StockImageFinder([broken, commons], fetch=lambda url: b"data")
        found, data = finder.find(["q"], used={"https://commons/used"})
        self.assertEqual(found.title, "good")
        self.assertEqual(data, b"data")

    def test_tries_next_query_when_nothing_found(self):
        commons = FakeSource("wikimedia", {"b": [cand("from_b")]})
        found, _ = StockImageFinder([commons], fetch=lambda url: b"x").find(["a", "b"], set())
        self.assertEqual(found.title, "from_b")
        self.assertEqual(commons.queries, ["a", "b"])

    def test_generic_query_tries_the_photo_bank_first(self):
        commons = FakeSource("wikimedia", {"lightning storm at night": [cand("Denver")],
                                           "Lake Maracaibo": [cand("Maracaibo")]})
        pexels = FakeSource("pexels", {"lightning storm at night": [cand("PexelsStorm")],
                                       "Lake Maracaibo": [cand("PexelsLake")]})
        pexels.generic = True
        finder = StockImageFinder([commons, pexels], fetch=lambda url: b"x")
        self.assertEqual(finder.find(["lightning storm at night"], set())[0].title, "PexelsStorm")
        self.assertEqual(finder.find(["Lake Maracaibo"], set())[0].title, "Maracaibo")

    def test_each_query_is_searched_once_and_failed_downloads_are_not_retried(self):
        commons = FakeSource("wikimedia", {"q": [cand("broken"), cand("ok1"), cand("ok2")]})
        fetched = []

        def fetch(url):
            fetched.append(url)
            if "broken" in url:
                raise ConnectionError("404")
            return b"x"
        finder = StockImageFinder([commons], fetch=fetch)
        first, _ = finder.find(["q"], used=set())
        second, _ = finder.find(["Q"], used={first.page_url})
        self.assertEqual([first.title, second.title], ["ok1", "ok2"])
        self.assertEqual(commons.queries, ["q"])
        self.assertEqual(fetched.count("https://img/broken.jpg"), 1)

    def test_scene_queries_most_specific_first(self):
        scene = {"image_query": "Catatumbo lightning",
                 "image_prompt": "Wide shot of Lake Maracaibo at night with the Catatumbo River delta, "
                                 "cinematic historical illustration, dramatic lighting"}
        queries = scene_queries(scene, {"entity": "Relâmpago do Catatumbo", "topic_title": "O lago"})
        self.assertEqual(queries[0], "Catatumbo lightning")
        self.assertIn("Lake Maracaibo", queries)
        self.assertIn("Catatumbo River", queries)
        self.assertEqual(queries[-2:], ["Relâmpago do Catatumbo", "O lago"])


class ComposeTest(unittest.TestCase):
    def test_landscape_photo_is_kept_whole_over_blurred_background(self):
        img = fit_vertical(png((1600, 900), (200, 30, 30)))
        self.assertEqual(img.size, (1080, 1920))
        # Centro da foto (40% da altura) tem a cor original; o fundo nas bordas é mais escuro.
        r, g, b = img.getpixel((540, int(1920 * 0.40)))
        self.assertGreater(r, 180)
        top_r, _, _ = img.getpixel((540, 40))
        self.assertLess(top_r, 150)

    def test_portrait_photo_is_cropped_full_bleed(self):
        img = fit_vertical(png((1200, 1800), (30, 200, 30)))
        self.assertEqual(img.getpixel((540, 40))[1], img.getpixel((540, 960))[1])


class StockEpisodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.out, self.episodes = root / "output", root / "episodes"
        folder = self.out / DAY.isoformat()
        folder.mkdir(parents=True)
        scenes = [{"scene_id": i, "narration_text": "Uma frase curta de teste para a cena.",
                   "image_query": q, "image_prompt": "x", "motion_type": "slow_ken_burns",
                   "sfx_cue": "whoosh"} for i, q in enumerate(["lake", "nothing", "bolivar"], start=1)]
        (folder / "current_episode.json").write_text(json.dumps(
            {"topic_id": 9, "entity": "", "topic_title": "", "scenes": scenes}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def generator(self, source):
        finder = StockImageFinder([source], fetch=lambda url: png())
        return VisualGenerator(output_dir=self.out, episodes_dir=self.episodes, stock_finder=finder,
                               render=lambda image, motion, duration, output: output.write_bytes(b"c"))

    def test_downloads_reuses_nearest_when_missing_and_writes_credits(self):
        source = FakeSource("wikimedia", {"lake": [cand("Lake")], "bolivar": [cand("Bolivar")]})
        manifest = self.generator(source).process_episode(DAY)
        visuals = manifest.parent
        self.assertEqual(sorted(p.name for p in visuals.glob("scene_*.png")),
                         ["scene_01.png", "scene_02.png", "scene_03.png"])
        credits = json.loads((visuals / "credits.json").read_text(encoding="utf-8"))
        self.assertEqual(credits["1"]["title"], "Lake")
        self.assertEqual(credits["2"], {"reused_from_scene": 1})
        self.assertEqual(json.loads(manifest.read_text())["image_provider"], "stock")

    def test_scene_without_results_takes_an_unused_image_from_a_neighbour_search(self):
        # Antes de repetir uma imagem, a cena 2 usa a busca da cena 1 (a mais próxima, mesmo tema).
        source = FakeSource("wikimedia", {"lake": [cand("Lake"), cand("Lake2")], "bolivar": [cand("Bolivar")]})
        visuals = self.generator(source).process_episode(DAY).parent
        credits = json.loads((visuals / "credits.json").read_text(encoding="utf-8"))
        self.assertEqual([credits[k]["title"] for k in ("1", "2", "3")], ["Lake", "Lake2", "Bolivar"])
        self.assertEqual(source.queries.count("lake"), 1)  # a 2ª passada reaproveita a busca

    def test_refresh_searches_again(self):
        self.generator(FakeSource("wikimedia", {"lake": [cand("Lake")]})).process_episode(DAY)
        again = FakeSource("wikimedia", {"nothing": [cand("A")], "bolivar": [cand("B")], "lake": [cand("C")]})
        visuals = self.generator(again).process_episode(DAY, refresh=True).parent
        credits = json.loads((visuals / "credits.json").read_text(encoding="utf-8"))
        self.assertEqual([credits[k]["title"] for k in ("1", "2", "3")], ["C", "A", "B"])

    def test_no_image_at_all_explains_options(self):
        with self.assertRaisesRegex(VisualGeneratorError, "PEXELS_API_KEY"):
            self.generator(FakeSource("wikimedia", {})).process_episode(DAY)

    def test_rerun_does_not_search_again(self):
        source = FakeSource("wikimedia", {"lake": [cand("Lake")], "nothing": [cand("N")],
                                          "bolivar": [cand("Bolivar")]})
        self.generator(source).process_episode(DAY)
        again = FakeSource("wikimedia", {})
        self.generator(again).process_episode(DAY)
        self.assertEqual(again.queries, [])

    def test_stock_is_the_default_provider(self):
        with mock.patch.dict(os.environ, {"IMAGE_PROVIDER": "", "PEXELS_API_KEY": ""}):
            gen = VisualGenerator(output_dir=self.out, episodes_dir=self.episodes)
        self.assertIsNotNone(gen.stock)
        self.assertEqual([s.name for s in gen.stock.sources], ["wikimedia"])


class PostCopyCreditsTest(unittest.TestCase):
    EPISODE = {"post_title": "T", "post_description": "D", "hashtags": ["#v"], "track": "fiction",
               "date": "2026-11-01", "topic_id": 9,
               "scenes": [{"scene_id": i, "narration_text": f"Narração da cena {i}."} for i in (1, 2, 3)]}

    def test_required_and_optional_credits_are_separated(self):
        import main

        pd = cand("Map")
        pd.license, pd.attribution_required = "Public domain", False
        with tempfile.TemporaryDirectory() as tmp:
            visuals = Path(tmp) / "9" / "visuals"
            visuals.mkdir(parents=True)
            (visuals / "credits.json").write_text(json.dumps({
                "1": cand("Lake").to_dict(), "2": pd.to_dict(), "3": {"reused_from_scene": 2}}))
            with mock.patch("main.episodes_dir", return_value=Path(tmp)):
                credits = main.load_image_credits(self.EPISODE)
        self.assertEqual(credits.required, ['Imagem: "Lake" — Ana — CC BY 4.0 '
                                            '(https://creativecommons.org/licenses/by/4.0/) — https://commons/Lake'])
        self.assertEqual(len(credits.optional), 1)
        self.assertEqual(credits.reused_scenes, 1)

        text = main.build_post_copy(self.EPISODE, credits)
        required = text.index("CRÉDITOS DAS IMAGENS (obrigatório")
        self.assertLess(required, text.index("recortadas e animadas"))  # a licença exige indicar a mudança
        self.assertLess(text.index("recortadas e animadas"), text.index("Ana — CC BY 4.0"))
        self.assertLess(text.index("Ana — CC BY 4.0"), text.index("Créditos opcionais"))
        self.assertIn("ATENÇÃO: 1 de 3 cenas repetem", text)
        self.assertIn("--date 2026-11-01 --redo-images", text)
        self.assertIn("Cena 1: Narração da cena 1. -> Lake (wikimedia)", text)
        self.assertIn("Cena 3: Narração da cena 3. -> (repete a imagem da cena 2)", text)

    def test_no_credits_section_without_real_images(self):
        import main

        text = main.build_post_copy(self.EPISODE)
        self.assertNotIn("CRÉDITOS", text)
        self.assertNotIn("ATENÇÃO", text)
        self.assertIn("geradas por IA", text)


if __name__ == "__main__":
    unittest.main()
