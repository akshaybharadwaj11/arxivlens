"""Generation using Gemini 2.5 Flash. Streaming-first."""
from __future__ import annotations

import os
from typing import AsyncIterator, Sequence

from google import genai
from google.genai import types

from arxivlens.config import settings
from arxivlens.logging import get_logger
from retrieval.hybrid import RetrievedChunk

log = get_logger("generator")

SYSTEM_PROMPT = """You are a research assistant for ML literature. Answer the user's
question using ONLY the provided context. Every claim must end with a citation tag of
the form [arxiv_id, chunk_id]. If the context does not support a clear answer, say so
explicitly. Do not use prior knowledge. Be concise."""


def _format_context(chunks: Sequence[RetrievedChunk]) -> str:
    blocks = []
    for c in chunks:
        header = f"[{c.modality} — {c.arxiv_id} — chunk_id={c.chunk_id}"
        if c.section:
            header += f" — section={c.section}"
        header += "]"
        blocks.append(f"{header}\n{c.content}")
    return "\n\n".join(blocks)


def _client() -> genai.Client:
    cfg = settings()
    api_key = cfg.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    return genai.Client(api_key=api_key)


async def generate_stream(
    query: str,
    chunks: Sequence[RetrievedChunk],
) -> AsyncIterator[str]:
    """Stream the answer token-by-token."""
    client = _client()
    cfg = settings()

    context = _format_context(chunks)
    user_msg = f"Question: {query}\n\nContext:\n{context}"

    log.info("generating", query=query[:80], n_chunks=len(chunks))

    stream = client.models.generate_content_stream(
        model=cfg.generation_model,
        contents=[
            types.Content(role="user", parts=[types.Part(text=user_msg)]),
        ],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
            max_output_tokens=1024,
        ),
    )

    for ev in stream:
        if ev.text:
            yield ev.text


def generate(query: str, chunks: Sequence[RetrievedChunk]) -> str:
    """Non-streaming variant."""
    client = _client()
    cfg = settings()
    context = _format_context(chunks)
    user_msg = f"Question: {query}\n\nContext:\n{context}"

    resp = client.models.generate_content(
        model=cfg.generation_model,
        contents=[types.Content(role="user", parts=[types.Part(text=user_msg)])],
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=0.2,
            max_output_tokens=1024,
        ),
    )
    return resp.text or ""
