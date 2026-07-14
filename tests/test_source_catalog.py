import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from news_pipeline.config import CONFIG_DIR, load_sources
from news_pipeline.source_catalog import (
    DeleteSources,
    SetSourceLanguages,
    UpsertSource,
    _apply_delete,
    _apply_languages,
    _apply_line_edits,
    _apply_upsert,
    _coerce_bool,
    _coerce_source_value,
    _direct_source_field_line,
    _find_source_range,
    apply_source_catalog_patch,
    load_source_records,
    load_source_records_from_lines,
    load_source_rows,
    _load_yaml_mapping,
    _newline_for,
    _ordered_source_record,
    _preferred_field_insert_line,
    _render_source_block,
    _source_block_key,
    _source_block_ranges,
    _source_record_for_key,
    _yaml_scalar,
)
from news_pipeline.source_checks import remove_source_blocks, write_source_languages
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
    def test_low_level_loading_and_block_helpers_cover_failure_branches(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing = root / "missing.yaml"
            bad = root / "bad.yaml"
            bad.write_text("- not-a-mapping\n", encoding="utf-8")
            list_only = root / "list_only.yaml"
            list_only.write_text("sources: 1\n", encoding="utf-8")
            rows_only = root / "rows_only.yaml"
            rows_only.write_text(
                yaml.safe_dump({"sources": [{"name": "Alpha"}, {"key": "Beta"}]}, sort_keys=False),
                encoding="utf-8",
            )

            self.assertEqual(_load_yaml_mapping(missing), {})
            with self.assertRaisesRegex(ValueError, "must contain a YAML mapping"):
                _load_yaml_mapping(bad)
            self.assertEqual(load_source_records(list_only), [])
            self.assertEqual(load_source_rows(rows_only), [])
            self.assertIsNone(_source_record_for_key([], " "))
            self.assertEqual(load_source_records_from_lines(["sources: [\n"]), [])
            self.assertEqual(load_source_records_from_lines(["sources: {}\n"]), [])
            self.assertEqual(
                _source_block_ranges(
                    [
                        "sources:\n",
                        "  - key: Alpha\n",
                        "    name: Alpha\n",
                        "not indented\n",
                        "still ignored\n",
                    ]
                ),
                [(1, 3)],
            )
            self.assertEqual(_source_block_key(["  - 1\n"], 0, 1), "")
            self.assertEqual(_source_block_key(["  - key: [oops\n"], 0, 1), "")
            self.assertEqual(_find_source_range(["sources:\n", "  - key: Alpha\n"], "missing"), None)

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

    def test_source_loading_excludes_non_english_sources_for_all_scopes(self) -> None:
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

    def test_render_and_coercion_helpers_cover_branchy_paths(self) -> None:
        self.assertEqual(_newline_for(["a\r\n", "b\r\n"]), "\r\n")
        self.assertEqual(_yaml_scalar(True), "true")
        self.assertEqual(_yaml_scalar(3), "3")
        self.assertEqual(_coerce_bool(True), True)
        self.assertEqual(_coerce_bool(None), False)
        self.assertEqual(_coerce_bool("yes"), True)
        self.assertEqual(_coerce_source_value("weight", ""), None)
        self.assertEqual(_coerce_source_value("source_match_aliases", "a, b\nc"), ["a", "b", "c"])
        self.assertEqual(_coerce_source_value("source_match_aliases", None), [])
        self.assertEqual(_coerce_source_value("source_match_aliases", [" a ", "", "b"]), ["a", "b"])
        self.assertEqual(_coerce_source_value("notes", None), None)

        ordered = _ordered_source_record(
            {
                "key": "Alpha",
                "name": "Alpha News",
                "url": "https://example.com/alpha.xml",
                "notes": "",
                "extra": "value",
            }
        )
        self.assertEqual(ordered["key"], "Alpha")
        self.assertIn("extra", ordered)
        self.assertNotIn("notes", ordered)

        lines = [
            "sources:\n",
            "  - key: Alpha\n",
            "    name: Alpha\n",
            "    url: https://example.com/alpha.xml\n",
        ]
        self.assertEqual(_direct_source_field_line(lines, 1, 4, "url"), 3)
        self.assertIsNone(_direct_source_field_line(lines, 1, 4, "language"))
        self.assertEqual(_preferred_field_insert_line(lines, 1, 4, ("language", "url")), 4)
        self.assertEqual(_preferred_field_insert_line(["sources:\n", "  - key: Alpha\n"], 1, 2, ("language",)), 2)
        self.assertEqual(
            _render_source_block(
                {
                    "key": "Alpha",
                    "name": "Alpha News",
                    "url": "https://example.com/alpha.xml",
                    "notes": "hello",
                },
                "\n",
            )[0],
            '  - key: "Alpha"\n',
        )

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

    def test_language_write_inserts_after_url_and_respects_overwrite_false(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sources.yaml"
            _write_sources(path)

            written = write_source_languages(
                path,
                [
                    {"key": "Alpha", "ok": True, "language": "fr"},
                    {"key": "Beta", "ok": True, "language": "de"},
                ],
                overwrite=False,
            )

            self.assertEqual(written, 1)
            text = path.read_text(encoding="utf-8")
            self.assertIn("    url: https://example.com/alpha.xml\n    language: fr\n", text)
            self.assertIn("    language: es\n", text)
            self.assertNotIn("    language: de\n", text)


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
