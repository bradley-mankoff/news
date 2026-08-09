#!/usr/bin/env python3
"""Compatibility entrypoint for the reusable PM harness."""

from __future__ import annotations

import sys

if __package__:
    from .pm_harness import engine as _engine
else:
    from pm_harness import engine as _engine

if __name__ == "__main__":
    raise SystemExit(_engine.main())

sys.modules[__name__] = _engine
