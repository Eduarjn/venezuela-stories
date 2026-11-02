"""Módulo 5 – Publicação automática no TikTok, Instagram, Facebook (e YouTube, se quiser).

Usa o Upload-Post (upload-post.com): você conecta as contas no painel deles uma vez, e cada
vídeo sobe numa chamada só, com a legenda própria de cada rede. (Pelas APIs oficiais, o
TikTok só deixa um app próprio postar vídeo público depois de uma auditoria, e
Instagram/Facebook pedem um app de desenvolvedor da Meta.) No .env (nunca no chat):

    UPLOAD_POST_API_KEY=...            (chave da API, no painel do Upload-Post)
    UPLOAD_POST_USER=...               (o perfil, em Manage Users, onde as redes estão conectadas)
    UPLOAD_POST_FACEBOOK_PAGE_ID=      (só se houver mais de uma página do Facebook conectada)
    PUBLICAR_REDES=tiktok,instagram,facebook        (acrescente youtube se quiser)
    ROTULO_IA=1                        (marca "gerado por IA": a narração é voz sintética)

Uso, sempre depois da revisão do vídeo:
    python publicar.py --date 2026-11-02 --teste      # mostra o que vai mandar, sem mandar
    python publicar.py --date 2026-11-02              # publica agora e espera a confirmação
    python publicar.py --date 2026-11-02 --quando "2026-11-02 19:00"     # agenda
    python publicar.py --agenda 2026-11-02 2026-11-03 2026-11-04 --inicio "2026-11-03 19:00"
        # rotina: 1 vídeo por dia no mesmo horário; o Upload-Post publica na hora marcada,
        # mesmo com o computador desligado
    python publicar.py --agenda 2026-11-05 2026-11-06 2026-11-07 2026-11-08 --inicio 2026-11-03 --horarios 12:00 19:00
        # 2 (ou mais) vídeos por dia: um em cada horário, a partir do dia de --inicio; horário
        # que já passou fica para o dia seguinte
    python publicar.py --status 2026-11-02            # o que já saiu em cada rede

Fila (para rodar todo dia no agendador do Windows ou numa rotina na nuvem): todo vídeo da
pasta que ainda não foi agendado ganha o próximo horário livre; o que já foi fica como está.
    python publicar.py --pasta "C:\\Downloads\\PRONTO_PARA_POSTAR" --horarios 12:00 19:00

Vídeo baixado (gerado em outro computador): o .mp4, o AAAA-MM-DD_postar.txt e, se tiver, o
AAAA-MM-DD_capa.jpg na mesma pasta.
    python publicar.py --video "C:\\Downloads\\2026-11-02_titulo.mp4" --quando "2026-11-02 19:00"
    python publicar.py --agenda-videos v1.mp4 v2.mp4 v3.mp4 --inicio "2026-11-03 19:00"
    python publicar.py --agenda-videos v1.mp4 v2.mp4 v3.mp4 v4.mp4 --inicio 2026-11-03 --horarios 12:00 19:00
    python publicar.py --status "C:\\Downloads\\2026-11-02_titulo.mp4"

Vídeo longo (mais de 1 minuto) sobe em todas as redes: TikTok até 10 min, Reels do
Instagram até 15 min, YouTube Shorts até 3 min (mais que isso vira vídeo normal do canal).
Reels do Facebook vão até 90 s; acima disso o vídeo entra como vídeo normal da página, com a
capa. Vídeo acima do limite de uma rede não sobe nela (as outras seguem) e o motivo aparece.

O resultado fica em output/AAAA-MM-DD/publicacao.json (ou <vídeo>.publicacao.json). Rodar de
novo só manda para as redes que falharam: o que já foi não duplica. Com todas as redes ok,
o tema é marcado como publicado no banco.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
API_URL = "https://api.upload-post.com/api/upload"
STATUS_URL = "https://api.upload-post.com/api/uploadposts/status"
NETWORKS = ("tiktok", "instagram", "facebook")
ALL_NETWORKS = (*NETWORKS, "youtube")
# Duração máxima de cada rede pela API (segundos). Acima disso o vídeo não sobe naquela rede.
MAX_SECONDS = {"tiktok": 600, "instagram": 900, "facebook": 4 * 3600, "youtube": 12 * 3600}
FACEBOOK_REELS_MAX = 90    # Reels do Facebook; acima disso vai como vídeo normal da página
YOUTUBE_SHORTS_MAX = 180   # o YouTube chama de Shorts o vídeo vertical de até 3 min
COVER_MAX_BYTES = 8 * 1024 * 1024  # capa do Instagram: JPEG de até 8 MB
POLL_SECONDS, POLL_LIMIT = 15, 600  # vídeo > 50 MB: consulta a cada 15 s, por até 10 min
FINISHED = ("completed", "failed", "not_found")
RUNNING = ("queued", "processing", "pending", "in_progress")
PROCESSING = "ainda processando no Upload-Post; confira depois com --status"

log = logging.getLogger("publicar")


class PublishError(RuntimeError):
    pass


@dataclass
class PostResult:
    network: str
    ok: bool
    detail: str
    scheduled_for: str | None = None
    url: str | None = None
    pending: bool = False  # o Upload-Post ainda está processando: não reenvia; confira com --status


def video_seconds(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def parse_when(text: str) -> datetime:
    """"2026-11-03 19:00" no fuso do computador (sai com o deslocamento, ex.: -03:00)."""
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        raise PublishError(f"horário inválido: {text!r} (use AAAA-MM-DD HH:MM, ex.: 2026-11-03 19:00)")


def parse_start(text: str) -> datetime:
    """Início da agenda: "2026-11-03 19:00" ou só o dia, "2026-11-03" (meia-noite)."""
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%d").astimezone()
    except ValueError:
        return parse_when(text)


def agenda_slots(start: datetime, count: int, every_hours: float = 24, times: list[str] | None = None,
                 now: datetime | None = None) -> list[datetime]:
    """Horário de cada vídeo da agenda. Com times (["12:00", "19:00"]): um vídeo em cada
    horário, dia após dia, a partir de start, pulando o que já passou. Sem times: start e
    depois a cada every_hours horas."""
    if not times:
        return [start + timedelta(hours=every_hours * i) for i in range(count)]
    try:
        clock = sorted({datetime.strptime(t.strip(), "%H:%M").time() for t in times})
    except ValueError:
        raise PublishError(f"horário inválido em --horarios: {' '.join(times)} (use HH:MM, ex.: 12:00 19:00)")
    first = max(start, (now or datetime.now(timezone.utc)) + timedelta(minutes=10))
    slots, day = [], start.date()
    while len(slots) < count:
        for moment in (datetime.combine(day, t).replace(tzinfo=start.tzinfo) for t in clock):
            if moment >= first and len(slots) < count:
                slots.append(moment)
        day += timedelta(days=1)
    return slots


def _minute(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(second=0, microsecond=0)


def folder_videos(folder: Path) -> list[Path]:
    """Vídeos prontos da pasta e das subpastas (com o AAAA-MM-DD_postar.txt ao lado), em ordem
    natural: a PRONTO_PARA_POSTAR já vem numerada na ordem de postagem (1_, 2_, ..., 10_)."""
    def natural(path: Path) -> list:
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", str(path.relative_to(folder)))]
    videos = [v for v in folder.rglob("*.mp4")
              if v.with_name(f"{v.name[:10]}_postar.txt").exists() or v.with_name("postar.txt").exists()]
    return sorted(videos, key=natural)


def queue_folder(folder: Path, times: list[str], networks: list[str] | None = None, start: datetime | None = None,
                 dry_run: bool = False, client=None, now: datetime | None = None
                 ) -> list[tuple[Path, datetime, list[PostResult]]]:
    """Fila: cada vídeo da pasta que ainda não foi para todas as redes ganha o próximo horário
    livre (um vídeo por horário de times). Rodar todo dia só agenda o que chegou de novo; um
    horário já ocupado por outro vídeo da pasta não é usado de novo."""
    folder = Path(folder)
    if not folder.is_dir():
        raise PublishError(f"{folder} não é uma pasta")
    if not times:
        raise PublishError("a fila precisa de --horarios (ex.: --horarios 12:00 19:00)")
    networks = networks or networks_from_env()
    taken: set[datetime] = set()
    pending: list[Path] = []
    for video in folder_videos(folder):
        record = _load_record(_record_path(None, video))
        sent = {r["network"] for r in record.get("results", []) if r.get("ok") or r.get("pending")}
        if set(networks) <= sent:
            if record.get("when") not in (None, "agora"):
                taken.add(_minute(datetime.fromisoformat(record["when"])))
            continue
        pending.append(video)
    if not pending:
        return []
    begin = start or datetime.now().astimezone()
    slots = [s for s in agenda_slots(begin, len(pending) + len(taken), times=times, now=now)
             if _minute(s) not in taken][:len(pending)]
    return [(video, when, publish_file(video, networks, when, dry_run=dry_run, client=client))
            for video, when in zip(pending, slots)]


def networks_from_env() -> list[str]:
    raw = os.getenv("PUBLICAR_REDES") or ",".join(NETWORKS)
    return check_networks([n.strip().lower() for n in raw.split(",") if n.strip()])


def check_networks(networks: list[str]) -> list[str]:
    unknown = sorted(set(networks) - set(ALL_NETWORKS))
    if unknown:
        raise PublishError(f"rede desconhecida: {', '.join(unknown)} (use {', '.join(ALL_NETWORKS)})")
    return networks


def facebook_kind(seconds: float) -> str:
    return "REELS" if seconds <= FACEBOOK_REELS_MAX else "VIDEO"


def _json(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {"message": response.text[:300]}
    return body if isinstance(body, dict) else {"message": str(body)[:300]}


def _by_platform(results) -> dict[str, dict]:
    """Resultado de cada rede: a resposta do envio traz um dicionário; a do status, uma lista."""
    if isinstance(results, dict):
        return {k: v for k, v in results.items() if isinstance(v, dict)}
    return {r["platform"]: r for r in results or [] if isinstance(r, dict) and r.get("platform")}


def _platform_result(network: str, answer: dict | None, when: str | None = None) -> PostResult:
    if answer is None:
        return PostResult(network, False, PROCESSING, when, pending=True)
    url = answer.get("url") or answer.get("post_url")
    if answer.get("skipped"):
        return PostResult(network, False, f"a conta do {network} não está conectada no perfil do Upload-Post "
                                          f"({answer.get('error', 'sem conta')})", when)
    if answer.get("fallback_to_inbox"):
        return PostResult(network, False, "o TikTok mandou o vídeo para os rascunhos (limite diário da API dele): "
                                          "abra o app do TikTok e publique de lá; reconectar o TikTok no "
                                          "Upload-Post evita isso", when, url)
    if answer.get("status") in RUNNING or str(answer.get("message", "")).lower() in RUNNING:
        return PostResult(network, False, PROCESSING, when, url, pending=True)
    if answer.get("status") == "retryable":
        return PostResult(network, False, f"falhou e o Upload-Post vai tentar de novo: {answer.get('error', '')}",
                          when, url, pending=True)
    if answer.get("success"):
        return PostResult(network, True, url or "publicado", when, url)
    return PostResult(network, False, str(answer.get("error") or answer.get("message") or "falhou")[:300], when)


class UploadPostClient:
    """Cliente mínimo da API do Upload-Post: um envio por vídeo, com o texto de cada rede."""

    def __init__(self, api_key: str, user: str, facebook_page_id: str | None = None, session=None,
                 url: str = API_URL, ai_label: bool = True, sleep=time.sleep) -> None:
        if not api_key or not user:
            raise PublishError("defina UPLOAD_POST_API_KEY e UPLOAD_POST_USER no .env (veja publicar.py)")
        self.api_key, self.user, self.facebook_page_id = api_key, user, facebook_page_id
        self.url, self.ai_label, self.sleep = url, ai_label, sleep
        if session is None:
            import requests

            session = requests.Session()
        self.session = session

    @classmethod
    def from_env(cls, session=None) -> "UploadPostClient":
        return cls(os.getenv("UPLOAD_POST_API_KEY", "").strip(), os.getenv("UPLOAD_POST_USER", "").strip(),
                   os.getenv("UPLOAD_POST_FACEBOOK_PAGE_ID", "").strip() or None, session,
                   ai_label=os.getenv("ROTULO_IA", "1").strip() != "0")

    def headers(self, key: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Apikey {self.api_key}", **({"Idempotency-Key": key} if key else {})}

    def fields(self, networks: list[str], posts: dict, when: datetime | None, seconds: float) -> list[tuple[str, str]]:
        """Campos do envio: cada rede com o seu texto ([rede]_title); title é o título curto."""
        data = [("user", self.user), *(("platform[]", n) for n in networks), ("title", posts[networks[0]]["title"])]
        for network in networks:
            post = posts[network]
            if network in ("tiktok", "instagram"):
                data.append((f"{network}_title", post["caption"]))
            elif network == "facebook":
                data += [("facebook_title", post["title"]), ("facebook_description", post["caption"]),
                         ("facebook_media_type", facebook_kind(seconds))]
                if self.facebook_page_id:
                    data.append(("facebook_page_id", self.facebook_page_id))
            elif network == "youtube":
                data += [("youtube_title", post["title"]), ("youtube_description", post["caption"])]
        if "instagram" in networks:
            data.append(("media_type", "REELS"))
        if self.ai_label:
            data.append(("is_ai_generated", "true"))
            if "facebook" in networks and facebook_kind(seconds) == "REELS":
                data.append(("facebook_is_ai_generated", "true"))
        if when is not None:
            data.append(("scheduled_date", when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
        else:
            data.append(("async_upload", "true"))  # a API responde logo; o resultado vem pelo status
        return data

    def send(self, video: Path, networks: list[str], posts: dict, when: datetime | None, seconds: float,
             cover: Path | None = None) -> tuple[list[PostResult], dict]:
        """Envia o vídeo para as redes; devolve o resultado de cada uma e o id do envio."""
        key = uuid.uuid4().hex  # a mesma chave na 2ª tentativa: o Upload-Post não duplica o post
        data = self.fields(networks, posts, when, seconds) + [("request_id", key)]
        covers = []
        if cover is not None and "instagram" in networks:
            covers.append("cover_image")
        if cover is not None and "facebook" in networks and facebook_kind(seconds) == "VIDEO":
            covers.append("facebook_thumbnail")
        response = None
        for attempt in (1, 2):
            try:
                with ExitStack() as stack:
                    files = [("video", (video.name, stack.enter_context(open(video, "rb")), "video/mp4"))]
                    files += [(name, (cover.name, stack.enter_context(open(cover, "rb")), "image/jpeg"))
                              for name in covers]
                    response = self.session.post(self.url, headers=self.headers(key), data=data, files=files,
                                                 timeout=900)
                break
            except OSError as exc:  # a rede caiu ou demorou: tenta mais 1 vez com a mesma chave
                if attempt == 2:
                    raise PublishError(f"o Upload-Post não respondeu ({exc.__class__.__name__}); confira com "
                                       f"--status antes de mandar de novo")
                log.warning("O Upload-Post não respondeu (%s); tentando de novo", exc.__class__.__name__)
        body = _json(response)
        if response.status_code >= 400:
            raise PublishError(f"HTTP {response.status_code}: "
                               f"{body.get('message') or body.get('error') or response.text[:300]}")
        scheduled = when.isoformat(timespec="minutes") if when else None
        if body.get("job_id"):  # agendado: o Upload-Post publica sozinho na hora marcada
            return [PostResult(n, True, f"agendado no Upload-Post (job {body['job_id']})", scheduled)
                    for n in networks], {"job_id": body["job_id"]}
        ids = {"request_id": body.get("request_id") or key}
        answers = _by_platform(body.get("results"))
        if not answers:
            try:
                answers = self.wait(ids["request_id"])
            except (PublishError, OSError) as exc:  # o vídeo já foi: fica "processando" até o --status
                log.warning("Não deu para consultar o status agora (%s)", exc)
        return [_platform_result(n, answers.get(n), scheduled) for n in networks], ids

    def status(self, ids: dict) -> dict:
        response = self.session.get(STATUS_URL, headers=self.headers(), params=ids, timeout=60)
        body = _json(response)
        if response.status_code >= 400 and body.get("status") != "not_found":
            raise PublishError(f"status: HTTP {response.status_code}: {body.get('message') or body.get('error') or ''}")
        return body

    def wait(self, request_id: str) -> dict[str, dict]:
        """Consulta o status até o envio terminar em todas as redes (ou até POLL_LIMIT segundos)."""
        waited = 0
        while True:
            body = self.status({"request_id": request_id})
            total, completed = body.get("total"), body.get("completed")
            if body.get("status") in FINISHED or (total and completed is not None and completed >= total):
                return _by_platform(body.get("results"))
            if waited >= POLL_LIMIT:
                return {}  # cada rede fica como "ainda processando"
            self.sleep(POLL_SECONDS)
            waited += POLL_SECONDS


def load_publication(day: date) -> tuple[dict, Path, dict]:
    """Episódio, vídeo final e legendas de cada rede do dia."""
    import main
    from publicacao import network_posts

    episode_path = main.day_dir(day) / "current_episode.json"
    if not episode_path.exists():
        raise PublishError(f"{episode_path} não existe: não há vídeo em {day}")
    episode = {**json.loads(episode_path.read_text(encoding="utf-8")), "date": day.isoformat()}
    final = main.Checkpoint.load(day).final_video
    video = Path(final) if final else None
    if video is None or not video.exists():
        videos = sorted(main.output_dir().glob(f"{day.isoformat()}_*.mp4"))
        if not videos:
            raise PublishError(f"o vídeo de {day} ainda não foi renderizado (rode python main.py --date {day})")
        video = videos[-1]
    credits = main.load_image_credits(episode)
    return episode, video, network_posts(episode, credits.required_items)


def parse_postar(text: str) -> dict[str, dict[str, str]]:
    """Legendas de cada rede a partir de um postar.txt (o arquivo que acompanha o vídeo)."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        header = re.match(r"^=== (.+?) ===$", line)
        if header:
            current = header.group(1).split()[0].lower()
            sections[current] = []
        elif current:
            sections[current].append(line)
    posts: dict[str, dict[str, str]] = {}
    for network in NETWORKS:
        body = "\n".join(sections.get(network, [])).strip()
        if body:
            posts[network] = {"title": body.splitlines()[0], "caption": body}
    youtube = "\n".join(sections.get("youtube", [])).strip()
    match = re.match(r"Título:\s*\n(.*?)\n\s*\nDescrição:\s*\n(.*)", youtube, re.S)
    if match:
        posts["youtube"] = {"title": match.group(1).strip(), "caption": match.group(2).strip()}
    return posts


def load_video_file(video: Path) -> tuple[Path, dict]:
    """Vídeo baixado + o postar.txt ao lado (AAAA-MM-DD_postar.txt ou postar.txt)."""
    if not video.exists():
        raise PublishError(f"{video} não existe")
    texts = [video.with_name(f"{video.name[:10]}_postar.txt"), video.with_name("postar.txt")]
    text = next((t for t in texts if t.exists()), None)
    if text is None:
        raise PublishError(f"falta o texto ao lado do vídeo: {texts[0].name} (vem junto com o vídeo)")
    posts = parse_postar(text.read_text(encoding="utf-8"))
    if not posts:
        raise PublishError(f"{text.name} não tem as seções das redes (=== TIKTOK ===...)")
    return video, posts


def find_cover(*candidates: Path) -> Path | None:
    """A capa (capa.jpg) que vai para o Instagram e, em vídeo normal, para o Facebook."""
    return next((c for c in candidates if c.exists() and c.stat().st_size <= COVER_MAX_BYTES), None)


def _record_path(day: date | None, video: Path) -> Path:
    if day is not None:
        import main

        return main.day_dir(day) / "publicacao.json"
    return video.with_name(video.stem + ".publicacao.json")


def _load_record(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _send_all(video: Path, posts: dict, networks: list[str], when: datetime | None, dry_run: bool,
              client, cover: Path | None = None) -> tuple[list[PostResult], dict | None]:
    """Confere a duração de cada rede e manda o vídeo de uma vez para as que aceitam."""
    if when is not None and when <= datetime.now(timezone.utc):
        raise PublishError(f"o horário {when.isoformat(timespec='minutes')} já passou")
    seconds = video_seconds(video)
    blocked: dict[str, PostResult] = {}
    for network in networks:
        if seconds > MAX_SECONDS[network]:
            blocked[network] = PostResult(network, False, f"vídeo com {seconds:.0f}s; o limite de {network} "
                                                          f"pela API é {MAX_SECONDS[network]}s")
        elif network not in posts:
            blocked[network] = PostResult(network, False, f"o texto não tem a seção de {network}")
    to_send = [n for n in networks if n not in blocked]
    sent: dict[str, PostResult] = {}
    ids = None
    if to_send and dry_run:
        scheduled = when.isoformat(timespec="minutes") if when else None
        for network in to_send:
            kind = {"tiktok": "vídeo", "instagram": "Reels",
                    "facebook": "Reels" if facebook_kind(seconds) == "REELS"
                    else f"vídeo normal da página (Reels só até {FACEBOOK_REELS_MAX}s)",
                    "youtube": "Shorts" if seconds <= YOUTUBE_SHORTS_MAX else "vídeo normal (Shorts só até 3 min)"}
            with_cover = cover is not None and (network == "instagram" or
                                                (network == "facebook" and facebook_kind(seconds) == "VIDEO"))
            sent[network] = PostResult(network, True, f"TESTE (nada enviado): {video.name} ({seconds:.0f}s) como "
                                                      f"{kind[network]}; legenda com {len(posts[network]['caption'])}"
                                                      f" caracteres{'; com a capa' if with_cover else ''}", scheduled)
    elif to_send:
        try:
            results, ids = (client or UploadPostClient.from_env()).send(video, to_send, posts, when, seconds, cover)
            sent = {r.network: r for r in results}
        except PublishError as exc:
            sent = {n: PostResult(n, False, str(exc)[:300]) for n in to_send}
    return [blocked.get(n) or sent[n] for n in networks], ids


def _publish(video: Path, posts: dict, networks: list[str], when: datetime | None, dry_run: bool, client,
             record_path: Path, day: str, cover: Path | None) -> list[PostResult]:
    """Manda só para as redes que ainda não foram: o publicacao.json guarda o que já saiu (ou
    ainda está processando), e rodar de novo não duplica o post."""
    previous = _load_record(record_path)
    stored = {r["network"]: PostResult(**r) for r in previous.get("results", [])}
    done = {n: r for n, r in stored.items() if r.ok or r.pending}
    to_send = [n for n in networks if n not in done]
    results, ids = _send_all(video, posts, to_send, when, dry_run, client, cover) if to_send else ([], None)
    fresh = {r.network: r for r in results}
    shown = []
    for n in networks:
        if n in fresh:
            shown.append(fresh[n])
        elif done[n].ok:
            shown.append(PostResult(n, True, f"já enviado antes ({done[n].detail}); para mandar de novo, apague "
                                             f"{record_path.name}", done[n].scheduled_for, done[n].url))
        else:
            shown.append(PostResult(n, False, f"o envio anterior {PROCESSING}", done[n].scheduled_for,
                                    pending=True))
    if not dry_run and to_send:
        when_text = when.isoformat(timespec="minutes") if when else "agora"
        sends = previous.get("sends", []) + ([{**ids, "networks": to_send, "when": when_text}] if ids else [])
        stored.update(fresh)
        record = {"date": day, "video": str(video), "when": when_text, "sends": sends,
                  "results": [asdict(r) for r in stored.values()]}
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return shown


def publish(day: date, networks: list[str] | None = None, when: datetime | None = None,
            dry_run: bool = False, client=None, mark_published=None) -> list[PostResult]:
    import main

    episode, video, posts = load_publication(day)
    cover = find_cover(main.day_dir(day) / "capa.jpg")
    results = _publish(video, posts, networks or networks_from_env(), when, dry_run, client,
                       _record_path(day, video), day.isoformat(), cover)
    if not dry_run and results and all(r.ok for r in results):
        (mark_published or _mark_published)(day)
    return results


def publish_file(video: Path, networks: list[str] | None = None, when: datetime | None = None,
                 dry_run: bool = False, client=None) -> list[PostResult]:
    """Publica um vídeo baixado (fora do projeto). O resultado fica em <vídeo>.publicacao.json."""
    video, posts = load_video_file(Path(video))
    cover = find_cover(video.with_name(f"{video.name[:10]}_capa.jpg"), video.with_name("capa.jpg"))
    return _publish(video, posts, networks or networks_from_env(), when, dry_run, client,
                    _record_path(None, video), video.name[:10], cover)


def check_status(target: str, client=None, mark_published=None) -> list[PostResult]:
    """Pergunta ao Upload-Post como ficou cada envio: agendado, publicado ou com erro."""
    day = date.fromisoformat(target) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", target) else None
    path = _record_path(day, Path(target)) if not target.endswith(".publicacao.json") else Path(target)
    record = _load_record(path)
    if not record.get("sends"):
        raise PublishError(f"nada enviado ainda: {path} não existe ou não tem envios")
    client = client or UploadPostClient.from_env()
    results = {r["network"]: PostResult(**r) for r in record.get("results", [])}
    for send in record["sends"]:
        ids = {k: send[k] for k in ("job_id", "request_id") if send.get(k)}
        body = client.status(ids)
        answers = _by_platform(body.get("results"))
        when = None if send.get("when") == "agora" else send.get("when")
        for network in send["networks"]:
            if network in answers:
                results[network] = _platform_result(network, answers[network], when)
            elif "job_id" in ids and body.get("status") in RUNNING:
                results[network] = PostResult(network, True, f"agendado; ainda não saiu (job {ids['job_id']})", when)
            elif body.get("status") in RUNNING:
                results[network] = PostResult(network, False, PROCESSING, when, pending=True)
            elif body.get("status") in ("failed", "not_found"):
                results[network] = PostResult(network, False, body.get("message") or body["status"], when)
    final = list(results.values())
    record["results"] = [asdict(r) for r in final]
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if day is not None and final and all(r.ok for r in final):
        (mark_published or _mark_published)(day)
    return final


def _mark_published(day: date) -> None:
    from database import StoryDatabase

    db = StoryDatabase()
    topic = db.topic_for_date(day)
    if topic is not None and topic.status != "published":
        db.set_status(topic.id, "published")


def schedule(days: list[date], start: datetime, every_hours: float = 24, times: list[str] | None = None,
             **kwargs) -> dict[date, list[PostResult]]:
    """Rotina: um vídeo por horário (times) ou a cada every_hours horas, a partir de start."""
    slots = agenda_slots(start, len(days), every_hours, times)
    return {day: publish(day, when=when, **kwargs) for day, when in zip(days, slots)}


def _print(day, results: list[PostResult]) -> bool:
    for r in results:
        when = f" (agendado para {r.scheduled_for})" if r.scheduled_for else ""
        mark = "[OK]" if r.ok else "[..]" if r.pending else "[X]"
        print(f"  {mark} {day} {r.network}{when}: {r.detail}")
    return all(r.ok for r in results)


def main(argv: list[str] | None = None) -> int:
    from main import utf8_console

    utf8_console()
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_DIR / ".env")
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Publica (ou agenda) o vídeo no TikTok, Instagram e Facebook.")
    parser.add_argument("--date", type=date.fromisoformat, help="dia do vídeo (AAAA-MM-DD)")
    parser.add_argument("--quando", help='agenda para "AAAA-MM-DD HH:MM" (sem isso, publica agora)')
    parser.add_argument("--agenda", nargs="+", type=date.fromisoformat, metavar="DIA",
                        help="vários vídeos, um a cada --intervalo-horas a partir de --inicio")
    parser.add_argument("--video", type=Path, help="vídeo baixado (.mp4), com o AAAA-MM-DD_postar.txt ao lado")
    parser.add_argument("--agenda-videos", nargs="+", type=Path, metavar="MP4",
                        help="vários vídeos baixados, um a cada --intervalo-horas a partir de --inicio")
    parser.add_argument("--inicio", help='primeiro horário da --agenda, "AAAA-MM-DD HH:MM"')
    parser.add_argument("--intervalo-horas", type=float, default=24.0)
    parser.add_argument("--horarios", nargs="+", metavar="HH:MM",
                        help="vários vídeos por dia, um em cada horário (ex.: --horarios 12:00 19:00)")
    parser.add_argument("--redes", help="ex.: tiktok,instagram (padrão: PUBLICAR_REDES do .env)")
    parser.add_argument("--teste", action="store_true", help="mostra o que seria enviado, sem enviar")
    parser.add_argument("--status", nargs="+", metavar="DIA_OU_MP4",
                        help="como ficou cada envio (AAAA-MM-DD ou o .mp4 baixado)")
    parser.add_argument("--pasta", type=Path,
                        help="fila: agenda os vídeos novos da pasta nos próximos --horarios livres")
    args = parser.parse_args(argv)
    try:
        if args.status:
            ok = True
            for target in args.status:
                ok &= _print(Path(target).name, check_status(target))
            return 0 if ok else 1
        networks = (check_networks([n.strip().lower() for n in args.redes.split(",") if n.strip()])
                    if args.redes else networks_from_env())
        if args.pasta:
            start = parse_start(args.inicio) if args.inicio else None
            outcome = queue_folder(args.pasta, args.horarios, networks, start, dry_run=args.teste)
            if not outcome:
                print("[OK] nada novo na pasta: todo vídeo já foi agendado ou publicado")
            ok = True
            for video, when, results in outcome:
                ok &= _print(video.name, results)
            return 0 if ok else 1
        if args.agenda or args.agenda_videos:
            if not args.inicio:
                parser.error('a agenda precisa de --inicio "AAAA-MM-DD HH:MM" (com --horarios, basta o dia)')
            items = args.agenda or args.agenda_videos
            start = parse_start(args.inicio) if args.horarios else parse_when(args.inicio)
            ok = True
            for item, when in zip(items, agenda_slots(start, len(items), args.intervalo_horas, args.horarios)):
                results = (publish(item, networks, when, dry_run=args.teste) if args.agenda
                           else publish_file(item, networks, when, dry_run=args.teste))
                ok &= _print(item if args.agenda else item.name, results)
            return 0 if ok else 1
        if args.video:
            when = parse_when(args.quando) if args.quando else None
            return 0 if _print(args.video.name, publish_file(args.video, networks, when, dry_run=args.teste)) else 1
        if not args.date:
            parser.error("diga o dia: --date AAAA-MM-DD (ou --agenda)")
        when = parse_when(args.quando) if args.quando else None
        return 0 if _print(args.date, publish(args.date, networks, when, dry_run=args.teste)) else 1
    except PublishError as exc:
        print(f"[X] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
