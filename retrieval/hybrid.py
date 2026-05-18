"""Hybrid retrieval: dense (pgvector) + sparse (Postgres BM25) fused with RRF.

The whole hybrid search runs as a single Postgres query for v1 — pgvector + tsvector
in the same table makes this clean. Metadata pre-filter is a WHERE clause.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vertexai.language_models import TextEmbeddingInput

from arxivlens.config import settings
from arxivlens.db import conn
from arxivlens.logging import get_logger
from embedding.embedder import get_model

log = get_logger("retriever")


@dataclass
class RetrievedChunk:
    chunk_id: str
    arxiv_id: str
    modality: str
    section: str | None
    content: str
    image_uri: str | None
    rrf_score: float
    dense_rank: int | None
    sparse_rank: int | None
    metadata: dict[str, Any]


def embed_query(query: str) -> list[float]:
    model = get_model()
    inputs = [TextEmbeddingInput(text=query, task_type="RETRIEVAL_QUERY")]
    return model.get_embeddings(inputs)[0].values


def _build_filter_clause(filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
    """Build a WHERE fragment from filter dict. Returns (sql, params)."""
    if not filters:
        return "", []
    clauses = []
    params: list[Any] = []
    if "year" in filters:
        clauses.append("(metadata->>'year')::int = %s")
        params.append(int(filters["year"]))
    if "year_min" in filters:
        clauses.append("(metadata->>'year')::int >= %s")
        params.append(int(filters["year_min"]))
    if "category" in filters:
        clauses.append("metadata->>'category' = %s")
        params.append(filters["category"])
    if "modality" in filters:
        clauses.append("modality = %s")
        params.append(filters["modality"])
    if "arxiv_ids" in filters:
        clauses.append("arxiv_id = ANY(%s)")
        params.append(list(filters["arxiv_ids"]))
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def hybrid_search(
    query: str,
    filters: dict[str, Any] | None = None,
    top_k: int = 30,
    figure_boost: float = 0.0,
) -> list[RetrievedChunk]:
    """
    Hybrid search using RRF over dense + sparse rankings.

    Single SQL query for performance. The CTEs:
      - dense: cosine similarity ranking
      - sparse: BM25-style ts_rank ranking
      - fused: RRF combination
    """
    cfg = settings()
    embedding = embed_query(query)
    filter_sql, filter_params = _build_filter_clause(filters)

    sql = f"""
    WITH
    dense AS (
      SELECT chunk_id,
             ROW_NUMBER() OVER (ORDER BY embedding <=> %s::vector) AS rnk
      FROM chunks
      WHERE 1=1 {filter_sql}
      ORDER BY embedding <=> %s::vector
      LIMIT {cfg.top_k_dense}
    ),
    sparse AS (
      SELECT chunk_id,
             ROW_NUMBER() OVER (
               ORDER BY ts_rank(content_tsv, plainto_tsquery('english', %s)) DESC
             ) AS rnk
      FROM chunks
      WHERE content_tsv @@ plainto_tsquery('english', %s) {filter_sql}
      LIMIT {cfg.top_k_sparse}
    ),
    fused AS (
      SELECT COALESCE(d.chunk_id, s.chunk_id) AS chunk_id,
             d.rnk AS dense_rank,
             s.rnk AS sparse_rank,
             COALESCE(1.0 / ({cfg.rrf_k} + d.rnk), 0)
             + COALESCE(1.0 / ({cfg.rrf_k} + s.rnk), 0) AS rrf_score
      FROM dense d
      FULL OUTER JOIN sparse s USING (chunk_id)
    )
    SELECT
      c.chunk_id, c.arxiv_id, c.modality, c.section, c.content,
      c.image_uri, c.metadata,
      f.dense_rank, f.sparse_rank,
      f.rrf_score + CASE WHEN c.modality = 'figure' THEN %s ELSE 0 END AS final_score
    FROM fused f
    JOIN chunks c USING (chunk_id)
    ORDER BY final_score DESC
    LIMIT %s
    """

    # Param order matches placeholder order:
    #   dense embedding, dense filter, dense ORDER BY embedding,
    #   sparse query (×2), sparse filter,
    #   figure_boost, top_k
    params = [
        embedding,
        *filter_params,
        embedding,
        query,
        query,
        *filter_params,
        figure_boost,
        top_k,
    ]

    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    results = [
        RetrievedChunk(
            chunk_id=r[0],
            arxiv_id=r[1],
            modality=r[2],
            section=r[3],
            content=r[4],
            image_uri=r[5],
            metadata=r[6] or {},
            dense_rank=r[7],
            sparse_rank=r[8],
            rrf_score=float(r[9]),
        )
        for r in rows
    ]

    log.info(
        "hybrid_search",
        query=query[:80],
        n_results=len(results),
        filters=filters,
    )
    return results
