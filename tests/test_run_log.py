"""Focused unit coverage for the shared run-log normalization primitive.

These tests exercise ``news_pipeline/run_log`` in isolation from the pipeline
and UI so the raw-normalization and classification policy has one
deterministic home. Style follows the other ``tests/`` modules: plain
``unittest``, synthetic raw strings, no external dependencies.
"""

from __future__ import annotations

import unittest

from news_pipeline.run_log import (
    ConciseLogWriter,
    RunLogEvent,
    clean_line,
    normalize_file_text,
    normalize_lines,
    normalize_text,
    parse_event,
    parse_stream,
)

_METER_LINE = "[3/9 clustering] [####----------------] 10000/200000 steps"
_METER_FINAL = "[3/9 clustering] [####################] 200000/200000 steps"


class NormalizeTextTests(unittest.TestCase):
    def test_crlf_is_a_newline_and_lone_cr_overwrites(self) -> None:
        self.assertEqual(normalize_text("one\r\ntwo"), "one\ntwo")
        self.assertEqual(normalize_text("one\rtwo"), "two")
        self.assertEqual(normalize_text("one\rtwo\rthree"), "three")

    def test_repeated_cr_meter_updates_keep_the_newest_segment(self) -> None:
        raw = "\r[3/9 clustering] [#-------------------] 1/200000 steps\033[K" + "\r" + _METER_LINE + "\033[K"
        self.assertEqual(normalize_text(raw), _METER_LINE)

    def test_trailing_carriage_return_does_not_erase_the_line(self) -> None:
        self.assertEqual(normalize_text("hello\r"), "hello")
        self.assertEqual(normalize_text("\r"), "")

    def test_ansi_csi_and_control_sequences_are_removed(self) -> None:
        self.assertEqual(normalize_text("a\033[Kb"), "ab")
        self.assertEqual(normalize_text("a\033[2Kb"), "ab")
        self.assertEqual(normalize_text("a\033[1;5Hb"), "ab")
        self.assertEqual(normalize_text("a\x07b"), "ab")
        self.assertEqual(normalize_text("a\x1bb"), "ab")

    def test_ordinary_punctuation_and_data_text_is_preserved(self) -> None:
        text = "data: not SSE framing here\nFile \"x.py\", line 3, in <module>\n"
        self.assertEqual(normalize_text(text), text)

    def test_empty_and_whitespace_input_normalize_to_nothing(self) -> None:
        self.assertEqual(normalize_text(""), "")
        self.assertEqual(normalize_text("   "), "   ")
        self.assertEqual(normalize_lines(""), [])
        self.assertEqual(normalize_lines("   \n\t\n"), [])

    def test_mixed_raw_stream_with_messages_and_meters(self) -> None:
        raw = "WARNING: low coverage\n" + "\r" + _METER_LINE + "\033[K\r\n" + _METER_FINAL + "\n"
        self.assertEqual(normalize_lines(raw), ["WARNING: low coverage", _METER_LINE, _METER_FINAL])


class CleanLineTests(unittest.TestCase):
    def test_progress_prefix_and_email_wrappers_are_normalized(self) -> None:
        self.assertEqual(clean_line("[progress] hello"), "hello")
        self.assertEqual(clean_line("  [progress]   hello  "), "hello")
        # The historical cleaner rewrites only the wrapper label and keeps the
        # surrounding text; continuation indentation is preserved separately.
        self.assertEqual(clean_line("--- [EMAIL]: sent ---"), "[email] sent ---")
        self.assertEqual(clean_line("--- [UNSUBSCRIBE]: done ---"), "[unsubscribe] done ---")

    def test_plain_lines_are_only_stripped(self) -> None:
        self.assertEqual(clean_line("  keep me  "), "keep me")
        self.assertEqual(clean_line(""), "")


class ParseEventTests(unittest.TestCase):
    def test_meter_line_classifies_as_progress_with_stable_stage(self) -> None:
        event = parse_event(_METER_LINE)
        self.assertEqual(event.kind, "progress")
        self.assertEqual(event.stage, "clustering")
        self.assertFalse(event.complete)
        self.assertFalse(event.replace)

    def test_final_counts_mark_complete_progress(self) -> None:
        event = parse_event(_METER_FINAL)
        self.assertEqual(event.kind, "progress")
        self.assertTrue(event.complete)

    def test_stage_key_uses_the_rendered_label(self) -> None:
        self.assertEqual(parse_event("[7/9 story drafting] [####----------------] 12/47 stories").stage, "story drafting")
        self.assertEqual(parse_event("[2/9 sources] [###-----------------] 1/57 sources").stage, "sources")
        self.assertEqual(parse_event("[9/9 report] [##------------------] 2/5 steps").stage, "report")
        self.assertEqual(parse_event("[custom] [###-----------------] 1/3 steps").stage, "custom")

    def test_meter_detail_suffix_does_not_break_classification(self) -> None:
        event = parse_event(f"{_METER_LINE} | pairwise similarity | 42 linked pairs")
        self.assertEqual(event.kind, "progress")
        self.assertEqual(event.stage, "clustering")

    def test_stage_headers_warnings_and_errors_stay_messages(self) -> None:
        for line in (
            "[3/9 clustering] Clustering 139 candidate articles.",
            "[2/9 sources] Starting source 1/57: Reuters",
            "WARNING: low coverage for topic",
            "Retrying task: attempt 1/3 failed (TimeoutError: boom)",
            "RuntimeError: synthetic failure",
            "[9/9 finalize] Daily news run complete.",
            "No steps selected.",
        ):
            self.assertEqual(parse_event(line).kind, "message", line)

    def test_meter_lookalikes_without_exact_bar_shape_stay_messages(self) -> None:
        self.assertEqual(parse_event("[3/9 clustering] [----] 1/2 steps").kind, "message")
        self.assertEqual(parse_event("[3/9 clustering] [####] 1/2 steps").kind, "message")
        self.assertEqual(parse_event("prefix [3/9 clustering] [####----------------] 1/2 steps").kind, "message")

    def test_event_dict_shape_is_json_ready(self) -> None:
        self.assertEqual(
            parse_event(_METER_LINE).to_dict(),
            {"line": _METER_LINE, "kind": "progress", "stage": "clustering"},
        )
        self.assertEqual(
            parse_event(_METER_FINAL).to_dict(),
            {"line": _METER_FINAL, "kind": "progress", "stage": "clustering", "complete": True},
        )
        self.assertEqual(parse_event("hello").to_dict(), {"line": "hello", "kind": "message"})

    def test_run_log_event_to_dict_honors_replace_metadata(self) -> None:
        event = RunLogEvent(line="x", kind="progress", stage="s", replace=True)
        self.assertEqual(event.to_dict(), {"line": "x", "kind": "progress", "stage": "s", "replace": True})


class ParseStreamTests(unittest.TestCase):
    def test_multiline_messages_preserve_every_line(self) -> None:
        traceback = (
            "Traceback (most recent call last):\n"
            "  File \"news_pipeline/pipeline.py\", line 123, in run\n"
            "RuntimeError: synthetic failure\n"
        )
        events = parse_stream(traceback)
        self.assertEqual([event.kind for event in events], ["message", "message", "message"])
        self.assertEqual([event.line for event in events], traceback.rstrip("\n").split("\n"))

    def test_stream_mixes_messages_and_meters_in_order(self) -> None:
        events = parse_stream(
            "WARNING: careful\n" + _METER_LINE + "\n" + "Retrying task: 1/2\n" + _METER_FINAL + "\n"
        )
        self.assertEqual(
            [(event.kind, event.line) for event in events],
            [
                ("message", "WARNING: careful"),
                ("progress", _METER_LINE),
                ("message", "Retrying task: 1/2"),
                ("progress", _METER_FINAL),
            ],
        )

    def test_duplicate_snapshots_parse_to_identical_events(self) -> None:
        first = parse_stream(_METER_LINE + "\n")
        second = parse_stream(_METER_LINE + "\n")
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[0].line, _METER_LINE)


class NormalizeFileTextTests(unittest.TestCase):
    def test_raw_transcript_collapses_meter_runs_but_keeps_meaningful_lines(self) -> None:
        raw = (
            "[3/9 clustering] [###-----------------] 1000/200000 steps\n"
            "[3/9 clustering] [##------------------] 2000/200000 steps\n"
            "[3/9 clustering] [#-------------------] 3000/200000 steps\n"
            "WARNING: low coverage\n"
            "[3/9 clustering] [####################] 200000/200000 steps\n"
            "Traceback (most recent call last):\n"
            "RuntimeError: synthetic failure\n"
        )
        normalized = normalize_file_text(raw)
        self.assertIn("[3/9 clustering] [###-----------------] 1000/200000 steps", normalized)
        self.assertIn("[3/9 clustering] [#-------------------] 3000/200000 steps", normalized)
        self.assertIn("WARNING: low coverage", normalized)
        self.assertIn("[3/9 clustering] [####################] 200000/200000 steps", normalized)
        self.assertIn("Traceback (most recent call last):", normalized)
        self.assertIn("RuntimeError: synthetic failure", normalized)
        self.assertNotIn("2000/200000 steps", normalized)

    def test_cr_and_ansi_are_removed_before_collapsing(self) -> None:
        raw = (
            "\r[3/9 clustering] [###-----------------] 1000/200000 steps\033[K\n"
            "[3/9 clustering] [##------------------] 2000/200000 steps\033[K\n"
            "\r[3/9 clustering] [####################] 200000/200000 steps\033[K\n"
            "WARNING: done\n"
        )
        normalized = normalize_file_text(raw)
        self.assertNotIn("\r", normalized)
        self.assertNotIn("\033", normalized)
        self.assertIn("1000/200000 steps", normalized)
        self.assertIn("200000/200000 steps", normalized)
        self.assertNotIn("2000/200000 steps", normalized)
        self.assertIn("WARNING: done", normalized)

    def test_timestamped_concise_lines_are_not_collapsed(self) -> None:
        raw = (
            "2026-06-06T10:00:01 [3/9 clustering] [###-----------------] 1000/200000 steps\n"
            "2026-06-06T10:00:02 [3/9 clustering] [####################] 200000/200000 steps\n"
        )
        self.assertEqual(normalize_file_text(raw), raw.rstrip("\n"))

    def test_stage_boundaries_keep_first_and_last_of_each_run(self) -> None:
        raw = (
            "[2/9 sources] [###-----------------] 1/57 sources\n"
            "[2/9 sources] [##------------------] 2/57 sources\n"
            "[3/9 clustering] [###-----------------] 1000/200000 steps\n"
            "[3/9 clustering] [##------------------] 2000/200000 steps\n"
        )
        normalized = normalize_file_text(raw)
        self.assertIn("[2/9 sources] [###-----------------] 1/57 sources", normalized)
        self.assertIn("[2/9 sources] [##------------------] 2/57 sources", normalized)
        self.assertIn("[3/9 clustering] [###-----------------] 1000/200000 steps", normalized)
        self.assertIn("[3/9 clustering] [##------------------] 2000/200000 steps", normalized)

    def test_empty_input_yields_empty_text(self) -> None:
        self.assertEqual(normalize_file_text(""), "")
        self.assertEqual(normalize_file_text("  \n\t\n"), "")


class ConciseLogWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.written: list[str] = []
        self.writer = ConciseLogWriter(write_line=self.written.append)

    def test_initial_and_final_snapshots_append_immediately(self) -> None:
        self.writer.meter("clustering", "1000/200000", final=False)
        self.writer.meter("clustering", "200000/200000", final=True)
        self.assertEqual(self.written, ["1000/200000", "200000/200000"])

    def test_intermediate_and_duplicate_snapshots_are_suppressed(self) -> None:
        self.writer.meter("clustering", "1000/200000", final=False)
        self.writer.meter("clustering", "50000/200000", final=False)
        self.writer.meter("clustering", "50000/200000", final=False)
        self.writer.meter("clustering", "200000/200000", final=True)
        self.assertEqual(self.written, ["1000/200000", "200000/200000"])

    def test_transition_flushes_pending_intermediate_snapshot(self) -> None:
        self.writer.meter("clustering", "1000/200000", final=False)
        self.writer.meter("clustering", "50000/200000", final=False)
        self.writer.message("[4/9 model] Starting model server.")
        self.writer.meter("model", "1/3", final=False)
        self.assertEqual(
            self.written,
            ["1000/200000", "50000/200000", "[4/9 model] Starting model server.", "1/3"],
        )

    def test_failure_close_flushes_pending_snapshot(self) -> None:
        self.writer.meter("clustering", "1000/200000", final=False)
        self.writer.meter("clustering", "50000/200000", final=False)
        self.writer.flush()
        self.assertEqual(self.written, ["1000/200000", "50000/200000"])

    def test_stage_switch_without_message_flushes_previous_run(self) -> None:
        self.writer.meter("sources", "1/57", final=False)
        self.writer.meter("sources", "2/57", final=False)
        self.writer.meter("clustering", "1000/200000", final=False)
        self.assertEqual(self.written, ["1/57", "2/57", "1000/200000"])

    def test_final_supersedes_pending_intermediate_without_flushing_it(self) -> None:
        self.writer.meter("clustering", "1000/200000", final=False)
        self.writer.meter("clustering", "50000/200000", final=False)
        self.writer.meter("clustering", "200000/200000", final=True)
        self.assertEqual(self.written, ["1000/200000", "200000/200000"])

    def test_duplicate_final_writes_once(self) -> None:
        self.writer.meter("clustering", "200000/200000", final=True)
        self.writer.meter("clustering", "200000/200000", final=True)
        self.assertEqual(self.written, ["200000/200000"])

    def test_messages_append_in_order_after_pending_flush(self) -> None:
        self.writer.meter("sources", "1/57", final=False)
        self.writer.message("WARNING: careful")
        self.writer.message("Traceback line")
        self.assertEqual(self.written, ["1/57", "WARNING: careful", "Traceback line"])


if __name__ == "__main__":
    unittest.main()
