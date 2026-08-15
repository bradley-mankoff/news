"""Model Catalog integrity, runtime-fit, and Hugging Face integration tests.

The drift-guard test asserts that every curated catalog alias resolves to the
same model reference as ``config.MODEL_ALIASES``, so the catalog never forks
the pipeline's single source of truth (mirrors
``test_prompt_catalog.py::test_profile_ids_match_registry_keys``).
"""

from __future__ import annotations

import builtins
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import yaml
from huggingface_hub import errors

from news_pipeline import config, model_catalog


_CUSTOM_ENTRY = """\
models:
  my-mlx-model:
    reference: mlx-community/example-model
    name: Example MLX Model
    backend: mlx-lm
    hf_repo: mlx-community/example-model
    context_length: 8192
    description: A user-verified MLX language model.
    task_notes:
      speed: Fast local model.
  my-vlm-model:
    reference: mlx-community/example-vlm
    name: Example MLX VLM
    backend: mlx-vlm
    hf_repo: mlx-community/example-vlm
    description: A user-verified MLX vision-language model.
"""


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
        self.assertEqual(len(model_catalog.CATALOG_MODELS), 4)
        for entry in model_catalog.CATALOG_MODELS.values():
            self.assertTrue(entry.alias)
            self.assertTrue(entry.reference)
            self.assertTrue(entry.name)
            self.assertTrue(entry.backend)
            self.assertIn(entry.backend, config.SUPPORTED_MODEL_BACKENDS)
            self.assertTrue(entry.hf_repo)
            self.assertTrue(entry.description)
            self.assertTrue(
                set(entry.task_notes).issubset(set(model_catalog.MODEL_RECOMMENDATION_TASKS)),
                f"{entry.alias} task_notes use unknown tasks",
            )
            if entry.backend == "llama.cpp":
                # File-qualified launch identity under a bare hf_repo page id
                # (issue #75); the file name is the final path segment.
                self.assertEqual(
                    entry.reference.rsplit("/", 1)[0],
                    entry.hf_repo,
                    f"{entry.alias} reference must live under hf_repo",
                )
                self.assertTrue(entry.reference.lower().endswith(".gguf"))
                self.assertNotIn(".gguf", entry.hf_repo.lower())
            else:
                # Exact equality: runtime_fit_for_hf_model matches on hf_repo
                # only (issue #92); a file-qualified or suffixed reference
                # would silently break that invariant.
                self.assertEqual(
                    entry.reference,
                    entry.hf_repo,
                    f"{entry.alias} reference must equal hf_repo (issue #92)",
                )

    def test_catalog_backends_agree_with_runtime_inference(self) -> None:
        # Drift-guard (ship-review finding, #146): the picker must never offer
        # a curated card whose backend label disagrees with the backend the
        # pipeline actually infers for it (HANDOFF: "Model picker must validate
        # runtime support").
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
        self.assertIn("qwythos-9b-4bit", model_catalog.CATALOG_MODELS)
        self.assertIn("qwythos-9b-8bit", model_catalog.CATALOG_MODELS)
        self.assertEqual(
            model_catalog.CATALOG_MODELS["qwythos-9b-4bit"].backend,
            "llama.cpp",
        )
        for entry in model_catalog.CATALOG_MODELS.values():
            if entry.backend != "llama.cpp":
                self.assertEqual(entry.reference, entry.hf_repo)
                # i.e. reference == hf_repo exactly (no file suffix)

    # -- YAML overlay loading and merge (issue #90) -------------------------

    def test_default_yaml_template_is_empty(self) -> None:
        """The checked-in template must parse to an empty override map so the
        default merged catalog stays behavior-compatible with the built-ins."""
        payload = yaml.safe_load(model_catalog.MODEL_CATALOG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(payload, {"models": {}})
        self.assertEqual(model_catalog.load_model_catalog(), dict(model_catalog.BUILTIN_CATALOG_MODELS))

    def test_load_model_catalog_missing_file_preserves_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            merged = model_catalog.load_model_catalog(Path(tmpdir) / "missing.yaml")
        self.assertEqual(list(merged), list(model_catalog.BUILTIN_CATALOG_MODELS))
        self.assertEqual(merged, model_catalog.BUILTIN_CATALOG_MODELS)

    def test_load_model_catalog_null_payload_preserves_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            null_file = root / "null.yaml"
            null_file.write_text("null\n", encoding="utf-8")
            self.assertEqual(model_catalog.load_model_catalog(null_file), model_catalog.BUILTIN_CATALOG_MODELS)
            models_null = root / "models_null.yaml"
            models_null.write_text("models: null\n", encoding="utf-8")
            self.assertEqual(
                model_catalog.load_model_catalog(models_null),
                model_catalog.BUILTIN_CATALOG_MODELS,
            )
            empty_models = root / "empty_models.yaml"
            empty_models.write_text("models: {}\n", encoding="utf-8")
            self.assertEqual(
                model_catalog.load_model_catalog(empty_models),
                model_catalog.BUILTIN_CATALOG_MODELS,
            )

    def test_load_model_catalog_rejects_malformed_shapes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bad_root = root / "bad_root.yaml"
            bad_root.write_text("- not-a-mapping\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must contain a YAML mapping"):
                model_catalog.load_model_catalog(bad_root)

            bad_models = root / "bad_models.yaml"
            bad_models.write_text("models: []\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must define models as a mapping"):
                model_catalog.load_model_catalog(bad_models)

            false_root = root / "false_root.yaml"
            false_root.write_text("false\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must contain a YAML mapping"):
                model_catalog.load_model_catalog(false_root)

            unknown_top = root / "unknown_top.yaml"
            unknown_top.write_text("models: {}\nunknown: 1\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unknown top-level key") as ctx:
                model_catalog.load_model_catalog(unknown_top)
            self.assertIn(str(unknown_top), str(ctx.exception))

    def test_load_model_catalog_normalizes_parser_and_safe_loader_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            malformed = root / "malformed.yaml"
            malformed.write_text("models: [\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Could not load model catalog") as ctx:
                model_catalog.load_model_catalog(malformed)
            self.assertIn(str(malformed), str(ctx.exception))

            marker = root / "unsafe-executed"
            unsafe = root / "unsafe.yaml"
            unsafe.write_text(
                "models:\n"
                f"  exploit: !!python/object/apply:os.system ['touch {marker}']\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "Could not load model catalog") as ctx:
                model_catalog.load_model_catalog(unsafe)
            self.assertIn(str(unsafe), str(ctx.exception))
            self.assertFalse(marker.exists())

    def test_load_model_catalog_existing_alias_metadata_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "override.yaml"
            path.write_text(
                "models:\n"
                "  gemma-e2b-tiny:\n"
                "    name: Local Tiny Wording\n"
                "    description: Local wording for the tiny verification model.\n"
                "    context_length: 4096\n"
                "    task_notes:\n"
                "      speed: Use this entry for fast local checks.\n",
                encoding="utf-8",
            )
            merged = model_catalog.load_model_catalog(path)

        self.assertEqual(list(merged), list(model_catalog.BUILTIN_CATALOG_MODELS))
        tiny = merged["gemma-e2b-tiny"]
        self.assertEqual(tiny.name, "Local Tiny Wording")
        self.assertEqual(tiny.description, "Local wording for the tiny verification model.")
        self.assertEqual(tiny.context_length, 4096)
        # Task notes merge by task key; the built-in note is replaced, other
        # built-in identity is untouched.
        self.assertEqual(tiny.task_notes, {"speed": "Use this entry for fast local checks."})
        self.assertEqual(tiny.reference, model_catalog.BUILTIN_CATALOG_MODELS["gemma-e2b-tiny"].reference)
        self.assertEqual(tiny.backend, "mlx-lm")
        self.assertEqual(tiny.hf_repo, model_catalog.BUILTIN_CATALOG_MODELS["gemma-e2b-tiny"].hf_repo)
        # The other built-in entry is untouched and the default is unchanged.
        self.assertEqual(merged["gemma-4-12b-it-4bit"], model_catalog.BUILTIN_CATALOG_MODELS["gemma-4-12b-it-4bit"])
        self.assertEqual(merged[model_catalog.DEFAULT_CATALOG_MODEL_ALIAS].backend, "mlx-vlm")

    def test_load_model_catalog_existing_alias_identity_change_rejected(self) -> None:
        for field, value in (
            ("reference", "other-org/other-model"),
            ("backend", "external"),
            ("hf_repo", "other-org/other-model"),
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "identity.yaml"
                path.write_text(
                    f"models:\n  gemma-e2b-tiny:\n    {field}: {value}\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "does not match the built-in entry") as ctx:
                    model_catalog.load_model_catalog(path)
                self.assertIn(field, str(ctx.exception))
                self.assertIn(str(path), str(ctx.exception))

    def test_load_model_catalog_existing_alias_unknown_field_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "unknown_field.yaml"
            path.write_text(
                "models:\n  gemma-e2b-tiny:\n    temperature: 0.5\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unknown field") as ctx:
                model_catalog.load_model_catalog(path)
            self.assertIn("temperature", str(ctx.exception))

    def test_load_model_catalog_valid_new_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "custom.yaml"
            path.write_text(_CUSTOM_ENTRY, encoding="utf-8")
            merged = model_catalog.load_model_catalog(path)

        # Built-ins first (code order), then YAML additions in YAML order.
        self.assertEqual(
            list(merged),
            [
                "gemma-4-12b-it-4bit",
                "gemma-e2b-tiny",
                "qwythos-9b-4bit",
                "qwythos-9b-8bit",
                "my-mlx-model",
                "my-vlm-model",
            ],
        )
        self.assertEqual(merged["my-mlx-model"].reference, "mlx-community/example-model")
        self.assertEqual(merged["my-mlx-model"].backend, "mlx-lm")
        self.assertEqual(merged["my-mlx-model"].context_length, 8192)
        self.assertEqual(merged["my-mlx-model"].task_notes, {"speed": "Fast local model."})
        self.assertIsNone(merged["my-vlm-model"].context_length)
        self.assertEqual(merged["my-vlm-model"].task_notes, {})

    def test_load_model_catalog_new_entry_missing_required_fields(self) -> None:
        for missing in ("reference", "name", "backend", "hf_repo", "description"):
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "missing.yaml"
                lines = ["models:", "  my-model:"]
                for field in ("reference", "name", "backend", "hf_repo", "description"):
                    if field != missing:
                        lines.append(f"    {field}: {field}-value")
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                with self.assertRaisesRegex(ValueError, f"must provide: {missing}"):
                    model_catalog.load_model_catalog(path)

    def test_load_model_catalog_bad_backend_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "bad_backend.yaml"
            path.write_text(
                "models:\n"
                "  my-model:\n"
                "    reference: owner/repo\n"
                "    name: My Model\n"
                "    backend: tensorrt\n"
                "    hf_repo: owner/repo\n"
                "    description: Unsupported backend.\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "backend.*not supported") as ctx:
                model_catalog.load_model_catalog(path)
            self.assertIn("llama.cpp", str(ctx.exception))

    def test_load_model_catalog_reference_identity_rules(self) -> None:
        cases = {
            "mismatch": (
                "    reference: owner/one\n"
                "    hf_repo: owner/two\n",
                "reference must equal hf_repo",
            ),
            "gguf": (
                "    reference: owner/repo.gguf\n"
                "    hf_repo: owner/repo.gguf\n",
                "GGUF",
            ),
            "url": (
                "    reference: https://huggingface.co/owner/repo\n"
                "    hf_repo: https://huggingface.co/owner/repo\n",
                "exactly one '/'",
            ),
            "file_qualified": (
                "    reference: owner/repo/file.gguf\n"
                "    hf_repo: owner/repo/file.gguf\n",
                "GGUF",
            ),
            "bare_org": (
                "    reference: owner\n"
                "    hf_repo: owner\n",
                "exactly one '/'",
            ),
        }
        for label, (lines, expected) in cases.items():
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / f"identity_{label}.yaml"
                path.write_text(
                    "models:\n"
                    "  my-model:\n"
                    f"{lines}"
                    "    name: My Model\n"
                    "    backend: mlx-lm\n"
                    "    description: Identity rule case.\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, expected):
                    model_catalog.load_model_catalog(path)

    def test_load_model_catalog_llama_cpp_identity_rules(self) -> None:
        """llama.cpp entries use a file-qualified .gguf reference under a bare
        hf_repo page id; every other shape fails closed (issue #75)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            valid = root / "valid_llama.yaml"
            valid.write_text(
                "models:\n"
                "  my-gguf-model:\n"
                "    reference: owner/example-repo/example.Q4_K.gguf\n"
                "    name: Example GGUF\n"
                "    backend: llama.cpp\n"
                "    hf_repo: owner/example-repo\n"
                "    description: A user-verified GGUF model.\n",
                encoding="utf-8",
            )
            merged = model_catalog.load_model_catalog(valid)
            self.assertEqual(
                merged["my-gguf-model"].reference,
                "owner/example-repo/example.Q4_K.gguf",
            )
            self.assertEqual(merged["my-gguf-model"].backend, "llama.cpp")
            self.assertEqual(merged["my-gguf-model"].hf_repo, "owner/example-repo")

            cases = {
                "bare_repo": (
                    "    reference: owner/example-repo\n"
                    "    hf_repo: owner/example-repo\n",
                    "file-qualified",
                ),
                "repo_mismatch": (
                    "    reference: owner/other-repo/example.Q4_K.gguf\n"
                    "    hf_repo: owner/example-repo\n",
                    "under hf_repo",
                ),
                "non_gguf_file": (
                    "    reference: owner/example-repo/example.safetensors\n"
                    "    hf_repo: owner/example-repo\n",
                    r"\.gguf file",
                ),
                "traversal": (
                    "    reference: owner/example-repo/../evil.gguf\n"
                    "    hf_repo: owner/example-repo\n",
                    "file-qualified",
                ),
                "gguf_hf_repo": (
                    "    reference: owner/example-repo/example.Q4_K.gguf\n"
                    "    hf_repo: owner/example-repo.gguf\n",
                    "GGUF",
                ),
                "url_form": (
                    "    reference: https://huggingface.co/owner/repo/file.gguf\n"
                    "    hf_repo: owner/repo\n",
                    "file-qualified",
                ),
            }
            for label, (lines, expected) in cases.items():
                with self.subTest(label=label):
                    path = root / f"llama_{label}.yaml"
                    path.write_text(
                        "models:\n"
                        "  my-gguf-model:\n"
                        f"{lines}"
                        "    name: Example GGUF\n"
                        "    backend: llama.cpp\n"
                        "    description: Identity rule case.\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, expected):
                        model_catalog.load_model_catalog(path)

    def test_load_model_catalog_invalid_context_length(self) -> None:
        for raw in ("0", "-1", "true", "'8192'", "1.5"):
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "bad_ctx.yaml"
                path.write_text(
                    "models:\n"
                    "  my-model:\n"
                    "    reference: owner/repo\n"
                    "    name: My Model\n"
                    "    backend: mlx-lm\n"
                    "    hf_repo: owner/repo\n"
                    "    description: Bad context length.\n"
                    f"    context_length: {raw}\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "context_length must be") as ctx:
                    model_catalog.load_model_catalog(path)
                self.assertIn("my-model", str(ctx.exception))

    def test_load_model_catalog_invalid_task_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            unknown_task = root / "unknown_task.yaml"
            unknown_task.write_text(
                "models:\n"
                "  my-model:\n"
                "    reference: owner/repo\n"
                "    name: My Model\n"
                "    backend: mlx-lm\n"
                "    hf_repo: owner/repo\n"
                "    description: Bad task note.\n"
                "    task_notes:\n"
                "      not-a-task: nope\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "not a known recommendation task") as ctx:
                model_catalog.load_model_catalog(unknown_task)
            self.assertIn("factual_extraction", str(ctx.exception))

            empty_note = root / "empty_note.yaml"
            empty_note.write_text(
                "models:\n"
                "  my-model:\n"
                "    reference: owner/repo\n"
                "    name: My Model\n"
                "    backend: mlx-lm\n"
                "    hf_repo: owner/repo\n"
                "    description: Empty note.\n"
                "    task_notes:\n"
                "      speed: '  '\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "non-empty string"):
                model_catalog.load_model_catalog(empty_note)

            bad_notes = root / "bad_notes.yaml"
            bad_notes.write_text(
                "models:\n"
                "  my-model:\n"
                "    reference: owner/repo\n"
                "    name: My Model\n"
                "    backend: mlx-lm\n"
                "    hf_repo: owner/repo\n"
                "    description: List notes.\n"
                "    task_notes: []\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "task_notes must be a mapping"):
                model_catalog.load_model_catalog(bad_notes)

    def test_load_model_catalog_invalid_aliases(self) -> None:
        bad_aliases = ("Bad Alias", "UPPER", "-leading", "trailing-", " spaced ", "")
        for alias in bad_aliases:
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "bad_alias.yaml"
                path.write_text(
                    f"models:\n  {alias!r}: {{}}\n" if alias else "models:\n  '': {}\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ValueError, "alias"):
                    model_catalog.load_model_catalog(path)
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "non_string_alias.yaml"
            path.write_text("models:\n  123: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "aliases must be strings"):
                model_catalog.load_model_catalog(path)

    def test_catalog_model_backend_helper(self) -> None:
        # Built-in alias/reference lookups.
        self.assertEqual(model_catalog.catalog_model_backend("gemma-4-12b-it-4bit"), "mlx-vlm")
        self.assertEqual(
            model_catalog.catalog_model_backend("mlx-community/gemma-4-12B-it-4bit"),
            "mlx-vlm",
        )
        self.assertEqual(model_catalog.catalog_model_backend("gemma-e2b-tiny"), "mlx-lm")
        # Exact matching only: bare org and prefix siblings are unknown.
        self.assertIsNone(model_catalog.catalog_model_backend("mlx-community"))
        self.assertIsNone(model_catalog.catalog_model_backend("mlx-community/gemma-4-12B-it-4bit-other"))
        self.assertIsNone(model_catalog.catalog_model_backend("gpt-4o-mini"))
        self.assertIsNone(model_catalog.catalog_model_backend(""))

    def test_custom_catalog_aliases_and_backend_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "custom.yaml"
            path.write_text(_CUSTOM_ENTRY, encoding="utf-8")
            custom = model_catalog.load_model_catalog(path)
        with patch.object(model_catalog, "_CATALOG_SNAPSHOT", custom):
            self.assertEqual(
                model_catalog.custom_catalog_aliases(),
                {
                    "my-mlx-model": "mlx-community/example-model",
                    "my-vlm-model": "mlx-community/example-vlm",
                },
            )
            self.assertEqual(model_catalog.catalog_model_backend("my-mlx-model"), "mlx-lm")
            self.assertEqual(model_catalog.catalog_model_backend("mlx-community/example-vlm"), "mlx-vlm")
            self.assertIsNone(model_catalog.catalog_model_backend("owner/repo"))
            records = model_catalog.list_model_catalog()
            self.assertEqual([record["alias"] for record in records][-2:], ["my-mlx-model", "my-vlm-model"])
            # Runtime-fit matching and recommendations see the merged registry.
            managed_fit = model_catalog.runtime_fit_for_hf_model(
                {
                    "id": "mlx-community/example-model",
                    "tags": [],
                    "library_name": "unknown",
                    "pipeline_tag": None,
                }
            )
            self.assertEqual(managed_fit["status"], model_catalog.RUNTIME_FIT_MANAGED_MLX_LM)
            self.assertIn("advisory", managed_fit["reason"])
            self.assertEqual(
                model_catalog.runtime_fit_for_hf_model(
                    {"id": "mlx-community/example-vlm", "tags": [], "library_name": "unknown", "pipeline_tag": None}
                )["status"],
                model_catalog.RUNTIME_FIT_MANAGED_MLX_VLM,
            )
            picks = model_catalog.recommend_models("speed")
            self.assertEqual(
                [pick["alias"] for pick in picks],
                ["gemma-e2b-tiny", "my-mlx-model", "gemma-4-12b-it-4bit"],
            )
            # The default marker stays code-owned: YAML additions are never default.
            defaults = [record for record in records if record["is_default"]]
            self.assertEqual([record["alias"] for record in defaults], ["gemma-4-12b-it-4bit"])
            self.assertTrue(
                all(record["hf_url"].startswith("https://huggingface.co/") for record in records)
            )

    def test_custom_external_catalog_entry_is_external_only_and_advisory(self) -> None:
        custom = dict(model_catalog.BUILTIN_CATALOG_MODELS)
        custom["my-ext-model"] = model_catalog.CatalogModel(
            alias="my-ext-model",
            reference="external-org/openai-compatible",
            name="External Model",
            backend="external",
            hf_repo="external-org/openai-compatible",
            context_length=None,
            description="A user-declared external endpoint model.",
            task_notes={},
        )
        with patch.object(model_catalog, "_CATALOG_SNAPSHOT", custom):
            fit = model_catalog.runtime_fit_for_hf_model(
                {
                    "id": "external-org/openai-compatible",
                    "tags": [],
                    "library_name": "unknown",
                    "pipeline_tag": None,
                }
            )

        self.assertEqual(fit["status"], model_catalog.RUNTIME_FIT_EXTERNAL_ONLY)
        self.assertIn("external", fit["reason"])
        self.assertIn("advisory", fit["reason"])

    def test_env_path_selection_and_relative_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_path = Path(tmpdir) / "custom_catalog.yaml"
            custom_path.write_text(_CUSTOM_ENTRY, encoding="utf-8")
            with patch.dict(os.environ, {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: str(custom_path)}, clear=False):
                self.assertEqual(model_catalog._catalog_path_from_env(), custom_path)
                merged = model_catalog.load_model_catalog()
            self.assertIn("my-mlx-model", merged)
        # Missing env var: the checked-in default path.
        with patch.dict(os.environ, {}, clear=False):
            self.assertEqual(model_catalog._catalog_path_from_env(), model_catalog.MODEL_CATALOG_PATH)
        # Relative paths resolve from the repository root.
        with patch.dict(os.environ, {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: "config/model_catalog.yaml"}, clear=False):
            self.assertEqual(
                model_catalog._catalog_path_from_env(),
                model_catalog.ROOT_DIR / "config" / "model_catalog.yaml",
            )

    def test_merged_snapshot_honors_env_path_and_restores_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_path = Path(tmpdir) / "custom_catalog.yaml"
            custom_path.write_text(_CUSTOM_ENTRY, encoding="utf-8")
            with patch.dict(os.environ, {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: str(custom_path)}, clear=False):
                with patch.object(model_catalog, "_CATALOG_SNAPSHOT", None):
                    snapshot = model_catalog.CATALOG_MODELS
            self.assertIn("my-mlx-model", snapshot)
        # The per-process snapshot falls back to the default (built-ins only).
        self.assertNotIn("my-mlx-model", model_catalog.CATALOG_MODELS)

    def test_merged_snapshot_is_stable_until_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_path = Path(tmpdir) / "custom_catalog.yaml"
            custom_path.write_text(_CUSTOM_ENTRY, encoding="utf-8")
            with patch.dict(
                os.environ,
                {model_catalog.MODEL_CATALOG_YAML_ENV_VAR: str(custom_path)},
                clear=False,
            ), patch.object(model_catalog, "_CATALOG_SNAPSHOT", None):
                first = model_catalog._merged_catalog_snapshot()
                custom_path.write_text("models: {}\n", encoding="utf-8")
                second = model_catalog._merged_catalog_snapshot()

        self.assertIs(first, second)
        self.assertIn("my-mlx-model", second)

    def test_catalog_backends_match_config_supported_backends(self) -> None:
        self.assertEqual(
            set(model_catalog.CATALOG_MODEL_BACKENDS),
            set(config.SUPPORTED_MODEL_BACKENDS),
        )

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
        # Speed's curated pick is the tiny test model, with the default model
        # appended exactly once as the fallback when it is not already a pick.
        speed_picks = model_catalog.recommend_models("speed")
        self.assertEqual(
            [pick["alias"] for pick in speed_picks],
            ["gemma-e2b-tiny", model_catalog.DEFAULT_CATALOG_MODEL_ALIAS],
        )
        self.assertEqual(speed_picks[-1]["alias"], model_catalog.DEFAULT_CATALOG_MODEL_ALIAS)
        # The default model covers the quality tasks first and is never
        # duplicated when it is already a curated pick.
        for task in ("factual_extraction", "structured_output", "synthesis", "citation_fidelity", "context_length"):
            picks = model_catalog.recommend_models(task)
            self.assertEqual(
                [pick["alias"] for pick in picks],
                [model_catalog.DEFAULT_CATALOG_MODEL_ALIAS],
            )
        # Every returned pick record is JSON-ready and carries the lean
        # recommendation contract fields (no catalog-card-only extras).
        for task in model_catalog.MODEL_RECOMMENDATION_TASKS:
            for pick in model_catalog.recommend_models(task):
                self.assertEqual(
                    set(pick),
                    {"alias", "name", "backend", "hf_repo", "reason"},
                )

    def test_recommend_unknown_task_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown model recommendation task"):
            model_catalog.recommend_models("not-a-task")
        with self.assertRaisesRegex(ValueError, "factual_extraction"):
            model_catalog.recommend_models("not-a-task")

    def test_list_model_catalog_is_json_ready(self) -> None:
        records = model_catalog.list_model_catalog()
        self.assertEqual(len(records), 4)
        for record in records:
            self.assertIsInstance(record, dict)
            self.assertTrue(record["hf_url"].startswith("https://huggingface.co/"))
            self.assertIn("task_notes", record)
            self.assertIn("is_default", record)
        defaults = [record for record in records if record["is_default"]]
        self.assertEqual([record["alias"] for record in defaults], ["gemma-4-12b-it-4bit"])
        records_by_alias = {record["alias"]: record for record in records}
        self.assertEqual(records_by_alias["gemma-e2b-tiny"]["backend"], "mlx-lm")
        qwythos = {record["alias"]: record for record in records if record["alias"].startswith("qwythos")}
        self.assertEqual(set(qwythos), {"qwythos-9b-4bit", "qwythos-9b-8bit"})
        for record in qwythos.values():
            self.assertEqual(record["backend"], "llama.cpp")
            self.assertTrue(record["reference"].endswith(".gguf"))
            self.assertEqual(
                record["hf_url"],
                "https://huggingface.co/huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF",
            )

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
                model_catalog.RUNTIME_FIT_MANAGED_MLX_LM,
            ),
            (
                {"id": "someone/arbitrary-gguf", "tags": ["gguf", "text-generation"], "library_name": "transformers", "pipeline_tag": "text-generation"},
                model_catalog.RUNTIME_FIT_MANAGED_LLAMA_CPP,
            ),
            (
                {"id": "someone/multimodal-gguf", "tags": ["gguf", "image-text-to-text"], "library_name": "transformers", "pipeline_tag": "image-text-to-text"},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
            (
                {"id": "someone/text2text-gguf", "tags": ["gguf"], "library_name": "transformers", "pipeline_tag": "text2text-generation"},
                model_catalog.RUNTIME_FIT_MANAGED_LLAMA_CPP,
            ),
            (
                {"id": "someone/audio-gguf", "tags": ["gguf"], "library_name": "transformers", "pipeline_tag": "audio-classification"},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
            (
                {"id": "someone/embedding-gguf", "tags": ["gguf"], "library_name": "transformers", "pipeline_tag": "feature-extraction"},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
            (
                {"id": "someone/unknown-task-gguf", "tags": ["gguf"], "library_name": "transformers", "pipeline_tag": None},
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
            # Org-id false positives (issue #92): a bare org id must never
            # match a curated entry, even when it is the org prefix of one.
            # huihui-ai is the historical pin from the PR #73 report (the
            # Qwythos-era reference lived under that org); mlx-community is
            # the live org-prefix collision with the current default repo.
            (
                {"id": "huihui-ai", "tags": [], "library_name": "unknown", "pipeline_tag": None},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
            (
                {"id": "mlx-community", "tags": [], "library_name": "unknown", "pipeline_tag": None},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
            # Org-id false positive for the mlx-lm curated entry (issue #92):
            # deadbydawn101 is the org of gemma-e2b-tiny, not curated.
            (
                {"id": "deadbydawn101", "tags": [], "library_name": "unknown", "pipeline_tag": None},
                model_catalog.RUNTIME_FIT_EXTERNAL_ONLY,
            ),
            # Prefix-collision sibling (issue #92): a repo whose name merely
            # starts with a curated repo's name must not match the curated
            # entry (which would yield MANAGED_MLX_VLM here); the generic MLX
            # heuristic verdict is the expected, correct outcome.
            (
                {"id": "mlx-community/gemma-4-12B-it-4bit-other", "tags": ["mlx"], "library_name": "mlx", "pipeline_tag": "text-generation"},
                model_catalog.RUNTIME_FIT_MANAGED_MLX_LM,
            ),
            # Prefix-collision sibling of the mlx-lm curated entry (issue
            # #92): the curated and generic-MLX verdicts coincide here, so
            # this row pins behavior; the deadbydawn101 org-id row above is
            # the discriminating guard for the mlx-lm entry.
            (
                {"id": "deadbydawn101/gemma-4-E2B-Heretic-Uncensored-mlx-4bit-other", "tags": ["mlx"], "library_name": "mlx", "pipeline_tag": "text-generation"},
                model_catalog.RUNTIME_FIT_MANAGED_MLX_LM,
            ),
            (
                {"id": "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF", "tags": ["gguf"], "library_name": "transformers", "pipeline_tag": "text-generation"},
                model_catalog.RUNTIME_FIT_MANAGED_LLAMA_CPP,
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

        # Org-id search result must not be flagged as in-catalog (issue #92).
        org_api = MagicMock()
        org_api.list_models.return_value = iter(
            [_fake_model_info(id="mlx-community", tags=[], library_name="unknown", pipeline_tag=None)]
        )
        with patch("huggingface_hub.HfApi", return_value=org_api):
            org_item = model_catalog.search_huggingface_models("mlx-community")[0]
        self.assertFalse(org_item["in_catalog"])
        self.assertEqual(org_item["runtime_fit"]["status"], model_catalog.RUNTIME_FIT_EXTERNAL_ONLY)

    def test_search_error_propagates(self) -> None:
        fake_api = SimpleNamespace(list_models=lambda **kwargs: (_ for _ in ()).throw(OSError("network down")))
        with patch("huggingface_hub.HfApi", return_value=fake_api):
            with self.assertRaises(OSError):
                model_catalog.search_huggingface_models("qwythos")

    def test_hf_api_missing_dependency_reports_actionable_guidance(self) -> None:
        original_import = builtins.__import__
        missing = ImportError("missing")
        delegated: list[str] = []

        def fake_import(
            name: str,
            globals: dict[str, object] | None = None,
            locals: dict[str, object] | None = None,
            fromlist: tuple[str, ...] | list[str] = (),
            level: int = 0,
        ) -> object:
            if name == "huggingface_hub":
                raise missing
            delegated.append(name)
            return original_import(name, globals, locals, fromlist, level)

        with patch("builtins.__import__", side_effect=fake_import):
            with self.assertRaisesRegex(
                ImportError, "huggingface-hub is required for Hugging Face search"
            ) as ctx:
                model_catalog._hf_api()
            imported = __import__("json")

        self.assertIn("uv add huggingface-hub", str(ctx.exception))
        self.assertIs(ctx.exception.__cause__, missing)
        self.assertEqual(imported.__name__, "json")
        self.assertIn("json", delegated)
        self.assertIs(builtins.__import__, original_import)

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
