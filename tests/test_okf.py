from __future__ import annotations

import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

import yaml

from news_pipeline.article_summary_records import ArticleSummaryRecord
from news_pipeline.diagnostics import RunDiagnostics
from news_pipeline.okf import OKFRunBundleSerializer, okf_run_bundle_path, write_okf_run_bundle
from news_pipeline.story_records import StoryRecord


_MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)\s]+)\)")
_HTTP_AUTOLINK = re.compile(r"<(https?://[^>]+)>")


class OKFRunBundleTests(unittest.TestCase):
    def test_checked_in_knowledge_bundle_conforms_to_okf_v02(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        knowledge_root = repository_root / "knowledge"
        root_index = knowledge_root / "index.md"
        root_log = knowledge_root / "log.md"
        markdown_paths = sorted(knowledge_root.rglob("*.md"))
        self.assertTrue(root_log.is_file(), root_log)
        root_frontmatter, _root_body = self._document(root_index)
        self.assertEqual(root_frontmatter, {"okf_version": "0.2"})

        for markdown_path in markdown_paths:
            if markdown_path.name == "index.md":
                if markdown_path != root_index:
                    self.assertIsNone(self._optional_frontmatter(markdown_path), markdown_path)
            elif markdown_path.name == "log.md":
                self.assertIsNone(self._optional_frontmatter(markdown_path), markdown_path)
                headings = [
                    line[3:].strip()
                    for line in markdown_path.read_text(encoding="utf-8").splitlines()
                    if line.startswith("## ")
                ]
                self.assertTrue(headings, markdown_path)
                for heading in headings:
                    try:
                        parsed_date = date.fromisoformat(heading)
                    except ValueError:
                        self.fail(f"log heading is not an ISO date: {markdown_path}: {heading!r}")
                    self.assertEqual(parsed_date.isoformat(), heading, markdown_path)
            else:
                frontmatter, _body = self._document(markdown_path)
                self.assertNotIn("okf_version", frontmatter, markdown_path)
                concept_type = frontmatter.get("type")
                self.assertIsInstance(concept_type, str, markdown_path)
                self.assertTrue(concept_type.strip(), markdown_path)

                sources = frontmatter.get("sources", [])
                for source in sources:
                    resource = str(source.get("resource") or "").strip()
                    if not resource:
                        continue
                    parsed_resource = urlparse(resource)
                    if parsed_resource.scheme or parsed_resource.netloc:
                        continue
                    resolved_resource = (
                        repository_root / parsed_resource.path.lstrip("/")
                        if parsed_resource.path.startswith("/")
                        else markdown_path.parent / parsed_resource.path
                    ).resolve()
                    self.assertTrue(
                        resolved_resource.is_relative_to(repository_root),
                        (markdown_path, resource),
                    )
                    self.assertTrue(resolved_resource.exists(), (markdown_path, resource))

            text = markdown_path.read_text(encoding="utf-8")
            for target in _MARKDOWN_LINK.findall(text):
                parsed_target = urlparse(target)
                if parsed_target.scheme or parsed_target.netloc:
                    continue
                target_path = parsed_target.path
                if target_path.startswith("/"):
                    resolved_target = knowledge_root / target_path.lstrip("/")
                elif target_path:
                    resolved_target = markdown_path.parent / target_path
                else:
                    resolved_target = markdown_path
                resolved_target = resolved_target.resolve()
                self.assertTrue(
                    resolved_target.is_relative_to(repository_root),
                    (markdown_path, target),
                )
                self.assertTrue(resolved_target.exists(), (markdown_path, target))

    def test_bundle_concepts_have_frontmatter_provenance_and_resolving_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_db = root / "output" / "history" / "news_history.duckdb"
            diagnostics = self._diagnostics("completed")
            report_body = "\nRendered report\n\nA useful report."
            article_b = ArticleSummaryRecord(
                title="Café / Trade",
                source="Example Wire",
                published="2026-06-01T09:00:00Z",
                url="https://example.com/articles/b",
                article_id="article-b",
                story="Global / Update",
                summary="Summary for article B.",
            )
            article_a = {
                "title": "Cafe: Trade",
                "source": "Example Desk",
                "published": "2026-06-01T08:00:00Z",
                "url": "https://example.com/articles/a",
                "article_id": "article-a",
                "story": "Global: Update",
                "summary": "Summary for article A.",
            }
            story_b = StoryRecord(
                story_key="story-b",
                story_title="Global / Update",
                article_ids=("article-b",),
                cluster_article_ids=("article-b",),
                article_count=1,
                cluster_article_count=1,
                selected_article_count=1,
                average_similarity=0.82,
            )
            story_a = {
                "story_key": "story-a",
                "story_title": "Global: Update",
                "article_ids": ["article-a"],
                "cluster_article_ids": ["article-a"],
                "article_count": 1,
                "cluster_article_count": 1,
                "selected_article_count": 1,
                "extras_note": "mapping input",
            }

            bundle = write_okf_run_bundle(
                history_db,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
                report_body=report_body,
                # Deliberately mix structured dataclasses and mapping adapters.
                article_summary_records=[article_b, article_a],
                story_records=[story_b, story_a],
            )

            self.assertEqual(
                bundle,
                root / "output" / "history" / "okf" / "2026-06-01_10-00-00",
            )
            for relative_path in (
                "report.md",
                "index.md",
                "log.md",
                "articles/index.md",
                "stories/index.md",
            ):
                self.assertTrue((bundle / relative_path).is_file(), relative_path)

            concept_paths = self._concept_paths(bundle)
            self.assertEqual(
                {path.relative_to(bundle).as_posix() for path in concept_paths},
                {
                    "report.md",
                    "articles/cafe-trade.md",
                    "articles/cafe-trade--2.md",
                    "stories/global-update.md",
                    "stories/global-update--2.md",
                },
            )
            for concept_path in concept_paths:
                frontmatter, body = self._document(concept_path)
                self.assertTrue(str(frontmatter.get("type") or "").strip())
                self.assertNotIn("okf_version", frontmatter)
                self.assertTrue(body.strip(), concept_path)

            root_frontmatter, root_body = self._document(bundle / "index.md")
            self.assertEqual(root_frontmatter.get("okf_version"), "0.2")
            self.assertIn("# OKF Run Bundle", root_body)
            for link in ("(report.md)", "(articles/)", "(stories/)"):
                self.assertIn(link, root_body)

            log_text = (bundle / "log.md").read_text(encoding="utf-8")
            self.assertTrue(log_text.startswith("# Directory Update Log\n\n## 2026-06-01"))
            self.assertIn(
                "Generated the [Daily News Report](report.md) and 2 story concept(s)",
                log_text,
            )
            self.assertIn("Lifecycle status: **stable**.", root_body)

            article_paths = {
                self._document(path)[0]["article_id"]: path
                for path in concept_paths
                if path.parent.name == "articles"
            }
            story_paths = {
                self._document(path)[0]["title"]: path
                for path in concept_paths
                if path.parent.name == "stories"
            }
            article_urls = {
                "article-a": "https://example.com/articles/a",
                "article-b": "https://example.com/articles/b",
            }
            article_stories = {
                "article-a": "Global: Update",
                "article-b": "Global / Update",
            }
            story_articles = {
                "Global: Update": "article-a",
                "Global / Update": "article-b",
            }
            for article_id, article_path in article_paths.items():
                frontmatter, body = self._document(article_path)
                url = article_urls[article_id]
                self.assertEqual(frontmatter.get("resource"), url)
                self.assertTrue(
                    any(source.get("resource") == url for source in frontmatter.get("sources", []))
                )
                self.assertIn(f"- Original URL: <{url}>", body)
                parsed_url = urlparse(url)
                self.assertEqual(parsed_url.scheme, "https")
                self.assertTrue(parsed_url.netloc)
                story_title = article_stories[article_id]
                self.assertIn(
                    f"- [{story_title}](../stories/{story_paths[story_title].name})",
                    body,
                )

            story_keys = {
                "Global: Update": "story-a",
                "Global / Update": "story-b",
            }
            for story_title, story_path in story_paths.items():
                frontmatter, body = self._document(story_path)
                article_id = story_articles[story_title]
                self.assertEqual(frontmatter.get("story_key"), story_keys[story_title])
                self.assertTrue(
                    any(
                        source.get("resource")
                        == f"../articles/{article_paths[article_id].name}"
                        for source in frontmatter.get("sources", [])
                    )
                )
                self.assertIn(
                    f"- [{article_id}](../articles/{article_paths[article_id].name})",
                    body,
                )

            report_frontmatter, report_body_text = self._document(bundle / "report.md")
            self.assertTrue(
                report_body_text.startswith("\n" + report_body),
                "the rendered report body must remain the prefix of report.md",
            )
            for story_title, story_path in story_paths.items():
                self.assertIn(f"- [{story_title}](stories/{story_path.name})", report_body_text)
                self.assertTrue(
                    any(
                        source.get("resource") == f"stories/{story_path.name}"
                        for source in report_frontmatter.get("sources", [])
                    )
                )

            self._assert_markdown_links_resolve(bundle)
            self._assert_frontmatter_sources_resolve(bundle, concept_paths)
            for path in bundle.rglob("*.md"):
                if path == bundle / "index.md":
                    continue
                optional_frontmatter = self._optional_frontmatter(path)
                if optional_frontmatter is not None:
                    self.assertNotIn("okf_version", optional_frontmatter, path)

    def test_filename_sanitization_and_collisions_are_stable_across_input_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_db = root / "output" / "history" / "news_history.duckdb"
            diagnostics = self._diagnostics("completed")
            articles = [
                {
                    "title": "Café / Trade",
                    "source": "Wire",
                    "url": "https://example.com/b",
                    "article_id": "article-b",
                    "story": "Global / Update",
                    "summary": "B",
                },
                {
                    "title": "Cafe: Trade",
                    "source": "Desk",
                    "url": "https://example.com/a",
                    "article_id": "article-a",
                    "story": "Global: Update",
                    "summary": "A",
                },
            ]
            stories = [
                {
                    "story_key": "story-b",
                    "story_title": "Global / Update",
                    "article_ids": ["article-b"],
                },
                {
                    "story_key": "story-a",
                    "story_title": "Global: Update",
                    "article_ids": ["article-a"],
                },
            ]

            bundle = write_okf_run_bundle(
                history_db,
                run_id="collision-run",
                diagnostics=diagnostics,
                article_summary_records=articles,
                story_records=stories,
            )
            first_snapshot = self._file_snapshot(bundle)
            write_okf_run_bundle(
                history_db,
                run_id="collision-run",
                diagnostics=diagnostics,
                article_summary_records=list(reversed(articles)),
                story_records=list(reversed(stories)),
            )
            second_snapshot = self._file_snapshot(bundle)

            self.assertEqual(first_snapshot, second_snapshot)
            article_names = {
                self._document(path)[0]["article_id"]: path.name
                for path in self._concept_paths(bundle)
                if path.parent.name == "articles"
            }
            self.assertEqual(article_names, {"article-a": "cafe-trade.md", "article-b": "cafe-trade--2.md"})
            story_names = {
                self._document(path)[0]["title"]: path.name
                for path in self._concept_paths(bundle)
                if path.parent.name == "stories"
            }
            self.assertEqual(
                story_names,
                {"Global: Update": "global-update.md", "Global / Update": "global-update--2.md"},
            )

    def test_same_run_replacement_removes_stale_article_and_story_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_db = root / "output" / "history" / "news_history.duckdb"
            diagnostics = self._diagnostics("completed")
            old_article = {
                "title": "Old headline",
                "source": "Wire",
                "url": "https://example.com/old",
                "article_id": "old-article",
                "story": "Old story",
                "summary": "Old summary",
            }
            old_story = {
                "story_key": "old-story",
                "story_title": "Old story",
                "article_ids": ["old-article"],
            }
            bundle = write_okf_run_bundle(
                history_db,
                run_id="same-run",
                diagnostics=diagnostics,
                report_body="Old report",
                article_summary_records=[old_article],
                story_records=[old_story],
            )
            old_article_path = bundle / "articles" / "old-headline.md"
            old_story_path = bundle / "stories" / "old-story.md"
            self.assertTrue(old_article_path.exists())
            self.assertTrue(old_story_path.exists())

            new_article = {
                "title": "Fresh headline",
                "source": "Desk",
                "url": "https://example.com/new",
                "article_id": "new-article",
                "story": "Fresh story",
                "summary": "Fresh summary",
            }
            new_story = {
                "story_key": "new-story",
                "story_title": "Fresh story",
                "article_ids": ["new-article"],
            }
            write_okf_run_bundle(
                history_db,
                run_id="same-run",
                diagnostics=diagnostics,
                report_body="Fresh report",
                article_summary_records=[new_article],
                story_records=[new_story],
            )

            self.assertFalse(old_article_path.exists())
            self.assertFalse(old_story_path.exists())
            self.assertTrue((bundle / "articles" / "fresh-headline.md").exists())
            self.assertTrue((bundle / "stories" / "fresh-story.md").exists())
            for path in self._concept_paths(bundle):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("Old headline", text)
                self.assertNotIn("Old story", text)
                self.assertNotIn("Old report", text)

    def test_completed_is_stable_and_failed_aborted_unknown_are_draft(self) -> None:
        cases = (
            ("completed", "stable"),
            ("failed", "draft"),
            ("aborted", "draft"),
            (None, "draft"),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_db = root / "output" / "history" / "news_history.duckdb"
            for event_label, expected_status in cases:
                with self.subTest(event=event_label):
                    diagnostics = self._diagnostics(event_label)
                    bundle = write_okf_run_bundle(
                        history_db,
                        run_id=f"status-{event_label or 'unknown'}",
                        diagnostics=diagnostics,
                        article_summary_records=[self._article_mapping("status-article", "Status article")],
                        story_records=[self._story_mapping("status-story", "Status story", "status-article")],
                    )
                    statuses = {
                        self._document(path)[0].get("status")
                        for path in self._concept_paths(bundle)
                    }
                    self.assertEqual(statuses, {expected_status})

    def test_empty_run_still_emits_report_indexes_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_db = root / "output" / "history" / "news_history.duckdb"
            bundle = write_okf_run_bundle(
                history_db,
                run_id="empty-run",
                diagnostics=self._diagnostics("completed"),
            )

            for relative_path in (
                "report.md",
                "index.md",
                "log.md",
                "articles/index.md",
                "stories/index.md",
            ):
                self.assertTrue((bundle / relative_path).is_file(), relative_path)
            self.assertEqual(
                {path.relative_to(bundle).as_posix() for path in self._concept_paths(bundle)},
                {"report.md"},
            )
            report_frontmatter, report_body = self._document(bundle / "report.md")
            self.assertTrue(report_body.strip())
            self.assertTrue(str(report_frontmatter.get("type") or "").strip())
            self.assertIn("No story concepts were recorded", report_body)
            self.assertIn("No article or story concepts were recorded", (bundle / "index.md").read_text(encoding="utf-8"))
            self.assertIn("No concepts were recorded", (bundle / "articles/index.md").read_text(encoding="utf-8"))
            self.assertIn("No concepts were recorded", (bundle / "stories/index.md").read_text(encoding="utf-8"))
            self._assert_markdown_links_resolve(bundle)

    def test_story_metrics_preserve_safe_values_without_dumping_unsafe_objects(self) -> None:
        class UnsafeMetric:
            def __str__(self) -> str:
                return "UNSAFE_OBJECT_SHOULD_NOT_BE_SERIALIZED"

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_db = root / "output" / "history" / "news_history.duckdb"
            story = StoryRecord(
                story_key="metric-story",
                story_title="Metric story",
                article_ids=("metric-article",),
                cluster_article_ids=("metric-article",),
                article_count=1,
                cluster_article_count=1,
                selected_article_count=1,
                average_similarity=0.91,
                pruned_article_ids=("metric-article",),
                extras={
                    "quality": {"label": "strong", "score": 0.8},
                    "unsafe": UnsafeMetric(),
                    "bad_float": float("nan"),
                    "_internal": "not a public metric",
                },
            )
            bundle = write_okf_run_bundle(
                history_db,
                run_id="metric-run",
                diagnostics=self._diagnostics("completed"),
                article_summary_records=[self._article_mapping("metric-article", "Metric article")],
                story_records=[story],
            )

            story_path = next(
                path
                for path in self._concept_paths(bundle)
                if path.parent.name == "stories"
            )
            story_text = story_path.read_text(encoding="utf-8")
            self.assertIn("## Metrics", story_text)
            self.assertIn("- `average_similarity`: 0.91", story_text)
            self.assertIn('- `pruned_article_ids`: ["metric-article"]', story_text)
            self.assertIn('- `quality`: {"label": "strong", "score": 0.8}', story_text)
            self.assertNotIn("UNSAFE_OBJECT_SHOULD_NOT_BE_SERIALIZED", story_text)
            self.assertNotIn("unsafe", story_text)
            self.assertNotIn("bad_float", story_text)
            self.assertNotIn("_internal", story_text)

    def test_public_bundle_path_helper_matches_serializer_and_sanitizes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            history_db = root / "output" / "history" / "news_history.duckdb"
            diagnostics = self._diagnostics("completed")
            serializer = OKFRunBundleSerializer(
                history_db,
                run_id="2026-06-01_10-00-00",
                diagnostics=diagnostics,
            )
            self.assertEqual(
                okf_run_bundle_path(history_db, "2026-06-01_10-00-00"),
                serializer.bundle_path,
            )
            self.assertEqual(
                okf_run_bundle_path(history_db, "../evil/run"),
                root / "output" / "history" / "okf" / "evil-run",
            )
            self.assertEqual(
                okf_run_bundle_path(history_db, ""),
                root / "output" / "history" / "okf" / "unknown-run",
            )

    @staticmethod
    def _diagnostics(event_label: str | None) -> RunDiagnostics:
        diagnostics = RunDiagnostics(
            run_started_at="2026-06-01T10:00:00",
            settings={"preset_id": "test", "history_db_path": "history.duckdb"},
        )
        if event_label:
            diagnostics.event(event_label)
        return diagnostics

    @staticmethod
    def _article_mapping(article_id: str, title: str) -> dict[str, str]:
        return {
            "title": title,
            "source": "Test source",
            "published": "2026-06-01T09:00:00Z",
            "url": f"https://example.com/{article_id}",
            "article_id": article_id,
            "story": "Status story" if article_id == "status-article" else "Metric story",
            "summary": f"Summary for {title}.",
        }

    @staticmethod
    def _story_mapping(story_key: str, story_title: str, article_id: str) -> dict[str, object]:
        return {
            "story_key": story_key,
            "story_title": story_title,
            "article_ids": [article_id],
            "cluster_article_ids": [article_id],
            "article_count": 1,
            "cluster_article_count": 1,
            "selected_article_count": 1,
        }

    @staticmethod
    def _concept_paths(bundle: Path) -> list[Path]:
        paths = [bundle / "report.md"]
        paths.extend(sorted(path for path in (bundle / "articles").glob("*.md") if path.name != "index.md"))
        paths.extend(sorted(path for path in (bundle / "stories").glob("*.md") if path.name != "index.md"))
        return paths

    @staticmethod
    def _document(path: Path) -> tuple[dict[str, object], str]:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise AssertionError(f"missing YAML frontmatter: {path}")
        delimiter = "\n---\n"
        end = text.find(delimiter, 4)
        if end < 0:
            raise AssertionError(f"unterminated YAML frontmatter: {path}")
        frontmatter = yaml.safe_load(text[4:end + 1]) or {}
        if not isinstance(frontmatter, dict):
            raise AssertionError(f"frontmatter is not a mapping: {path}")
        return frontmatter, text[end + len(delimiter):]

    @staticmethod
    def _optional_frontmatter(path: Path) -> dict[str, object] | None:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            return None
        delimiter = "\n---\n"
        end = text.find(delimiter, 4)
        if end < 0:
            raise AssertionError(f"unterminated YAML frontmatter: {path}")
        frontmatter = yaml.safe_load(text[4:end + 1]) or {}
        if not isinstance(frontmatter, dict):
            raise AssertionError(f"frontmatter is not a mapping: {path}")
        return frontmatter

    def _assert_markdown_links_resolve(self, bundle: Path) -> None:
        bundle_root = bundle.resolve()
        for markdown_path in sorted(bundle.rglob("*.md")):
            text = markdown_path.read_text(encoding="utf-8")
            for target in _HTTP_AUTOLINK.findall(text):
                parsed = urlparse(target)
                self.assertEqual(parsed.scheme, "https", target)
                self.assertTrue(parsed.netloc, target)
            for target in _MARKDOWN_LINK.findall(text):
                parsed = urlparse(target)
                if parsed.scheme or parsed.netloc:
                    self.assertIn(parsed.scheme, {"http", "https"}, target)
                    self.assertTrue(parsed.netloc, target)
                    continue
                relative_target = target.split("#", 1)[0]
                resolved = (markdown_path.parent / relative_target).resolve()
                self.assertTrue(resolved.is_relative_to(bundle_root), (markdown_path, target))
                self.assertTrue(resolved.exists(), (markdown_path, target))

    def _assert_frontmatter_sources_resolve(self, bundle: Path, concept_paths: list[Path]) -> None:
        bundle_root = bundle.resolve()
        for concept_path in concept_paths:
            frontmatter, _body = self._document(concept_path)
            for source in frontmatter.get("sources", []):
                resource = str(source.get("resource") or "")
                parsed = urlparse(resource)
                if parsed.scheme or parsed.netloc:
                    self.assertIn(parsed.scheme, {"http", "https"}, resource)
                    self.assertTrue(parsed.netloc, resource)
                    continue
                resolved = (concept_path.parent / resource).resolve()
                self.assertTrue(resolved.is_relative_to(bundle_root), (concept_path, resource))
                self.assertTrue(resolved.exists(), (concept_path, resource))

    @staticmethod
    def _file_snapshot(bundle: Path) -> dict[str, bytes]:
        return {
            path.relative_to(bundle).as_posix(): path.read_bytes()
            for path in sorted(path for path in bundle.rglob("*") if path.is_file())
        }


if __name__ == "__main__":
    unittest.main()
