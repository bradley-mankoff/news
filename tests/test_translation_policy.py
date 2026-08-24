"""Tests for the deterministic translation policy (slice 01 / DN-10)."""

from __future__ import annotations

import unittest

from news_pipeline.translation_policy import (
    DEFAULT_TARGET_LANGUAGE,
    STATUS_DISABLED,
    STATUS_NEEDS_TRANSLATION,
    STATUS_SAME_LANGUAGE,
    STATUS_UNKNOWN_LANGUAGE,
    build_translation_provenance,
    normalize_declared_language,
    translation_decision,
)


class TranslationDecisionTests(unittest.TestCase):
    def test_translation_runs_only_when_enabled_and_declared_language_differs(self) -> None:
        self.assertEqual(
            translation_decision(enabled=True, source_language="fr"),
            {
                "runs": True,
                "status": STATUS_NEEDS_TRANSLATION,
                "source_language": "fr",
                "target_language": "en",
            },
        )
        self.assertFalse(translation_decision(enabled=False, source_language="fr")["runs"])
        self.assertEqual(
            translation_decision(enabled=True, source_language="en")["status"],
            STATUS_SAME_LANGUAGE,
        )

    def test_disabled_gate_takes_precedence_and_records_its_status(self) -> None:
        self.assertEqual(
            translation_decision(enabled=False, source_language="fr")["status"],
            STATUS_DISABLED,
        )
        # Disabled wins even when the declaration would also be unknown.
        self.assertEqual(
            translation_decision(enabled=False, source_language=None)["status"],
            STATUS_DISABLED,
        )

    def test_unknown_language_is_recorded_never_guessed(self) -> None:
        for undeclared in (None, "", "   "):
            with self.subTest(undeclared=undeclared):
                decision = translation_decision(
                    enabled=True, source_language=undeclared
                )
                self.assertFalse(decision["runs"])
                self.assertEqual(decision["status"], STATUS_UNKNOWN_LANGUAGE)
                self.assertIsNone(decision["source_language"])

    def test_declared_languages_normalize_without_mapping(self) -> None:
        self.assertEqual(normalize_declared_language("  FR "), "fr")
        self.assertEqual(normalize_declared_language("zh_Hans"), "zh-hans")
        # Normalization folds spelling only; it never guesses another language.
        self.assertNotEqual(normalize_declared_language("zh-Hans"), "en")
        self.assertIsNone(normalize_declared_language(None))

    def test_target_language_defaults_to_english_and_normalizes(self) -> None:
        self.assertEqual(DEFAULT_TARGET_LANGUAGE, "en")
        self.assertEqual(
            translation_decision(
                enabled=True, source_language="de", target_language=" DE "
            )["target_language"],
            "de",
        )
        self.assertTrue(
            translation_decision(
                enabled=True, source_language="de", target_language="FR"
            )["runs"]
        )


class TranslationProvenanceTests(unittest.TestCase):
    def test_provenance_keeps_original_and_translated_text_with_context(self) -> None:
        provenance = build_translation_provenance(
            original_text="Bonjour le monde",
            translated_text="Hello world",
            source_language="fr",
            target_language="en",
            model="mlx-community/translate-gemma",
            status="translated",
        )

        self.assertEqual(provenance.original_text, "Bonjour le monde")
        self.assertEqual(provenance.translated_text, "Hello world")
        self.assertEqual(provenance.source_language, "fr")
        self.assertEqual(provenance.target_language, "en")
        self.assertEqual(provenance.model, "mlx-community/translate-gemma")
        self.assertEqual(provenance.status, "translated")
        self.assertEqual(
            provenance.as_dict(),
            {
                "original_text": "Bonjour le monde",
                "translated_text": "Hello world",
                "source_language": "fr",
                "target_language": "en",
                "model": "mlx-community/translate-gemma",
                "status": "translated",
            },
        )

    def test_provenance_preserves_original_when_translation_is_absent(self) -> None:
        skipped = build_translation_provenance(
            original_text="Bonjour",
            translated_text="Bonjour",
            source_language=None,
            target_language="en",
            model=None,
            status=STATUS_UNKNOWN_LANGUAGE,
        )

        self.assertEqual(skipped.original_text, skipped.translated_text)
        self.assertIsNone(skipped.source_language)
        self.assertIsNone(skipped.model)
        self.assertEqual(skipped.status, STATUS_UNKNOWN_LANGUAGE)

    def test_raw_inputs_coerce_without_inventing_values(self) -> None:
        provenance = build_translation_provenance(
            original_text=None,
            translated_text=None,
            source_language="FR",
            target_language=None,
            model="  ",
            status="unchanged",
        )

        self.assertEqual(provenance.original_text, "")
        self.assertEqual(provenance.translated_text, "")
        self.assertEqual(provenance.source_language, "fr")
        self.assertEqual(provenance.target_language, "en")
        self.assertIsNone(provenance.model)


if __name__ == "__main__":
    unittest.main()
