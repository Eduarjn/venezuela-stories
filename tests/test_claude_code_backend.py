import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import StoryDatabase  # noqa: E402
from story_engine import (  # noqa: E402
    ClaudeCodeBackend,
    SearchEvidence,
    StoryEngine,
    StoryEngineError,
    canonical_url,
    resolve_backend,
)
from test_story_engine import SRC_A, SRC_B, episode_data, fake_fetch  # noqa: E402


def stream(result_text, session="sess-1", is_error=False, search_urls=()):
    """stdout no formato --output-format stream-json do Claude Code."""
    lines = [{"type": "system", "subtype": "init", "session_id": session}]
    if search_urls:
        links = json.dumps([{"title": "t", "url": u} for u in search_urls])
        lines.append({"type": "user", "session_id": session, "message": {"content": [
            {"type": "tool_result", "tool_use_id": "x",
             "content": [{"type": "text", "text": f"Web search results. Links: {links}"}]}]}})
    lines.append({"type": "result", "subtype": "success", "is_error": is_error,
                  "result": result_text, "session_id": session})
    return "\n".join(json.dumps(line) for line in lines)


class FakeRunner:
    def __init__(self, *stdouts):
        self.stdouts = list(stdouts)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return SimpleNamespace(stdout=self.stdouts.pop(0), stderr="", returncode=0)


class ClaudeCodeBackendTest(unittest.TestCase):
    def backend(self, *stdouts):
        self.runner = FakeRunner(*stdouts)
        return ClaudeCodeBackend(model=None, runner=self.runner, claude_bin="claude")

    def test_command_only_allows_web_tools_and_sends_prompt_via_stdin(self):
        backend = self.backend(stream("ok"))
        self.assertEqual(backend.ask([{"role": "user", "content": "PROMPT"}], SearchEvidence()), "ok")
        cmd, kwargs = self.runner.calls[0]
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertIn("WebSearch,WebFetch", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--bare", cmd)  # --bare ignoraria o login da assinatura
        self.assertNotIn("--resume", cmd)
        self.assertEqual(kwargs["input"], "PROMPT")
        self.assertEqual(kwargs["encoding"], "utf-8")

    def test_feedback_resumes_the_same_session(self):
        backend = self.backend(stream("primeira", session="abc"), stream("segunda", session="abc"))
        backend.ask([{"role": "user", "content": "P"}], SearchEvidence())
        backend.ask([{"role": "user", "content": "P"}, {"role": "assistant", "content": "x"},
                     {"role": "user", "content": "corrija"}], SearchEvidence())
        cmd, kwargs = self.runner.calls[1]
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertEqual(kwargs["input"], "corrija")

    def test_search_result_urls_become_evidence(self):
        evidence = SearchEvidence()
        self.backend(stream("ok", search_urls=[SRC_B])).ask([{"role": "user", "content": "P"}], evidence)
        self.assertTrue(evidence.was_returned(SRC_B))

    def test_not_logged_in_gives_clear_instruction(self):
        backend = self.backend(stream("Invalid API key · Please run /login", is_error=True))
        with self.assertRaisesRegex(StoryEngineError, "/login"):
            backend.ask([{"role": "user", "content": "P"}], SearchEvidence())

    def test_missing_claude_binary(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(StoryEngineError, "install"):
                ClaudeCodeBackend()

    def test_backend_choice(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_BACKEND": "", "ANTHROPIC_API_KEY": ""}):
            self.assertEqual(resolve_backend(), "claude_code")
        with mock.patch.dict(os.environ, {"ANTHROPIC_BACKEND": "", "ANTHROPIC_API_KEY": "sk"}):
            self.assertEqual(resolve_backend(), "api")
        with mock.patch.dict(os.environ, {"ANTHROPIC_BACKEND": "claude_code", "ANTHROPIC_API_KEY": "sk"}):
            self.assertEqual(resolve_backend(), "claude_code")


class EngineWithClaudeCodeTest(unittest.TestCase):
    def test_full_episode_through_claude_code_with_a_validation_retry(self):
        bad = episode_data(climax_scene_id=4)
        runner = FakeRunner(stream(json.dumps(bad), session="s9"),
                            stream(json.dumps(episode_data()), session="s9"))
        backend = ClaudeCodeBackend(runner=runner, claude_bin="claude")
        with tempfile.TemporaryDirectory() as tmp:
            db = StoryDatabase(Path(tmp) / "t.db")
            engine = StoryEngine(db, fetcher=fake_fetch, output_dir=Path(tmp) / "out", backend=backend)
            path = engine.create_episode(date(2026, 11, 1))
            self.assertTrue(path.exists())
            self.assertEqual(db.topic_for_date(date(2026, 11, 1)).status, "verified")
        second_cmd, second_kwargs = runner.calls[1]
        self.assertIn("--resume", second_cmd)
        self.assertIn("clímax", second_kwargs["input"])
        self.assertEqual(canonical_url(SRC_A), canonical_url(SRC_A))


if __name__ == "__main__":
    unittest.main()
