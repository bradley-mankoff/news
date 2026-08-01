from __future__ import annotations

import builtins
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

import news_pipeline.embeddings as emb


class FakeSentenceTransformer:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def encode(self, texts: list[str], **kwargs: object) -> np.ndarray:
        self.calls.append((list(texts), dict(kwargs)))
        rows = []
        for index, _text in enumerate(texts, start=1):
            rows.append([float(index), float(index + 10)])
        return np.asarray(rows, dtype=np.float32)


class EmbeddingsTests(unittest.TestCase):
    def test_embed_texts_handles_empty_and_uses_fake_sentence_transformer(self) -> None:
        self.assertEqual(emb.embed_texts([]).shape, (0, 0))

        fake_module = types.ModuleType("sentence_transformers")
        fake_module.SentenceTransformer = FakeSentenceTransformer
        original_model = emb._model
        self.addCleanup(setattr, emb, "_model", original_model)

        with patch.dict(sys.modules, {"sentence_transformers": fake_module}):
            result = emb.embed_texts(["hello", "world"])

        self.assertEqual(result.shape, (2, 2))
        np.testing.assert_array_equal(
            result,
            np.asarray([[1.0, 11.0], [2.0, 12.0]], dtype=np.float32),
        )
        self.assertIsInstance(emb._model, FakeSentenceTransformer)
        self.assertEqual(emb._model.model_name, emb.EMBEDDING_MODEL_NAME)
        self.assertIs(emb._load_model(), emb._model)
        self.assertEqual(
            emb._model.calls[0],
            (
                ["hello", "world"],
                {
                    "normalize_embeddings": True,
                    "show_progress_bar": False,
                    "batch_size": 64,
                    "convert_to_numpy": True,
                },
            ),
        )

    def test_load_model_import_error_branch_is_reported(self) -> None:
        original_model = emb._model
        self.addCleanup(setattr, emb, "_model", original_model)

        def fake_import(
            name: str,
            globals: dict[str, object] | None = None,
            locals: dict[str, object] | None = None,
            fromlist: tuple[str, ...] | list[str] = (),
            level: int = 0,
        ) -> object:
            if name == "sentence_transformers":
                raise ImportError("missing")
            return original_import(name, globals, locals, fromlist, level)

        original_import = builtins.__import__
        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(ImportError, "sentence-transformers is required"):
                emb._load_model()



    def test_dedup_story_drafts_keeps_higher_source_count(self) -> None:
        drafts = [
            {"paragraph": "First story", "source_count": 3, "story_title": "First"},
            {"story_text": "Second story", "source_count": 1, "story_title": "Second"},
        ]
        vectors = np.asarray([[1.0, 0.0], [0.9, 0.0]], dtype=np.float32)

        with patch.object(emb, "embed_texts", return_value=vectors) as mock_embed_texts:
            kept = emb.dedup_story_drafts(drafts, threshold=0.85)

        mock_embed_texts.assert_called_once_with(["First story", "Second story"])
        self.assertEqual(kept, [drafts[0]])

    def test_dedup_story_drafts_skips_dropped_inner_items(self) -> None:
        drafts = [
            {"paragraph": "Shared story", "source_count": 3, "story_title": "First"},
            {"story_text": "Middle story", "source_count": 2, "story_title": "Middle"},
            {"story_title": "Shared story", "source_count": 1},
        ]
        vectors = np.asarray([[1.0, 0.0], [0.0, 1.0], [0.9, 0.0]], dtype=np.float32)

        with patch.object(emb, "embed_texts", return_value=vectors) as mock_embed_texts:
            kept = emb.dedup_story_drafts(drafts, threshold=0.85)

        mock_embed_texts.assert_called_once_with(["Shared story", "Middle story", "Shared story"])
        self.assertEqual(kept, drafts[:2])

    def test_dedup_story_drafts_returns_input_for_one_item(self) -> None:
        draft = [{"story_title": "Solo"}]

        self.assertIs(emb.dedup_story_drafts(draft), draft)


if __name__ == "__main__":
    unittest.main()
