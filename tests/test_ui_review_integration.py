"""Real TCP/HTTP coverage for the Report Review and history routes.

Each test starts the real ``NewsUIServer`` on an ephemeral localhost port via
the shared :class:`tests.ui_review_fixtures.ReviewFixture` and asserts the
four read-only review/history endpoints over a live socket with real
temporary DuckDB/OKF artifacts. No route reader is patched: only the
fixture's environment isolation is active.
"""

from __future__ import annotations

import http.client
import json
import unittest
from typing import Any

from tests.ui_review_fixtures import (
    COMPLETED_RUN_ID,
    FAILED_RUN_ID,
    HISTORICAL_REPORT_BODY,
    LATEST_REPORT_BODY,
    ReviewFixture,
)


class ReviewRouteIntegrationTests(unittest.TestCase):
    def _get(
        self, fixture: ReviewFixture, path: str
    ) -> tuple[int, dict[str, str], str]:
        connection = http.client.HTTPConnection(fixture.host, fixture.port, timeout=15)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            body = response.read().decode("utf-8")
            return response.status, dict(response.getheaders()), body
        finally:
            connection.close()

    def test_latest_review_route_over_real_socket(self) -> None:
        with ReviewFixture() as fixture:
            status, headers, body = self._get(fixture, "/api/review/latest")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        payload = json.loads(body)
        self.assertIsNone(payload["error"])
        self.assertEqual(payload["run_id"], "2026-08-09_10-00-00")
        self.assertEqual(payload["run_status"], "completed")
        self.assertEqual(payload["report_status"], "available")
        self.assertEqual(payload["delivery_status"], "failed")
        self.assertEqual(payload["delivery"]["phase"], "send")
        self.assertEqual(
            payload["delivery"]["rejected_recipients"], ["reader@example.com"]
        )
        self.assertEqual(payload["report_text"], LATEST_REPORT_BODY)
        self.assertIn("<script>alert('latest')</script>", payload["report_text"])
        # The wrong-shaped sibling settings field is reported without hiding
        # valid status/report/delivery data and without echoing raw JSON.
        self.assertEqual(
            payload["metadata_read_errors"], {"settings": "expected a JSON object"}
        )
        self.assertNotIn("not", payload["metadata_read_errors"]["settings"])
        self.assertIn(str(fixture.output_dir), payload["paths"]["latest_run_markdown"])

    def test_history_list_route_over_real_socket(self) -> None:
        with ReviewFixture() as fixture:
            status, headers, body = self._get(fixture, "/api/history")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        payload = json.loads(body)
        self.assertIsNone(payload["error"])
        runs = {run["run_id"]: run for run in payload["runs"]}
        self.assertEqual(
            set(runs), {COMPLETED_RUN_ID, FAILED_RUN_ID}
        )
        completed = runs[COMPLETED_RUN_ID]
        self.assertEqual(completed["run_status"], "completed")
        self.assertEqual(completed["report_status"], "available")
        self.assertEqual(completed["delivery_status"], "failed")
        self.assertEqual(completed["report_count"], 1)
        self.assertGreaterEqual(completed["artifact_count"], 1)
        failed = runs[FAILED_RUN_ID]
        self.assertEqual(failed["run_status"], "failed")
        self.assertEqual(failed["report_status"], "not_generated")
        self.assertEqual(failed["delivery_status"], "failed")
        self.assertEqual(failed["report_count"], 0)

    def test_completed_run_detail_route_over_real_socket(self) -> None:
        with ReviewFixture() as fixture:
            status, _, body = self._get(fixture, f"/api/history/{COMPLETED_RUN_ID}")
        self.assertEqual(status, 200)
        details: dict[str, Any] = json.loads(body)
        self.assertEqual(details["run_id"], COMPLETED_RUN_ID)
        self.assertEqual(details["run_status"], "completed")
        self.assertEqual(details["report_status"], "available")
        self.assertEqual(details["delivery_status"], "failed")
        self.assertEqual(details["delivery"]["error_message"], "refused recipient")
        self.assertEqual(details["report_count"], 1)
        self.assertTrue(
            any(artifact["family"] == "final_report" for artifact in details["artifacts"])
        )
        self.assertEqual(
            details["metadata_read_errors"], {"settings": "expected object metadata"}
        )
        self.assertTrue(details["okf_path"].endswith(f"okf/{COMPLETED_RUN_ID}"))

    def test_historical_report_route_over_real_socket(self) -> None:
        with ReviewFixture() as fixture:
            status, headers, body = self._get(
                fixture, f"/api/history/{COMPLETED_RUN_ID}/report"
            )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn(HISTORICAL_REPORT_BODY, body)
        self.assertIn("<script>alert('historical')</script>", body)

    def test_failed_detail_and_missing_run_report_negative_routes(self) -> None:
        with ReviewFixture() as fixture:
            status, _, body = self._get(fixture, f"/api/history/{FAILED_RUN_ID}")
        self.assertEqual(status, 200)
        details = json.loads(body)
        self.assertEqual(details["run_status"], "failed")
        self.assertEqual(details["report_status"], "not_generated")
        self.assertEqual(details["delivery_status"], "failed")
        self.assertEqual(details["report_count"], 0)
        self.assertEqual(details["artifacts"], [])

        with ReviewFixture() as fixture:
            status, _, body = self._get(
                fixture, f"/api/history/{FAILED_RUN_ID}/report"
            )
            self.assertEqual(status, 404)
            self.assertEqual(
                json.loads(body), {"error": "Report not available for this run."}
            )
            status, _, body = self._get(fixture, "/api/history/not-a-run")
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(body), {"error": "Run not found."})
            status, _, body = self._get(fixture, "/api/history/not-a-run/report")
            self.assertEqual(status, 404)
            self.assertEqual(
                json.loads(body), {"error": "Report not available for this run."}
            )


if __name__ == "__main__":
    unittest.main()
