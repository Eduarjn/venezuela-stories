"""Imagens reais, grátis e com licença de uso para cada cena (sem IA, custo zero).

Fontes, nesta ordem:
1. Wikimedia Commons (sem chave): fotos e pinturas históricas. Aceita domínio público,
   CC0 e CC BY (crédito no post). CC BY-SA só com ALLOW_SHARE_ALIKE=1.
2. Pexels (chave grátis em pexels.com/api, opcional): fotos genéricas de alta qualidade,
   licença Pexels (uso comercial liberado; o post credita o fotógrafo e a Pexels).

Imagens sem licença clara, com "NC" (não comercial) ou "ND" (sem derivações) nunca entram.
"""

from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import asdict, dataclass
from typing import Callable
from urllib.parse import unquote

log = logging.getLogger("image_sources")

USER_AGENT = "venezuela-stories/1.0 (gerador de vídeos educativos; https://github.com/Eduarjn/venezuela-stories)"
MIN_SHORT_SIDE = 600
SEARCH_LIMIT = 20  # muitos resultados do Commons são PDF, SVG ou vídeo e são descartados
DOWNLOAD_WIDTH = 1280  # tamanho padrão de miniatura do Wikimedia (tamanhos fora do padrão são limitados)
# Próximo tamanho padrão abaixo de 1280 (https://www.mediawiki.org/wiki/Common_thumbnail_sizes).
SMALLER_STANDARD_WIDTH = 960


@dataclass
class Candidate:
    source: str            # "wikimedia" | "pexels"
    download_url: str
    page_url: str
    title: str
    author: str
    license: str           # nome curto, ex.: "Public domain", "CC BY 4.0", "Pexels License"
    license_url: str
    width: int
    height: int
    attribution_required: bool
    kind: str = "Imagem"   # "Imagem" ou "Vídeo" (filmagem real numa cena)

    def license_link(self) -> str:
        """URL da licença: CC BY (2.0, 3.0 e 4.0) exige o link, não só o nome."""
        if self.license_url:
            return self.license_url
        match = re.search(r"cc[ -]by(-sa)?[ -](\d\.\d)", self.license.lower())
        return f"https://creativecommons.org/licenses/by{match.group(1) or ''}/{match.group(2)}/" if match else ""

    def credit_line(self) -> str:
        if self.source == "pexels":
            return f"Foto: {self.author} / Pexels ({self.page_url})"
        # O Commons às vezes repete o autor ("Unknown author Unknown author").
        author = re.sub(r"^(.+?) \1$", r"\1", self.author) or "autor desconhecido"
        license = self.license
        if self.attribution_required and self.license_link():
            license += f" ({self.license_link()})"
        return f"{self.kind}: \"{self.title}\" — {author} — {license} — {self.page_url}"

    def to_dict(self) -> dict:
        return asdict(self)


RETRY_STATUS = {429, 503}
MAX_TRIES = 4
MAX_WAIT_SECONDS = 30.0


def _request(url: str, *, sleep: Callable[[float], None] | None = None, **kwargs):
    """GET que espera e tenta de novo quando o servidor pede calma (429/503).

    O Wikimedia responde 429 quando várias buscas chegam em sequência (comum em IP
    compartilhado, como a nuvem). Sem isso, todas as cenas falham e o episódio
    parece não ter imagem livre nenhuma.
    """
    import time

    import requests

    sleep = sleep or time.sleep
    for attempt in range(1, MAX_TRIES + 1):
        response = requests.get(url, **kwargs)
        if response.status_code not in RETRY_STATUS or attempt == MAX_TRIES:
            response.raise_for_status()
            return response
        try:
            wait = float(response.headers.get("Retry-After", ""))
        except ValueError:
            wait = 2.0 ** attempt
        wait = min(max(wait, 1.0), MAX_WAIT_SECONDS)
        log.info("%s respondeu %s; nova tentativa em %.0fs", url.split("?")[0], response.status_code, wait)
        sleep(wait)
    raise AssertionError("inalcançável")


def _get_json(url: str, params: dict, headers: dict) -> dict:
    response = _request(url, params=params, headers={"User-Agent": USER_AGENT, **headers}, timeout=20)
    return response.json()


def download(url: str) -> bytes:
    response = _request(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    return response.content


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def classify_license(code: str, short_name: str) -> str | None:
    """'pd', 'by', 'by-sa' ou None (proibida ou desconhecida)."""
    text = f"{code} {short_name}".lower().replace("_", "-")
    if re.search(r"\b(nc|nd)\b|-nc\b|-nd\b|noncommercial|non-commercial|noderiv|no-deriv", text):
        return None
    if "cc0" in text or "public domain" in text or re.search(r"(^|\s)pd\b|^pd-", text):
        return "pd"
    if re.search(r"by-sa|by sa|attribution-sharealike|attribution share ?alike", text):
        return "by-sa"
    if re.search(r"cc-by|cc by|attribution", text):
        return "by"
    return None


def _download_url(info: dict) -> str:
    """URL de download em tamanho padrão de miniatura do Wikimedia.

    Para foto mais estreita que DOWNLOAD_WIDTH a API devolve o arquivo original, e o
    Wikimedia limita o download de originais (429 "use thumbnail images in sizes listed
    on https://w.wiki/GHai"). Nesse caso usa a miniatura padrão de 960 px, desde que ela
    continue com o lado menor >= MIN_SHORT_SIDE; senão fica o original.
    """
    thumb = info.get("thumburl") or ""
    original = info.get("url") or ""
    if "/thumb/" in thumb or not original:
        return thumb or original
    width, height = int(info.get("width") or 0), int(info.get("height") or 0)
    if width < SMALLER_STANDARD_WIDTH or not height:
        return thumb or original
    scaled_short = min(SMALLER_STANDARD_WIDTH, round(height * SMALLER_STANDARD_WIDTH / width))
    marker = "/wikipedia/commons/"
    if scaled_short < MIN_SHORT_SIDE or marker not in original:
        return thumb or original
    path = original.split("?", 1)[0].split(marker, 1)[1]  # ex.: b/bf/Nome.jpg
    name = path.rsplit("/", 1)[-1]
    return f"https://thumb.wikimedia.org{marker}thumb/{path}/{SMALLER_STANDARD_WIDTH}px-{name}"


class WikimediaSource:
    name = "wikimedia"
    API = "https://commons.wikimedia.org/w/api.php"
    PHOTO_MIMES = ("image/jpeg",)  # fotos e digitalizações; evita mapas/diagramas em SVG/PNG

    def __init__(self, allow_share_alike: bool = False,
                 get_json: Callable[[str, dict, dict], dict] = _get_json) -> None:
        self.allow_share_alike = allow_share_alike
        self.get_json = get_json

    def search(self, query: str) -> list[Candidate]:
        data = self.get_json(self.API, {
            "action": "query", "format": "json", "formatversion": "2",
            "generator": "search", "gsrsearch": query, "gsrnamespace": "6",
            "gsrlimit": str(SEARCH_LIMIT), "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata", "iiurlwidth": str(DOWNLOAD_WIDTH),
        }, {})
        pages = sorted((data.get("query") or {}).get("pages") or [], key=lambda p: p.get("index", 0))
        results = []
        for page in pages:
            info = (page.get("imageinfo") or [{}])[0]
            if info.get("mime") not in self.PHOTO_MIMES:
                continue
            meta = info.get("extmetadata") or {}
            value = lambda key: (meta.get(key) or {}).get("value") or ""  # noqa: E731
            kind = classify_license(value("License"), value("LicenseShortName"))
            if kind is None or (kind == "by-sa" and not self.allow_share_alike):
                continue
            title = re.sub(r"^File:|\.[a-zA-Z0-9]+$", "", page.get("title", "")).strip()
            results.append(Candidate(
                source=self.name,
                download_url=_download_url(info),
                page_url=info.get("descriptionurl") or "",
                title=title,
                author=_strip_html(value("Artist")) or _strip_html(value("Credit")),
                license=value("LicenseShortName") or ("Public domain" if kind == "pd" else kind.upper()),
                license_url=value("LicenseUrl"),
                width=int(info.get("width") or 0),
                height=int(info.get("height") or 0),
                attribution_required=kind != "pd",
            ))
        return results


class PexelsSource:
    name = "pexels"
    generic = True  # banco de fotos: melhor para buscas genéricas ("lightning storm at night")
    API = "https://api.pexels.com/v1/search"

    def __init__(self, api_key: str, get_json: Callable[[str, dict, dict], dict] = _get_json) -> None:
        self.api_key = api_key
        self.get_json = get_json

    def search(self, query: str) -> list[Candidate]:
        data = self.get_json(self.API, {"query": query, "per_page": str(SEARCH_LIMIT),
                                        "orientation": "portrait"},
                             {"Authorization": self.api_key})
        results = []
        for photo in data.get("photos") or []:
            src = photo.get("src") or {}
            results.append(Candidate(
                source=self.name,
                download_url=src.get("large2x") or src.get("original") or "",
                page_url=photo.get("url") or "",
                title=photo.get("alt") or query,
                author=photo.get("photographer") or "",
                license="Pexels License",
                license_url="https://www.pexels.com/license/",
                width=int(photo.get("width") or 0),
                height=int(photo.get("height") or 0),
                attribution_required=False,
            ))
        return results


def region_from_env() -> str:
    """Região que amarra as buscas genéricas no acervo (IMAGE_REGION; vazio desliga)."""
    return os.getenv("IMAGE_REGION", "Venezuela").strip()


def share_alike_allowed() -> bool:
    return os.getenv("ALLOW_SHARE_ALIKE", "0").strip().lower() in ("1", "true", "sim", "yes")


def wikimedia_video(ref: str, allow_share_alike: bool = False,
                    get_json: Callable[[str, dict, dict], dict] = _get_json) -> tuple[Candidate, list[str]]:
    """Filmagem real do Wikimedia Commons, pelo nome ("File:X.webm") ou pelo link da página.

    Devolve o crédito e as URLs para baixar, da melhor para a pior: o original primeiro (o
    Commons costuma limitar originais com 429) e depois as versões convertidas até 1080p.
    Mesma regra de licença das imagens: nada de NC/ND; CC BY-SA só com allow_share_alike.
    """
    name = ref.split("/wiki/", 1)[-1].split("?", 1)[0].split("#", 1)[0]
    title = unquote(name).replace("_", " ").strip()
    if not title.lower().startswith("file:"):
        title = f"File:{title}"
    data = get_json(WikimediaSource.API, {
        "action": "query", "format": "json", "formatversion": "2", "titles": title,
        "prop": "videoinfo", "viprop": "url|size|mime|extmetadata|derivatives",
    }, {})
    page = ((data.get("query") or {}).get("pages") or [{}])[0]
    info = (page.get("videoinfo") or [None])[0]
    if not info:
        raise ValueError(f"{title}: arquivo não encontrado no Wikimedia Commons")
    if not str(info.get("mime", "")).startswith("video/"):
        raise ValueError(f"{title}: não é vídeo ({info.get('mime')})")
    meta = info.get("extmetadata") or {}
    value = lambda key: (meta.get(key) or {}).get("value") or ""  # noqa: E731
    kind = classify_license(value("License"), value("LicenseShortName"))
    if kind is None:
        raise ValueError(f"{title}: licença não permite uso ({value('LicenseShortName') or 'sem licença'})")
    if kind == "by-sa" and not allow_share_alike:
        raise ValueError(f"{title}: licença CC BY-SA; só com ALLOW_SHARE_ALIKE=1 no .env")
    converted = sorted((d for d in info.get("derivatives") or []
                        if d.get("src") and 0 < int(d.get("height") or 0) <= 1080),
                       key=lambda d: int(d.get("height") or 0), reverse=True)
    urls = list(dict.fromkeys(u.split("?", 1)[0] for u in [info.get("url") or ""] + [d["src"] for d in converted]
                              if u))
    candidate = Candidate(
        source="wikimedia",
        download_url=urls[0],
        page_url=info.get("descriptionurl") or "",
        title=re.sub(r"^File:|\.[a-zA-Z0-9]+$", "", page.get("title") or title).strip(),
        author=_strip_html(value("Artist")) or _strip_html(value("Credit")),
        license=value("LicenseShortName") or ("Public domain" if kind == "pd" else kind.upper()),
        license_url=value("LicenseUrl"),
        width=int(info.get("width") or 0),
        height=int(info.get("height") or 0),
        attribution_required=kind != "pd",
        kind="Vídeo",
    )
    return candidate, urls


def sources_from_env() -> list:
    sources: list = [WikimediaSource(allow_share_alike=share_alike_allowed())]
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if key:
        sources.append(PexelsSource(key))
    return sources


class StockImageFinder:
    """Procura, por ordem de consultas e fontes, a primeira imagem boa ainda não usada."""

    def __init__(self, sources: list, fetch: Callable[[str], bytes] = download,
                 min_short_side: int = MIN_SHORT_SIDE, region: str = "") -> None:
        self.sources = sources
        self.fetch = fetch
        self.min_short_side = min_short_side
        self.region = region.strip()
        # Cada busca roda uma vez por episódio: as cenas vizinhas reaproveitam os resultados.
        self._results: dict[tuple[str, str], list[Candidate]] = {}
        self._failed_downloads: set[str] = set()

    def _search(self, source, query: str) -> list[Candidate]:
        key = (source.name, query.lower())
        if key not in self._results:
            try:
                self._results[key] = source.search(query)
            except Exception as exc:  # rede, cota, chave errada: tenta a próxima fonte
                log.warning("%s falhou para '%s': %s", source.name, query, exc)
                self._results[key] = []
        return self._results[key]

    def query_for(self, source, query: str) -> str:
        """Busca genérica (toda em minúsculas) no acervo ganha a região: "calm lake at dawn"
        sozinha trazia lago dos Pirineus, submarino americano, ponte do Oregon. Sem resultado
        na região, a cena cai no reaproveitamento de imagem das cenas vizinhas. O banco de
        fotos (Pexels) recebe a busca original: foto de banco não mostra lugar reconhecível."""
        if self.region and query.islower() and not getattr(source, "generic", False):
            return f"{query} {self.region}"
        return query

    def _sources_for(self, query: str) -> list:
        # Busca sem nome próprio ("lightning storm at night"): banco de fotos primeiro (Pexels:
        # vertical, sem crédito obrigatório). Com nome próprio ("Lake Maracaibo"): acervo primeiro.
        if query.islower():
            return sorted(self.sources, key=lambda s: not getattr(s, "generic", False))
        return self.sources

    def find(self, queries: list[str], used: set[str]) -> tuple[Candidate, bytes] | None:
        for query in queries:
            for source in self._sources_for(query):
                for candidate in self._search(source, self.query_for(source, query)):
                    if candidate.page_url in used or not candidate.download_url:
                        continue
                    if candidate.download_url in self._failed_downloads:
                        continue
                    if min(candidate.width, candidate.height) < self.min_short_side:
                        continue
                    try:
                        data = self.fetch(candidate.download_url)
                    except Exception as exc:
                        log.warning("download falhou (%s): %s", candidate.download_url, exc)
                        self._failed_downloads.add(candidate.download_url)
                        continue
                    log.info("'%s' -> %s (%s, %s)", query, candidate.title, candidate.source, candidate.license)
                    return candidate, data
        return None


_STYLE_WORDS = {"Cinematic", "Scene", "No", "Dramatic", "Wide", "Close", "Aerial", "Medium", "Low",
                "High", "A", "An", "The", "Night", "Silhouette", "Silhouettes"}


def scene_queries(scene: dict, episode: dict) -> list[str]:
    """Consultas para a cena, da mais específica para a mais genérica."""
    queries: list[str] = []
    if scene.get("image_query"):
        queries.append(scene["image_query"].strip())
    # Nomes próprios do prompt de imagem (ex.: "Lake Maracaibo", "Catatumbo River").
    prompt = re.split(r"cinematic historical illustration", scene.get("image_prompt", ""), flags=re.I)[0]
    for phrase in re.findall(r"[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s+(?:de|del|la|of|the)?\s*[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)+", prompt):
        words = [w for w in phrase.split() if w not in _STYLE_WORDS]
        if len(words) >= 2:
            queries.append(" ".join(words))
    for key in ("entity", "topic_title"):
        if episode.get(key):
            queries.append(str(episode[key]))
    seen, unique = set(), []
    for q in queries:
        k = q.lower()
        if q and k not in seen:
            seen.add(k)
            unique.append(q)
    return unique
