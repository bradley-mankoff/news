"""Sentence embedding utilities for cached text embeddings and global story dedup."""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

EMBEDDING_MODEL_NAME = os.getenv("NEWS_EMBEDDING_MODEL", "all-mpnet-base-v2")
ARTICLE_EMBED_BODY_CHARS = 1500
_CACHE_DB_PATH = os.path.join(
    os.path.dirname(__file__), "..", "output", "embedding_cache.db"
)

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


def _article_embed_text(article: dict) -> str:
    title = str(article.get("title") or "").strip()
    body = str(article.get("text") or "").strip()
    description = str(article.get("description") or "").strip()
    body_snippet = (body or description)[:ARTICLE_EMBED_BODY_CHARS]
    if body_snippet:
        return f"{title}. {body_snippet}"
    return title


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:40]


def _open_cache() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_CACHE_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_CACHE_DB_PATH, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS embeddings "
        "(hash TEXT PRIMARY KEY, vector BLOB NOT NULL)"
    )
    # Fresh caches use SQLite's implicit primary-key index; this covers older
    # cache files that may have been created before hash was the primary key.
    table_columns = conn.execute("PRAGMA table_info(embeddings)").fetchall()
    hash_is_primary_key = any(row[1] == "hash" and row[5] for row in table_columns)
    if not hash_is_primary_key:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_embeddings_hash ON embeddings(hash)"
        )
    conn.commit()
    return conn


def embed_articles(articles: list[dict]) -> "np.ndarray":  # type: ignore[name-defined]
    """Embed a list of articles with SQLite caching. Returns (N, D) float32 array."""
    import numpy as np

    if not articles:
        return np.empty((0, 0), dtype=np.float32)

    texts = [_article_embed_text(a) for a in articles]
    hashes = [_content_hash(t) for t in texts]

    cached: dict[str, Any] = {}
    conn = _open_cache()
    try:
        for h in hashes:
            row = conn.execute(
                "SELECT vector FROM embeddings WHERE hash = ?", (h,)
            ).fetchone()
            if row:
                cached[h] = pickle.loads(row[0])

        missing_idx = [i for i, h in enumerate(hashes) if h not in cached]
        if missing_idx:
            new_vecs = embed_texts([texts[i] for i in missing_idx])
            for pos, idx in enumerate(missing_idx):
                h = hashes[idx]
                v = new_vecs[pos]
                cached[h] = v
                conn.execute(
                    "INSERT OR REPLACE INTO embeddings (hash, vector) VALUES (?, ?)",
                    (h, pickle.dumps(v, protocol=4)),
                )
            conn.commit()
    finally:
        conn.close()

    return np.stack([cached[h] for h in hashes]).astype(np.float32)


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
