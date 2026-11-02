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
    wikimedia_video,
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


class CommonsThumbFallbackTest(unittest.TestCase):
    def test_upload_urls_map_to_commons_thumb_php(self):
        from image_sources import commons_thumb_php

        thumb = "https://upload.wikimedia.org/wikipedia/commons/thumb/b/bd/Catrina.jpg/960px-Catrina.jpg"
        self.assertEqual(commons_thumb_php(thumb), "https://commons.wikimedia.org/w/thumb.php?f=Catrina.jpg&w=960")
        original = "https://upload.wikimedia.org/wikipedia/commons/a/a1/Altar.JPG?utm_source=x"
        self.assertEqual(commons_thumb_php(original), "https://commons.wikimedia.org/w/thumb.php?f=Altar.JPG&w=960")
        self.assertIsNone(commons_thumb_php("https://upload.wikimedia.org/wikipedia/commons/b/bd/Storm.ogg"))
        self.assertIsNone(commons_thumb_php("https://images.pexels.com/photos/1/x.jpg"))


class WikimediaVideoTest(unittest.TestCase):
    TITLE = 'File:Catatumbo "raios" (CIRA).webm'

    def api(self, license_code="pd", short="Public domain", mime="video/webm"):
        def get_json(url, params, headers):
            self.assertEqual(params["titles"], self.TITLE)
            self.assertIn("derivatives", params["viprop"])
            return {"query": {"pages": [{"title": self.TITLE, "videoinfo": [{
                "url": "https://upload.wikimedia.org/c/Cat.webm?utm_source=commons",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Cat.webm",
                "width": 1440, "height": 1080, "mime": mime,
                "extmetadata": {"License": {"value": license_code}, "LicenseShortName": {"value": short},
                                "Artist": {"value": "<a href='x'>GOES: CIRA &amp; NOAA</a>"}},
                "derivatives": [{"src": "https://u/t/Cat.webm.480p.vp9.webm", "height": 480},
                                {"src": "https://u/t/Cat.webm.2160p.vp9.webm", "height": 2160},
                                {"src": "https://u/t/Cat.webm.1080p.vp9.webm", "height": 1080}],
            }]}]}}
        return get_json

    def test_page_link_gives_credit_and_original_first_then_converted_versions(self):
        link = "https://commons.wikimedia.org/wiki/File:Catatumbo_%22raios%22_(CIRA).webm?uselang=pt"
        candidate, urls = wikimedia_video(link, get_json=self.api())
        self.assertEqual(urls, ["https://upload.wikimedia.org/c/Cat.webm",  # sem o ?utm_source
                                "https://u/t/Cat.webm.1080p.vp9.webm", "https://u/t/Cat.webm.480p.vp9.webm"])
        self.assertEqual(candidate.kind, "Vídeo")
        self.assertFalse(candidate.attribution_required)
        self.assertEqual(candidate.credit_line(), 'Vídeo: "Catatumbo "raios" (CIRA)" — GOES: CIRA & NOAA — '
                                                  "Public domain — https://commons.wikimedia.org/wiki/File:Cat.webm")

    def test_same_license_rules_as_images(self):
        with self.assertRaises(ValueError):  # CC BY-SA sem ALLOW_SHARE_ALIKE
            wikimedia_video(self.TITLE, get_json=self.api("cc-by-sa-4.0", "CC BY-SA 4.0"))
        candidate, _ = wikimedia_video(self.TITLE, True, get_json=self.api("cc-by-sa-4.0", "CC BY-SA 4.0"))
        self.assertTrue(candidate.attribution_required)
        with self.assertRaises(ValueError):  # não comercial nunca
            wikimedia_video(self.TITLE, True, get_json=self.api("cc-by-nc-4.0", "CC BY-NC 4.0"))
        with self.assertRaises(ValueError):  # foto não é filmagem
            wikimedia_video(self.TITLE, get_json=self.api(mime="image/jpeg"))


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

    def test_generic_query_is_tied_to_the_region_in_the_archive_only(self):
        # "calm lake at dawn" sozinha trazia um lago dos Pirineus: no acervo vira "... Venezuela".
        commons = FakeSource("wikimedia", {"calm lake at dawn": [cand("Pirineus")],
                                           "calm lake at dawn Venezuela": [cand("Maracaibo")]})
        pexels = FakeSource("pexels", {})
        pexels.generic = True
        finder = StockImageFinder([commons, pexels], fetch=lambda url: b"x", region="Venezuela")
        self.assertEqual(finder.find(["calm lake at dawn"], set())[0].title, "Maracaibo")
        self.assertEqual(commons.queries, ["calm lake at dawn Venezuela"])
        self.assertEqual(pexels.queries, ["calm lake at dawn"])

    def test_proper_name_query_is_not_changed_and_region_can_be_off(self):
        commons = FakeSource("wikimedia", {"Lake Maracaibo": [cand("Maracaibo")], "storm": [cand("Denver")]})
        finder = StockImageFinder([commons], fetch=lambda url: b"x", region="Venezuela")
        finder.find(["Lake Maracaibo"], set())
        self.assertEqual(commons.queries, ["Lake Maracaibo"])
        sem_regiao = StockImageFinder([commons], fetch=lambda url: b"x", region="")
        self.assertEqual(sem_regiao.find(["storm"], set())[0].title, "Denver")

    def test_generic_query_without_regional_result_falls_back_to_next_query(self):
        # Sem foto "na Venezuela", passa para a próxima busca da cena em vez de pegar o exterior.
        commons = FakeSource("wikimedia", {"ship engine room": [cand("Submarino")],
                                           "Lake Maracaibo": [cand("Maracaibo")]})
        finder = StockImageFinder([commons], fetch=lambda url: b"x", region="Venezuela")
        self.assertEqual(finder.find(["ship engine room", "Lake Maracaibo"], set())[0].title, "Maracaibo")

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

    def test_rejecting_one_scene_keeps_the_others_and_never_brings_it_back(self):
        from visual_generator import reject_scene_images

        source = FakeSource("wikimedia", {"lake": [cand("Lake"), cand("Lake2")], "bolivar": [cand("Bolivar")]})
        visuals = self.generator(source).process_episode(DAY).parent  # 1 Lake, 2 Lake2, 3 Bolivar
        reject_scene_images(visuals, [2])
        self.assertFalse((visuals / "scene_02.png").exists())
        self.assertTrue((visuals / "scene_01.png").exists())
        more = FakeSource("wikimedia", {"lake": [cand("Lake"), cand("Lake2"), cand("Lake3")],
                                        "bolivar": [cand("Bolivar")]})
        self.generator(more).process_episode(DAY)
        credits = json.loads((visuals / "credits.json").read_text(encoding="utf-8"))
        self.assertEqual([credits[k]["title"] for k in ("1", "2", "3")], ["Lake", "Lake3", "Bolivar"])

    def test_scenes_copying_a_rejected_image_are_redone_too(self):
        from visual_generator import reject_scene_images

        visuals = self.generator(FakeSource("wikimedia", {"lake": [cand("Lake")]})).process_episode(DAY).parent
        reject_scene_images(visuals, [1])  # 2 e 3 copiavam a imagem da 1
        self.assertEqual(list(visuals.glob("scene_*.png")), [])
        self.generator(FakeSource("wikimedia", {"lake": [cand("Lake"), cand("LakeB")]})).process_episode(DAY)
        credits = json.loads((visuals / "credits.json").read_text(encoding="utf-8"))
        self.assertEqual(credits["1"]["title"], "LakeB")

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


class StandardThumbnailTest(unittest.TestCase):
    """O Wikimedia limita o download de originais: foto menor que 1280 px usa a miniatura de 960."""

    ORIGINAL = ("https://upload.wikimedia.org/wikipedia/commons/b/bf/Rafael_Urdaneta_Bridge%2C_1970s.jpg"
                "?utm_source=commons.wikimedia.org&utm_campaign=imageinfo&utm_content=")

    def info(self, w, h, thumb_kind="thumbnail_unscaled"):
        return {"url": self.ORIGINAL + "original", "thumburl": self.ORIGINAL + thumb_kind, "width": w, "height": h}

    def test_small_photo_uses_the_960_standard_thumbnail(self):
        from image_sources import _download_url

        self.assertEqual(_download_url(self.info(996, 670)),
                         "https://thumb.wikimedia.org/wikipedia/commons/thumb/b/bf/"
                         "Rafael_Urdaneta_Bridge%2C_1970s.jpg/960px-Rafael_Urdaneta_Bridge%2C_1970s.jpg")

    def test_large_photo_keeps_the_api_thumbnail(self):
        from image_sources import _download_url

        info = {"url": self.ORIGINAL + "original", "width": 4010, "height": 2337,
                "thumburl": "https://thumb.wikimedia.org/wikipedia/commons/thumb/e/e6/B.jpg/1280px-B.jpg"}
        self.assertEqual(_download_url(info), info["thumburl"])

    def test_keeps_original_when_960_would_be_too_small(self):
        from image_sources import _download_url

        # 800x600: não existe miniatura padrão >= 600 px de lado menor; 1200x700 a 960 vira 560.
        self.assertEqual(_download_url(self.info(800, 600)), self.ORIGINAL + "thumbnail_unscaled")
        self.assertEqual(_download_url(self.info(1200, 700)), self.ORIGINAL + "thumbnail_unscaled")


class RetryOnRateLimitTest(unittest.TestCase):
    """O Wikimedia responde 429 a buscas seguidas; o pedido precisa esperar e repetir."""

    def resp(self, status, retry_after=None):
        from types import SimpleNamespace
        import requests

        def raise_for_status():
            if status >= 400:
                raise requests.HTTPError(f"{status} Client Error")
        headers = {"Retry-After": retry_after} if retry_after else {}
        return SimpleNamespace(status_code=status, headers=headers, raise_for_status=raise_for_status,
                               json=lambda: {"ok": True}, content=b"img")

    def test_waits_retry_after_and_succeeds(self):
        from unittest import mock
        import image_sources

        waits = []
        with mock.patch("requests.get", side_effect=[self.resp(429, "3"), self.resp(200)]) as get:
            r = image_sources._request("https://commons.wikimedia.org/w/api.php", sleep=waits.append)
        self.assertEqual(r.json(), {"ok": True})
        self.assertEqual(get.call_count, 2)
        self.assertEqual(waits, [3.0])

    def test_gives_up_after_max_tries_with_the_http_error(self):
        from unittest import mock
        import requests
        import image_sources

        waits = []
        with mock.patch("requests.get", return_value=self.resp(429, "999")) as get:
            with self.assertRaises(requests.HTTPError):
                image_sources._request("https://x", sleep=waits.append)
        self.assertEqual(get.call_count, image_sources.MAX_TRIES)
        self.assertTrue(all(w == image_sources.MAX_WAIT_SECONDS for w in waits))

    def test_404_does_not_retry(self):
        from unittest import mock
        import requests
        import image_sources

        with mock.patch("requests.get", return_value=self.resp(404)) as get:
            with self.assertRaises(requests.HTTPError):
                image_sources._request("https://x", sleep=lambda s: None)
        self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
