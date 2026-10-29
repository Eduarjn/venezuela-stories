"""Módulo 1 – Motor de storytelling e retenção.

Gera o episódio do dia: escolhe um tema inédito da Venezuela, pesquisa na web,
escreve o roteiro em cenas curtas e salva output/AAAA-MM-DD/current_episode.json.

Garantias antes de salvar:
- JSON validado pelo schema (Pydantic). Se falhar, o erro volta para o modelo
  e ele tenta de novo (até MAX_ATTEMPTS).
- Tema inédito no banco (database.py). Se repetir, o modelo escolhe outro.
- Trilha "fact": cada fonte é aberta e o trecho citado precisa aparecer na
  página. São necessárias 2+ fontes de domínios diferentes.

Uso:
    python story_engine.py                     # episódio de hoje
    python story_engine.py --date 2026-11-05 --language es-419
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from database import (
    CATEGORIES,
    DaySlotTakenError,
    DuplicateTopicError,
    StoryDatabase,
    base_domain,
    normalize,
)

log = logging.getLogger("story_engine")

PROJECT_DIR = Path(__file__).parent
MAX_ATTEMPTS = 4
MAX_TOKENS = 8000
MAX_PAUSE_CONTINUATIONS = 3
WEB_SEARCH_MAX_USES = 6

MIN_SCENES, MAX_SCENES = 12, 22
MIN_SCENE_WORDS, MAX_SCENE_WORDS = 3, 16
HOOK_MAX_WORDS = 10
# Velocidade média de narração (palavras por segundo) usada para estimar duração.
WORDS_PER_SECOND = 2.6
MIN_DURATION_S, MAX_DURATION_S = 35, 90

STYLE_ANCHOR = (
    "cinematic historical illustration, dramatic lighting, rich textures, "
    "film grain, 9:16 vertical composition"
)

MotionType = Literal["zoom_in_fast", "pan_left_to_right", "shake_impact", "slow_ken_burns", "pulse_glow"]
SfxCue = Literal["whoosh", "thunder", "old_film_click", "suspense_riser", "crowd_murmur"]
Language = Literal["pt-BR", "es-419"]

BANNED_OPENINGS = {
    "pt-BR": ("hoje vou falar", "hoje eu vou falar", "neste video", "nesse video", "voce sabia",
              "ola", "oi pessoal", "fala galera", "bem vindos", "seja bem vindo"),
    "es-419": ("hoy voy a hablar", "hoy les voy a hablar", "en este video", "sabias que",
               "hola", "bienvenidos", "que tal amigos"),
}
FICTION_LABELS = ("lenda", "leyenda", "ficcao", "ficcion", "conto popular", "relato popular", "folclore")

# Ideias de partida. O modelo pode escolher outro tema; o banco impede repetição.
TOPIC_SEEDS = [
    "Relâmpago do Catatumbo (Lago de Maracaibo)",
    "El Helicoide: de shopping futurista a outro destino",
    "A 'Venezuela Saudita' dos anos 1970",
    "Salto Ángel e a origem do nome",
    "Francisco de Miranda e o Arco do Triunfo",
    "Colonia Tovar, a vila alemã nos Andes",
    "Ponte General Rafael Urdaneta sobre o Lago de Maracaibo",
    "Teleférico de Mérida",
    "Os tepuis da Gran Sabana e o 'Mundo Perdido'",
    "A Tragédia de Vargas (1999) e a reconstrução",
    "O primeiro poço de petróleo comercial da Venezuela",
    "A lenda do Silbón (trilha ficção, com rótulo)",
]


# --------------------------------------------------------------------------- schema


class Scene(BaseModel):
    scene_id: int = Field(ge=1)
    narration_text: str
    image_prompt: str
    motion_type: MotionType
    sfx_cue: SfxCue

    @field_validator("narration_text")
    @classmethod
    def _scene_length(cls, text: str) -> str:
        words = len(text.split())
        if not MIN_SCENE_WORDS <= words <= MAX_SCENE_WORDS:
            raise ValueError(
                f"cada cena precisa ter {MIN_SCENE_WORDS}-{MAX_SCENE_WORDS} palavras "
                f"(3 a 5 segundos); esta tem {words}: {text!r}"
            )
        return text.strip()

    @field_validator("image_prompt")
    @classmethod
    def _consistent_style(cls, prompt: str) -> str:
        prompt = prompt.strip()
        if STYLE_ANCHOR.split(",")[0] not in prompt.lower():
            prompt = f"{prompt.rstrip('. ')}, {STYLE_ANCHOR}"
        return prompt


class Source(BaseModel):
    url: str
    title: str
    supporting_quote: str = Field(
        description="Trecho curto copiado literalmente da página, no idioma da página."
    )

    @field_validator("url")
    @classmethod
    def _http_url(cls, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"URL inválida: {url!r}")
        base_domain(url)
        return url


class Claim(BaseModel):
    claim: str
    source_urls: list[str] = Field(min_length=1)


class Episode(BaseModel):
    topic_title: str
    entity: str
    category: Literal[CATEGORIES]  # type: ignore[valid-type]
    track: Literal["fact", "fiction"]
    language: Language
    open_question: str
    open_question_scene_id: int
    climax_scene_id: int
    claims: list[Claim] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    scenes: list[Scene]
    post_title: str
    post_description: str
    hashtags: list[str] = Field(min_length=3, max_length=8)

    @model_validator(mode="after")
    def _retention_rules(self) -> "Episode":
        n = len(self.scenes)
        if not MIN_SCENES <= n <= MAX_SCENES:
            raise ValueError(f"o vídeo precisa de {MIN_SCENES}-{MAX_SCENES} cenas; veio com {n}")
        if [s.scene_id for s in self.scenes] != list(range(1, n + 1)):
            raise ValueError("scene_id precisa ser sequencial, começando em 1")

        hook = self.scenes[0].narration_text
        if len(hook.split()) > HOOK_MAX_WORDS:
            raise ValueError(f"o hook (cena 1) precisa caber em 3s: no máximo {HOOK_MAX_WORDS} palavras")
        hook_key = normalize(hook).replace("-", " ")
        for banned in BANNED_OPENINGS[self.language]:
            if hook_key.startswith(banned):
                raise ValueError(f"hook proibido: não pode começar com {banned!r}")

        if not 1 <= self.open_question_scene_id <= 3:
            raise ValueError("a pergunta em aberto precisa ser lançada nas cenas 1 a 3")
        if not (n * 0.6 <= self.climax_scene_id < n):
            raise ValueError(
                f"o clímax (resposta da pergunta) precisa ficar entre a cena {int(n * 0.6 + 0.999)} "
                f"e a {n - 1}; veio na {self.climax_scene_id}"
            )

        words = sum(len(s.narration_text.split()) for s in self.scenes)
        seconds = words / WORDS_PER_SECOND
        if not MIN_DURATION_S <= seconds <= MAX_DURATION_S:
            raise ValueError(
                f"duração estimada {seconds:.0f}s fora do alvo {MIN_DURATION_S}-{MAX_DURATION_S}s "
                f"({words} palavras)"
            )

        if self.track == "fact":
            domains = {base_domain(s.url) for s in self.sources}
            if len(domains) < 2:
                raise ValueError("trilha fact precisa de fontes de pelo menos 2 domínios diferentes")
            if not self.claims:
                raise ValueError("trilha fact precisa listar as afirmações (claims) e suas fontes")
            known = {s.url for s in self.sources}
            for c in self.claims:
                missing = [u for u in c.source_urls if u not in known]
                if missing:
                    raise ValueError(f"claim cita URL que não está em sources: {missing}")
        else:
            opening = " ".join(normalize(s.narration_text) for s in self.scenes[:4]).replace("-", " ")
            if not any(label in opening for label in FICTION_LABELS):
                raise ValueError(
                    "trilha fiction precisa dizer nas 4 primeiras cenas que é lenda/ficção"
                )
        return self

    @property
    def estimated_seconds(self) -> float:
        return sum(len(s.narration_text.split()) for s in self.scenes) / WORDS_PER_SECOND


# --------------------------------------------------------------------------- prompts

SYSTEM_PROMPT = f"""Você é roteirista e estrategista de retenção de um canal de vídeos curtos
verticais (TikTok, Reels, Shorts) sobre a Venezuela: história real, mistérios históricos,
curiosidades econômicas, culturais, geográficas e construções marcantes.

REGRAS DE CONTEÚDO
- Trilha "fact": tudo o que for narrado é real e verificável. Use a ferramenta web_search
  para confirmar cada dado (datas, números, nomes) em pelo menos 2 sites de domínios
  diferentes. Se um dado não se confirmar, ele sai do roteiro. Não invente diálogo,
  personagem ou detalhe "para dar cor".
- Trilha "fiction" (só para lendas/folclore): diga explicitamente nas primeiras cenas que é
  uma lenda. Nunca apresente lenda como fato.
- Temas políticos: narre o que aconteceu e atribua cada versão a quem a defende. Sem
  adjetivar governos ou oposição.
- Tragédias: só eventos passados (mais de 30 dias). Foco na experiência humana e na
  reconstrução. Número de vítimas aparece uma vez, sem drama. Nunca especule sobre
  catástrofes futuras.

ESTRUTURA DE RETENÇÃO
- Cena 1 = HOOK de no máximo {HOOK_MAX_WORDS} palavras (cabe em 3 segundos): um fato concreto
  e chocante que crie uma lacuna. Proibido começar com "Hoje vou falar", "Neste vídeo",
  "Você sabia", saudações ou equivalentes em espanhol.
- {MIN_SCENES} a {MAX_SCENES} cenas, cada uma com {MIN_SCENE_WORDS} a {MAX_SCENE_WORDS} palavras
  (3 a 5 segundos de fala). Cada frase curta = uma nova imagem.
- LOOP DE CURIOSIDADE: lance uma pergunta em aberto nas cenas 1-3 e só responda no clímax,
  que fica nos últimos 40% do vídeo (mas não na última cena). A última cena fecha a
  história e convida a seguir o canal sem pedir like.
- Duração total entre {MIN_DURATION_S} e {MAX_DURATION_S} segundos de narração.
- Pontuação pensada para emoção: frases curtas, reticências e pontos finais para pausas.

IMAGENS
- image_prompt em INGLÊS, descrevendo exatamente o que aconteceu na cena: enquadramento,
  época, luz, atmosfera. Estilo fixo em todas as cenas: "{STYLE_ANCHOR}".
- Nunca o rosto de uma pessoa real identificável: use silhueta, de costas, multidão,
  objetos ou lugares.

motion_type permitido: zoom_in_fast, pan_left_to_right, shake_impact, slow_ken_burns, pulse_glow.
sfx_cue permitido: whoosh, thunder, old_film_click, suspense_riser, crowd_murmur.

SAÍDA: responda SOMENTE com um objeto JSON válido (sem markdown, sem texto antes ou depois)."""

JSON_TEMPLATE = {
    "topic_title": "título curto do tema",
    "entity": "assunto principal (pessoa, lugar ou evento)",
    "category": "|".join(CATEGORIES),
    "track": "fact|fiction",
    "language": "<idioma pedido>",
    "open_question": "a pergunta que prende até o clímax",
    "open_question_scene_id": 2,
    "climax_scene_id": 13,
    "claims": [{"claim": "afirmação factual narrada", "source_urls": ["https://..."]}],
    "sources": [
        {"url": "https://...", "title": "título da página",
         "supporting_quote": "trecho curto COPIADO LITERALMENTE da página que confirma o fato"}
    ],
    "scenes": [
        {"scene_id": 1, "narration_text": "...", "image_prompt": "...",
         "motion_type": "shake_impact", "sfx_cue": "thunder"}
    ],
    "post_title": "título da publicação",
    "post_description": "descrição curta (na trilha fiction, a 1ª linha diz que é lenda)",
    "hashtags": ["#venezuela", "..."],
}

LANGUAGE_NOTES = {
    "pt-BR": "Narre em português do Brasil para um público brasileiro curioso: explique o "
             "contexto que um brasileiro não conhece (moeda, instituições, geografia), sem soar "
             "didático. Nada de espanhol narrado, exceto nomes próprios.",
    "es-419": "Narra en español latinoamericano neutro, para un público venezolano/latino que "
              "conoce el contexto: no expliques lo obvio, usa ese tiempo en detalles de la "
              "historia. Sin jerga regional.",
}


def build_user_prompt(language: str, used_topics: list[str], excluded: list[str]) -> str:
    used = "\n".join(f"- {t}" for t in used_topics) or "- (nenhum ainda)"
    seeds = "\n".join(f"- {t}" for t in TOPIC_SEEDS)
    extra = "\n".join(f"- {t}" for t in excluded)
    return f"""Crie o episódio de hoje. Idioma: {language}.
{LANGUAGE_NOTES[language]}

TEMAS JÁ USADOS (proibido repetir o assunto, mesmo com outro ângulo):
{used}
{f"TAMBÉM PROIBIDOS NESTA RODADA:{chr(10)}{extra}" if excluded else ""}
Ideias de partida (opcional, pode escolher outro tema inédito e forte):
{seeds}

Pesquise com web_search antes de escrever. Em "sources", supporting_quote deve ser copiado
literalmente da página (será conferido automaticamente).

Formato exato do JSON:
{json.dumps(JSON_TEMPLATE, ensure_ascii=False, indent=2)}"""


# --------------------------------------------------------------------------- verificação


def _page_text(raw_html: str) -> str:
    raw_html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_html)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw_html))
    return normalize(text).replace("-", " ")


def quote_found(quote: str, page_text: str, min_token_ratio: float = 0.8) -> bool:
    """O trecho aparece na página? Aceita pequenas diferenças de pontuação/formatação."""
    q = normalize(quote).replace("-", " ")
    if not q:
        return False
    if q in page_text:
        return True
    tokens = [t for t in q.split() if len(t) > 3]
    if len(tokens) < 4:
        return False
    page_tokens = set(page_text.split())
    return sum(t in page_tokens for t in tokens) / len(tokens) >= min_token_ratio


def fetch_url(url: str, timeout: float = 20) -> str:
    import requests

    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 (venezuela-stories fact-check)"},
    )
    response.raise_for_status()
    return response.text


@dataclass
class VerificationResult:
    verified: list[Source] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def domains(self) -> set[str]:
        return {base_domain(s.url) for s in self.verified}

    @property
    def ok(self) -> bool:
        return len(self.domains) >= 2


def verify_sources(sources: list[Source], fetcher: Callable[[str], str] = fetch_url) -> VerificationResult:
    result = VerificationResult()
    for source in sources:
        try:
            page = _page_text(fetcher(source.url))
        except Exception as exc:  # rede, 404, timeout...
            result.problems.append(f"{source.url}: não abriu ({exc.__class__.__name__})")
            continue
        if quote_found(source.supporting_quote, page):
            result.verified.append(source)
        else:
            result.problems.append(f"{source.url}: o trecho citado não aparece na página")
    return result


# --------------------------------------------------------------------------- motor


class StoryEngineError(RuntimeError):
    pass


def extract_json(text: str) -> dict:
    """Pega o objeto JSON da resposta, mesmo que venha com ```json ou texto em volta."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("a resposta não contém um objeto JSON")
    return json.loads(candidate[start : end + 1])


class StoryEngine:
    def __init__(
        self,
        db: StoryDatabase,
        client=None,
        model: str | None = None,
        fetcher: Callable[[str], str] = fetch_url,
        output_dir: Path | None = None,
    ) -> None:
        self.db = db
        self.model = model or os.getenv("ANTHROPIC_MODEL")
        if not self.model:
            raise StoryEngineError("Defina ANTHROPIC_MODEL no .env")
        if client is None:
            import anthropic

            if not os.getenv("ANTHROPIC_API_KEY"):
                raise StoryEngineError("Defina ANTHROPIC_API_KEY no .env")
            # O SDK já repete sozinho erros de rede, 429 e 5xx com backoff.
            client = anthropic.Anthropic(max_retries=4, timeout=300)
        self.client = client
        self.fetcher = fetcher
        self.output_dir = output_dir or PROJECT_DIR / os.getenv("OUTPUT_DIR", "output")

    # -- chamada ao modelo --------------------------------------------------

    def _ask(self, messages: list[dict]) -> str:
        """Chama o modelo com web_search e devolve o texto final.

        Buscas longas podem pausar (stop_reason == "pause_turn"); nesse caso a
        conversa continua de onde parou.
        """
        messages = list(messages)
        for _ in range(MAX_PAUSE_CONTINUATIONS + 1):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=[{"type": "web_search_20250305", "name": "web_search",
                        "max_uses": WEB_SEARCH_MAX_USES}],
            )
            if response.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": response.content})
                continue
            if response.stop_reason == "refusal":
                raise StoryEngineError("o modelo recusou o pedido")
            if response.stop_reason == "max_tokens":
                raise ValueError("a resposta foi cortada (max_tokens); seja mais conciso")
            return "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        raise StoryEngineError("a pesquisa não terminou após várias continuações")

    # -- fluxo principal ----------------------------------------------------

    def create_episode(self, day: date | None = None, language: str = "pt-BR") -> Path:
        day = day or date.today()
        if language not in LANGUAGE_NOTES:
            raise StoryEngineError(f"idioma não suportado: {language}")
        existing = self.db.topic_for_date(day)
        if existing:
            raise DaySlotTakenError(f"{day} já tem o tema #{existing.id} '{existing.title}'")

        excluded: list[str] = []
        messages = [{"role": "user",
                     "content": build_user_prompt(language, self.db.used_topics_summary(), excluded)}]

        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            log.info("Tentativa %s/%s", attempt, MAX_ATTEMPTS)
            try:
                raw = self._ask(messages)
            except ValueError as exc:  # resposta cortada
                last_error = str(exc)
                messages = messages[:1]
                continue

            feedback = self._check(raw, language, excluded)
            if isinstance(feedback, Episode):
                return self._save(feedback, day)

            last_error = feedback
            log.warning("Tentativa %s rejeitada: %s", attempt, feedback)
            messages = [
                messages[0],
                {"role": "assistant", "content": raw or "(vazio)"},
                {"role": "user", "content":
                    f"Sua resposta foi rejeitada pelo validador:\n{feedback}\n\n"
                    "Corrija e devolva o JSON completo novamente, só o JSON."},
            ]
        raise StoryEngineError(f"não foi possível gerar um episódio válido: {last_error}")

    def _check(self, raw: str, language: str, excluded: list[str]) -> Episode | str:
        """Devolve o Episode aprovado ou a mensagem de erro para o modelo corrigir."""
        try:
            data = extract_json(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            return f"JSON inválido: {exc}"
        try:
            episode = Episode.model_validate(data)
        except ValidationError as exc:
            errors = "\n".join(f"- {'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
            return f"O JSON não segue as regras:\n{errors}"
        if episode.language != language:
            return f'language precisa ser "{language}"'

        duplicate = self.db.find_duplicate(episode.topic_title, episode.entity)
        if duplicate:
            excluded.append(episode.entity)
            return (f"O tema '{episode.topic_title}' repete #{duplicate.id} '{duplicate.title}'. "
                    "Escolha OUTRO tema inédito e escreva o episódio inteiro de novo.")

        if episode.track == "fact":
            check = verify_sources(episode.sources, self.fetcher)
            if not check.ok:
                problems = "\n".join(f"- {p}" for p in check.problems)
                return ("A verificação automática das fontes falhou (são necessárias 2+ fontes de "
                        f"domínios diferentes com o trecho citado literalmente):\n{problems}\n"
                        "Pesquise de novo, troque as fontes ou retire os dados que não se confirmam.")
            unverified = {s.url for s in episode.sources} - {s.url for s in check.verified}
            for claim in episode.claims:
                if not set(claim.source_urls) - unverified:
                    return (f"A afirmação '{claim.claim}' só cita fontes que não foram confirmadas. "
                            "Apoie-a em uma fonte confirmada ou retire do roteiro.")
            episode.sources = check.verified
        return episode

    def _save(self, episode: Episode, day: date) -> Path:
        topic = self.db.reserve_topic(
            episode.topic_title, episode.entity, episode.category, episode.track, day
        )
        for source in episode.sources:
            self.db.add_source(topic.id, source.url, source.title)
        self.db.set_status(topic.id, "scripted")
        self.db.set_status(topic.id, "verified")

        folder = self.output_dir / day.isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "current_episode.json"
        payload = {
            "topic_id": topic.id,
            "date": day.isoformat(),
            "estimated_seconds": round(episode.estimated_seconds, 1),
            **episode.model_dump(),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Episódio #%s salvo em %s (%.0fs, %s cenas)",
                 topic.id, path, episode.estimated_seconds, len(episode.scenes))
        return path


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_DIR / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description="Gera o roteiro do episódio do dia.")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--language", choices=list(LANGUAGE_NOTES),
                        default=(os.getenv("VIDEO_LANGUAGES") or "pt-BR").split(",")[0].strip())
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        engine = StoryEngine(StoryDatabase())
        path = engine.create_episode(args.date, args.language)
    except (StoryEngineError, DaySlotTakenError, DuplicateTopicError) as exc:
        log.error("%s", exc)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
