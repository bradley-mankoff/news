"""Sentence embedding utilities for semantic article-topic classification and story dedup.

Primary entry points:
  classify_articles_by_topic(articles, topics, threshold) -> list[dict]
      Overwrites each article's topic_key/topic_title with the best semantic match above
      threshold, then deduplicates by (url, topic_key).

  dedup_story_drafts_within_topic(story_drafts, threshold) -> list[dict]
      Removes near-duplicate story drafts within a single topic using cosine similarity
      on their drafted paragraph text.

Model:
  Loads sentence-transformers lazily; falls back gracefully if not installed.
  Default: all-mpnet-base-v2 (768d, CPU/MPS-friendly, strong semantic quality).
  Override with NEWS_EMBEDDING_MODEL env var.

Cache:
  Article embeddings are cached in output/embedding_cache.db (SQLite + pickle).
  Cache is keyed by SHA-256 of the input text so reruns skip re-embedding.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pickle
import re
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


def embed_topics(topics: list[dict]) -> dict[str, "np.ndarray"]:  # type: ignore[name-defined]
    """Embed topic descriptions. Returns {topic_key: embedding_vector}."""
    if not topics:
        return {}
    topic_keys = [str(t.get("key") or t.get("id") or "") for t in topics]
    if len(set(topic_keys)) != len(topic_keys):
        seen = set()
        dupes = []
        for key in topic_keys:
            if key in seen:
                dupes.append(key)
            else:
                seen.add(key)
        logger.warning("Duplicate topic keys detected, embeddings will be overwritten: %s", dupes)
    topic_texts = [
        str(t.get("description") or t.get("rationale") or t.get("title") or "")
        for t in topics
    ]
    vecs = embed_texts(topic_texts)
    return {k: vecs[i] for i, k in enumerate(topic_keys)}


def classify_articles_by_topic(
    articles: list[dict],
    topics: list[dict],
    *,
    threshold: float = 0.20,
) -> list[dict]:
    """Assign each article to every topic whose cosine similarity meets threshold.

    An article may appear multiple times in the output — once per matching topic —
    so it can contribute to story clusters in each relevant topic independently.
    Articles that match no topic above threshold are dropped entirely.

    After classification, duplicates with identical (url, topic_key) are dropped so
    the same article is never processed twice for the same topic.

    Logs per-topic article counts for diagnostic tuning.
    """
    import numpy as np

    if not articles or not topics:
        return articles

    logger.info(
        "Embedding-classifying %d articles across %d topics (threshold=%.2f).",
        len(articles),
        len(topics),
        threshold,
    )

    article_vecs = embed_articles(articles)
    topic_emb = embed_topics(topics)
    topic_keys = [str(t.get("key") or t.get("id") or "") for t in topics]
    topic_titles = {
        str(t.get("key") or t.get("id") or ""): str(t.get("title") or "")
        for t in topics
    }

    topic_matrix = np.stack([topic_emb[k] for k in topic_keys])  # (T, D)
    sims = article_vecs @ topic_matrix.T  # (N, T)

    classified: list[dict] = []
    dropped = 0
    for i, article in enumerate(articles):
        matched = False
        for j, topic_key in enumerate(topic_keys):
            score = float(sims[i, j])
            if score >= threshold:
                classified.append(
                    {
                        **article,
                        "topic_key": topic_key,
                        "topic_title": topic_titles.get(topic_key, ""),
                        "embedding_topic_score": round(score, 4),
                    }
                )
                matched = True
        if not matched:
            dropped += 1

    if dropped:
        logger.info(
            "Embedding classification: dropped %d off-topic article(s) below threshold %.2f.",
            dropped,
            threshold,
        )

    classified = _dedup_by_url_topic(classified)

    counts: dict[str, int] = {}
    for a in classified:
        k = str(a.get("topic_key") or "unassigned")
        counts[k] = counts.get(k, 0) + 1
    for t in topics:
        k = str(t.get("key") or t.get("id") or "")
        title = topic_titles.get(k, k)
        logger.info("  %s: %d articles after embedding classification", title, counts.get(k, 0))

    return classified


def _dedup_by_url_topic(articles: list[dict]) -> list[dict]:
    """Drop duplicates with the same (canonical_url, topic_key) pair."""
    seen: set[tuple[str, str]] = set()
    result: list[dict] = []
    for article in articles:
        url = _canonical_url(
            str(article.get("resolved_url") or article.get("url") or "")
        )
        topic_key = str(article.get("topic_key") or "").strip()
        key = (url, topic_key)
        if url and key in seen:
            continue
        if url:
            seen.add(key)
        result.append(article)
    return result


def _canonical_url(url: str) -> str:
    raw = url.strip()
    if not raw:
        return ""
    raw = re.sub(r"^https?://", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"^www\.", "", raw, flags=re.IGNORECASE)
    raw = raw.split("#", 1)[0]
    if "?" in raw:
        base, query = raw.split("?", 1)
        kept = [
            kv
            for kv in query.split("&")
            if kv
            and not re.match(
                r"^(utm_|gclid|fbclid|mc_cid|mc_eid|ref|ref_src|cmpid|cmp|igshid)",
                kv,
                flags=re.IGNORECASE,
            )
        ]
        raw = base + ("?" + "&".join(kept) if kept else "")
    return raw.rstrip("/").lower()


def dedup_story_drafts_within_topic(
    story_drafts: list[dict],
    *,
    threshold: float = 0.85,
) -> list[dict]:
    """Remove near-duplicate story drafts within a single topic.

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
            "Story dedup: dropped %d near-duplicate story draft(s) within topic "
            "(threshold=%.2f).",
            len(dropped),
            threshold,
        )
    return kept
