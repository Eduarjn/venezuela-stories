import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, time as dtime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daily_scheduler  # noqa: E402
import main  # noqa: E402
from main import Checkpoint, PipelineError, RunLock, build_post_copy, run_pipeline, slugify_title  # noqa: E402

DAY = date(2026, 11, 1)


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"OUTPUT_DIR": self.tmp.name})
        self.env.start()
        self.calls = []
        self.waits = []

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def steps(self, fail=None):
        """fail = {"voice": 2} -> o passo voice falha nas 2 primeiras chamadas."""
        fail = dict(fail or {})

        def make(name):
            def step(day):
                self.calls.append(name)
                if fail.get(name, 0) > 0:
                    fail[name] -= 1
                    raise ConnectionError(f"{name} caiu")
                if name == "edit":
                    return Path("v.mp4"), Path("post_copy.txt")
            return step
        return {n: make(n) for n in main.STEPS}

    def run_(self, steps, online=lambda: True):
        return run_pipeline(DAY, steps=steps, sleep=self.waits.append,
                            connectivity=online, check_health=False)

    def test_runs_all_steps_in_order_and_saves_checkpoint(self):
        cp = self.run_(self.steps())
        self.assertEqual(self.calls, ["story", "visuals", "voice", "edit"])
        self.assertTrue(cp.done)
        saved = json.loads(main.checkpoint_path(DAY).read_text(encoding="utf-8"))
        self.assertEqual(saved["final_video"], "v.mp4")

    def test_transient_failure_is_retried_with_backoff(self):
        self.run_(self.steps(fail={"visuals": 1}))
        self.assertEqual(self.calls, ["story", "visuals", "visuals", "voice", "edit"])
        self.assertEqual(self.waits, [main.STEP_BACKOFF_SECONDS[0]])

    def test_resume_skips_finished_steps_after_a_crash(self):
        with self.assertRaises(PipelineError):
            self.run_(self.steps(fail={"voice": 3}))
        self.assertEqual(Checkpoint.load(DAY).steps,
                         {"story": "done", "visuals": "done", "voice": "failed", "edit": "pending"})

        self.calls.clear()
        cp = self.run_(self.steps())
        self.assertEqual(self.calls, ["voice", "edit"])  # nada de gastar API de novo
        self.assertTrue(cp.done)
        self.assertEqual(len(cp.errors), 3)

    def test_finished_day_does_nothing(self):
        self.run_(self.steps())
        self.calls.clear()
        self.run_(self.steps())
        self.assertEqual(self.calls, [])

    def test_waits_for_internet_to_come_back(self):
        answers = iter([False, False, True] + [True] * 10)
        self.run_(self.steps(), online=lambda: next(answers))
        self.assertEqual(self.waits[:2], [60, 60])

    def test_pipeline_error_is_not_retried(self):
        steps = self.steps()

        def broken(day):
            self.calls.append("story")
            raise PipelineError("configuração errada")
        steps["story"] = broken
        with self.assertRaises(PipelineError):
            self.run_(steps)
        self.assertEqual(self.calls, ["story"])

    def test_health_check_lists_missing_keys(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_BACKEND": "api", "ANTHROPIC_API_KEY": "",
                                          "TTS_PROVIDER": "elevenlabs", "ELEVENLABS_API_KEY": ""}):
            problems = main.health_problems()
        self.assertTrue(any("ANTHROPIC_API_KEY" in p for p in problems))
        self.assertTrue(any("ELEVENLABS_API_KEY" in p for p in problems))

    def test_claude_code_backend_needs_no_api_key_but_needs_claude(self):
        env = {"ANTHROPIC_BACKEND": "claude_code", "ANTHROPIC_API_KEY": "", "ANTHROPIC_MODEL": ""}
        with mock.patch.dict(os.environ, env), mock.patch("shutil.which",
                                                         side_effect=lambda t: None if t == "claude" else "/x"):
            problems = main.health_problems()
        self.assertFalse(any("ANTHROPIC_API_KEY" in p or "ANTHROPIC_MODEL" in p for p in problems), problems)
        self.assertTrue(any("Claude Code" in p for p in problems), problems)


class PublishingTest(unittest.TestCase):
    def test_slug_is_safe_for_file_names(self):
        self.assertEqual(slugify_title("O Relâmpago do Catatumbo: 260 noites?!"),
                         "o-relampago-do-catatumbo-260-noites")

    def test_post_copy_has_title_description_hashtags_and_sources(self):
        text = build_post_copy({
            "post_title": "O lago que acende o céu", "post_description": "Fenômeno real.",
            "hashtags": ["#venezuela", "historia"], "track": "fact", "date": "2026-11-01",
            "sources": [{"title": "Wikipédia", "url": "https://pt.wikipedia.org/x"}],
        })
        self.assertIn("O lago que acende o céu", text)
        self.assertIn("#venezuela #historia", text)
        self.assertIn("https://pt.wikipedia.org/x", text)
        self.assertIn("--mark-published 2026-11-01", text)


class LockTest(unittest.TestCase):
    def test_second_run_is_blocked_while_first_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pipeline.lock"
            with RunLock(path):
                with self.assertRaises(PipelineError):
                    with RunLock(path):
                        pass
            self.assertFalse(path.exists())


class SchedulerTest(unittest.TestCase):
    AT = dtime(8, 0)

    def decide(self, now, state):
        return daily_scheduler.should_run(now, self.AT, state, retry_minutes=30, max_runs=4)

    def test_waits_until_scheduled_time(self):
        self.assertFalse(self.decide(datetime(2026, 11, 1, 7, 59), {}))
        self.assertTrue(self.decide(datetime(2026, 11, 1, 8, 0), {}))

    def test_catches_up_when_computer_was_off(self):
        yesterday = {"day": "2026-10-31", "runs": 1, "done": True}
        self.assertTrue(self.decide(datetime(2026, 11, 1, 14, 30), yesterday))

    def test_runs_only_once_after_success(self):
        state = {"day": "2026-11-01", "runs": 1, "done": True, "last_attempt": "2026-11-01T08:00:00"}
        self.assertFalse(self.decide(datetime(2026, 11, 1, 20, 0), state))

    def test_retries_after_interval_until_limit(self):
        state = {"day": "2026-11-01", "runs": 1, "done": False, "last_attempt": "2026-11-01T08:00:00"}
        self.assertFalse(self.decide(datetime(2026, 11, 1, 8, 20), state))
        self.assertTrue(self.decide(datetime(2026, 11, 1, 8, 31), state))
        self.assertFalse(self.decide(datetime(2026, 11, 1, 12, 0), {**state, "runs": 4}))

    def test_pipeline_runs_without_extra_flags_outside_windows(self):
        if os.name == "nt":
            self.skipTest("só fora do Windows")
        python, flags = daily_scheduler.pipeline_python()
        self.assertEqual((python, flags), (sys.executable, 0))

    def test_single_instance_lock(self):
        port = 47999
        first = daily_scheduler.acquire_single_instance(port)
        self.assertIsNotNone(first)
        self.assertIsNone(daily_scheduler.acquire_single_instance(port))
        first.close()
        second = daily_scheduler.acquire_single_instance(port)
        self.assertIsNotNone(second)
        second.close()


if __name__ == "__main__":
    unittest.main()
