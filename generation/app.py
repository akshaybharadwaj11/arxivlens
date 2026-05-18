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
from arxivlens.langfuse_client import trace as lf_trace
from arxivlens.logging import get_logger, setup_logging
from arxivlens.metrics import record_chat, setup_metrics
from arxivlens.tracing import get_tracer, instrument_fastapi, setup_tracing
from generation.generator import generate_stream
from retrieval.hybrid import hybrid_search
from retrieval.reranker import rerank
from safety.input_guard import check_input
from safety.verifier import faithfulness_score, verify_answer

setup_logging()
log = get_logger("api")

app = FastAPI(title="ArXivLens", version="0.1.0")
setup_tracing("arxivlens-api")
setup_metrics("arxivlens-api")
instrument_fastapi(app)

tracer = get_tracer(__name__)

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

    with tracer.start_as_current_span("retrieve.request") as root:
        root.set_attribute("query", req.query[:200])
        root.set_attribute("top_k", req.top_k)

        with tracer.start_as_current_span("retrieve.hybrid") as s:
            candidates = hybrid_search(req.query, req.filters, top_k=30)
            s.set_attribute("n_candidates", len(candidates))

        with tracer.start_as_current_span("retrieve.rerank") as s:
            final = rerank(req.query, candidates, top_k=req.top_k)
            s.set_attribute("n_results", len(final))

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
        # Two parallel observability streams:
        #   - OTel/Cloud Trace: latency per operation
        #   - Langfuse: full LLM workflow (prompt + chunks + answer + scores)
        with (
            lf_trace("chat.request", query=req.query[:200], top_k=req.top_k) as lf,
            tracer.start_as_current_span("chat.request") as root,
        ):
            root.set_attribute("query", req.query[:200])
            root.set_attribute("top_k", req.top_k)
            root.set_attribute("query_id", query_id)

            # 1. Retrieve
            with tracer.start_as_current_span("retrieve.hybrid") as s:
                candidates = hybrid_search(req.query, req.filters, top_k=30)
                s.set_attribute("n_candidates", len(candidates))

            # 2. Rerank
            with tracer.start_as_current_span("retrieve.rerank") as s:
                top = rerank(req.query, candidates, top_k=req.top_k)
                s.set_attribute("n_results", len(top))

            # Log retrieval to Langfuse as a span
            lf.span(
                name="retrieve",
                input={"query": req.query, "filters": req.filters},
                output={
                    "chunks": [
                        {
                            "chunk_id": c.chunk_id,
                            "modality": c.modality,
                            "section": c.section,
                            "rrf_score": c.rrf_score,
                        }
                        for c in top
                    ],
                    "n_results": len(top),
                },
                metadata={"n_candidates": len(candidates)},
            )

            # Emit chunks first so the UI can render them while tokens stream
            yield _sse(
                "chunks",
                {
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
                },
            )

            # 3. Generate (one call; tokens streamed and collected)
            full_answer_parts: list[str] = []
            with tracer.start_as_current_span("generate.stream") as s:
                async for token in generate_stream(req.query, top):
                    full_answer_parts.append(token)
                    yield _sse("token", {"text": token})
                s.set_attribute("answer_chars", sum(len(t) for t in full_answer_parts))

            full_answer = "".join(full_answer_parts)

            # Log generation to Langfuse — this is where they shine. Full prompt
            # context, model, output, all clickable in their UI.
            lf.generation(
                name="gemini.generate",
                model="gemini-2.5-flash",
                input={
                    "query": req.query,
                    "n_chunks": len(top),
                    "chunk_ids": [c.chunk_id for c in top],
                },
                output=full_answer,
                metadata={"streaming": True},
            )

            # 4. Verify
            with tracer.start_as_current_span("verify.nli") as s:
                verifications = verify_answer(full_answer, top)
                faith = faithfulness_score(verifications)
                s.set_attribute("faithfulness", faith)
                s.set_attribute("n_sentences", len(verifications))
                s.set_attribute("n_supported", sum(1 for v in verifications if v.supported))

            # Langfuse: log faithfulness as a score (their first-class metric type)
            lf.score(
                name="faithfulness",
                value=faith,
                comment=f"{sum(1 for v in verifications if v.supported)}/{len(verifications)} sentences supported",
            )
            lf.span(
                name="verify",
                input={"answer": full_answer},
                output={
                    "faithfulness": faith,
                    "sentences": [
                        {
                            "sentence": v.sentence[:200],
                            "supported": v.supported,
                            "score": v.entailment_score,
                        }
                        for v in verifications
                    ],
                },
            )

            yield _sse(
                "verification",
                {
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
                },
            )

            # 5. Log to Postgres (best-effort)
            latency = int((time.perf_counter() - started) * 1000)
            with tracer.start_as_current_span("log.query"):
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

            root.set_attribute("latency_ms", latency)
            root.set_attribute("faithfulness", faith)

            record_chat(
                faithfulness=faith,
                latency_ms=latency,
                n_cited=sum(1 for v in verifications if v.citation_chunk_ids),
                n_supported=sum(1 for v in verifications if v.supported),
                status="ok",
            )
            yield _sse("done", {"query_id": query_id, "latency_ms": latency})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
