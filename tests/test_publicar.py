import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import publicar  # noqa: E402

DAY, DAY2 = date(2026, 11, 2), date(2026, 11, 3)
EPISODE = {"topic_id": 5, "track": "fact", "post_title": "O desfile que nasceu de um filme",
           "post_description": "O desfile do Dia dos Mortos na Cidade do México começou em 2016.",
           "hashtags": ["#mexico", "#diadosmortos", "#curiosidades", "#jamesbond", "#historia", "#viagem"],
           "sources": [{"title": "Fonte", "url": "https://exemplo.org"}], "scenes": []}
BRT = timezone(timedelta(hours=-3))


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {"success": True}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def done(*networks, **extra):
    """Resposta de envio que terminou na hora, com sucesso em cada rede."""
    return FakeResponse(body={"success": True, "results": {
        n: {"success": True, "url": f"https://{n}.example/post", **extra.get(n, {})} for n in networks}})


class FakeSession:
    def __init__(self, uploads=(), statuses=()):
        self.uploads, self.statuses = list(uploads), list(statuses)
        self.calls, self.status_calls = [], []

    def post(self, url, headers, data, files, timeout):
        self.calls.append({"url": url, "headers": headers, "data": data,
                           "files": [(name, value[0]) for name, value in files]})
        answer = self.uploads.pop(0) if self.uploads else done(*self.platforms(-1))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, url, headers, params, timeout):
        self.status_calls.append(params)
        return self.statuses.pop(0)

    def platforms(self, i):
        return [v for k, v in self.calls[i]["data"] if k == "platform[]"]

    def field(self, i, name):
        return dict(self.calls[i]["data"]).get(name)


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [mock.patch.dict(os.environ, {"OUTPUT_DIR": str(root / "output")}),
                        mock.patch("main.episodes_dir", return_value=root / "episodes"),
                        mock.patch("publicar.video_seconds", return_value=75.0)]
        for p in self.patches:
            p.start()
        for day in (DAY, DAY2):
            main.day_dir(day).mkdir(parents=True)
            (main.day_dir(day) / "current_episode.json").write_text(json.dumps(EPISODE), encoding="utf-8")
            (main.day_dir(day) / "capa.jpg").write_bytes(b"jpg")
            (main.output_dir() / f"{day}_desfile.mp4").write_bytes(b"mp4")
        self.marked = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def client(self, session):
        return publicar.UploadPostClient("chave", "meu_perfil", "123456", session=session, sleep=lambda s: None)

    def run_publish(self, session, day=DAY, **kwargs):
        kwargs.setdefault("networks", ["tiktok", "instagram", "facebook"])
        return publicar.publish(day, client=self.client(session), mark_published=self.marked.append, **kwargs)

    def test_one_upload_with_each_network_text_cover_and_ai_label(self):
        session = FakeSession()
        results = self.run_publish(session)
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual(len(session.calls), 1)  # o vídeo sobe uma vez só para as 3 redes
        self.assertEqual(session.platforms(0), ["tiktok", "instagram", "facebook"])
        self.assertEqual(session.field(0, "instagram_title").count("#"), 5)    # Instagram: no máximo 5
        self.assertEqual(session.field(0, "facebook_description").count("#"), 3)
        self.assertIn("#jamesbond", session.field(0, "tiktok_title"))
        self.assertEqual(session.field(0, "facebook_title"), EPISODE["post_title"])
        self.assertEqual(session.field(0, "facebook_media_type"), "REELS")
        self.assertEqual(session.field(0, "facebook_page_id"), "123456")
        self.assertEqual(session.field(0, "media_type"), "REELS")
        self.assertEqual(session.field(0, "is_ai_generated"), "true")
        self.assertEqual(session.field(0, "async_upload"), "true")
        headers = session.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Apikey chave")
        self.assertEqual(headers["Idempotency-Key"], session.field(0, "request_id"))
        self.assertEqual(session.calls[0]["files"], [("video", "2026-11-02_desfile.mp4"), ("cover_image", "capa.jpg")])
        self.assertEqual(results[0].url, "https://tiktok.example/post")
        self.assertEqual(self.marked, [DAY])

    def test_a_network_failing_inside_an_ok_answer_is_a_failure_and_rerun_sends_only_it(self):
        failing = FakeResponse(body={"success": True, "results": {
            "tiktok": {"success": True, "url": "https://tiktok.example/1"},
            "instagram": {"success": False, "error": "Expired access token"},
            "facebook": {"success": False, "skipped": True, "error": "no Facebook account configured"}}})
        session = FakeSession([failing])
        results = self.run_publish(session)
        self.assertEqual([r.ok for r in results], [True, False, False])
        self.assertIn("Expired access token", results[1].detail)
        self.assertIn("não está conectada", results[2].detail)
        self.assertEqual(self.marked, [])  # nem todas as redes: não marca como publicado

        again = FakeSession()
        results = self.run_publish(again)
        self.assertEqual(again.platforms(0), ["instagram", "facebook"])  # o TikTok não duplica
        self.assertTrue(all(r.ok for r in results))
        self.assertIn("já enviado antes", results[0].detail)
        self.assertEqual(self.marked, [DAY])
        record = json.loads((main.day_dir(DAY) / "publicacao.json").read_text(encoding="utf-8"))
        self.assertEqual(len(record["sends"]), 2)
        self.assertTrue(all(r["ok"] for r in record["results"]))

    def test_long_video_goes_to_facebook_as_a_page_video_and_over_10_min_skips_tiktok(self):
        session = FakeSession()
        with mock.patch("publicar.video_seconds", return_value=120):
            results = self.run_publish(session)
        self.assertTrue(all(r.ok for r in results))  # 2 min: sobe nas 3 redes
        self.assertEqual(session.field(0, "facebook_media_type"), "VIDEO")
        self.assertIsNone(session.field(0, "facebook_is_ai_generated"))  # rótulo do Facebook é só para Reels
        self.assertIn(("facebook_thumbnail", "capa.jpg"), session.calls[0]["files"])

        session = FakeSession()
        with mock.patch("publicar.video_seconds", return_value=700):
            results = self.run_publish(session, day=DAY2)
        self.assertEqual([(r.network, r.ok) for r in results], [("tiktok", False), ("instagram", True),
                                                                 ("facebook", True)])
        self.assertIn("600s", results[0].detail)
        self.assertEqual(session.platforms(0), ["instagram", "facebook"])

    def test_background_upload_waits_for_the_status_and_tiktok_drafts_are_not_success(self):
        started = FakeResponse(body={"success": True, "message": "Upload initiated successfully in background.",
                                     "request_id": "abc"})
        running = FakeResponse(body={"status": "in_progress", "completed": 1, "total": 3, "results": [
            {"platform": "tiktok", "success": True, "message": "Queued"}]})
        finished = FakeResponse(body={"status": "completed", "completed": 3, "total": 3, "results": [
            {"platform": "tiktok", "success": True, "fallback_to_inbox": True},
            {"platform": "instagram", "success": True, "url": "https://instagram.example/p/1"},
            {"platform": "facebook", "success": True, "url": "https://facebook.example/r/1"}]})
        session = FakeSession([started], [running, finished])
        results = self.run_publish(session)
        self.assertEqual(session.status_calls, [{"request_id": "abc"}, {"request_id": "abc"}])
        self.assertEqual([r.ok for r in results], [False, True, True])
        self.assertIn("rascunhos", results[0].detail)
        self.assertEqual(results[1].url, "https://instagram.example/p/1")

    def test_still_processing_after_the_wait_is_not_resent_and_status_resolves_it(self):
        started = FakeResponse(body={"success": True, "request_id": "abc"})
        running = FakeResponse(body={"status": "processing", "completed": 0, "total": 1, "results": []})
        session = FakeSession([started], [running] * 50)
        results = self.run_publish(session, networks=["tiktok"])
        self.assertTrue(results[0].pending and not results[0].ok)

        again = FakeSession()
        results = self.run_publish(again, networks=["tiktok"])
        self.assertEqual(again.calls, [])  # ainda processando: não manda de novo
        self.assertTrue(results[0].pending)

        checked = FakeSession(statuses=[FakeResponse(body={"status": "completed", "completed": 1, "total": 1,
                                                           "results": [{"platform": "tiktok", "success": True,
                                                                        "post_url": "https://tiktok.example/9"}]})])
        results = publicar.check_status(DAY.isoformat(), client=self.client(checked),
                                        mark_published=self.marked.append)
        self.assertEqual([(r.network, r.ok, r.url) for r in results], [("tiktok", True, "https://tiktok.example/9")])
        self.assertEqual(self.marked, [DAY])

    def test_schedule_one_video_per_day_in_utc_and_status_follows_the_job(self):
        start = (datetime.now(BRT) + timedelta(days=2)).replace(hour=19, minute=0, second=0, microsecond=0)
        session = FakeSession([FakeResponse(202, {"success": True, "job_id": "job1"}),
                               FakeResponse(202, {"success": True, "job_id": "job2"})])
        done_days = publicar.schedule([DAY, DAY2], start, 24, networks=["tiktok"], client=self.client(session),
                                      mark_published=self.marked.append)
        utc = [(start + timedelta(hours=h)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for h in (0, 24)]
        self.assertEqual([session.field(i, "scheduled_date") for i in (0, 1)], utc)
        self.assertIsNone(session.field(0, "async_upload"))
        self.assertTrue(all(r.ok for results in done_days.values() for r in results))
        self.assertIn("job1", done_days[DAY][0].detail)

        waiting = FakeSession(statuses=[FakeResponse(body={"job_id": "job1", "status": "pending", "results": []})])
        results = publicar.check_status(DAY.isoformat(), client=self.client(waiting), mark_published=self.marked.append)
        self.assertEqual(waiting.status_calls, [{"job_id": "job1"}])
        self.assertTrue(results[0].ok and "ainda não saiu" in results[0].detail)

    def test_two_videos_per_day_one_at_each_time(self):
        start = (datetime.now(BRT) + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        session = FakeSession([FakeResponse(202, {"success": True, "job_id": "a"}),
                               FakeResponse(202, {"success": True, "job_id": "b"})])
        publicar.schedule([DAY, DAY2], start, times=["19:00", "12:00"], networks=["tiktok"],
                          client=self.client(session), mark_published=self.marked.append)
        expected = [start.replace(hour=12), start.replace(hour=19)]  # o mesmo dia, nos 2 horários
        self.assertEqual([session.field(i, "scheduled_date") for i in (0, 1)],
                         [e.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") for e in expected])

    def test_agenda_slots_skip_what_already_passed_and_reject_bad_times(self):
        now = datetime(2026, 11, 2, 13, 0, tzinfo=BRT)
        slots = publicar.agenda_slots(datetime(2026, 11, 2, tzinfo=BRT), 3, times=["12:00", "19:00"], now=now)
        self.assertEqual([s.strftime("%d %H:%M") for s in slots], ["02 19:00", "03 12:00", "03 19:00"])
        self.assertEqual(publicar.parse_start("2026-11-03").strftime("%Y-%m-%d %H:%M"), "2026-11-03 00:00")
        with self.assertRaises(publicar.PublishError):
            publicar.agenda_slots(now, 2, times=["meio-dia"])

    def test_timeout_retries_once_with_the_same_key(self):
        session = FakeSession([ConnectionError("caiu"), done("tiktok")])
        results = self.run_publish(session, networks=["tiktok"])
        self.assertTrue(results[0].ok)
        keys = [call["headers"]["Idempotency-Key"] for call in session.calls]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        session = FakeSession()
        results = self.run_publish(session, dry_run=True)
        self.assertTrue(all(r.ok and "TESTE" in r.detail for r in results))
        self.assertIn("com a capa", results[1].detail)
        self.assertEqual(session.calls, [])
        self.assertFalse((main.day_dir(DAY) / "publicacao.json").exists())
        self.assertEqual(self.marked, [])

    def test_missing_keys_bad_or_past_time_and_unknown_network_are_clear_errors(self):
        with mock.patch.dict(os.environ, {"UPLOAD_POST_API_KEY": "", "UPLOAD_POST_USER": ""}):
            with self.assertRaises(publicar.PublishError):
                publicar.UploadPostClient.from_env(session=FakeSession())
        with self.assertRaises(publicar.PublishError):
            publicar.parse_when("amanhã às 7")
        self.assertIsNotNone(publicar.parse_when("2026-11-03 19:00").utcoffset())
        with self.assertRaises(publicar.PublishError):
            self.run_publish(FakeSession(), when=datetime.now(BRT) - timedelta(hours=1))
        with self.assertRaises(publicar.PublishError):
            publicar.check_networks(["tiktok", "orkut"])


class DownloadedVideoTest(unittest.TestCase):
    """Vídeo baixado no PC: o .mp4, o AAAA-MM-DD_postar.txt e a capa na mesma pasta, sem o projeto."""

    def test_postar_txt_round_trip_gives_the_same_captions(self):
        from publicacao import build_ready_posts, network_posts

        text = build_ready_posts({**EPISODE, "date": "2026-11-02"}, [])
        parsed = publicar.parse_postar(text)
        expected = network_posts({**EPISODE, "date": "2026-11-02"}, [])
        for network in ("tiktok", "instagram", "facebook", "youtube"):
            self.assertEqual(parsed[network]["caption"], expected[network]["caption"], network)
        self.assertEqual(parsed["youtube"]["title"], expected["youtube"]["title"])
        self.assertEqual(parsed["facebook"]["title"], EPISODE["post_title"])

    def test_publishes_a_downloaded_video_with_the_text_and_cover_next_to_it(self):
        from publicacao import build_ready_posts

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "2026-11-02_o-desfile.mp4"
            video.write_bytes(b"mp4")
            client = publicar.UploadPostClient("k", "u", session=FakeSession(), sleep=lambda s: None)
            with self.assertRaises(publicar.PublishError):  # sem o texto ao lado
                publicar.publish_file(video, ["tiktok"], client=client)
            (folder / "2026-11-02_postar.txt").write_text(build_ready_posts({**EPISODE, "date": "2026-11-02"}, []),
                                                          encoding="utf-8")
            (folder / "2026-11-02_capa.jpg").write_bytes(b"jpg")
            session = FakeSession()
            with mock.patch("publicar.video_seconds", return_value=74):
                results = publicar.publish_file(video, ["tiktok", "instagram", "facebook"],
                                                client=publicar.UploadPostClient("k", "u", session=session,
                                                                                 sleep=lambda s: None))
            self.assertTrue(all(r.ok for r in results))
            self.assertEqual(len(session.calls), 1)
            self.assertIn(("cover_image", "2026-11-02_capa.jpg"), session.calls[0]["files"])
            self.assertTrue((folder / "2026-11-02_o-desfile.publicacao.json").exists())


if __name__ == "__main__":
    unittest.main()
