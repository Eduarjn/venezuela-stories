import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import StoryDatabase  # noqa: E402
from story_engine import (  # noqa: E402
    STYLE_ANCHOR,
    Episode,
    FetchBlocked,
    SearchEvidence,
    Source,
    StoryEngine,
    StoryEngineError,
    canonical_url,
    extract_json,
    quote_found,
    verify_sources,
)

DAY = date(2026, 11, 1)
SRC_A = "https://pt.wikipedia.org/wiki/Relampago_do_Catatumbo"
SRC_B = "https://www.example-geografia.com/catatumbo"
QUOTE_A = "O fenômeno ocorre sobre a foz do rio Catatumbo"
QUOTE_B = "tempestades elétricas frequentes sobre o lago"
FILLER = "<p>" + "Texto de contexto sobre a geografia e a história da região. " * 10 + "</p>"
PAGES = {
    # Tags no meio da frase não podem atrapalhar a verificação.
    SRC_A: f"<html><body>{FILLER}<p>O fenômeno ocorre sobre a foz do rio <a href='#'>Catatumbo</a>, "
           f"no Lago de <b>Maracaibo</b>.</p></body></html>",
    SRC_B: f"<html><script>x()</script>{FILLER}<div>Há {QUOTE_B} durante muitas noites.</div></html>",
}

NARRATION = [
    "Um lago na Venezuela acende o céu quase toda noite.",
    "Navegantes usavam essa luz como farol natural.",
    "Mas por que os raios caem sempre ali?",
    "O lugar é o Lago de Maracaibo, no oeste do país.",
    "Ali deságua o rio Catatumbo, vindo das montanhas.",
    "À noite, nuvens enormes se formam sobre a foz.",
    "E então o céu começa a piscar, em silêncio.",
    "De longe, quase não se ouve nenhum trovão.",
    "Pescadores cresceram vendo esse espetáculo das varandas.",
    "Cientistas passaram anos medindo cada tempestade dali.",
    "A resposta está no encontro do ar com as montanhas.",
    "O vento quente do lago sobe e encontra o ar frio.",
    "Esse choque cria tempestades elétricas noite após noite.",
    "Siga o canal: a Venezuela ainda guarda muitos segredos.",
]


def episode_data(**overrides):
    scenes = [
        {
            "scene_id": i,
            "narration_text": text,
            "image_prompt": f"Wide shot of lake at night, scene {i}",
            "motion_type": "slow_ken_burns",
            "sfx_cue": "thunder",
        }
        for i, text in enumerate(NARRATION, start=1)
    ]
    data = {
        "topic_title": "O Relâmpago do Catatumbo",
        "entity": "Relâmpago do Catatumbo",
        "category": "fenomeno_natural",
        "track": "fact",
        "language": "pt-BR",
        "open_question": "Por que os raios caem sempre ali?",
        "open_question_scene_id": 3,
        "climax_scene_id": 11,
        "claims": [{"claim": "Os raios ocorrem na foz do Catatumbo", "source_urls": [SRC_A, SRC_B]}],
        "sources": [
            {"url": SRC_A, "title": "Wikipédia", "supporting_quote": QUOTE_A},
            {"url": SRC_B, "title": "Geografia", "supporting_quote": QUOTE_B},
        ],
        "scenes": scenes,
        "post_title": "O lago que acende o céu",
        "post_description": "Um fenômeno real da Venezuela.",
        "hashtags": ["#venezuela", "#curiosidades", "#historia"],
    }
    data.update(overrides)
    return data


def response(text, stop_reason="end_turn", search_urls=(), citations=()):
    """Resposta falsa da API. citations = [(url, cited_text), ...]"""
    content = []
    if search_urls:
        content.append(SimpleNamespace(
            type="web_search_tool_result",
            content=[SimpleNamespace(type="web_search_result", url=u, title="t") for u in search_urls],
        ))
    cites = [SimpleNamespace(type="web_search_result_location", url=u, cited_text=t)
             for u, t in citations]
    content.append(SimpleNamespace(type="text", text=text, citations=cites or None))
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs["messages"]))
        return self.replies.pop(0)


BLOCKED = "https://www.cloudflare-protected.com/historia"
JS_ONLY = "https://www.js-only-site.com/historia"


def fake_fetch(url):
    if url == BLOCKED:
        raise FetchBlocked("HTTP 403")
    if url == JS_ONLY:
        return "<html><body><div id='root'></div><script>render()</script></body></html>"
    if url not in PAGES:
        raise ConnectionError(url)
    return PAGES[url]


def no_fetch(url):
    raise AssertionError(f"não deveria abrir {url}")


class StoryEngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = StoryDatabase(root / "test.db")
        self.out = root / "output"

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, *replies):
        self.client = FakeClient(replies)
        return StoryEngine(self.db, client=self.client, model="test-model",
                           fetcher=fake_fetch, output_dir=self.out)

    def last_feedback(self):
        return self.client.calls[-1][-1]["content"]

    # -- caminho feliz -------------------------------------------------------

    def test_valid_episode_is_saved_and_topic_verified(self):
        path = self.engine(response(json.dumps(episode_data()))).create_episode(DAY)

        self.assertEqual(path, self.out / "2026-11-01" / "current_episode.json")
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["scenes"]), 14)
        self.assertEqual(saved["scenes"][0]["scene_id"], 1)
        self.assertTrue(saved["scenes"][0]["image_prompt"].endswith(STYLE_ANCHOR))
        self.assertGreater(saved["estimated_seconds"], 35)

        topic = self.db.topic_for_date(DAY)
        self.assertEqual(topic.status, "verified")
        self.assertEqual(saved["topic_id"], topic.id)
        self.assertEqual({s["domain"] for s in self.db.sources(topic.id)},
                         {"wikipedia.org", "example-geografia.com"})

    def test_json_inside_markdown_fence_is_accepted(self):
        text = "Aqui está:\n```json\n" + json.dumps(episode_data()) + "\n```"
        self.assertTrue(self.engine(response(text)).create_episode(DAY).exists())

    def test_pause_turn_continues_the_conversation(self):
        engine = self.engine(response("", "pause_turn"), response(json.dumps(episode_data())))
        engine.create_episode(DAY)
        self.assertEqual(len(self.client.calls), 2)
        self.assertEqual(self.client.calls[1][-1]["role"], "assistant")

    # -- retry ---------------------------------------------------------------

    def test_broken_json_triggers_retry_with_feedback(self):
        engine = self.engine(response("{ isto não é json"), response(json.dumps(episode_data())))
        engine.create_episode(DAY)
        self.assertEqual(len(self.client.calls), 2)
        self.assertIn("JSON inválido", self.last_feedback())

    def test_banned_hook_is_rejected(self):
        bad = episode_data()
        bad["scenes"][0]["narration_text"] = "Hoje vou falar sobre um lago."
        engine = self.engine(response(json.dumps(bad)), response(json.dumps(episode_data())))
        engine.create_episode(DAY)
        self.assertIn("hook proibido", self.last_feedback())

    def test_long_scene_is_rejected(self):
        bad = episode_data()
        bad["scenes"][4]["narration_text"] = " ".join(["palavra"] * 25)
        engine = self.engine(response(json.dumps(bad)), response(json.dumps(episode_data())))
        engine.create_episode(DAY)
        self.assertIn("3 a 5 segundos", self.last_feedback())

    def test_climax_must_be_late(self):
        engine = self.engine(response(json.dumps(episode_data(climax_scene_id=4))),
                             response(json.dumps(episode_data())))
        engine.create_episode(DAY)
        self.assertIn("clímax", self.last_feedback())

    def test_quote_missing_from_page_is_rejected(self):
        bad = episode_data()
        bad["sources"][1]["supporting_quote"] = "uma frase que a página não tem de jeito nenhum"
        engine = self.engine(response(json.dumps(bad)), response(json.dumps(episode_data())))
        engine.create_episode(DAY)
        self.assertIn("não aparece na página", self.last_feedback())

    def test_unreachable_source_is_rejected(self):
        bad = episode_data()
        bad["sources"][1]["url"] = "https://site-que-nao-existe.org/x"
        bad["claims"][0]["source_urls"] = [SRC_A, "https://site-que-nao-existe.org/x"]
        engine = self.engine(response(json.dumps(bad)), response(json.dumps(episode_data())))
        engine.create_episode(DAY)
        self.assertIn("não abriu", self.last_feedback())

    def test_repeated_topic_asks_for_another(self):
        self.db.reserve_topic("Relâmpago do Catatumbo", "Relâmpago do Catatumbo",
                              "fenomeno_natural", "fact", date(2026, 10, 1))
        other = episode_data(topic_title="A ponte sobre o Lago de Maracaibo",
                             entity="Ponte General Rafael Urdaneta")
        engine = self.engine(response(json.dumps(episode_data())), response(json.dumps(other)))
        engine.create_episode(DAY)
        self.assertIn("OUTRO tema", self.last_feedback())
        self.assertEqual(self.db.topic_for_date(DAY).title, "A ponte sobre o Lago de Maracaibo")

    def test_gives_up_after_max_attempts_without_touching_the_db(self):
        engine = self.engine(*[response("nada")] * 4)
        with self.assertRaises(StoryEngineError):
            engine.create_episode(DAY)
        self.assertEqual(self.db.list_topics(), [])

    def test_day_already_used_fails_fast(self):
        self.engine(response(json.dumps(episode_data()))).create_episode(DAY)
        with self.assertRaises(ValueError):
            self.engine().create_episode(DAY)

    def test_blocked_site_found_by_search_is_accepted(self):
        data = episode_data()
        data["sources"][1] = {"url": BLOCKED, "title": "Bloqueado", "supporting_quote": QUOTE_B}
        data["claims"][0]["source_urls"] = [SRC_A, BLOCKED]
        engine = self.engine(response(json.dumps(data), search_urls=[BLOCKED + "/", SRC_A]))
        saved = json.loads(engine.create_episode(DAY).read_text(encoding="utf-8"))
        self.assertEqual(saved["source_verification"], {SRC_A: "page", BLOCKED: "search_result"})
        methods = {s["url"]: s["verification"] for s in self.db.sources(saved["topic_id"])}
        self.assertEqual(methods[BLOCKED], "search_result")

    def test_blocked_site_not_from_search_is_rejected(self):
        data = episode_data()
        data["sources"][1] = {"url": BLOCKED, "title": "Bloqueado", "supporting_quote": QUOTE_B}
        data["claims"][0]["source_urls"] = [SRC_A, BLOCKED]
        engine = self.engine(response(json.dumps(data)), response(json.dumps(episode_data())))
        engine.create_episode(DAY)
        self.assertIn("bloqueou a leitura", self.last_feedback())

    # -- trilha ficção -------------------------------------------------------

    def test_fiction_must_be_labelled(self):
        data = episode_data(track="fiction", category="lenda", claims=[], sources=[],
                            topic_title="A lenda do Silbón", entity="El Silbón")
        with self.assertRaises(ValueError):
            Episode.model_validate(data)

        data["scenes"][1]["narration_text"] = "Esta é uma lenda do folclore venezuelano."
        self.assertEqual(Episode.model_validate(data).track, "fiction")

    # -- utilitários ---------------------------------------------------------

    def test_extract_json_ignores_surrounding_text(self):
        self.assertEqual(extract_json('ok {"a": 1} fim'), {"a": 1})

    def test_quote_found_tolerates_formatting(self):
        page = "o fenomeno ocorre sobre a foz do rio catatumbo no lago"
        self.assertTrue(quote_found("O fenômeno ocorre, sobre a foz do rio Catatumbo", page))
        self.assertFalse(quote_found("algo completamente diferente que nao esta aqui", page))


class VerifySourcesTest(unittest.TestCase):
    def src(self, url, quote=QUOTE_A):
        return Source(url=url, title="t", supporting_quote=quote)

    def test_citation_from_search_is_enough_without_opening_the_page(self):
        evidence = SearchEvidence()
        evidence.add_response(response("x", citations=[
            (SRC_A, "Texto... " + QUOTE_A + " ..."), (SRC_B, QUOTE_B)]).content)
        result = verify_sources([self.src(SRC_A), self.src(SRC_B, QUOTE_B)], no_fetch, evidence)
        self.assertTrue(result.ok)
        self.assertEqual(set(result.methods.values()), {"citation"})

    def test_page_that_opens_without_the_quote_is_rejected_even_if_search_found_it(self):
        evidence = SearchEvidence()
        evidence.add_response(response("x", search_urls=[SRC_B]).content)
        result = verify_sources([self.src(SRC_B, "frase inventada que nunca esteve nesta página")],
                                fake_fetch, evidence)
        self.assertEqual(result.verified, [])
        self.assertIn("não aparece na página", result.problems[0])

    def test_javascript_only_page_counts_as_blocked(self):
        evidence = SearchEvidence()
        evidence.add_response(response("x", search_urls=[JS_ONLY]).content)
        result = verify_sources([self.src(JS_ONLY)], fake_fetch, evidence)
        self.assertEqual(result.methods, {JS_ONLY: "search_result"})

    def test_two_weak_sources_are_not_enough(self):
        other = "https://outro-bloqueado.org/x"

        def all_blocked(url):
            raise FetchBlocked("HTTP 403")

        evidence = SearchEvidence()
        evidence.add_response(response("x", search_urls=[BLOCKED, other]).content)
        result = verify_sources([self.src(BLOCKED), self.src(other)], all_blocked, evidence)
        self.assertEqual(len(result.domains), 2)
        self.assertFalse(result.ok)

    def test_canonical_url_ignores_www_slash_anchor_and_utm(self):
        self.assertEqual(canonical_url("https://www.site.com/a/?utm_source=x#topo"),
                         canonical_url("http://site.com/a"))


if __name__ == "__main__":
    unittest.main()
