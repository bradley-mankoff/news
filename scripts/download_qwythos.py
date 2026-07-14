"""Download the two Qwythos GGUF quantizations the news project now uses.

Run as a background job; logs progress to stdout.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huggingface_hub import hf_hub_download


REPO_ID = "huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF"
TARGETS = [
    "Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q4_K.gguf",
    "Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-Q8_0.gguf",
    "mmproj-model-bf16.gguf",
]


def main() -> int:
    for filename in TARGETS:
        started = time.monotonic()
        print(f"[download] {filename}: starting", flush=True)
        try:
            path = hf_hub_download(repo_id=REPO_ID, filename=filename)
        except Exception as error:  # pragma: no cover - logged for the operator
            print(f"[download] {filename}: FAILED ({type(error).__name__}: {error})", flush=True)
            return 1
        elapsed = time.monotonic() - started
        size_gb = Path(path).stat().st_size / (1024 ** 3)
        print(
            f"[download] {filename}: OK in {elapsed:.1f}s ({size_gb:.2f} GiB) -> {path}",
            flush=True,
        )
    print("[download] all Qwythos targets downloaded.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
