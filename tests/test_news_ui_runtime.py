from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

import automation.news_ui_runtime as news_ui_runtime
from automation.news_ui_runtime import sync_local_develop


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class SyncLocalDevelopTest(unittest.TestCase):
    """The poller owns a clean UI worktree and never kills foreign UIs."""

    def test_dry_run_never_touches_git(self):
        with patch("automation.news_ui_runtime.DRY_RUN", True), \
                patch("automation.news_ui_runtime.subprocess.run",
                      side_effect=AssertionError("must not run")) as run:
            self.assertIn("dry-run", sync_local_develop())
            run.assert_not_called()

    def test_fetch_failure_is_loud(self):
        with patch.object(
            news_ui_runtime,
            "_run_git",
            return_value=_cp(1, stderr="boom"),
        ):
            msg = sync_local_develop()
        self.assertIn("LOCAL SYNC FAILED", msg)
        self.assertIn("fetch", msg)

    def test_root_worktree_changes_do_not_block_runtime_sync(self):
        with (
            patch.object(news_ui_runtime, "_run_git", return_value=_cp()) as git,
            patch.object(
                news_ui_runtime,
                "_sync_ui_runtime",
                return_value=(True, "UI runtime already at abc; UI already running"),
            ) as sync,
        ):
            msg = sync_local_develop()
        self.assertIn("LOCAL SYNC:", msg)
        sync.assert_called_once_with()
        self.assertEqual(git.call_count, 1)
        self.assertEqual(git.call_args.args[0], ["fetch", "origin", "develop"])

    def test_runtime_dirty_is_a_failure_not_a_silent_skip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "ui-runtime"
            runtime.mkdir()
            (runtime / ".git").write_text("gitdir: /tmp/worktree")
            with (
                patch.object(news_ui_runtime, "UI_RUNTIME_WORKTREE", runtime),
                patch.object(
                    news_ui_runtime,
                    "_run_git",
                    return_value=_cp(stdout=" M local-change\n"),
                ),
            ):
                ok, detail = news_ui_runtime._sync_ui_runtime()
        self.assertFalse(ok)
        self.assertIn("dirty", detail)

    def test_runtime_update_restarts_owned_ui(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "ui-runtime"
            runtime.mkdir()
            (runtime / ".git").write_text("gitdir: /tmp/worktree")

            def fake_git(args, **_):
                if args == ["status", "--porcelain"]:
                    return _cp()
                if args == ["rev-parse", "origin/develop"]:
                    return _cp(stdout="abcdef123456789\n")
                if args == ["rev-parse", "HEAD"]:
                    return _cp(stdout="oldhead\n")
                if args == ["reset", "--hard", "origin/develop"]:
                    return _cp(stdout="HEAD is now at abc\n")
                raise AssertionError(args)

            with (
                patch.object(news_ui_runtime, "UI_RUNTIME_WORKTREE", runtime),
                patch.object(news_ui_runtime, "_run_git", side_effect=fake_git) as git,
                patch.object(news_ui_runtime, "_ui_running", return_value=False),
                patch.object(news_ui_runtime, "_port_open", return_value=False),
                patch.object(news_ui_runtime, "_restart_ui", return_value=True) as restart,
            ):
                ok, detail = news_ui_runtime._sync_ui_runtime()
        self.assertTrue(ok)
        self.assertIn("updated", detail)
        restart.assert_called_once_with()
        self.assertIn(
            ["reset", "--hard", "origin/develop"],
            [call.args[0] for call in git.call_args_list],
        )

    def test_foreign_port_is_not_killed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "ui-runtime"
            runtime.mkdir()
            (runtime / ".git").write_text("gitdir: /tmp/worktree")

            def fake_git(args, **_):
                if args == ["status", "--porcelain"]:
                    return _cp()
                if args == ["rev-parse", "origin/develop"]:
                    return _cp(stdout="samehead\n")
                if args == ["rev-parse", "HEAD"]:
                    return _cp(stdout="samehead\n")
                raise AssertionError(args)

            with (
                patch.object(news_ui_runtime, "UI_RUNTIME_WORKTREE", runtime),
                patch.object(news_ui_runtime, "_run_git", side_effect=fake_git),
                patch.object(news_ui_runtime, "_ui_running", return_value=False),
                patch.object(news_ui_runtime, "_port_open", return_value=True),
                patch.object(news_ui_runtime, "_load_cmux_registration", return_value=None),
                patch.object(news_ui_runtime, "_restart_ui") as restart,
            ):
                ok, detail = news_ui_runtime._sync_ui_runtime()
        self.assertFalse(ok)
        self.assertIn("unmanaged", detail)
        restart.assert_not_called()
    def test_registered_cmux_can_adopt_existing_ui_port(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "ui-runtime"
            runtime.mkdir()
            (runtime / ".git").write_text("gitdir: /tmp/worktree")

            def fake_git(args, **_):
                if args == ["status", "--porcelain"]:
                    return _cp()
                if args == ["rev-parse", "origin/develop"]:
                    return _cp(stdout="samehead\n")
                if args == ["rev-parse", "HEAD"]:
                    return _cp(stdout="samehead\n")
                raise AssertionError(args)

            with (
                patch.object(news_ui_runtime, "UI_RUNTIME_WORKTREE", runtime),
                patch.object(news_ui_runtime, "_run_git", side_effect=fake_git),
                patch.object(news_ui_runtime, "_ui_running", return_value=False),
                patch.object(news_ui_runtime, "_port_open", return_value=True),
                patch.object(
                    news_ui_runtime,
                    "_load_cmux_registration",
                    return_value={"socket_path": "/tmp/s", "workspace_ref": "w", "surface_ref": "s"},
                ),
                patch.object(news_ui_runtime, "_restart_ui", return_value=True) as restart,
            ):
                ok, detail = news_ui_runtime._sync_ui_runtime()
        self.assertTrue(ok)
        self.assertIn("started", detail)
        restart.assert_called_once_with()


    def test_managed_process_launch_has_explicit_runtime_ownership(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "ui-runtime"
            binary = runtime / ".venv/bin/news"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n")
            state = Path(tmpdir) / "ui-state.json"
            log_path = Path(tmpdir) / "ui.log"
            with (
                patch.object(news_ui_runtime, "UI_RUNTIME_WORKTREE", runtime),
                patch.object(news_ui_runtime, "UI_RUNTIME_STATE_PATH", state),
                patch.object(news_ui_runtime, "UI_LOG_PATH", log_path),
                patch.object(news_ui_runtime, "UI_NEWS_BIN", binary),
                patch.object(news_ui_runtime, "_port_open", return_value=False),
                patch.object(news_ui_runtime, "_wait_for_ui", return_value=True),
                patch.object(news_ui_runtime, "_process_start_marker", return_value="marker"),
                patch("automation.news_ui_runtime.subprocess.Popen") as popen,
            ):
                popen.return_value.pid = 12345
                self.assertTrue(news_ui_runtime._start_managed_process())
                command = popen.call_args.args[0]
                kwargs = popen.call_args.kwargs
                state_data = json.loads(state.read_text())
        self.assertEqual(command[-4:], ["--host", news_ui_runtime.UI_HOST,
                                        "--port", str(news_ui_runtime.UI_PORT)])
        self.assertEqual(kwargs["cwd"], str(runtime))
        self.assertTrue(kwargs["env"]["PYTHONPATH"].startswith(str(runtime)))
        self.assertEqual(state_data["pid"], 12345)
    def test_stale_pid_marker_is_never_killed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "ui-state.json"
            state.write_text(json.dumps({
                "mode": "process",
                "pid": 12345,
                "start_marker": "old-process",
            }))
            with (
                patch.object(news_ui_runtime, "UI_RUNTIME_STATE_PATH", state),
                patch.object(news_ui_runtime, "_pid_alive", return_value=True),
                patch.object(news_ui_runtime, "_process_start_marker", return_value="new-process"),
                patch("automation.news_ui_runtime.os.kill") as kill,
            ):
                self.assertTrue(news_ui_runtime._stop_managed_process())
        kill.assert_not_called()
        self.assertFalse(state.exists())


    def test_register_ui_surface_persists_terminal_target(self):
        identify = {
            "socket_path": "/tmp/cmux.sock",
            "caller": {
                "surface_type": "terminal",
                "workspace_ref": "workspace:1",
                "surface_ref": "surface:2",
            },
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "cmux.json"
            request = Path(tmpdir) / "request.json"
            with (
                patch.object(news_ui_runtime, "UI_CMUX_STATE_PATH", state),
                patch.object(news_ui_runtime, "UI_CMUX_REQUEST_PATH", request),
                patch(
                    "automation.news_ui_runtime.subprocess.run",
                    return_value=_cp(stdout=json.dumps(identify)),
                ),
            ):
                self.assertEqual(news_ui_runtime.register_ui_surface(), 0)
            self.assertEqual(json.loads(state.read_text()), {
                "socket_path": "/tmp/cmux.sock",
                "workspace_ref": "workspace:1",
                "surface_ref": "surface:2",
                "request_path": str(request),
            })


    def test_cmux_start_writes_runtime_request_for_registered_surface(self):
        registration = {
            "socket_path": "/tmp/cmux.sock",
            "workspace_ref": "workspace:1",
            "surface_ref": "surface:2",
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "ui-state.json"
            request = Path(tmpdir) / "request.json"
            registration["request_path"] = str(request)
            with (
                patch.object(news_ui_runtime, "UI_RUNTIME_STATE_PATH", state),
                patch.object(news_ui_runtime, "_port_open", return_value=False),
                patch.object(news_ui_runtime, "_wait_for_ui", return_value=True),
            ):
                self.assertTrue(news_ui_runtime._start_cmux_ui(registration))
                state_data = json.loads(state.read_text())
                request_data = json.loads(request.read_text())
        self.assertEqual(request_data["action"], "start")
        self.assertEqual(request_data["worktree"], str(news_ui_runtime.UI_RUNTIME_WORKTREE))
        self.assertEqual(request_data["news_bin"], str(news_ui_runtime.UI_NEWS_BIN))
        self.assertEqual(request_data["host"], news_ui_runtime.UI_HOST)
        self.assertEqual(request_data["port"], news_ui_runtime.UI_PORT)
        self.assertEqual(state_data["mode"], "cmux")

    def test_sync_failure_writes_error_field_for_mechanic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "ui-state.json"
            with (
                patch.object(news_ui_runtime, "DRY_RUN", False),
                patch.object(news_ui_runtime, "UI_RUNTIME_STATE_PATH", state),
                patch.object(news_ui_runtime, "_run_git",
                             return_value=_cp(returncode=1,
                                              stderr="fetch failed")),
            ):
                result = news_ui_runtime.sync_local_develop()
            data = json.loads(state.read_text())
        self.assertTrue(result.startswith("LOCAL SYNC FAILED"))
        self.assertIn("error", data)
        self.assertIn("fetch failed", data["error"])
        self.assertIn("last_sync_failed_at", data)

    def test_sync_success_clears_error_field(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = Path(tmpdir) / "ui-state.json"
            state.write_text(json.dumps({"mode": "process", "pid": 1,
                                         "start_marker": "x",
                                         "error": "old failure"}))
            with (
                patch.object(news_ui_runtime, "DRY_RUN", False),
                patch.object(news_ui_runtime, "UI_RUNTIME_STATE_PATH", state),
                patch.object(news_ui_runtime, "_run_git", return_value=_cp()),
                patch.object(news_ui_runtime, "_sync_ui_runtime",
                             return_value=(True, "ok")),
            ):
                result = news_ui_runtime.sync_local_develop()
            data = json.loads(state.read_text())
        self.assertTrue(result.startswith("LOCAL SYNC"))
        self.assertNotIn("error", data)
        self.assertEqual(data["mode"], "process")

    def test_occupied_port_line_names_the_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "ui-runtime"
            runtime.mkdir()
            (runtime / ".git").mkdir()
            with (
                patch.object(news_ui_runtime, "UI_RUNTIME_WORKTREE", runtime),
                patch.object(news_ui_runtime, "_run_git", return_value=_cp()),
                patch.object(news_ui_runtime, "_ui_running", return_value=False),
                patch.object(news_ui_runtime, "_port_open", return_value=True),
                patch.object(news_ui_runtime, "_load_cmux_registration",
                             return_value=None),
                patch.object(news_ui_runtime, "_port_owner",
                             return_value="news pid 42"),
            ):
                ok, detail = news_ui_runtime._sync_ui_runtime()
        self.assertFalse(ok)
        self.assertIn("news pid 42", detail)
        self.assertIn("occupied by an unmanaged process", detail)

    def test_start_failure_line_includes_log_tail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = Path(tmpdir) / "ui-runtime"
            runtime.mkdir()
            (runtime / ".git").mkdir()
            log_path = Path(tmpdir) / "news-ui.log"
            log_path.write_text("line1\nline2: boom\n")
            with (
                patch.object(news_ui_runtime, "UI_RUNTIME_WORKTREE", runtime),
                patch.object(news_ui_runtime, "UI_LOG_PATH", log_path),
                patch.object(news_ui_runtime, "_run_git", return_value=_cp()),
                patch.object(news_ui_runtime, "_ui_running", return_value=False),
                patch.object(news_ui_runtime, "_port_open", return_value=False),
                patch.object(news_ui_runtime, "_restart_ui",
                             return_value=False),
            ):
                ok, detail = news_ui_runtime._sync_ui_runtime()
        self.assertFalse(ok)
        self.assertIn("line2: boom", detail)
        self.assertIn("UI start failed", detail)


if __name__ == "__main__":
    unittest.main()
