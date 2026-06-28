import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from news_pipeline.config import CONFIG_DIR, load_sources, write_source_translation_flags
from news_pipeline.source_catalog import (
    DeleteSources,
    UpsertSource,
    apply_source_catalog_patch,
    load_source_records,
    load_source_rows,
)
from news_pipeline.source_checks import remove_source_blocks
from news_pipeline.ui import delete_source, upsert_source


def _write_sources(path: Path, newline: str = "\n") -> None:
    text = newline.join(
        [
            "sources:",
            "  - key: Alpha",
            "    name: Alpha News",
            "    url: https://example.com/alpha.xml",
            "    tier: core",
            "  - key: Beta",
            "    name: Beta News",
            "    region: Europe",
            "    url: https://example.com/beta.xml",
            "    language: es",
            "    requires_translation: false",
            "",
        ]
    )
    path.write_bytes(text.encode("utf-8"))


class SourceCatalogTests(unittest.TestCase):
    def test_core_sources_do_not_require_translation(self) -> None:
        sources_path = CONFIG_DIR / "sources.yaml"
        payload = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
        core_translation_sources = [
            str(source.get("key") or source.get("name") or "")
            for source in payload.get("sources", [])
            if isinstance(source, dict)
            and str(source.get("tier") or "").strip().lower() == "core"
            and (
                str(source.get("language") or "").strip().lower() != "en"
                or bool(source.get("requires_translation", source.get("translate")))
            )
        ]

        self.assertEqual(core_translation_sources, [])

    def test_source_loading_excludes_translation_sources_for_all_scopes(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            path.write_text(
                yaml.safe_dump(
                    {
                        "sources": [
                            {
                                "key": "EnglishCore",
                                "url": "https://example.com/core.xml",
                                "language": "en",
                                "tier": "core",
                            },
                            {
                                "key": "EnglishPeripheral",
                                "url": "https://example.com/peripheral.xml",
                                "language": "en",
                                "tier": "peripheral",
                            },
                            {
                                "key": "TranslationRequired",
                                "url": "https://example.com/translate.xml",
                                "language": "en",
                                "tier": "peripheral",
                                "requires_translation": True,
                            },
                            {
                                "key": "Spanish",
                                "url": "https://example.com/es.xml",
                                "language": "es",
                                "tier": "peripheral",
                            },
                        ],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            self.assertEqual(list(load_sources(path, source_scope="core")), ["EnglishCore"])
            self.assertEqual(
                list(load_sources(path, source_scope="peripheral")),
                ["EnglishCore", "EnglishPeripheral"],
            )

    def test_load_records_and_source_rows(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            _write_sources(path)

            self.assertEqual([record["key"] for record in load_source_records(path)], ["Alpha", "Beta"])
            self.assertEqual(
                load_source_rows(path),
                [
                    {
                        "section": "sources",
                        "key": "Alpha",
                        "name": "Alpha News",
                        "url": "https://example.com/alpha.xml",
                        "fetcher": "rss",
                        "language": "",
                    },
                    {
                        "section": "sources",
                        "key": "Beta",
                        "name": "Beta News",
                        "url": "https://example.com/beta.xml",
                        "fetcher": "rss",
                        "language": "es",
                    },
                ],
            )

    def test_upsert_existing_source_orders_fields_and_removes_empty_optional_values(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            _write_sources(path)

            apply_source_catalog_patch(
                path,
                [
                    UpsertSource(
                        "Alpha",
                        {
                            "language": "en",
                            "weight": "1.5",
                            "nations": "US\nCA",
                            "notes": "",
                        },
                    )
                ],
            )

            text = path.read_text(encoding="utf-8")
            self.assertIn(
                '  - key: "Alpha"\n'
                '    name: "Alpha News"\n'
                '    language: "en"\n'
                '    tier: "core"\n'
                "    nations:\n"
                '      - "US"\n'
                '      - "CA"\n'
                '    url: "https://example.com/alpha.xml"\n'
                "    weight: 1.5\n",
                text,
            )
            self.assertNotIn("notes:", text)

    def test_upsert_rejects_unknown_source_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            _write_sources(path)

            with self.assertRaisesRegex(ValueError, "Unsupported source field 'langauge'"):
                apply_source_catalog_patch(path, [UpsertSource("Alpha", {"langauge": "en"})])

    def test_ui_append_only_rejects_duplicate_source(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            _write_sources(path)

            with patch.dict("os.environ", {"NEWS_SOURCES_YAML": str(path)}):
                with self.assertRaisesRegex(ValueError, "already exists"):
                    upsert_source({"key": "Alpha", "updates": {"url": "https://changed.example"}}, append_only=True)

    def test_ui_delete_removes_selected_source_only(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            _write_sources(path)

            with patch.dict("os.environ", {"NEWS_SOURCES_YAML": str(path)}):
                result = delete_source("Alpha")

            self.assertEqual(result["deleted"], "Alpha")
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("key: Alpha", text)
            self.assertIn("key: Beta", text)

    def test_translation_flags_write_requires_translation_and_source_language(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            _write_sources(path)

            written = write_source_translation_flags(path, {"Alpha": "fr", "Beta": "es"})

            self.assertEqual(written, 4)
            text = path.read_text(encoding="utf-8")
            self.assertIn(
                "    url: https://example.com/alpha.xml\n"
                "    requires_translation: true\n"
                "    translation_source_language: fr\n",
                text,
            )
            self.assertIn("    requires_translation: true\n    translation_source_language: es\n", text)

    def test_remove_source_blocks_wrapper_preserves_other_yaml(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            _write_sources(path)

            self.assertEqual(remove_source_blocks(path, {"Beta"}), 1)
            text = path.read_text(encoding="utf-8")
            self.assertIn("key: Alpha", text)
            self.assertNotIn("key: Beta", text)

    def test_crlf_source_file_keeps_crlf_when_editing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            _write_sources(path, newline="\r\n")

            apply_source_catalog_patch(path, [DeleteSources({"Alpha"})])

            data = path.read_bytes()
            self.assertIn(b"\r\n", data)
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_written_source_file_ends_with_newline(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            path.write_text(
                "sources:\n"
                "  - key: Alpha\n"
                "    url: https://example.com/alpha.xml\n"
                "  - key: Beta\n"
                "    url: https://example.com/beta.xml",
                encoding="utf-8",
            )

            apply_source_catalog_patch(path, [DeleteSources({"Alpha"})])

            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))


if __name__ == "__main__":
    unittest.main()
