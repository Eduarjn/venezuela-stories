import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import (  # noqa: E402
    DaySlotTakenError,
    DuplicateTopicError,
    InvalidTransitionError,
    NotEnoughSourcesError,
    StoryDatabase,
    base_domain,
    normalize,
)

DAY1 = date(2026, 11, 1)
DAY2 = date(2026, 11, 2)
DAY3 = date(2026, 11, 3)


class StoryDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = StoryDatabase(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def reserve_miranda(self, day=DAY1):
        return self.db.reserve_topic(
            "Francisco de Miranda no Arco do Triunfo",
            "Francisco de Miranda",
            "personagem",
            "fact",
            day,
        )

    def test_normalize_strips_accents_and_punctuation(self):
        self.assertEqual(normalize("Relâmpago do Catatumbo!"), "relampago-do-catatumbo")

    def test_base_domain_merges_subdomains_and_country_tlds(self):
        self.assertEqual(base_domain("https://pt.wikipedia.org/wiki/X"), "wikipedia.org")
        self.assertEqual(base_domain("https://es.wikipedia.org/wiki/X"), "wikipedia.org")
        self.assertEqual(base_domain("http://www.historia.gob.ve/a"), "historia.gob.ve")

    def test_reserve_creates_topic_for_the_day(self):
        topic = self.reserve_miranda()
        self.assertEqual(topic.status, "reserved")
        self.assertEqual(topic.scheduled_date, "2026-11-01")
        self.assertEqual(self.db.topic_for_date(DAY1).id, topic.id)

    def test_same_title_is_blocked(self):
        self.reserve_miranda()
        with self.assertRaises(DuplicateTopicError):
            self.reserve_miranda(DAY2)

    def test_same_entity_with_other_title_is_blocked(self):
        self.reserve_miranda()
        with self.assertRaises(DuplicateTopicError):
            self.db.reserve_topic(
                "O general das três revoluções", "francisco de miranda", "personagem", "fact", DAY2
            )

    def test_near_identical_title_is_blocked(self):
        self.reserve_miranda()
        with self.assertRaises(DuplicateTopicError):
            self.db.reserve_topic(
                "Francisco de Miranda no Arco de Triunfo", "Miranda (general)", "personagem", "fact", DAY2
            )

    def test_different_topic_is_allowed(self):
        self.reserve_miranda()
        topic = self.db.reserve_topic(
            "O Relâmpago do Catatumbo", "Relâmpago do Catatumbo", "fenomeno_natural", "fact", DAY2
        )
        self.assertEqual(topic.scheduled_date, "2026-11-02")

    def test_only_one_video_per_day(self):
        self.reserve_miranda()
        with self.assertRaises(DaySlotTakenError):
            self.db.reserve_topic(
                "O Relâmpago do Catatumbo", "Relâmpago do Catatumbo", "fenomeno_natural", "fact", DAY1
            )

    def test_fact_needs_two_independent_sources_to_be_verified(self):
        topic = self.reserve_miranda()
        self.db.set_status(topic.id, "scripted")
        self.db.add_source(topic.id, "https://pt.wikipedia.org/wiki/Francisco_de_Miranda")
        self.db.add_source(topic.id, "https://es.wikipedia.org/wiki/Francisco_de_Miranda")
        with self.assertRaises(NotEnoughSourcesError):
            self.db.set_status(topic.id, "verified")

        self.db.add_source(topic.id, "https://www.britannica.com/biography/Francisco-de-Miranda")
        self.assertEqual(self.db.set_status(topic.id, "verified").status, "verified")

    def test_fiction_does_not_need_sources(self):
        topic = self.db.reserve_topic("A lenda do Silbón", "El Silbón", "lenda", "fiction", DAY1)
        self.db.set_status(topic.id, "scripted")
        self.assertEqual(self.db.set_status(topic.id, "verified").status, "verified")

    def test_cannot_skip_steps(self):
        topic = self.reserve_miranda()
        with self.assertRaises(InvalidTransitionError):
            self.db.set_status(topic.id, "published")

    def test_failed_topic_frees_the_day_and_can_be_retried(self):
        topic = self.reserve_miranda()
        failed = self.db.set_status(topic.id, "failed", error="Gemini timeout")
        self.assertIsNone(failed.scheduled_date)
        self.assertIsNone(self.db.topic_for_date(DAY1))

        retried = self.reserve_miranda(DAY2)
        self.assertEqual(retried.id, topic.id)
        self.assertEqual(retried.status, "reserved")
        self.assertIsNone(retried.error)

    def test_rejected_topic_is_blocked_forever(self):
        topic = self.reserve_miranda()
        self.db.set_status(topic.id, "scripted")
        self.db.set_status(topic.id, "rejected", error="fato não confirmado")
        with self.assertRaises(DuplicateTopicError):
            self.reserve_miranda(DAY3)

    def test_full_pipeline_marks_published(self):
        topic = self.reserve_miranda()
        self.db.set_status(topic.id, "scripted")
        self.db.add_source(topic.id, "https://es.wikipedia.org/wiki/Francisco_de_Miranda")
        self.db.add_source(topic.id, "https://www.britannica.com/biography/Francisco-de-Miranda")
        self.db.set_status(topic.id, "verified")
        self.db.set_status(topic.id, "rendered", video_path="output/2026-11-01/final.mp4")
        published = self.db.set_status(topic.id, "published")
        self.assertIsNotNone(published.published_at)
        self.assertEqual(published.video_path, "output/2026-11-01/final.mp4")
        self.assertEqual(self.db.used_topics_summary(), [
            "francisco-de-miranda: Francisco de Miranda no Arco do Triunfo"
        ])


if __name__ == "__main__":
    unittest.main()
