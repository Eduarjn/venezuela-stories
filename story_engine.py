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
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import urlparse

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
    # Busca de imagem real (Wikimedia/Pexels): 2-6 palavras concretas. Opcional para
    # manter compatíveis os episódios antigos.
    image_query: str = ""
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
- Na trilha "fact", história que só aparece em sites de curiosidades, turismo ou
  divulgação (sem historiador, órgão oficial, enciclopédia ou artigo científico que a
  confirme) é narrada como tradição: "conta-se que", "segundo a tradição". Ex.: o relâmpago
  do Catatumbo ter guiado a frota de Padilla em 1823.
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
- image_query: 2 a 6 palavras em INGLÊS para buscar uma FOTO OU PINTURA REAL num acervo
  (Wikimedia Commons, Pexels). Prefira o lugar, objeto, evento ou pessoa histórica REAL da
  cena, com o nome próprio em maiúscula: "Lake Maracaibo oil derricks", "Francisco de Miranda
  portrait", "Angel Falls aerial". Quando a narração cita um lugar, busque esse lugar. Busca
  genérica só para cena sem lugar específico, e então toda em minúsculas ("lightning storm at
  night"). Nada abstrato ("tensão", "mistério"). Cenas vizinhas podem repetir o mesmo assunto
  com outra busca.
- Varie o tipo de imagem ao longo do vídeo (foto, pintura, gravura, mapa, satélite): no máximo
  2 mapas ou imagens de satélite ("from space", "map") por vídeo, nunca em cenas seguidas.
- Pessoas na image_query: só figuras históricas já falecidas (retrato, pintura ou foto de
  época). Nunca busque pessoas vivas nem pessoas comuns pelo nome.
- image_prompt em INGLÊS, descrevendo exatamente o que aconteceu na cena: enquadramento,
  época, luz, atmosfera. Estilo fixo em todas as cenas: "{STYLE_ANCHOR}".
- No image_prompt (imagem gerada por IA), nunca o rosto de uma pessoa real identificável:
  use silhueta, de costas, multidão, objetos ou lugares.

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
        {"scene_id": 1, "narration_text": "...", "image_query": "Lake Maracaibo lightning",
         "image_prompt": "...", "motion_type": "shake_impact", "sfx_cue": "thunder"}
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

# Respostas que significam "o site barrou o robô", não "a fonte é falsa".
BLOCKED_STATUS = {401, 403, 429, 451, 503}
# Abaixo disso a página provavelmente é montada por JavaScript (texto não vem no HTML).
MIN_PAGE_CHARS = 400


class FetchBlocked(Exception):
    """O site impediu a leitura (403, Cloudflare, timeout, conteúdo via JavaScript)."""


def _page_text(raw_html: str) -> str:
    """Texto visível da página, sem tags, sem acento, só palavras separadas por espaço."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()
        text = soup.get_text(" ")
    except ImportError:
        cleaned = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_html)
        text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", cleaned))
    return normalize(text).replace("-", " ")


def quote_found(quote: str, page_text: str, min_token_ratio: float = 0.8) -> bool:
    """O trecho aparece no texto? Aceita diferenças de pontuação, acento e formatação."""
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


def canonical_url(url: str) -> str:
    """Mesma página com ou sem www, barra final, âncora ou parâmetros de rastreio."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    query = "&".join(p for p in parsed.query.split("&") if p and not p.startswith("utm_"))
    path = parsed.path.rstrip("/")
    return f"{host}{path}" + (f"?{query}" if query else "")


@dataclass
class SearchEvidence:
    """O que a ferramenta web_search da Anthropic devolveu nesta geração."""

    result_urls: set[str] = field(default_factory=set)
    citations: dict[str, list[str]] = field(default_factory=dict)  # url canônica -> cited_text

    def add_response(self, content: list) -> None:
        for block in content:
            kind = getattr(block, "type", "")
            if kind == "web_search_tool_result" and isinstance(block.content, list):
                for result in block.content:
                    if getattr(result, "url", None):
                        self.result_urls.add(canonical_url(result.url))
            elif kind == "text":
                for citation in getattr(block, "citations", None) or []:
                    if getattr(citation, "type", "") == "web_search_result_location":
                        key = canonical_url(citation.url)
                        self.citations.setdefault(key, []).append(citation.cited_text)
                        self.result_urls.add(key)

    def cited(self, url: str, quote: str) -> bool:
        texts = self.citations.get(canonical_url(url), [])
        return any(quote_found(quote, normalize(t).replace("-", " ")) for t in texts)

    def was_returned(self, url: str) -> bool:
        return canonical_url(url) in self.result_urls


def fetch_url(url: str, timeout: float = 20) -> str:
    import requests

    try:
        response = requests.get(
            url,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept-Language": "es,pt-BR;q=0.9,en;q=0.8",
            },
        )
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise FetchBlocked(exc.__class__.__name__) from exc
    if response.status_code in BLOCKED_STATUS:
        raise FetchBlocked(f"HTTP {response.status_code}")
    response.raise_for_status()  # 404/410 etc.: link quebrado, a fonte não serve
    return response.text


@dataclass
class VerificationResult:
    verified: list[Source] = field(default_factory=list)
    methods: dict[str, str] = field(default_factory=dict)  # url -> page|citation|search_result
    problems: list[str] = field(default_factory=list)

    @property
    def domains(self) -> set[str]:
        return {base_domain(s.url) for s in self.verified}

    @property
    def ok(self) -> bool:
        # 2+ domínios, e pelo menos uma fonte com o trecho conferido de verdade
        # (na página ou na citação da busca), não só "a busca achou a URL".
        strong = any(m in ("page", "citation") for m in self.methods.values())
        return len(self.domains) >= 2 and strong


def verify_sources(
    sources: list[Source],
    fetcher: Callable[[str], str] = fetch_url,
    evidence: SearchEvidence | None = None,
) -> VerificationResult:
    """Confere cada fonte, do método mais forte ao mais fraco:

    1. citation: o trecho bate com o cited_text que a busca da Anthropic devolveu.
    2. page: a página abriu e o trecho está no texto visível.
    3. search_result: a página bloqueou o robô, mas a busca da Anthropic retornou
       essa URL. Só vale quando o acesso é BLOQUEADO; se a página abriu e o trecho
       não está lá, a fonte é reprovada (sinal de citação inventada).
    """
    evidence = evidence or SearchEvidence()
    result = VerificationResult()

    def accept(source: Source, method: str) -> None:
        result.verified.append(source)
        result.methods[source.url] = method

    for source in sources:
        if evidence.cited(source.url, source.supporting_quote):
            accept(source, "citation")
            continue
        try:
            page = _page_text(fetcher(source.url))
            if len(page) < MIN_PAGE_CHARS:
                raise FetchBlocked("página sem texto no HTML (provável JavaScript)")
        except FetchBlocked as exc:
            if evidence.was_returned(source.url):
                accept(source, "search_result")
            else:
                result.problems.append(
                    f"{source.url}: bloqueou a leitura ({exc}) e não veio da busca web_search"
                )
            continue
        except Exception as exc:  # 404, URL quebrada...
            result.problems.append(f"{source.url}: não abriu ({exc.__class__.__name__})")
            continue
        if quote_found(source.supporting_quote, page):
            accept(source, "page")
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


def resolve_backend() -> str:
    """Quem escreve o roteiro: "api" (chave da API, cobrada no console da Anthropic) ou
    "claude_code" (o Claude Code instalado e logado neste PC, usando a sua assinatura).
    Sem ANTHROPIC_BACKEND: usa a API se houver ANTHROPIC_API_KEY, senão o Claude Code."""
    backend = (os.getenv("ANTHROPIC_BACKEND") or "").strip().lower()
    if not backend:
        backend = "api" if os.getenv("ANTHROPIC_API_KEY") else "claude_code"
    if backend not in ("api", "claude_code"):
        raise StoryEngineError(f"ANTHROPIC_BACKEND inválido: {backend!r} (use api ou claude_code)")
    return backend


_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")
CLAUDE_CODE_TIMEOUT_SECONDS = 900
CLAUDE_CODE_NOTE = """

Ferramentas disponíveis aqui: WebSearch (pesquisa) e WebFetch (abrir uma página). Use as duas
para confirmar os fatos. Não há outras ferramentas. A resposta final deve ser só o JSON."""


def find_claude_binary() -> str | None:
    """Caminho do Claude Code. CLAUDE_BIN no .env tem prioridade.

    No Windows prefere o claude.exe nativo: o atalho claude.cmd do npm passa pelo
    cmd.exe, que pode estragar acentos e aspas dos argumentos.
    """
    configured = os.getenv("CLAUDE_BIN", "").strip()
    if configured:
        return configured
    if os.name == "nt":
        found = shutil.which("claude.exe")
        if found:
            return found
        native = Path.home() / ".local" / "bin" / "claude.exe"  # instalador nativo
        if native.exists():
            return str(native)
    return shutil.which("claude")


class ClaudeCodeBackend:
    """Roda o Claude Code deste PC (`claude -p`), já logado na sua conta.

    Usa o binário oficial com o login dele; o projeto nunca lê nem guarda o token.
    Só WebSearch e WebFetch ficam liberados; qualquer outra ferramenta é negada
    (--permission-mode dontAsk). Correções do validador continuam a mesma conversa
    (--resume), e as URLs devolvidas pela pesquisa entram como evidência.
    """

    def __init__(self, model: str | None = None, runner=subprocess.run,
                 claude_bin: str | None = None) -> None:
        self.bin = claude_bin or find_claude_binary()
        if not self.bin:
            raise StoryEngineError(
                "Claude Code não encontrado. Instale (irm https://claude.ai/install.ps1 | iex), "
                "rode 'claude' uma vez para fazer login, ou use ANTHROPIC_BACKEND=api"
            )
        self.model = model
        self.runner = runner
        self.session_id: str | None = None

    def ask(self, messages: list[dict], evidence: SearchEvidence) -> str:
        if len(messages) == 1:
            self.session_id = None  # nova conversa
        prompt = messages[-1]["content"]
        with tempfile.TemporaryDirectory() as tmp:
            system_file = Path(tmp) / "system.txt"
            system_file.write_text(SYSTEM_PROMPT + CLAUDE_CODE_NOTE, encoding="utf-8")
            cmd = [self.bin, "-p", "Siga as instruções recebidas pela entrada padrão.",
                   "--output-format", "stream-json", "--verbose",
                   "--append-system-prompt-file", str(system_file),
                   "--allowedTools", "WebSearch,WebFetch",
                   "--permission-mode", "dontAsk"]
            if self.model:
                cmd += ["--model", self.model]
            if self.session_id:
                cmd += ["--resume", self.session_id]
            try:
                # cwd vazio: o Claude Code não carrega CLAUDE.md/hooks de outra pasta.
                proc = self.runner(cmd, input=prompt, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", cwd=tmp,
                                   timeout=CLAUDE_CODE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as exc:
                raise ValueError("o Claude Code passou do tempo limite; tente de novo") from exc

        final = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("session_id"):
                self.session_id = event["session_id"]
            if event.get("type") == "result":
                final = event
            elif event.get("type") == "user":
                for block in (event.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        content = block.get("content")
                        text = content if isinstance(content, str) else " ".join(
                            c.get("text", "") for c in content or [] if isinstance(c, dict))
                        for url in _URL_RE.findall(text):
                            evidence.result_urls.add(canonical_url(url.rstrip(".,;")))

        if final is None:
            raise StoryEngineError(
                f"o Claude Code não respondeu (código {proc.returncode}): {proc.stderr.strip()[-400:]}")
        if final.get("is_error"):
            message = str(final.get("result") or final.get("subtype") or "erro")
            if any(k in message.lower() for k in ("login", "401", "oauth", "authenticat")):
                raise StoryEngineError(
                    f"o Claude Code não está logado ({message}). Abra um terminal, rode 'claude' "
                    "e faça /login; depois rode de novo")
            raise StoryEngineError(f"o Claude Code falhou: {message}")
        return str(final.get("result") or "")


class StoryEngine:
    def __init__(
        self,
        db: StoryDatabase,
        client=None,
        model: str | None = None,
        fetcher: Callable[[str], str] = fetch_url,
        output_dir: Path | None = None,
        backend: ClaudeCodeBackend | None = None,
    ) -> None:
        self.db = db
        self.fetcher = fetcher
        self.output_dir = output_dir or PROJECT_DIR / os.getenv("OUTPUT_DIR", "output")
        self.model = model or os.getenv("ANTHROPIC_MODEL")
        self.backend = backend
        if backend is None and client is None and resolve_backend() == "claude_code":
            self.backend = ClaudeCodeBackend(self.model or None)
        if self.backend is not None:
            return
        if not self.model:
            raise StoryEngineError("Defina ANTHROPIC_MODEL no .env")
        if client is None:
            import anthropic

            if not os.getenv("ANTHROPIC_API_KEY"):
                raise StoryEngineError("Defina ANTHROPIC_API_KEY no .env")
            # O SDK já repete sozinho erros de rede, 429 e 5xx com backoff.
            client = anthropic.Anthropic(max_retries=4, timeout=300)
        self.client = client

    # -- chamada ao modelo --------------------------------------------------

    def _ask(self, messages: list[dict], evidence: SearchEvidence) -> str:
        """Chama o modelo com web_search e devolve o texto final.

        Os resultados e citações da busca vão para `evidence`, usados depois na
        verificação das fontes. Buscas longas podem pausar (stop_reason ==
        "pause_turn"); nesse caso a conversa continua de onde parou.
        """
        if self.backend is not None:
            return self.backend.ask(messages, evidence)
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
            evidence.add_response(response.content)
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
        # As buscas de tentativas anteriores continuam valendo como evidência.
        evidence = SearchEvidence()
        messages = [{"role": "user",
                     "content": build_user_prompt(language, self.db.used_topics_summary(), excluded)}]

        last_error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            log.info("Tentativa %s/%s", attempt, MAX_ATTEMPTS)
            try:
                raw = self._ask(messages, evidence)
            except ValueError as exc:  # resposta cortada
                last_error = str(exc)
                messages = messages[:1]
                continue

            feedback = self._check(raw, language, excluded, evidence)
            if not isinstance(feedback, str):
                episode, methods = feedback
                return self._save(episode, day, methods)

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

    def _check(
        self, raw: str, language: str, excluded: list[str], evidence: SearchEvidence
    ) -> tuple[Episode, dict[str, str]] | str:
        """Devolve (episódio aprovado, método de verificação por URL) ou o erro para o modelo corrigir."""
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

        if episode.track != "fact":
            return episode, {}

        check = verify_sources(episode.sources, self.fetcher, evidence)
        if not check.ok:
            problems = "\n".join(f"- {p}" for p in check.problems) or "- nenhum problema de acesso"
            return ("A verificação automática das fontes falhou. É preciso 2+ fontes de domínios "
                    "diferentes, e pelo menos uma com o trecho citado conferido na página ou na "
                    f"citação da busca:\n{problems}\n"
                    "Pesquise de novo, troque as fontes ou retire os dados que não se confirmam.")
        unverified = {s.url for s in episode.sources} - {s.url for s in check.verified}
        for claim in episode.claims:
            if not set(claim.source_urls) - unverified:
                return (f"A afirmação '{claim.claim}' só cita fontes que não foram confirmadas. "
                        "Apoie-a em uma fonte confirmada ou retire do roteiro.")
        episode.sources = check.verified
        for url, method in check.methods.items():
            log.info("Fonte confirmada (%s): %s", method, url)
        return episode, check.methods

    def _save(self, episode: Episode, day: date, methods: dict[str, str]) -> Path:
        topic = self.db.reserve_topic(
            episode.topic_title, episode.entity, episode.category, episode.track, day
        )
        for source in episode.sources:
            self.db.add_source(topic.id, source.url, source.title, methods.get(source.url))
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
            "source_verification": methods,
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
