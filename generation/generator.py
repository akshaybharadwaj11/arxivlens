"""Generation using Gemini 2.5 Flash via Vertex AI. Streaming-first.

Authenticated via Application Default Credentials (ADC) — no API key needed.
On Cloud Run / Compute Engine, ADC comes from the attached service account.
Locally, ADC comes from `gcloud auth application-default login`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import vertexai
from vertexai.generative_models import GenerationConfig, GenerativeModel

from arxivlens.config import settings
from arxivlens.logging import get_logger
from retrieval.hybrid import RetrievedChunk

log = get_logger("generator")

SYSTEM_PROMPT = """You are a research assistant for ML literature.

CRITICAL RULES:
1. Answer using ONLY the provided context. Do not use prior knowledge.
2. Every claim MUST end with a citation tag using the exact chunk_id from the context. Format: [chunk_id_exactly_as_shown]
3. ONLY cite chunk_ids that appear in the context — never invent or modify them.
4. If the context does not support an answer, say so explicitly. Do not pad with prior knowledge.
5. Be concise. Three sentences maximum unless asked for more.

Example: "The model achieves 92% accuracy [2410.12345:4:2]." — uses the exact chunk_id from context."""


def _format_context(chunks: Sequence[RetrievedChunk]) -> str:
    blocks = []
    for c in chunks:
        header = f"[{c.modality} — {c.arxiv_id} — chunk_id={c.chunk_id}"
        if c.section:
            header += f" — section={c.section}"
        header += "]"
        blocks.append(f"{header}\n{c.content}")
    return "\n\n".join(blocks)


_model: GenerativeModel | None = None


def _get_model() -> GenerativeModel:
    global _model
    if _model is None:
        cfg = settings()
        vertexai.init(project=cfg.project_id, location=cfg.region)
        _model = GenerativeModel(
            cfg.generation_model,
            system_instruction=SYSTEM_PROMPT,
        )
    return _model


async def generate_stream(
    query: str,
    chunks: Sequence[RetrievedChunk],
) -> AsyncIterator[str]:
    """Stream the answer token-by-token."""
    model = _get_model()
    context = _format_context(chunks)
    user_msg = f"Question: {query}\n\nContext:\n{context}"

    log.info("generating", query=query[:80], n_chunks=len(chunks))

    stream = model.generate_content(
        user_msg,
        generation_config=GenerationConfig(
            temperature=0.2,
            max_output_tokens=1024,
        ),
        stream=True,
    )

    for chunk in stream:
        if chunk.text:
            yield chunk.text


def generate(query: str, chunks: Sequence[RetrievedChunk]) -> str:
    """Non-streaming variant — used by the eval runner."""
    model = _get_model()
    context = _format_context(chunks)
    user_msg = f"Question: {query}\n\nContext:\n{context}"

    resp = model.generate_content(
        user_msg,
        generation_config=GenerationConfig(
            temperature=0.2,
            max_output_tokens=1024,
        ),
    )
    return resp.text or ""
