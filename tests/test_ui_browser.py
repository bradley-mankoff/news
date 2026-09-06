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

import json
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

    def test_model_catalog_is_closed_and_search_is_lazy_on_both_surfaces(self) -> None:
        """The catalog stays compact until opened and search stays explicit."""
        with ReviewFixture() as fixture:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    for path, guided in (("?wizard=0", False), ("", True)):
                        page = browser.new_page()
                        search_requests: list[str] = []

                        def handle_search(route) -> None:
                            search_requests.append(route.request.url)
                            route.fulfill(json={"models": [], "error": None})

                        page.route("**/api/models/search**", handle_search)
                        page.goto(f"{fixture.base_url}{path}", wait_until="load")
                        if guided:
                            page.locator("#wizardNext").click()
                            page.locator("#wizardNext").click()

                        disclosure = page.locator("#modelCatalogDisclosure")
                        expect(disclosure).to_have_count(1)
                        self.assertIsNone(disclosure.get_attribute("open"))
                        expect(page.locator("#modelSearchBtn")).not_to_be_visible()
                        self.assertEqual(search_requests, [])

                        page.locator("#modelCatalogDisclosure > summary").click()
                        expect(page.locator("#modelSearchBtn")).to_be_visible()
                        self.assertEqual(search_requests, [])

                        page.locator("#modelSearchQuery").fill("gemma")
                        page.locator("#modelSearchBtn").click()
                        expect(page.locator("#modelSearchResults")).to_contain_text(
                            "No models found."
                        )
                        self.assertEqual(len(search_requests), 1)
                        page.close()
                finally:
                    browser.close()

    def test_model_catalog_handlers_survive_wizard_and_setup_rerenders(self) -> None:
        """Search and model selection remain wired after fresh markup renders."""
        model = {
            "id": "owner/test-model",
            "hf_url": "https://example.test/test-model",
            "pipeline_tag": "text-generation",
            "library_name": "mlx",
            "downloads": 1,
            "likes": 1,
            "context_length": 4096,
            "runtime_fit": {
                "status": "managed_mlx_vlm",
                "reason": "managed test model",
            },
        }
        with ReviewFixture() as fixture:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    search_requests: list[str] = []

                    def handle_search(route) -> None:
                        search_requests.append(route.request.url)
                        route.fulfill(json={"models": [model], "error": None})

                    page.route("**/api/models/search**", handle_search)
                    page.goto(fixture.base_url, wait_until="load")
                    page.locator("#wizardNext").click()
                    page.locator("#wizardNext").click()

                    page.locator("#modelCatalogDisclosure > summary").click()
                    page.locator("#modelSearchQuery").fill("gemma")
                    page.locator("#modelSearchBtn").click()
                    expect(page.locator("#modelSearchResults")).to_contain_text(
                        "owner/test-model"
                    )
                    self.assertEqual(len(search_requests), 1)
                    page.locator(
                        'button[data-use-hf-model="owner/test-model"]'
                    ).click()
                    expect(page.locator('[data-env="NEWS_MODEL"]')).to_have_value(
                        "owner/test-model"
                    )

                    # Next/Back replaces the wizard markup. The fresh Model
                    # step must retain the selected model before any control
                    # is used again.
                    page.locator("#wizardNext").click()
                    page.locator("#wizardBack").click()
                    expect(page.locator('[data-env="NEWS_MODEL"]')).to_have_value(
                        "owner/test-model"
                    )

                    # The fresh Model step must also bind both the search and
                    # keyboard handlers.
                    page.locator("#modelCatalogDisclosure > summary").click()
                    page.locator("#modelSearchQuery").fill("gemma")
                    page.locator("#modelSearchQuery").press("Enter")
                    expect(page.locator("#modelSearchResults")).to_contain_text(
                        "owner/test-model"
                    )
                    self.assertEqual(len(search_requests), 2)
                    page.locator(
                        'button[data-use-hf-model="owner/test-model"]'
                    ).click()
                    expect(page.locator('[data-env="NEWS_MODEL"]')).to_have_value(
                        "owner/test-model"
                    )

                    # A non-wizard setup render has the same post-innerHTML
                    # binding requirement. Reloading gives it fresh markup.
                    legacy_page = browser.new_page()
                    legacy_requests: list[str] = []

                    def handle_legacy_search(route) -> None:
                        legacy_requests.append(route.request.url)
                        route.fulfill(json={"models": [model], "error": None})

                    legacy_page.route("**/api/models/search**", handle_legacy_search)
                    legacy_page.goto(f"{fixture.base_url}?wizard=0", wait_until="load")
                    legacy_page.locator("#modelCatalogDisclosure > summary").click()
                    legacy_page.locator("#modelSearchQuery").fill("gemma")
                    legacy_page.locator("#modelSearchBtn").click()
                    expect(legacy_page.locator("#modelSearchResults")).to_contain_text(
                        "owner/test-model"
                    )
                    self.assertEqual(len(legacy_requests), 1)
                    legacy_page.locator(
                        'button[data-use-hf-model="owner/test-model"]'
                    ).click()
                    expect(legacy_page.locator('[data-env="NEWS_MODEL"]')).to_have_value(
                        "owner/test-model"
                    )
                    legacy_page.reload(wait_until="load")
                    legacy_page.locator("#modelCatalogDisclosure > summary").click()
                    legacy_page.locator("#modelSearchQuery").fill("gemma")
                    legacy_page.locator("#modelSearchQuery").press("Enter")
                    expect(legacy_page.locator("#modelSearchResults")).to_contain_text(
                        "owner/test-model"
                    )
                    self.assertEqual(len(legacy_requests), 2)
                    legacy_page.locator(
                        'button[data-use-hf-model="owner/test-model"]'
                    ).click()
                    expect(legacy_page.locator('[data-env="NEWS_MODEL"]')).to_have_value(
                        "owner/test-model"
                    )
                    legacy_page.close()
                    page.close()
                finally:
                    browser.close()



if __name__ == "__main__":
    unittest.main()
