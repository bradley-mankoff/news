"""Unit tests for the stdlib-only llama.cpp adapter (issue #75).

The adapter must be safe, deterministic, and cross-platform with no network
or native-runtime side effects: reference parsing, command construction, and
executable availability checks are all tested offline.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from news_pipeline.llama_cpp_adapter import (
    DEFAULT_LLAMA_CPP_SERVER,
    LlamaCppModelSource,
    build_llama_cpp_server_command,
    ensure_llama_cpp_server_available,
    parse_llama_cpp_model_reference,
)


class ParseModelReferenceTests(unittest.TestCase):
    def test_hf_file_qualified_reference(self) -> None:
        source = parse_llama_cpp_model_reference(
            "unsloth/gemma-4-E2B-it-GGUF/"
            "gemma-4-E2B-it-UD-Q4_K_XL.gguf"
        )
        self.assertEqual(
            source,
            LlamaCppModelSource(
                kind="hf_file",
                hf_repo="unsloth/gemma-4-E2B-it-GGUF",
                hf_file="gemma-4-E2B-it-UD-Q4_K_XL.gguf",
            ),
        )

    def test_bare_hf_repo_reference(self) -> None:
        source = parse_llama_cpp_model_reference("owner/repo")
        self.assertEqual(source, LlamaCppModelSource(kind="hf_repo", hf_repo="owner/repo"))

    def test_normalized_hf_url_forms(self) -> None:
        for url in (
            "https://huggingface.co/owner/repo",
            "https://hf.co/owner/repo",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    parse_llama_cpp_model_reference(url),
                    LlamaCppModelSource(kind="hf_repo", hf_repo="owner/repo"),
                )
        for url in (
            "https://huggingface.co/owner/repo/model.Q4_K.gguf",
            "https://hf.co/owner/repo/model.Q4_K.gguf",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    parse_llama_cpp_model_reference(url),
                    LlamaCppModelSource(
                        kind="hf_file",
                        hf_repo="owner/repo",
                        hf_file="model.Q4_K.gguf",
                    ),
                )

    def test_local_paths(self) -> None:
        cases = {
            "/absolute/models/model.gguf": "local",
            "./relative/model.gguf": "local",
            "../up/one/model.gguf": "local",
            "models/my model.gguf": "local",
            "bare-file.gguf": "local",
            "models/foo.gguf": "local",
            "C:/models/model.gguf": "local",
        }
        for value, kind in cases.items():
            with self.subTest(value=value):
                source = parse_llama_cpp_model_reference(value)
                self.assertEqual(source.kind, kind)
                self.assertEqual(source.model_path, value)

    def test_empty_and_whitespace_rejected(self) -> None:
        for value in ("", "   ", "\t", "\n"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "non-empty"):
                    parse_llama_cpp_model_reference(value)

    def test_leading_trailing_whitespace_rejected(self) -> None:
        for value in (" owner/repo", "owner/repo ", " owner/repo "):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "whitespace"):
                    parse_llama_cpp_model_reference(value)

    def test_control_characters_rejected(self) -> None:
        for value in ("owner/repo\x00", "owner/re\npo", "owner/repo\x7f/model.gguf"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "control characters"):
                    parse_llama_cpp_model_reference(value)

    def test_non_hf_url_schemes_rejected(self) -> None:
        for value in (
            "https://example.com/owner/repo",
            "ftp://huggingface.co/owner/repo",
            "file:///tmp/model.gguf",
            "git+https://huggingface.co/owner/repo",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "URL scheme"):
                    parse_llama_cpp_model_reference(value)

    def test_hf_url_query_and_fragment_rejected(self) -> None:
        for value in (
            "https://huggingface.co/owner/repo?download=true",
            "https://hf.co/owner/repo/file.gguf#fragment",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "query strings or fragments"):
                    parse_llama_cpp_model_reference(value)

    def test_file_qualified_non_gguf_rejected(self) -> None:
        for value in (
            "owner/repo/model.safetensors",
            "owner/repo/model.bin",
            "https://huggingface.co/owner/repo/weights.pt",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, r"\.gguf file"):
                    parse_llama_cpp_model_reference(value)

    def test_traversal_and_separators_in_hf_segments_rejected(self) -> None:
        for value in (
            "owner/repo/..",
            "owner/../repo",
            "owner/repo/..%2Fevil.gguf",
            "owner/repo/..%2fevil.gguf",
            "owner/repo/..%5Cevil.gguf",
            "owner/repo/%2e%2e%2Fevil.gguf",
            "https://huggingface.co/owner/repo/..%5cevil.gguf",
            "owner//repo",
            "owner/repo/",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "Invalid Hugging Face model reference"):
                    parse_llama_cpp_model_reference(value)

    def test_backslash_values_are_treated_as_local_paths(self) -> None:
        # A backslash makes the value a local-path candidate (Windows
        # separator); a .gguf suffix keeps it valid and shell-quoted.
        source = parse_llama_cpp_model_reference(r"owner/repo/a\b.gguf")
        self.assertEqual(source.kind, "local")
        self.assertEqual(source.model_path, r"owner/repo/a\b.gguf")

    def test_too_many_segments_rejected_unless_local_gguf(self) -> None:
        with self.assertRaisesRegex(ValueError, "neither an HF owner/repo"):
            parse_llama_cpp_model_reference("owner/repo/file/extra")
        # A deep local path is a valid local source.
        source = parse_llama_cpp_model_reference("a/b/c/d.gguf")
        self.assertEqual(source.kind, "local")
        self.assertEqual(source.model_path, "a/b/c/d.gguf")

    def test_neither_hf_nor_local_gguf_rejected(self) -> None:
        for value in ("gpt-4o-mini", "not-a-path", "/owner/repo"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "neither an HF owner/repo"):
                    parse_llama_cpp_model_reference(value)
        # A three-segment value that is not .gguf is a malformed
        # file-qualified reference.
        with self.assertRaisesRegex(ValueError, r"\.gguf file"):
            parse_llama_cpp_model_reference("owner/repo/model")


class BuildCommandTests(unittest.TestCase):
    def test_hf_file_command_shape(self) -> None:
        command = build_llama_cpp_server_command(
            "owner/repo/model.Q4_K.gguf",
            alias="model.Q4_K.gguf",
            port=8081,
            parallel=4,
            max_tokens=2048,
        )
        self.assertEqual(
            command,
            "llama-server --hf-repo owner/repo --hf-file model.Q4_K.gguf "
            "--alias model.Q4_K.gguf --parallel 4 --host 127.0.0.1 "
            "--port 8081 --n-predict 2048",
        )

    def test_hf_repo_command_shape(self) -> None:
        command = build_llama_cpp_server_command(
            "owner/repo",
            alias="owner/repo",
            port=8080,
            parallel=2,
        )
        self.assertEqual(
            command,
            "llama-server --hf-repo owner/repo --alias owner/repo --parallel 2 "
            "--host 127.0.0.1 --port 8080",
        )

    def test_local_path_command_shape_and_quoting(self) -> None:
        command = build_llama_cpp_server_command(
            "/models/my model.gguf",
            alias="my model",
            port=8080,
            parallel=1,
        )
        self.assertEqual(
            command,
            "llama-server --model '/models/my model.gguf' --alias 'my model' "
            "--parallel 1 --host 127.0.0.1 --port 8080",
        )
        # shlex.split round-trips the quoted string for the launch contract.
        import shlex

        tokens = shlex.split(command)
        self.assertEqual(tokens[2], "/models/my model.gguf")

    def test_custom_binary_is_used(self) -> None:
        command = build_llama_cpp_server_command(
            "owner/repo/model.gguf",
            alias="model",
            port=8080,
            parallel=1,
            binary="/opt/llama/llama-server",
        )
        self.assertTrue(command.startswith("/opt/llama/llama-server --hf-repo"))

    def test_blank_binary_falls_back_to_default(self) -> None:
        command = build_llama_cpp_server_command(
            "owner/repo/model.gguf",
            alias="model",
            port=8080,
            parallel=1,
            binary="   ",
        )
        self.assertTrue(command.startswith(f"{DEFAULT_LLAMA_CPP_SERVER} --hf-repo"))

    def test_max_tokens_omitted_when_none(self) -> None:
        command = build_llama_cpp_server_command(
            "owner/repo/model.gguf",
            alias="model",
            port=8080,
            parallel=1,
            max_tokens=None,
        )
        self.assertNotIn("--n-predict", command)

    def test_concurrency_and_port_are_clamped(self) -> None:
        command = build_llama_cpp_server_command(
            "owner/repo/model.gguf",
            alias="model",
            port=0,
            parallel=0,
        )
        self.assertIn("--parallel 1", command)
        self.assertIn("--port 0", command)

    def test_empty_alias_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "alias must be a non-empty string"):
            build_llama_cpp_server_command(
                "owner/repo/model.gguf",
                alias="   ",
                port=8080,
                parallel=1,
            )

    def test_no_mlx_flags_ever_emitted(self) -> None:
        command = build_llama_cpp_server_command(
            "owner/repo/model.gguf",
            alias="model",
            port=8080,
            parallel=2,
            max_tokens=512,
        )
        for token in ("--prefill-step-size", "--prompt-cache-size", "--prompt-cache-bytes"):
            self.assertNotIn(token, command)
        self.assertNotIn("mlx_lm", command)
        self.assertNotIn("mlx_vlm", command)

    def test_invalid_reference_propagates_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty"):
            build_llama_cpp_server_command(
                "",
                alias="model",
                port=8080,
                parallel=1,
            )


class EnsureServerAvailableTests(unittest.TestCase):
    def test_path_name_resolved_via_which(self) -> None:
        with patch.object(shutil, "which", return_value="/usr/local/bin/llama-server"):
            self.assertEqual(
                ensure_llama_cpp_server_available("llama-server"),
                "/usr/local/bin/llama-server",
            )

    def test_missing_path_name_raises_actionable_error(self) -> None:
        with patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "llama.cpp server binary") as ctx:
                ensure_llama_cpp_server_available("llama-server")
        message = str(ctx.exception)
        self.assertIn("NEWS_LLAMA_CPP_SERVER", message)
        self.assertIn("llama.cpp/releases", message)
        self.assertIn("llama-server", message)

    def test_blank_binary_falls_back_to_default(self) -> None:
        with patch.object(shutil, "which", return_value="/bin/llama-server"):
            self.assertEqual(
                ensure_llama_cpp_server_available("   "),
                "/bin/llama-server",
            )

    def test_explicit_existing_executable_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            binary = Path(tmpdir) / "llama-server"
            binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binary.chmod(0o755)
            self.assertEqual(ensure_llama_cpp_server_available(str(binary)), str(binary))

    def test_explicit_missing_path_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not available"):
            ensure_llama_cpp_server_available("/definitely/missing/llama-server")

    def test_explicit_nonexecutable_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            binary = Path(tmpdir) / "llama-server"
            binary.write_text("not executable", encoding="utf-8")
            binary.chmod(0o644)
            with self.assertRaisesRegex(RuntimeError, "not available"):
                ensure_llama_cpp_server_available(str(binary))

    def test_relative_explicit_path_checked_on_filesystem(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            original = os.getcwd()
            os.chdir(tmpdir)
            try:
                binary = Path("local-llama-server")
                binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                binary.chmod(0o755)
                self.assertEqual(
                    ensure_llama_cpp_server_available("./local-llama-server"),
                    "./local-llama-server",
                )
            finally:
                os.chdir(original)


if __name__ == "__main__":
    unittest.main()
