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

DAY = date(2026, 11, 2)
EPISODE = {"topic_id": 5, "track": "fact", "post_title": "O desfile que nasceu de um filme",
           "post_description": "O desfile do Dia dos Mortos na Cidade do México começou em 2016.",
           "hashtags": ["#mexico", "#diadosmortos", "#curiosidades", "#jamesbond", "#historia", "#viagem"],
           "sources": [{"title": "Fonte", "url": "https://exemplo.org"}], "scenes": []}


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {"success": True}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def post(self, url, headers, data, files, timeout):
        fields = dict(data)
        self.calls.append({"url": url, "headers": headers, "data": data, "file": files["video"][0]})
        if fields["platform[]"] == self.fail_on:
            return FakeResponse(401, {"error": "Unauthorized"})
        return FakeResponse(body={"success": True, "platform": fields["platform[]"]})


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.patches = [mock.patch.dict(os.environ, {"OUTPUT_DIR": str(root / "output")}),
                        mock.patch("main.episodes_dir", return_value=root / "episodes")]
        for p in self.patches:
            p.start()
        main.day_dir(DAY).mkdir(parents=True)
        (main.day_dir(DAY) / "current_episode.json").write_text(json.dumps(EPISODE), encoding="utf-8")
        self.video = main.output_dir() / "2026-11-02_desfile.mp4"
        self.video.write_bytes(b"mp4")
        self.marked = []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def run_publish(self, seconds=75.0, **kwargs):
        with mock.patch("publicar.video_seconds", return_value=seconds):
            return publicar.publish(DAY, mark_published=self.marked.append, **kwargs)

    def client(self, session):
        return publicar.UploadPostClient("chave", "meu_perfil", "123456", session=session)

    def test_posts_each_network_with_its_own_caption_and_marks_published(self):
        session = FakeSession()
        results = self.run_publish(networks=["tiktok", "instagram", "facebook"], client=self.client(session))
        self.assertTrue(all(r.ok for r in results))
        self.assertEqual([dict(c["data"])["platform[]"] for c in session.calls], ["tiktok", "instagram", "facebook"])
        ig, fb = dict(session.calls[1]["data"]), dict(session.calls[2]["data"])
        self.assertEqual(ig["media_type"], "REELS")
        self.assertEqual(fb["facebook_page_id"], "123456")
        self.assertEqual(session.calls[0]["headers"]["Authorization"], "Apikey chave")
        self.assertEqual(ig["title"].count("#"), 5)   # Instagram: no máximo 5 hashtags
        self.assertEqual(fb["title"].count("#"), 3)   # Facebook: só as principais
        self.assertEqual(session.calls[0]["file"], "2026-11-02_desfile.mp4")
        self.assertEqual(self.marked, [DAY])
        record = json.loads((main.day_dir(DAY) / "publicacao.json").read_text(encoding="utf-8"))
        self.assertEqual(record["when"], "agora")

    def test_video_over_90s_skips_facebook_reels_but_not_the_others(self):
        session = FakeSession()
        results = self.run_publish(seconds=120, networks=["tiktok", "facebook"], client=self.client(session))
        self.assertEqual([(r.network, r.ok) for r in results], [("tiktok", True), ("facebook", False)])
        self.assertIn("90s", results[1].detail)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.marked, [])  # nem todas as redes: não marca como publicado

    def test_one_network_failing_does_not_stop_the_others(self):
        session = FakeSession(fail_on="instagram")
        results = self.run_publish(networks=["tiktok", "instagram", "facebook"], client=self.client(session))
        self.assertEqual([r.ok for r in results], [True, False, True])
        self.assertIn("HTTP 401", results[1].detail)

    def test_schedule_spaces_videos_and_sends_local_time_with_offset(self):
        session = FakeSession()
        start = datetime(2026, 11, 3, 19, 0, tzinfo=timezone(timedelta(hours=-3)))
        with mock.patch("publicar.video_seconds", return_value=70):
            done = publicar.schedule([DAY, DAY], start, 24, networks=["tiktok"], client=self.client(session),
                                     mark_published=self.marked.append)
        dates = [dict(c["data"])["scheduled_date"] for c in session.calls]
        self.assertEqual(dates, ["2026-11-03T19:00:00-03:00", "2026-11-04T19:00:00-03:00"])
        self.assertTrue(all(r.ok for results in done.values() for r in results))

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        session = FakeSession()
        results = self.run_publish(networks=["tiktok", "instagram"], client=self.client(session), dry_run=True)
        self.assertTrue(all(r.ok and "TESTE" in r.detail for r in results))
        self.assertEqual(session.calls, [])
        self.assertFalse((main.day_dir(DAY) / "publicacao.json").exists())
        self.assertEqual(self.marked, [])

    def test_missing_keys_and_bad_time_are_clear_errors(self):
        with mock.patch.dict(os.environ, {"UPLOAD_POST_API_KEY": "", "UPLOAD_POST_USER": ""}):
            with self.assertRaises(publicar.PublishError):
                publicar.UploadPostClient.from_env(session=FakeSession())
        with self.assertRaises(publicar.PublishError):
            publicar.parse_when("amanhã às 7")
        self.assertIsNotNone(publicar.parse_when("2026-11-03 19:00").utcoffset())


class DownloadedVideoTest(unittest.TestCase):
    """Vídeo baixado no PC: o .mp4 e o AAAA-MM-DD_postar.txt na mesma pasta, sem o projeto."""

    def test_postar_txt_round_trip_gives_the_same_captions(self):
        from publicacao import build_ready_posts, network_posts

        text = build_ready_posts({**EPISODE, "date": "2026-11-02"}, [])
        parsed = publicar.parse_postar(text)
        expected = network_posts({**EPISODE, "date": "2026-11-02"}, [])
        for network in ("tiktok", "instagram", "facebook", "youtube"):
            self.assertEqual(parsed[network]["caption"], expected[network]["caption"], network)
        self.assertEqual(parsed["youtube"]["title"], expected["youtube"]["title"])

    def test_publishes_a_downloaded_video_with_the_text_next_to_it(self):
        from publicacao import build_ready_posts

        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video = folder / "2026-11-02_o-desfile.mp4"
            video.write_bytes(b"mp4")
            with self.assertRaises(publicar.PublishError):  # sem o texto ao lado
                publicar.publish_file(video, ["tiktok"], client=publicar.UploadPostClient("k", "u",
                                                                                         session=FakeSession()))
            (folder / "2026-11-02_postar.txt").write_text(build_ready_posts({**EPISODE, "date": "2026-11-02"}, []),
                                                          encoding="utf-8")
            session = FakeSession()
            with mock.patch("publicar.video_seconds", return_value=74):
                results = publicar.publish_file(video, ["tiktok", "instagram", "facebook"],
                                                client=publicar.UploadPostClient("k", "u", session=session))
            self.assertTrue(all(r.ok for r in results))
            self.assertEqual(len(session.calls), 3)
            self.assertTrue((folder / "2026-11-02_o-desfile.publicacao.json").exists())


if __name__ == "__main__":
    unittest.main()
