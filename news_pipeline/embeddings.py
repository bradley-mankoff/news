"""Sentence embedding utilities for global story deduplication."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

EMBEDDING_MODEL_NAME = os.getenv("NEWS_EMBEDDING_MODEL", "all-mpnet-base-v2")

_model: Any = None


def _load_model() -> Any:
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "sentence-transformers is required for embedding classification. "
            "Run: uv add sentence-transformers"
        ) from exc
    _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    logger.info("Loaded embedding model: %s", EMBEDDING_MODEL_NAME)
    return _model


def embed_texts(texts: list[str]) -> "np.ndarray":  # type: ignore[name-defined]
    import numpy as np

    if not texts:
        return np.empty((0, 0), dtype=np.float32)
    model = _load_model()
    return model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
        batch_size=64,
        convert_to_numpy=True,
    )










def dedup_story_drafts(
    story_drafts: list[dict],
    *,
    threshold: float = 0.85,
) -> list[dict]:
    """Remove near-duplicate story drafts globally.

    Compares paragraph text embeddings pairwise. When two stories exceed threshold,
    the one with more source articles is kept; recency breaks ties.
    """
    if len(story_drafts) <= 1:
        return story_drafts

    texts = [
        str(
            s.get("paragraph") or s.get("story_text") or s.get("story_title") or ""
        )
        for s in story_drafts
    ]
    vecs = embed_texts(texts)  # already L2-normalised
    sims = vecs @ vecs.T

    dropped: set[int] = set()
    for i in range(len(story_drafts)):
        if i in dropped:
            continue
        for j in range(i + 1, len(story_drafts)):
            if j in dropped:
                continue
            if float(sims[i, j]) >= threshold:
                sources_i = int(story_drafts[i].get("source_count") or 0)
                sources_j = int(story_drafts[j].get("source_count") or 0)
                drop = j if sources_i >= sources_j else i
                dropped.add(drop)

    kept = [s for idx, s in enumerate(story_drafts) if idx not in dropped]
    if dropped:
        logger.info(
            "Story dedup: dropped %d near-duplicate story draft(s) globally "
            "(threshold=%.2f).",
            len(dropped),
            threshold,
        )
    return kept
