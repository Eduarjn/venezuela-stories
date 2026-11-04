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
HTML_SEARCH_LIMIT = 8  # busca pela página do Commons: lê a página de até 8 arquivos JPEG


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
# A API do Commons limitada (429) costuma continuar assim por minutos: nesse intervalo as
# buscas vão direto à página de busca, sem esperar a nova tentativa de cada cena.
API_COOLDOWN_SECONDS = 600.0


def _request(url: str, *, sleep: Callable[[float], None] | None = None, tries: int = MAX_TRIES, **kwargs):
    """GET que espera e tenta de novo quando o servidor pede calma (429/503).

    O Wikimedia responde 429 quando várias buscas chegam em sequência (comum em IP
    compartilhado, como a nuvem). Sem isso, todas as cenas falham e o episódio
    parece não ter imagem livre nenhuma.
    """
    import time

    import requests

    sleep = sleep or time.sleep
    for attempt in range(1, tries + 1):
        response = requests.get(url, **kwargs)
        if response.status_code not in RETRY_STATUS or attempt == tries:
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


def _get_json(url: str, params: dict, headers: dict, tries: int = MAX_TRIES) -> dict:
    response = _request(url, params=params, headers={"User-Agent": USER_AGENT, **headers}, timeout=20, tries=tries)
    return response.json()


def _get_json_quick(url: str, params: dict, headers: dict) -> dict:
    """Busca na API do Commons com 1 nova tentativa só: se continuar limitada (429), a busca
    vai para a página do Commons, que responde mesmo quando a API não responde."""
    return _get_json(url, params, headers, tries=2)


def _get_html(url: str, params: dict) -> str:
    return _request(url, params=params, headers={"User-Agent": USER_AGENT}, timeout=30).text


def _rate_limited(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in RETRY_STATUS


UPLOAD_HOST = "upload.wikimedia.org"


def commons_thumb_php(url: str, width: int = SMALLER_STANDARD_WIDTH) -> str | None:
    """Mesma imagem pelo thumb.php do Commons (commons.wikimedia.org), para quando o
    upload.wikimedia.org devolve 429 a um IP compartilhado (nuvem). Só para imagens."""
    if UPLOAD_HOST not in url:
        return None
    path = url.split("?", 1)[0].split("/wikipedia/commons/", 1)[-1]
    match = re.match(r"thumb/[0-9a-f]/[0-9a-f]{2}/([^/]+)/(\d+)px-", path)
    if match:
        name, width = match.group(1), int(match.group(2))
    else:
        match = re.match(r"[0-9a-f]/[0-9a-f]{2}/([^/]+)$", path)
        if not match:
            return None
        name = match.group(1)
    if not re.search(r"\.(jpe?g|png|gif|tiff?|webp)$", name, re.I):
        return None
    return f"https://commons.wikimedia.org/w/thumb.php?f={name}&w={width}"


def download(url: str) -> bytes:
    if UPLOAD_HOST in url:  # 1 tentativa; com 429 vai direto para o thumb.php do Commons
        import requests

        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
        if response.status_code == 200:
            return response.content
        fallback = commons_thumb_php(url) if response.status_code == 429 else None
        if fallback:
            log.info("%s pediu calma (429); baixando pelo thumb.php do Commons", UPLOAD_HOST)
            url = fallback
    response = _request(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    return response.content


def _strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def classify_license(code: str, short_name: str) -> str | None:
    """'pd', 'by', 'by-sa' ou None (proibida ou desconhecida)."""
    text = f"{code} {short_name}".lower().replace("_", "-")
    if re.search(r"\b(nc|nd)\b|-nc\b|-nd\b|noncommercial|non-commercial|noderiv|no-deriv", text):
        return None
    # Domínio público também com outros rótulos do Commons: PDM (Public Domain Mark, comum
    # nas digitalizações da Biblioteca do Congresso) e "No restrictions" (Flickr Commons).
    if ("cc0" in text or "public domain" in text or re.search(r"(^|\s)pdm?\b|^pd-", text)
            or "no restrictions" in text or "no known copyright" in text):
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
    MEDIA_SEARCH = "https://commons.wikimedia.org/w/index.php"
    FILE_PAGE = "https://commons.wikimedia.org/wiki/"
    PHOTO_MIMES = ("image/jpeg",)  # fotos e digitalizações; evita mapas/diagramas em SVG/PNG

    def __init__(self, allow_share_alike: bool = False,
                 get_json: Callable[[str, dict, dict], dict] = _get_json_quick,
                 get_html: Callable[[str, dict], str] = _get_html) -> None:
        self.allow_share_alike = allow_share_alike
        self.get_json = get_json
        self.get_html = get_html
        self.api_limited_until = 0.0  # depois de um 429 da API, vai direto à página de busca

    def search(self, query: str) -> list[Candidate]:
        import time

        # No Commons, "-palavra" quer dizer "sem essa palavra": o título "USASC -22343" buscado
        # assim mesmo excluía o próprio arquivo. O hífen solto (" - ") não muda nada.
        query = re.sub(r"(^|\s)-(?=\w)", r"\1", query)
        if time.monotonic() < self.api_limited_until:
            return self.search_html(query)
        try:
            data = self.get_json(self.API, {
                "action": "query", "format": "json", "formatversion": "2",
                "generator": "search", "gsrsearch": query, "gsrnamespace": "6",
                "gsrlimit": str(SEARCH_LIMIT), "prop": "imageinfo",
                "iiprop": "url|size|mime|extmetadata", "iiurlwidth": str(DOWNLOAD_WIDTH),
            }, {})
        except Exception as exc:  # noqa: BLE001 - API limitada: a página de busca responde
            if not _rate_limited(exc):
                raise
            log.info("API do Commons limitada (429); buscando '%s' pela página de busca "
                     "(e as próximas %.0f min também)", query, API_COOLDOWN_SECONDS / 60)
            self.api_limited_until = time.monotonic() + API_COOLDOWN_SECONDS
            return self.search_html(query)
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

    def search_html(self, query: str, limit: int = HTML_SEARCH_LIMIT) -> list[Candidate]:
        """A mesma busca pela página do Commons (Special:MediaSearch) e a página de cada
        arquivo: licença, autor e tamanho saem dos campos que o Commons põe em toda página.
        Mesmas regras da API: só JPEG, só licença livre, CC BY-SA só com permissão."""
        page = self.get_html(self.MEDIA_SEARCH, {"title": "Special:MediaSearch", "type": "image", "search": query})
        titles = list(dict.fromkeys(unquote(t).replace("_", " ")
                                    for t in re.findall(r'/wiki/(File:[^"?#<>]+)', page)))
        results = []
        for title in [t for t in titles if re.search(r"\.jpe?g$", t, re.I)][:limit]:
            try:
                candidate = candidate_from_file_page(title, self.get_html(self.FILE_PAGE + title.replace(" ", "_"), {}),
                                                     self.allow_share_alike)
            except Exception as exc:  # noqa: BLE001 - uma página fora do ar não para a busca
                log.warning("página de %s falhou: %s", title, exc)
                continue
            if candidate is not None:
                results.append(candidate)
        return results


def _field(page: str, name: str) -> list[str]:
    """Valores dos campos licensetpl_* da página de um arquivo do Commons."""
    pattern = r'class="licensetpl(?:&#95;|_)' + name.replace("_", r"(?:&#95;|_)") + r'"[^>]*>([^<]*)'
    return [html.unescape(v).strip() for v in re.findall(pattern, page)]


def _file_page_author(page: str) -> str:
    """Autor da página de um arquivo: o nome do criador (obras de arte) ou o campo Author."""
    cell = re.search(r'id="fileinfotpl(?:&#95;|_)aut"[^>]*>.*?</td>\s*<td[^>]*>(.*?)</td>', page, re.S)
    if not cell:
        return ""
    value = re.sub(r"<style[^>]*>.*?</style>", " ", cell.group(1), flags=re.S)
    creator = re.search(r'id="creator"[^>]*>(.*?)</span>\s*</a>|id="creator"[^>]*>(.*?)</span>', value, re.S)
    text = _strip_html(next((g for g in creator.groups() if g), "")) if creator else ""
    return (text or _strip_html(value))[:120]


def candidate_from_file_page(title: str, page: str, allow_share_alike: bool = False) -> Candidate | None:
    """Candidate a partir da página HTML de um arquivo do Commons (None: licença ou arquivo
    que não serve)."""
    shorts = list(dict.fromkeys(s for s in _field(page, "short") if s))
    kinds = {classify_license(s, s) for s in shorts}
    kind = next((k for k in ("pd", "by", "by-sa") if k in kinds), None)
    if kind is None or (kind == "by-sa" and not allow_share_alike):
        return None
    original = re.search(r'href="(https://upload\.wikimedia\.org/wikipedia/commons/[0-9a-f]/[0-9a-f]{2}/[^"?]+)', page)
    size = re.search(r"\(([\d,.]+) × ([\d,.]+) pixels, file size", page)
    if not original or not size:
        return None
    width, height = (int(re.sub(r"\D", "", size.group(i))) for i in (1, 2))
    license = next((s for s in shorts if classify_license(s, s) == kind), shorts[0])
    links = _field(page, "link")
    marker = {"by": "/licenses/by/", "by-sa": "/licenses/by-sa/"}.get(kind, "")
    license_url = next((u for u in links if marker and marker in u), "")
    path = original.group(1).split("/wikipedia/commons/", 1)[1]  # ex.: a/a4/Nome.jpg
    # Como a API: miniatura padrão de 1280 px quando a foto é maior que isso; senão o original.
    thumb = (f"https://upload.wikimedia.org/wikipedia/commons/thumb/{path}/{DOWNLOAD_WIDTH}px-{path.rsplit('/', 1)[-1]}"
             if width > DOWNLOAD_WIDTH else original.group(1))
    name = re.sub(r"^File:|\.[a-zA-Z0-9]+$", "", title).strip()
    return Candidate(
        source="wikimedia",
        download_url=_download_url({"url": original.group(1), "thumburl": thumb, "width": width, "height": height}),
        page_url=f"https://commons.wikimedia.org/wiki/{title.replace(' ', '_')}",
        title=name,
        author=_file_page_author(page),
        license=license,
        license_url=license_url,
        width=width,
        height=height,
        attribution_required=kind != "pd",
    )


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
    """Filmagem real do Wikimedia Commons, pelo nome ("File:X.webm") ou pelo link da página."""
    return wikimedia_media(ref, allow_share_alike, get_json, kind="video")


def wikimedia_media(ref: str, allow_share_alike: bool = False,
                    get_json: Callable[[str, dict, dict], dict] = _get_json,
                    kind: str = "video") -> tuple[Candidate, list[str]]:
    """Vídeo (kind="video") ou som (kind="audio") do Wikimedia Commons, pelo nome ou link.

    Devolve o crédito e as URLs para baixar, da melhor para a pior: o original primeiro (o
    Commons costuma limitar originais com 429) e depois as versões convertidas (vídeo: até
    1080p). Mesma regra de licença das imagens: nada de NC/ND; CC BY-SA só com allow_share_alike.
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
    mime = str(info.get("mime", ""))
    if kind == "video" and not mime.startswith("video/"):
        raise ValueError(f"{title}: não é vídeo ({mime})")
    if kind == "audio" and not (mime.startswith("audio/") or mime == "application/ogg"):
        raise ValueError(f"{title}: não é som ({mime})")
    meta = info.get("extmetadata") or {}
    value = lambda key: (meta.get(key) or {}).get("value") or ""  # noqa: E731
    license_kind = classify_license(value("License"), value("LicenseShortName"))
    if license_kind is None:
        raise ValueError(f"{title}: licença não permite uso ({value('LicenseShortName') or 'sem licença'})")
    if license_kind == "by-sa" and not allow_share_alike:
        raise ValueError(f"{title}: licença CC BY-SA; só com ALLOW_SHARE_ALIKE=1 no .env")
    converted = sorted((d for d in info.get("derivatives") or []
                        if d.get("src") and (kind == "audio" or 0 < int(d.get("height") or 0) <= 1080)),
                       key=lambda d: int(d.get("height") or 0), reverse=True)
    urls = list(dict.fromkeys(u.split("?", 1)[0] for u in [info.get("url") or ""] + [d["src"] for d in converted]
                              if u))
    candidate = Candidate(
        source="wikimedia",
        download_url=urls[0],
        page_url=info.get("descriptionurl") or "",
        title=re.sub(r"^File:|\.[a-zA-Z0-9]+$", "", page.get("title") or title).strip(),
        author=_strip_html(value("Artist")) or _strip_html(value("Credit")),
        license=value("LicenseShortName") or ("Public domain" if license_kind == "pd" else license_kind.upper()),
        license_url=value("LicenseUrl"),
        width=int(info.get("width") or 0),
        height=int(info.get("height") or 0),
        attribution_required=license_kind != "pd",
        kind="Vídeo" if kind == "video" else "Som",
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
