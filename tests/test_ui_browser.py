"""Headless Chromium coverage for the Report Review surface.

Drives the real embedded UI served by the fixture's ``NewsUIServer`` over an
ephemeral localhost port: independent status badges and metadata warnings,
historical run selection with literal ``<script>`` report text, failed-run
visibility, and terminal completion/failure refresh lifecycles. All page
fetches hit the live fixture server and no API route is mocked. The only
substitution is ``ui_module.build_command`` in terminal tests, which replaces
the real model pipeline with tiny deterministic child processes.
"""

from __future__ import annotations

import re
import unittest
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

from news_pipeline import ui as ui_module
from tests.ui_review_fixtures import (
    COMPLETED_RUN_ID,
    FAILED_RUN_ID,
    REFRESHED_REPORT_BODY,
    REFRESHED_RUN_ID,
    ReviewFixture,
)


class ReportReviewBrowserTests(unittest.TestCase):
    def test_review_tab_badges_warnings_selection_and_literal_text(self) -> None:
        with ReviewFixture() as fixture:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    page.goto(fixture.base_url, wait_until="load")
                    page.locator('nav button[data-tab="review"]').click()

                    # Latest review: three independent badges plus the
                    # field-specific metadata warning from the malformed
                    # sibling ``settings`` field.
                    expect(page.locator("#reviewMount")).to_contain_text(
                        "Latest run review"
                    )
                    expect(page.locator("#historyTable")).to_be_visible()
                    badge_row = page.locator("#reviewMount .badge-row")
                    expect(badge_row).to_contain_text("run: completed")
                    expect(badge_row).to_contain_text("report: available")
                    expect(badge_row).to_contain_text("delivery: failed")
                    expect(page.locator("#reviewMount")).to_contain_text("settings:")
                    expect(page.locator("#reviewMount")).to_contain_text(
                        "expected a JSON object"
                    )
                    # The rolling report body is literal text, never markup.
                    report_pane = page.locator("#reviewReportPane")
                    expect(report_pane).to_contain_text(
                        "<script>alert('latest')</script>"
                    )
                    self.assertEqual(report_pane.locator("script").count(), 0)

                    # Completed historical selection: OKF report text is read
                    # from the real bundle and rendered as plain text.
                    completed_row = page.locator(
                        f'#historyTable tr[data-run-id="{COMPLETED_RUN_ID}"]'
                    )
                    expect(completed_row).to_be_visible()
                    completed_row.click()
                    expect(page.locator("#openReportBtn")).to_be_enabled()
                    expect(page.locator("#runDetail")).to_contain_text(
                        "run: completed"
                    )
                    expect(page.locator("#runDetail")).to_contain_text(
                        "report: available"
                    )
                    expect(page.locator("#runDetail")).to_contain_text(
                        "delivery: failed"
                    )
                    selected_pane = page.locator("#selectedReportPane")
                    expect(selected_pane).to_contain_text(
                        "<script>alert('historical')</script>"
                    )
                    self.assertEqual(selected_pane.locator("script").count(), 0)

                    # Failed historical selection stays visible with its
                    # independent not-generated report state.
                    failed_row = page.locator(
                        f'#historyTable tr[data-run-id="{FAILED_RUN_ID}"]'
                    )
                    expect(failed_row).to_be_visible()
                    failed_row.click()
                    expect(page.locator("#runDetail")).to_contain_text("run: failed")
                    expect(page.locator("#runDetail")).to_contain_text(
                        "report: not_generated"
                    )
                    expect(page.locator("#runDetail")).to_contain_text(
                        "delivery: failed"
                    )
                    expect(page.locator("#selectedReportPane")).to_contain_text(
                        "No report was generated for this run."
                    )
                finally:
                    browser.close()

    def test_terminal_failed_run_clears_controls_and_refreshes_review(self) -> None:
        with ReviewFixture(latest_completed=False) as fixture:
            with patch.object(
                ui_module,
                "build_command",
                return_value=fixture.failed_child_command(),
            ):
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.goto(fixture.base_url, wait_until="load")
                        page.locator("#runBtn").click()

                        # A non-zero child still closes the SSE lifecycle and
                        # restores the controls instead of leaving a spinner.
                        expect(page.locator("#status")).to_contain_text("failed")
                        expect(page.locator("#runBtn")).to_be_enabled()
                        expect(page.locator("#stopBtn")).to_be_disabled()

                        page.locator('nav button[data-tab="review"]').click()
                        expect(page.locator("#reviewMount")).to_contain_text(
                            REFRESHED_RUN_ID
                        )
                        expect(page.locator("#reviewMount")).to_contain_text(
                            "run: failed"
                        )
                        expect(page.locator("#reviewMount")).to_contain_text(
                            "report: not_generated"
                        )
                        expect(page.locator("#reviewMount")).to_contain_text(
                            "No report was generated for this run."
                        )
                        failed_row = page.locator(
                            f'#historyTable tr[data-run-id="{REFRESHED_RUN_ID}"]'
                        )
                        expect(failed_row).to_be_visible()
                        failed_row.click()
                        expect(page.locator("#runDetail")).to_contain_text(
                            "run: failed"
                        )
                        expect(page.locator("#runDetail")).to_contain_text(
                            "report: not_generated"
                        )
                        self.assertIsNone(ui_module.RUN_MANAGER.active())
                    finally:
                        browser.close()

    def test_terminal_completed_run_refreshes_review_and_navigates(self) -> None:
        with ReviewFixture(latest_completed=False) as fixture:
            with patch.object(
                ui_module, "build_command", return_value=fixture.child_command()
            ):
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    try:
                        page = browser.new_page()
                        page.goto(fixture.base_url, wait_until="load")

                        # Boot lands on Run Setup with a failed/unavailable
                        # latest review so the refresh transition is visible.
                        expect(page.locator("#historyTable")).to_contain_text(
                            FAILED_RUN_ID
                        )
                        expect(page.locator("#reviewMount")).to_contain_text(
                            "run: failed"
                        )
                        expect(page.locator("#reviewMount")).to_contain_text(
                            "No report was generated for this run."
                        )

                        # Real POST /api/run + SSE terminal lifecycle; the
                        # child atomically replaces each rolling artifact
                        # before exiting.
                        page.locator("#runBtn").click()

                        # Terminal completion navigates to the Review tab with
                        # the refreshed durable review data.
                        expect(page.locator("#review")).to_have_class(
                            re.compile(r"\bactive\b")
                        )
                        expect(page.locator("#reviewMount")).to_contain_text(
                            REFRESHED_RUN_ID
                        )
                        expect(page.locator("#reviewReportPane")).to_contain_text(
                            "<script>alert('latest')</script>"
                        )
                        badge_row = page.locator("#reviewMount .badge-row")
                        expect(badge_row).to_contain_text("run: completed")
                        expect(badge_row).to_contain_text("report: available")
                        expect(badge_row).to_contain_text("delivery: failed")

                        # The child also creates a durable history row and OKF
                        # bundle, so the refreshed run can be selected from
                        # history rather than only appearing in rolling files.
                        refreshed_row = page.locator(
                            f'#historyTable tr[data-run-id="{REFRESHED_RUN_ID}"]'
                        )
                        expect(refreshed_row).to_be_visible()
                        refreshed_row.click()
                        expect(page.locator("#openReportBtn")).to_be_enabled()
                        expect(page.locator("#runDetail")).to_contain_text(
                            "run: completed"
                        )
                        expect(page.locator("#runDetail")).to_contain_text(
                            "report: available"
                        )
                        expect(page.locator("#selectedReportPane")).to_contain_text(
                            REFRESHED_REPORT_BODY
                        )

                        # The failed historical run remains visible and
                        # selectable after the review refresh.
                        failed_row = page.locator(
                            f'#historyTable tr[data-run-id="{FAILED_RUN_ID}"]'
                        )
                        expect(failed_row).to_be_visible()
                        failed_row.click()
                        expect(page.locator("#runDetail")).to_contain_text(
                            "run: failed"
                        )
                        expect(page.locator("#runDetail")).to_contain_text(
                            "report: not_generated"
                        )
                        expect(page.locator("#selectedReportPane")).to_contain_text(
                            "No report was generated for this run."
                        )
                        self.assertIsNone(ui_module.RUN_MANAGER.active())
                    finally:
                        browser.close()


if __name__ == "__main__":
    unittest.main()
