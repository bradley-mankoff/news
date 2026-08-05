"""Model Catalog integrity, runtime-fit, and Hugging Face integration tests.

The drift-guard test asserts that every curated catalog alias resolves to the
same model reference as ``config.MODEL_ALIASES``, so the catalog never forks
the pipeline's single source of truth (mirrors
``test_prompt_catalog.py::test_profile_ids_match_registry_keys``).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from huggingface_hub import errors

from news_pipeline import config, model_catalog


def _fake_model_info(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "id": "owner/repo",
        "author": "owner",
        "pipeline_tag": "text-generation",
        "library_name": "transformers",
        "downloads": 1234,
        "likes": 42,
        "last_modified": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "card_data": SimpleNamespace(license="apache-2.0"),
        "config": {"max_position_embeddings": 4096},
        "tags": ["safetensors", "transformers", "pytorch"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class ModelCatalogTests(unittest.TestCase):
    def test_catalog_aliases_resolve_to_same_references(self) -> None:
        # Drift-guard: catalog entries must resolve to the exact references the
        # pipeline launches; a mismatch would let the picker offer a model that
        # resolves elsewhere (or not at all) at run time.
        for entry in model_catalog.CATALOG_MODELS.values():
            self.assertEqual(
                config.resolve_model_name(entry.alias),
                entry.reference,
                f"{entry.alias} drifts from config.MODEL_ALIASES",
            )

    def test_catalog_entries_are_complete(self) -> None:
        self.assertEqual(len(model_catalog.CATALOG_MODELS), 2)
        for entry in model_catalog.CATALOG_MODELS.values():
            self.assertTrue(entry.alias)
            self.assertTrue(entry.reference)
            self.assertTrue(entry.name)
            self.assertTrue(entry.backend)
            self.assertIn(entry.backend, config.SUPPORTED_MODEL_BACKENDS)
            self.assertTrue(entry.hf_repo)
            self.assertTrue(entry.description)

    def test_catalog_backends_agree_with_runtime_inference(self) -> None:
        # Drift-guard: the picker must never offer a curated card whose backend
        # label disagrees with the backend the pipeline actually infers for it
        # (HANDOFF: "Model picker must validate runtime support").
        for entry in model_catalog.CATALOG_MODELS.values():
            self.assertEqual(
                config.infer_model_backend(entry.alias),
                entry.backend,
                f"{entry.alias} backend label {entry.backend!r} disagrees with "
                f"config.infer_model_backend()",
            )
            fit = model_catalog.runtime_fit_for_hf_model(
                {"id": entry.hf_repo, "tags": [], "library_name": "", "pipeline_tag": None}
            )
            expected_fit = (
                model_catalog.RUNTIME_FIT_MANAGED_MLX_VLM
                if entry.backend == "mlx-vlm"
                else model_catalog.RUNTIME_FIT_MANAGED_MLX_LM
            )
            self.assertEqual(
                fit["status"],
                expected_fit,
                f"{entry.alias} curated fit verdict disagrees with its backend label",
            )
            self.assertTrue(
                set(entry.task_notes).issubset(set(model_catalog.MODEL_RECOMMENDATION_TASKS)),
                f"{entry.alias} task_notes use unknown tasks",
            )
            self.assertTrue(
                entry.reference.startswith(entry.hf_repo)
                or entry.reference == entry.hf_repo,
                f"{entry.alias} reference does not match hf_repo",
            )

    def test_default_catalog_model_is_the_default_alias(self) -> None:
        self.assertEqual(model_catalog.DEFAULT_CATALOG_MODEL_ALIAS, config.DEFAULT_MODEL_ALIAS)
        default = model_catalog.CATALOG_MODELS[model_catalog.DEFAULT_CATALOG_MODEL_ALIAS]
        self.assertEqual(default.backend, "mlx-vlm")
        self.assertEqual(default.reference, "mlx-community/gemma-4-12B-it-4bit")
        self.assertEqual(default.context_length, 262_144)
        self.assertNotIn(".gguf", default.reference)

    def test_default_catalog_entry_is_repo_id_not_gguf_file(self) -> None:
        default = model_catalog.CATALOG_MODELS[model_catalog.DEFAULT_CATALOG_MODEL_ALIAS]
        self.assertEqual(default.alias, "gemma-4-12b-it-4bit")
        self.assertEqual(default.reference, "mlx-community/gemma-4-12B-it-4bit")
        self.assertEqual(default.hf_repo, "mlx-community/gemma-4-12B-it-4bit")
        self.assertEqual(default.backend, "mlx-vlm")
        self.assertNotIn(".gguf", default.reference)
        self.assertNotIn("qwythos", model_catalog.CATALOG_MODELS)
        for entry in model_catalog.CATALOG_MODELS.values():
            self.assertEqual(entry.reference, entry.hf_repo)
            # i.e. reference == hf_repo exactly (no file suffix)

    def test_recommendations_cover_all_tasks(self) -> None:
        for task in model_catalog.MODEL_RECOMMENDATION_TASKS:
            picks = model_catalog.recommend_models(task)
            for pick in picks:
                self.assertIn("alias", pick)
                self.assertIn("name", pick)
                self.assertIn("backend", pick)
                self.assertIn("hf_repo", pick)
                self.assertIn("reason", pick)
                self.assertTrue(pick["reason"])
        # Translation is the documented honest gap: no verified curated pick.
        self.assertEqual(model_catalog.recommend_models("translation"), [])
        # Speed's curated pick is the tiny test model.
        self.assertEqual(model_catalog.recommend_models("speed")[0]["alias"], "gemma-e2b-tiny")
        # The default model covers the quality tasks first.
        self.assertEqual(
            model_catalog.recommend_models("synthesis")[0]["alias"],
            model_catalog.DEFAULT_CATALOG_MODEL_ALIAS,
        )

    def test_recommend_unknown_task_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown model recommendation task"):
            model_catalog.recommend_models("not-a-task")
        with self.assertRaisesRegex(ValueError, "factual_extraction"):
            model_catalog.recommend_models("not-a-task")

    def test_list_model_catalog_is_json_ready(self) -> None:
        records = model_catalog.list_model_catalog()
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertIsInstance(record, dict)
            self.assertTrue(record["hf_url"].startswith("https://huggingface.co/"))
            self.assertIn("task_notes", record)
            self.assertIn("is_default", record)
        defaults = [record for record in records if record["is_default"]]
        self.assertEqual([record["alias"] for record in defaults], ["gemma-4-12b-it-4bit"])

    def test_runtime_fit_matrix(self) -> None:
        curated_gemma = "mlx-community/gemma-4-12B-it-4bit"
        cases = [
            # (info dict, expected status)
            (
                {"id": curated_gemma, "tags": ["mlx"], "library_name": "mlx", "pipeline_tag": "image-text-to-text"},
                model_catalog.RUNTIME_FIT_MANAGED_MLX_VLM,
            ),
            (
                {"id": "someone/arbitrary-file.gguf", "tags": ["gguf", "mlx"], "library_name": "mlx", "pipeline_tag": "image-text-to-text"},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
            (
                {"id": "deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit", "tags": ["mlx", "vision"], "library_name": "mlx", "pipeline_tag": "image-text-to-text"},
                model_catalog.RUNTIME_FIT_MANAGED_MLX_VLM,
            ),
            (
                {"id": "someone/arbitrary-gguf", "tags": ["gguf", "text-generation"], "library_name": "transformers", "pipeline_tag": "text-generation"},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
            (
                {"id": "someone/mlx-text", "tags": ["mlx"], "library_name": "mlx", "pipeline_tag": "text-generation"},
                model_catalog.RUNTIME_FIT_MANAGED_MLX_LM,
            ),
            (
                {"id": "someone/mlx-vlm", "tags": ["mlx"], "library_name": "mlx", "pipeline_tag": "image-text-to-text"},
                model_catalog.RUNTIME_FIT_MANAGED_MLX_VLM,
            ),
            (
                {"id": "someone/transformers-text", "tags": ["safetensors"], "library_name": "transformers", "pipeline_tag": "text-generation"},
                model_catalog.RUNTIME_FIT_MANAGED_MLX_LM,
            ),
            (
                {"id": "someone/transformers-vlm", "tags": ["safetensors"], "library_name": "transformers", "pipeline_tag": "image-text-to-text"},
                model_catalog.RUNTIME_FIT_MANAGED_MLX_VLM,
            ),
            (
                {"id": "someone/transformers-other", "tags": ["safetensors"], "library_name": "transformers", "pipeline_tag": "audio-classification"},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
            (
                {"id": "someone/unknown", "tags": [], "library_name": "unknown", "pipeline_tag": None},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
        ]
        for info, expected in cases:
            with self.subTest(info=info):
                fit = model_catalog.runtime_fit_for_hf_model(info)
                self.assertEqual(fit["status"], expected)
                self.assertTrue(fit["reason"])

    def test_search_maps_and_annotates_results(self) -> None:
        fake_info = _fake_model_info()
        fake_api = MagicMock()
        fake_api.list_models.return_value = iter([fake_info])
        with patch("huggingface_hub.HfApi", return_value=fake_api):
            results = model_catalog.search_huggingface_models(
                "qwythos", pipeline_tag="text-generation", limit=999
            )

        self.assertEqual(len(results), 1)
        item = results[0]
        for key in (
            "id",
            "author",
            "hf_url",
            "pipeline_tag",
            "library_name",
            "downloads",
            "likes",
            "last_modified",
            "license",
            "context_length",
            "tags",
            "runtime_fit",
            "in_catalog",
        ):
            self.assertIn(key, item)
        self.assertEqual(item["id"], "owner/repo")
        self.assertEqual(item["license"], "apache-2.0")
        self.assertEqual(item["context_length"], 4096)
        self.assertEqual(item["last_modified"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(item["runtime_fit"]["status"], model_catalog.RUNTIME_FIT_MANAGED_MLX_LM)
        self.assertFalse(item["in_catalog"])

        # Limit is clamped to 1-50 and list_models receives keyword args only.
        kwargs = fake_api.list_models.call_args.kwargs
        self.assertEqual(kwargs["search"], "qwythos")
        self.assertEqual(kwargs["pipeline_tag"], "text-generation")
        self.assertEqual(kwargs["limit"], 50)
        self.assertEqual(kwargs["expand"], model_catalog.HF_SEARCH_EXPAND)

    def test_search_in_catalog_flag_and_empty_results(self) -> None:
        curated = "mlx-community/gemma-4-12B-it-4bit"
        empty_api = MagicMock()
        empty_api.list_models.return_value = iter([])
        with patch("huggingface_hub.HfApi", return_value=empty_api):
            self.assertEqual(model_catalog.search_huggingface_models("none"), [])

        fake_api = MagicMock()
        fake_api.list_models.return_value = iter(
            [_fake_model_info(id=curated, tags=["mlx"], library_name="mlx", pipeline_tag="image-text-to-text")]
        )
        with patch("huggingface_hub.HfApi", return_value=fake_api):
            item = model_catalog.search_huggingface_models("gemma")[0]
        self.assertTrue(item["in_catalog"])
        self.assertEqual(item["runtime_fit"]["status"], model_catalog.RUNTIME_FIT_MANAGED_MLX_VLM)

    def test_search_error_propagates(self) -> None:
        fake_api = SimpleNamespace(list_models=lambda **kwargs: (_ for _ in ()).throw(OSError("network down")))
        with patch("huggingface_hub.HfApi", return_value=fake_api):
            with self.assertRaises(OSError):
                model_catalog.search_huggingface_models("qwythos")

    def test_fetch_metadata_not_found_is_value_error(self) -> None:
        request = httpx.Request("GET", "https://huggingface.co/api/models/missing/repo")
        not_found = errors.RepositoryNotFoundError(
            "404 Client Error: Repository Not Found for url",
            response=httpx.Response(404, request=request),
        )
        fake_api = SimpleNamespace(
            model_info=lambda **kwargs: (_ for _ in ()).throw(not_found)
        )
        with patch("huggingface_hub.HfApi", return_value=fake_api):
            with self.assertRaisesRegex(ValueError, "Model not found on Hugging Face"):
                model_catalog.fetch_model_metadata("missing/repo")

    def test_fetch_metadata_network_error_propagates(self) -> None:
        fake_api = SimpleNamespace(
            model_info=lambda **kwargs: (_ for _ in ()).throw(OSError("network down"))
        )
        with patch("huggingface_hub.HfApi", return_value=fake_api):
            with self.assertRaisesRegex(OSError, "network down"):
                model_catalog.fetch_model_metadata("owner/repo")

    def test_fetch_metadata_success_shape(self) -> None:
        fake_api = MagicMock()
        fake_api.model_info.return_value = _fake_model_info()
        with patch("huggingface_hub.HfApi", return_value=fake_api):
            item = model_catalog.fetch_model_metadata("owner/repo")
        self.assertEqual(item["id"], "owner/repo")
        self.assertEqual(item["hf_url"], "https://huggingface.co/owner/repo")
        self.assertEqual(item["runtime_fit"]["status"], model_catalog.RUNTIME_FIT_MANAGED_MLX_LM)
        kwargs = fake_api.model_info.call_args.kwargs
        self.assertEqual(kwargs["repo_id"], "owner/repo")
        self.assertEqual(kwargs["expand"], model_catalog.HF_SEARCH_EXPAND)

    def test_missing_card_data_and_config_are_tolerated(self) -> None:
        fake_info = _fake_model_info(card_data=None, config=None, tags=None)
        fake_api = SimpleNamespace(model_info=lambda **kwargs: fake_info)
        with patch("huggingface_hub.HfApi", return_value=fake_api):
            item = model_catalog.fetch_model_metadata("owner/repo")
        self.assertIsNone(item["license"])
        self.assertIsNone(item["context_length"])
        self.assertEqual(item["tags"], [])


if __name__ == "__main__":
    unittest.main()
