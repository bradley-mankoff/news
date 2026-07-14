# Codex Execution Issues, 2026-06-28

This note records the environment and tooling problems encountered while resuming `plans/completed_2026-06-28-get-to-100-test-coverage.md`.

## Issues Observed

- `git status --short` through `/usr/bin/git` fails with:
  - `xcrun: error: invalid active developer path (/Library/Developer/CommandLineTools), missing xcrun at: /Library/Developer/CommandLineTools/usr/bin/xcrun`
- `/usr/bin/python3` fails with the same `xcrun` error, so the system Python stub is not usable here.
- `.venv/bin/python` is a broken virtualenv entrypoint in this sandbox:
  - it points at `/opt/homebrew/opt/python@3.12/bin/python3.12`
  - that target does not exist in the mounted environment
  - direct execution of `.venv/bin/python` returned `Operation not permitted`
- The expected Homebrew tool locations are effectively absent:
  - `/opt/homebrew` exists but is empty
  - `/usr/local` is empty
  - no working `uv` binary was found on the non-login PATH
- The working test runner path ended up being a uv-managed Python installed under:
  - `/private/tmp/news-uv-python/cpython-3.12.12-macos-aarch64-none/bin/python3.12`
  - it works with `uv run --isolated --no-project` plus `--with-editable /Users/home/personal_code/news`
- Full-suite pytest/coverage runs complete successfully, but the process prints a non-fatal exit-time warning from `nanobind`:
  - `RuntimeError: [metal::load_device] No Metal device available. This typically occurs in headless, sandboxed, or virtualized macOS sessions where the GPU is not accessible.`
- Because of the interpreter/tooling mismatch, I could not use the broken repo venv entrypoint, but the uv-managed path above provided a working route through the remaining coverage work.
- The plan ledger has since been brought current:
  - Step 8, Step 9, and Step 10 are now complete
  - the coverage report reached 100% for `news_pipeline`

## Impact

- I can read the repo and the plan.
- I cannot yet rely on the usual `git`, `uv`, `.venv/bin/python`, or `/usr/bin/python3` paths to run coverage or pytest.
- The next useful move is to either bootstrap a working Python test environment from the app-bundle interpreter or resolve the missing developer-tools path.

## Known Working Path

`uv` does work when it is:

- run from the repo root or another shell that can see `uv`
- pinned to the known-good interpreter at `/private/tmp/news-uv-python/cpython-3.12.12-macos-aarch64-none/bin/python3.12`
- run in isolated mode so it does not inspect the broken `.venv`

Example shape:

```bash
UV_CACHE_DIR=.uv-cache uv run --isolated --no-project \
  --python /private/tmp/news-uv-python/cpython-3.12.12-macos-aarch64-none/bin/python3.12 \
  --with-editable /Users/home/personal_code/news \
  --with pytest --with coverage \
  /bin/sh -c 'coverage run -m pytest -q && coverage report'
```

That command succeeds here and installs the project editable plus the requested tool packages without using the broken repo venv entrypoint.
