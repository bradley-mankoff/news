from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
