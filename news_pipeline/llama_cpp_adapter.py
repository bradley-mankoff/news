"""Stdlib-only llama.cpp adapter for managed GGUF model servers.

Parses supported GGUF model sources (Hugging Face repo ids, HF
file-qualified references, and local ``.gguf`` paths), builds shell-safe
``llama-server`` command strings, and validates the configured native
``llama-server`` executable immediately before process launch.

The adapter never downloads model files, never spawns a process, and never
imports ``news_pipeline.config`` (no import cycle): the existing managed
server lifecycle in ``news_pipeline/pipeline.py`` owns process startup,
readiness, logging, and teardown. The native ``llama-server`` binary is an
operator-installed prerequisite (official llama.cpp releases); the
application never installs or downloads it.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass
from urllib.parse import unquote

DEFAULT_LLAMA_CPP_SERVER = "llama-server"
HF_URL_PREFIXES = ("https://huggingface.co/", "https://hf.co/")
_GGUF_SUFFIX = ".gguf"
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class LlamaCppModelSource:
    """One validated GGUF model source for a llama-server command.

    ``kind`` is ``"hf_file"`` (mapped to ``--hf-repo`` plus ``--hf-file``),
    ``"hf_repo"`` (mapped to ``--hf-repo``; llama.cpp applies its documented
    default quantization), or ``"local"`` (mapped to ``--model``).
    """

    kind: str
    hf_repo: str | None = None
    hf_file: str | None = None
    model_path: str | None = None


def _strip_hf_url_prefix(value: str) -> str | None:
    for prefix in HF_URL_PREFIXES:
        if value.startswith(prefix):
            return value[len(prefix) :]
    return None


def _validate_hf_segment(segment: str, label: str) -> str:
    """Validate one owner/repo/file segment of an HF reference.

    Segments must be non-empty, free of path separators and traversal
    (``.``/``..``), and contain no control characters (checked globally by
    the caller)."""
    if not segment or segment in (".", ".."):
        raise ValueError(
            f"Invalid Hugging Face model reference: {label} segment {segment!r} "
            "must be non-empty and must not be '.' or '..'."
        )
    decoded = unquote(segment)
    if (
        "/" in segment
        or "\\" in segment
        or "/" in decoded
        or "\\" in decoded
        or decoded in (".", "..")
    ):
        raise ValueError(
            f"Invalid Hugging Face model reference: {label} segment {segment!r} "
            "must not contain path separators or traversal."
        )
    return segment


def _parse_hf_reference(value: str) -> LlamaCppModelSource:
    segments = value.split("/")
    if len(segments) == 2:
        owner = _validate_hf_segment(segments[0], "owner")
        repo = _validate_hf_segment(segments[1], "repo")
        return LlamaCppModelSource(kind="hf_repo", hf_repo=f"{owner}/{repo}")
    if len(segments) == 3:
        owner, repo, filename = (
            _validate_hf_segment(segments[0], "owner"),
            _validate_hf_segment(segments[1], "repo"),
            _validate_hf_segment(segments[2], "file name"),
        )
        if not filename.lower().endswith(_GGUF_SUFFIX):
            raise ValueError(
                f"File-qualified model reference must name a .gguf file: "
                f"{value!r}. Use owner/repo for a bare Hugging Face repo."
            )
        return LlamaCppModelSource(
            kind="hf_file",
            hf_repo=f"{owner}/{repo}",
            hf_file=filename,
        )
    raise ValueError(
        f"Invalid Hugging Face model reference {value!r}: expected owner/repo "
        "or owner/repo/file.gguf."
    )


def _looks_like_hf_reference(value: str) -> bool:
    """True when the value uses the strict HF ``owner/repo`` grammar.

    A two-segment value whose final segment ends in ``.gguf`` is treated as
    a local relative path (``models/foo.gguf``), and anything containing
    whitespace, backslashes, or leading ``.``/``/`` is a local path
    candidate; the strictly safe three-segment form is the HF
    ``owner/repo/file.gguf`` file-qualified reference.
    """
    if any(char.isspace() for char in value) or "\\" in value:
        return False
    if value.startswith((".", "/")):
        return False
    if len(value) >= 2 and value[1] == ":":  # Windows drive-letter path
        return False
    segments = value.split("/")
    if len(segments) == 2:
        return not segments[-1].lower().endswith(_GGUF_SUFFIX)
    return len(segments) == 3


def parse_llama_cpp_model_reference(reference: str) -> LlamaCppModelSource:
    """Parse a GGUF model source into a validated, launch-safe description.

    Supported inputs: normalized HF URLs (``https://huggingface.co/...`` and
    ``https://hf.co/...``), raw HF ``owner/repo`` ids, HF
    ``owner/repo/file.gguf`` file-qualified references, and local ``.gguf``
    paths (absolute, relative, or bare file names).

    Raises ``ValueError`` for empty/whitespace values, control characters,
    non-HF URL schemes, traversal or separator characters in HF segments,
    file-qualified non-GGUF references, and values that are neither a valid
    HF reference nor a local ``.gguf`` path.
    """
    value = reference or ""
    if not value.strip():
        raise ValueError("Model reference must be a non-empty string.")
    if value != value.strip():
        raise ValueError(
            f"Model reference must not have leading/trailing whitespace: {value!r}."
        )
    if _CONTROL_CHARS_RE.search(value):
        raise ValueError(
            f"Model reference must not contain control characters: {value!r}."
        )
    stripped = _strip_hf_url_prefix(value)
    if "://" in value and stripped is None:
        raise ValueError(
            f"Unsupported model reference URL scheme: {value!r}. Use an "
            "https://huggingface.co/... or https://hf.co/... reference, an "
            "owner/repo (or owner/repo/file.gguf) id, or a local .gguf path."
        )
    if stripped is not None:
        if "?" in stripped or "#" in stripped:
            raise ValueError(
                f"Invalid Hugging Face model reference {value!r}: URLs must not "
                "carry query strings or fragments."
            )
        return _parse_hf_reference(stripped)
    if _looks_like_hf_reference(value):
        return _parse_hf_reference(value)
    if not value.lower().endswith(_GGUF_SUFFIX):
        raise ValueError(
            f"Model reference {value!r} is neither an HF owner/repo (or "
            "owner/repo/file.gguf) id nor a local .gguf path."
        )
    return LlamaCppModelSource(kind="local", model_path=value)


def build_llama_cpp_server_command(
    model_reference: str,
    *,
    alias: str,
    port: int,
    parallel: int,
    max_tokens: int | None = None,
    binary: str = DEFAULT_LLAMA_CPP_SERVER,
) -> str:
    """Return a shell-safe ``llama-server`` command string (never executed).

    The string is consumed by ``shlex.split`` at process launch with
    ``shell=False``; ``shlex.join`` quotes every argument so references with
    spaces or shell metacharacters stay safe. ``--alias`` pins the OpenAI
    model id reported by ``/v1/models`` so the existing managed readiness
    model-match check is deterministic.

    HF file-qualified references map to ``--hf-repo`` plus ``--hf-file``,
    bare HF repos map to ``--hf-repo`` (default quantization), and local
    paths map to ``--model``. MLX-only flags are never emitted.
    """
    if not (alias or "").strip():
        raise ValueError("llama.cpp server alias must be a non-empty string.")
    source = parse_llama_cpp_model_reference(model_reference)
    executable = (binary or "").strip() or DEFAULT_LLAMA_CPP_SERVER
    args = [executable]
    if source.kind == "hf_file":
        args.extend(["--hf-repo", source.hf_repo, "--hf-file", source.hf_file])
    elif source.kind == "hf_repo":
        args.extend(["--hf-repo", source.hf_repo])
    else:
        args.extend(["--model", source.model_path])
    args.extend(["--alias", alias.strip()])
    args.extend(["--parallel", str(max(1, int(parallel)))])
    args.extend(["--host", "127.0.0.1", "--port", str(int(port))])
    if max_tokens is not None:
        args.extend(["--n-predict", str(max_tokens)])
    return shlex.join(args)


def _is_explicit_path(value: str) -> bool:
    return (
        "/" in value
        or "\\" in value
        or value.startswith(".")
        or (len(value) >= 2 and value[1] == ":")
    )


def ensure_llama_cpp_server_available(
    binary: str = DEFAULT_LLAMA_CPP_SERVER,
) -> str:
    """Resolve the llama-server executable or raise an actionable RuntimeError.

    A plain PATH name is resolved with ``shutil.which``; an explicit path
    (containing a separator, or starting with ``.``) must exist and be
    executable. Blank values fall back to ``llama-server``. This check runs
    only immediately before process launch, never during Runtime Config
    resolution or UI previews, and never downloads or installs anything.
    """
    raw = (binary or "").strip() or DEFAULT_LLAMA_CPP_SERVER
    if _is_explicit_path(raw):
        if os.path.isfile(raw) and os.access(raw, os.X_OK):
            return raw
    else:
        resolved = shutil.which(raw)
        if resolved:
            return resolved
    raise RuntimeError(
        f"llama.cpp server binary {raw!r} is not available. Install an official "
        "llama.cpp release for your platform "
        "(https://github.com/ggml-org/llama.cpp/releases) so it provides "
        f"{DEFAULT_LLAMA_CPP_SERVER!r}, or set NEWS_LLAMA_CPP_SERVER to the "
        "installed executable path."
    )
