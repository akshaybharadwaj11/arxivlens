"""Cross-encoder reranker using BGE-reranker-v2-m3.

Loads on first use. Runs on CPU. ~250ms p95 for 30 pairs.
"""
from __future__ import annotations

from collections.abc import Sequence

from sentence_transformers import CrossEncoder

from arxivlens.config import settings
from arxivlens.logging import get_logger
from retrieval.hybrid import RetrievedChunk

log = get_logger("reranker")

_model: CrossEncoder | None = None


def get_reranker() -> CrossEncoder:
    global _model
    if _model is None:
        cfg = settings()
        log.info("loading_reranker", model=cfg.reranker_model)
        _model = CrossEncoder(cfg.reranker_model, max_length=512)
    return _model


def rerank(
    query: str,
    candidates: Sequence[RetrievedChunk],
    top_k: int = 5,
) -> list[RetrievedChunk]:
    if not candidates:
        return []
    model = get_reranker()
    pairs = [(query, c.content[:2000]) for c in candidates]
    scores = model.predict(pairs)
    order = sorted(range(len(candidates)), key=lambda i: scores[i], reverse=True)
    reranked = [candidates[i] for i in order[:top_k]]
    log.info("reranked", n_in=len(candidates), n_out=len(reranked))
    return reranked
