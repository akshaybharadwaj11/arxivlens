"""FastAPI app for ArXivLens. Runs on Cloud Run.

Endpoints:
  GET  /health         — liveness check
  POST /retrieve       — hybrid search only (debugging/eval)
  POST /chat           — full RAG: retrieve + rerank + generate + verify (streaming SSE)
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from arxivlens.db import conn
from arxivlens.logging import get_logger, setup_logging
from generation.generator import generate_stream
from retrieval.hybrid import hybrid_search
from retrieval.reranker import rerank
from safety.input_guard import check_input
from safety.verifier import faithfulness_score, verify_answer

setup_logging()
log = get_logger("api")

app = FastAPI(title="ArXivLens", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class RetrieveRequest(BaseModel):
    query: str
    filters: dict[str, Any] | None = None
    top_k: int = 5


class ChatRequest(BaseModel):
    query: str
    filters: dict[str, Any] | None = None
    top_k: int = 5


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/retrieve")
def retrieve_endpoint(req: RetrieveRequest) -> dict:
    guard = check_input(req.query)
    if not guard.ok:
        raise HTTPException(400, detail=guard.reason)

    candidates = hybrid_search(req.query, req.filters, top_k=30)
    final = rerank(req.query, candidates, top_k=req.top_k)

    return {
        "query": req.query,
        "results": [
            {
                "chunk_id": c.chunk_id,
                "arxiv_id": c.arxiv_id,
                "modality": c.modality,
                "section": c.section,
                "content": c.content[:500],
                "image_uri": c.image_uri,
                "rrf_score": c.rrf_score,
            }
            for c in final
        ],
    }


@app.post("/chat")
async def chat_endpoint(req: ChatRequest) -> StreamingResponse:
    """SSE-streaming endpoint. Events: chunks → tokens → verification → done."""
    guard = check_input(req.query)
    if not guard.ok:
        raise HTTPException(400, detail=guard.reason)

    query_id = str(uuid.uuid4())
    started = time.perf_counter()

    async def event_stream():
        # 1. Retrieve
        candidates = hybrid_search(req.query, req.filters, top_k=30)
        top = rerank(req.query, candidates, top_k=req.top_k)

        yield _sse("chunks", {
            "query_id": query_id,
            "chunks": [
                {
                    "chunk_id": c.chunk_id,
                    "arxiv_id": c.arxiv_id,
                    "modality": c.modality,
                    "section": c.section,
                    "content_preview": c.content[:300],
                    "image_uri": c.image_uri,
                }
                for c in top
            ],
        })

        # 2. Generate (stream)
        full_answer_parts: list[str] = []
        async for tok in generate_stream(req.query, top):
            full_answer_parts.append(tok)
            yield _sse("token", {"text": tok})

        full_answer = "".join(full_answer_parts)

        # 3. Verify
        verifications = verify_answer(full_answer, top)
        faith = faithfulness_score(verifications)

        yield _sse("verification", {
            "faithfulness": faith,
            "sentences": [
                {
                    "sentence": v.sentence,
                    "citation_chunk_ids": v.citation_chunk_ids,
                    "entailment_score": v.entailment_score,
                    "supported": v.supported,
                }
                for v in verifications
            ],
        })

        # 4. Log to Postgres
        latency = int((time.perf_counter() - started) * 1000)
        try:
            with conn() as c, c.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO queries
                      (query_id, query_text, filters, retrieved_ids, answer,
                       faithfulness, latency_ms, model)
                    VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        query_id,
                        req.query,
                        json.dumps(req.filters or {}),
                        [c.chunk_id for c in top],
                        full_answer,
                        faith,
                        latency,
                        "gemini-2.5-flash",
                    ),
                )
        except Exception as e:
            log.warning("query_log_failed", error=str(e))

        yield _sse("done", {"query_id": query_id, "latency_ms": latency})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
