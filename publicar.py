"""Módulo 5 – Publicação automática no TikTok, Instagram e Facebook.

Usa o Upload-Post (upload-post.com), um serviço que já tem as autorizações das três redes
e publica o mesmo vídeo em cada uma por uma API só. (Pelas APIs oficiais, o TikTok só
deixa um app próprio postar vídeo público depois de uma auditoria, e Instagram/Facebook
pedem um app de desenvolvedor da Meta.) Você conecta as contas no painel do Upload-Post
uma vez; aqui ficam só a chave e o nome do perfil, no .env (nunca no chat):

    UPLOAD_POST_API_KEY=...            (chave da API, no painel do Upload-Post)
    UPLOAD_POST_USER=...               (o perfil onde as redes estão conectadas)
    UPLOAD_POST_FACEBOOK_PAGE_ID=      (opcional: a página do Facebook, se houver mais de uma)
    PUBLICAR_REDES=tiktok,instagram,facebook

Uso, sempre depois da revisão do vídeo:
    python publicar.py --date 2026-11-02 --teste      # mostra o que vai mandar, sem mandar
    python publicar.py --date 2026-11-02              # publica agora
    python publicar.py --date 2026-11-02 --quando "2026-11-02 19:00"     # agenda
    python publicar.py --agenda 2026-11-02 2026-11-03 2026-11-04 --inicio "2026-11-03 19:00"
        # rotina: 1 vídeo por dia no mesmo horário; o Upload-Post publica na hora marcada,
        # mesmo com o computador desligado

Duração: TikTok até 10 min, Reels do Instagram até 3 min, Reels do Facebook até 90 s.
Vídeo maior que o limite de uma rede não sobe nela (as outras seguem) e o motivo aparece.
Resultado em output/AAAA-MM-DD/publicacao.json; com todas as redes ok, o tema é marcado
como publicado no banco.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
API_URL = "https://api.upload-post.com/api/upload"
NETWORKS = ("tiktok", "instagram", "facebook")
MAX_SECONDS = {"tiktok": 600, "instagram": 180, "facebook": 90, "youtube": 180}

log = logging.getLogger("publicar")


class PublishError(RuntimeError):
    pass


@dataclass
class PostResult:
    network: str
    ok: bool
    detail: str
    scheduled_for: str | None = None


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


def networks_from_env() -> list[str]:
    raw = os.getenv("PUBLICAR_REDES") or ",".join(NETWORKS)
    chosen = [n.strip().lower() for n in raw.split(",") if n.strip()]
    unknown = sorted(set(chosen) - set(MAX_SECONDS))
    if unknown:
        raise PublishError(f"rede desconhecida em PUBLICAR_REDES: {', '.join(unknown)}")
    return chosen


class UploadPostClient:
    """Cliente mínimo da API do Upload-Post: um envio por rede, cada uma com a sua legenda."""

    def __init__(self, api_key: str, user: str, facebook_page_id: str | None = None,
                 session=None, url: str = API_URL) -> None:
        if not api_key or not user:
            raise PublishError("defina UPLOAD_POST_API_KEY e UPLOAD_POST_USER no .env (veja publicar.py)")
        self.api_key, self.user, self.facebook_page_id = api_key, user, facebook_page_id
        self.url = url
        if session is None:
            import requests

            session = requests.Session()
        self.session = session

    @classmethod
    def from_env(cls, session=None) -> "UploadPostClient":
        return cls(os.getenv("UPLOAD_POST_API_KEY", "").strip(), os.getenv("UPLOAD_POST_USER", "").strip(),
                   os.getenv("UPLOAD_POST_FACEBOOK_PAGE_ID", "").strip() or None, session)

    def fields(self, network: str, post: dict, when: datetime | None) -> list[tuple[str, str]]:
        data = [("user", self.user), ("platform[]", network), ("title", post["caption"])]
        if network == "instagram":
            data.append(("media_type", "REELS"))
        if network == "facebook" and self.facebook_page_id:
            data.append(("facebook_page_id", self.facebook_page_id))
        if when is not None:
            data.append(("scheduled_date", when.isoformat(timespec="seconds")))
        return data

    def send(self, video: Path, network: str, post: dict, when: datetime | None) -> dict:
        with open(video, "rb") as handle:
            response = self.session.post(self.url, headers={"Authorization": f"Apikey {self.api_key}"},
                                         data=self.fields(network, post, when),
                                         files={"video": (video.name, handle, "video/mp4")}, timeout=900)
        if response.status_code >= 400:
            raise PublishError(f"HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json()
        except ValueError:
            return {"resposta": response.text[:300]}


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


def publish(day: date, networks: list[str] | None = None, when: datetime | None = None,
            dry_run: bool = False, client=None, mark_published=None) -> list[PostResult]:
    import main

    networks = networks or networks_from_env()
    episode, video, posts = load_publication(day)
    seconds = video_seconds(video)
    if not dry_run and client is None:
        client = UploadPostClient.from_env()
    results: list[PostResult] = []
    for network in networks:
        limit = MAX_SECONDS[network]
        if seconds > limit:
            results.append(PostResult(network, False, f"vídeo com {seconds:.0f}s; o limite de {network} é {limit}s"))
            continue
        post = posts[network]
        if dry_run:
            fields = UploadPostClient("teste", "teste").fields(network, post, when) if client is None \
                else client.fields(network, post, when)
            shown = ", ".join(f"{k}={v[:40]!r}" for k, v in fields if k != "title")
            results.append(PostResult(network, True, f"TESTE (nada enviado): {video.name}; {shown}; "
                                                     f"legenda com {len(post['caption'])} caracteres",
                                      when.isoformat(timespec="minutes") if when else None))
            continue
        try:
            answer = client.send(video, network, post, when)
            results.append(PostResult(network, True, json.dumps(answer, ensure_ascii=False)[:300],
                                      when.isoformat(timespec="minutes") if when else None))
        except Exception as exc:  # noqa: BLE001 - uma rede falhar não impede as outras
            results.append(PostResult(network, False, str(exc)[:300]))
    if not dry_run:
        record = {"date": day.isoformat(), "video": str(video), "seconds": round(seconds, 1),
                  "when": when.isoformat(timespec="minutes") if when else "agora",
                  "results": [asdict(r) for r in results]}
        (main.day_dir(day) / "publicacao.json").write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                                           encoding="utf-8")
        if results and all(r.ok for r in results):
            (mark_published or _mark_published)(day)
    return results


def _mark_published(day: date) -> None:
    from database import StoryDatabase

    db = StoryDatabase()
    topic = db.topic_for_date(day)
    if topic is not None and topic.status != "published":
        db.set_status(topic.id, "published")


def schedule(days: list[date], start: datetime, every_hours: float = 24, **kwargs) -> dict[date, list[PostResult]]:
    """Rotina: um vídeo por vez, a partir de start, a cada every_hours horas."""
    return {day: publish(day, when=start + timedelta(hours=every_hours * i), **kwargs)
            for i, day in enumerate(days)}


def _print(day: date, results: list[PostResult]) -> bool:
    for r in results:
        when = f" (agendado para {r.scheduled_for})" if r.scheduled_for else ""
        print(f"  {'[OK]' if r.ok else '[X]'} {day} {r.network}{when}: {r.detail}")
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
    parser = argparse.ArgumentParser(description="Publica (ou agenda) o vídeo do dia no TikTok, Instagram e Facebook.")
    parser.add_argument("--date", type=date.fromisoformat, help="dia do vídeo (AAAA-MM-DD)")
    parser.add_argument("--quando", help='agenda para "AAAA-MM-DD HH:MM" (sem isso, publica agora)')
    parser.add_argument("--agenda", nargs="+", type=date.fromisoformat, metavar="DIA",
                        help="vários vídeos, um a cada --intervalo-horas a partir de --inicio")
    parser.add_argument("--inicio", help='primeiro horário da --agenda, "AAAA-MM-DD HH:MM"')
    parser.add_argument("--intervalo-horas", type=float, default=24.0)
    parser.add_argument("--redes", help="ex.: tiktok,instagram (padrão: PUBLICAR_REDES do .env)")
    parser.add_argument("--teste", action="store_true", help="mostra o que seria enviado, sem enviar")
    args = parser.parse_args(argv)
    try:
        networks = [n.strip().lower() for n in args.redes.split(",")] if args.redes else networks_from_env()
        if args.agenda:
            if not args.inicio:
                parser.error('--agenda precisa de --inicio "AAAA-MM-DD HH:MM"')
            done = schedule(args.agenda, parse_when(args.inicio), args.intervalo_horas,
                            networks=networks, dry_run=args.teste)
            return 0 if all([_print(day, results) for day, results in done.items()]) else 1
        if not args.date:
            parser.error("diga o dia: --date AAAA-MM-DD (ou --agenda)")
        when = parse_when(args.quando) if args.quando else None
        return 0 if _print(args.date, publish(args.date, networks, when, dry_run=args.teste)) else 1
    except PublishError as exc:
        print(f"[X] {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
