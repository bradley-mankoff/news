from __future__ import annotations

import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import automation.ui_surface_runner as ui_surface_runner


class UISurfaceRunnerTest(unittest.TestCase):
    def test_read_request_consumes_atomic_start_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            request_path = Path(tmpdir) / "request.json"
            request = {
                "action": "start",
                "host": "127.0.0.1",
                "port": 8766,
                "worktree": "/tmp/news-ui-runtime",
                "news_bin": "/tmp/news-ui-runtime/.venv/bin/news",
            }
            request_path.write_text(json.dumps(request))
            with patch.object(ui_surface_runner, "REQUEST_PATH", request_path):
                self.assertEqual(ui_surface_runner._read_request(), request)
            self.assertFalse(request_path.exists())

    def test_invalid_request_is_left_for_operator_inspection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            request_path = Path(tmpdir) / "request.json"
            request_path.write_text("not json")
            with patch.object(ui_surface_runner, "REQUEST_PATH", request_path):
                self.assertIsNone(ui_surface_runner._read_request())
            self.assertTrue(request_path.exists())

    def test_start_launches_owned_process_with_runtime_environment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "runtime"
            runtime.mkdir()
            news_bin = runtime / "news"
            news_bin.write_text("#!/bin/sh\n")
            request = {
                "worktree": str(runtime),
                "news_bin": str(news_bin),
                "host": "127.0.0.1",
                "port": 8766,
            }
            process = Mock(pid=123)
            with patch.object(
                ui_surface_runner.subprocess, "Popen", return_value=process
            ) as popen:
                started = ui_surface_runner._start(request)
        self.assertIs(started, process)
        self.assertEqual(
            popen.call_args.args[0],
            [str(news_bin), "ui", "--host", "127.0.0.1", "--port", "8766"],
        )
        self.assertEqual(popen.call_args.kwargs["cwd"], str(runtime))
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_stop_escalates_to_kill_when_ui_ignores_term(self):
        process = Mock(pid=99)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("ui", 10), None]
        with patch.object(ui_surface_runner.os, "killpg") as killpg:
            ui_surface_runner._stop(process)
        self.assertEqual(
            killpg.call_args_list,
            [
                call(99, signal.SIGTERM),
                call(99, signal.SIGKILL),
            ],
        )


if __name__ == "__main__":
    unittest.main()
