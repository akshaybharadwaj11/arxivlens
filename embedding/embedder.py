"""Chunk parsed manifests and embed them into Postgres (with pgvector).

Run modes:
  Local one-off:   python -m embedding.embedder --arxiv-id 2403.12345
  Subscriber:      python -m embedding.embedder --subscribe
  Backfill all:    python -m embedding.embedder --backfill
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Iterator

from google.cloud import aiplatform, pubsub_v1, storage
from vertexai.language_models import TextEmbeddingInput, TextEmbeddingModel

from arxivlens.config import settings
from arxivlens.db import conn
from arxivlens.logging import get_logger, setup_logging

setup_logging()
log = get_logger("embedder")

# Embedding model (loaded once)
_model: TextEmbeddingModel | None = None


def get_model() -> TextEmbeddingModel:
    global _model
    if _model is None:
        cfg = settings()
        aiplatform.init(project=cfg.project_id, location=cfg.region)
        _model = TextEmbeddingModel.from_pretrained(cfg.embedding_model)
    return _model


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Embed up to 250 texts at a time (Vertex AI batch limit)."""
    model = get_model()
    inputs = [TextEmbeddingInput(text=t, task_type="RETRIEVAL_DOCUMENT") for t in texts]
    embeddings = model.get_embeddings(inputs)
    return [e.values for e in embeddings]


def chunk_text(text: str, max_tokens: int = 500, overlap_tokens: int = 50) -> list[str]:
    """Naive whitespace chunker. ~4 chars/token, sentence boundaries when possible."""
    max_chars = max_tokens * 4
    overlap_chars = overlap_tokens * 4

    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end >= len(text):
            chunks.append(text[start:])
            break
        # Try to break on sentence boundary
        boundary = text.rfind(". ", start, end)
        if boundary == -1 or boundary < start + max_chars // 2:
            boundary = end
        chunks.append(text[start : boundary + 1])
        start = boundary + 1 - overlap_chars
    return chunks


def manifest_to_chunks(manifest: dict) -> Iterator[dict]:
    """Yield chunk dicts from a parsed manifest."""
    arxiv_id = manifest["arxiv_id"]
    base_meta = {
        "year": int(manifest.get("published", "1970-01-01")[:4]),
        "category": manifest.get("primary_category"),
        "authors": manifest.get("authors", [])[:5],
    }

    # 1. Text chunks (one per section, split if too long)
    for s_idx, section in enumerate(manifest.get("sections", [])):
        text = section.get("text", "").strip()
        if len(text) < 50:
            continue
        for c_idx, piece in enumerate(chunk_text(text)):
            yield {
                "chunk_id": f"{arxiv_id}:{s_idx}:{c_idx}",
                "arxiv_id": arxiv_id,
                "modality": "text",
                "section": section.get("heading"),
                "content": piece,
                "metadata": base_meta,
                "image_uri": None,
                "table_headers": None,
                "table_first_rows": None,
            }

    # 2. Figure chunks
    for fig in manifest.get("figures", []):
        content = "\n".join(filter(None, [fig.get("caption"), fig.get("context_text")]))
        if not content:
            continue
        yield {
            "chunk_id": f"{arxiv_id}:fig:{fig['id'].split('_')[1]}",
            "arxiv_id": arxiv_id,
            "modality": "figure",
            "section": None,
            "content": content,
            "metadata": base_meta,
            "image_uri": fig.get("image_uri"),
            "table_headers": None,
            "table_first_rows": None,
        }

    # 3. Table chunks
    for tbl in manifest.get("tables", []):
        content = (tbl.get("caption", "") + "\n" + tbl.get("markdown", "")).strip()
        yield {
            "chunk_id": f"{arxiv_id}:tbl:{tbl['id'].split('_')[1]}",
            "arxiv_id": arxiv_id,
            "modality": "table",
            "section": None,
            "content": content,
            "metadata": base_meta,
            "image_uri": None,
            "table_headers": tbl.get("headers"),
            "table_first_rows": json.dumps(tbl.get("rows", [])[:5]),
        }


def upsert_paper(manifest: dict) -> None:
    with conn() as c, c.cursor() as cur:
        cur.execute(
            """
            INSERT INTO papers (arxiv_id, title, authors, primary_category,
                                published, abstract, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (arxiv_id) DO UPDATE SET
              title = EXCLUDED.title,
              metadata = EXCLUDED.metadata
            """,
            (
                manifest["arxiv_id"],
                manifest.get("title", ""),
                manifest.get("authors", []),
                manifest.get("primary_category"),
                manifest.get("published"),
                manifest.get("abstract"),
                json.dumps({"figures_count": manifest.get("figures_count", 0)}),
            ),
        )


def upsert_chunks(chunks: list[dict], embeddings: list[list[float]]) -> None:
    rows = []
    for ch, emb in zip(chunks, embeddings, strict=False):
        ch_hash = hashlib.sha256(ch["content"].encode()).hexdigest()
        rows.append(
            (
                ch["chunk_id"],
                ch["arxiv_id"],
                ch["modality"],
                ch["section"],
                ch["content"],
                emb,
                json.dumps(ch["metadata"]),
                ch_hash,
                ch["image_uri"],
                ch["table_headers"],
                ch["table_first_rows"],
            )
        )

    with conn() as c, c.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO chunks (chunk_id, arxiv_id, modality, section, content,
                                embedding, metadata, content_hash, image_uri,
                                table_headers, table_first_rows)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (chunk_id) DO UPDATE SET
              content = EXCLUDED.content,
              embedding = EXCLUDED.embedding,
              content_hash = EXCLUDED.content_hash
            WHERE chunks.content_hash != EXCLUDED.content_hash
            """,
            rows,
        )


def embed_one(arxiv_id: str) -> bool:
    cfg = settings()
    storage_client = storage.Client()
    bucket = storage_client.bucket(cfg.parsed_bucket)
    blob = bucket.blob(f"manifest/{arxiv_id}.json")
    if not blob.exists():
        log.warning("manifest_not_found", arxiv_id=arxiv_id)
        return False

    manifest = json.loads(blob.download_as_text())
    chunks = list(manifest_to_chunks(manifest))
    if not chunks:
        log.warning("no_chunks", arxiv_id=arxiv_id)
        return True

    upsert_paper(manifest)

    # Embed in batches of 250
    BATCH = 5
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i : i + BATCH]
        texts = [c["content"][:7000] for c in batch]  # 8k char limit
        embeddings = embed_batch(texts)
        upsert_chunks(batch, embeddings)
        log.info("embedded_batch", arxiv_id=arxiv_id, n=len(batch))

    log.info("embedded_paper", arxiv_id=arxiv_id, total_chunks=len(chunks))
    return True


def subscribe() -> None:
    cfg = settings()
    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(cfg.project_id, "arxivlens-dev-embed-sub")

    def callback(message):
        try:
            payload = json.loads(message.data.decode())
            ok = embed_one(payload["arxiv_id"])
            message.ack() if ok else message.nack()
        except Exception as e:
            log.error("embed_subscriber_error", error=str(e))
            message.nack()

    streaming = subscriber.subscribe(sub_path, callback=callback)
    log.info("embed_subscriber_started")
    try:
        streaming.result(timeout=None)
    except KeyboardInterrupt:
        streaming.cancel()


def backfill() -> None:
    """List all manifests and embed any missing chunks."""
    cfg = settings()
    storage_client = storage.Client()
    bucket = storage_client.bucket(cfg.parsed_bucket)

    for blob in bucket.list_blobs(prefix="manifest/"):
        arxiv_id = blob.name.split("/")[-1].replace(".json", "")
        with conn() as c, c.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM chunks WHERE arxiv_id = %s LIMIT 1",
                (arxiv_id,),
            )
            if cur.fetchone():
                continue
        log.info("backfilling", arxiv_id=arxiv_id)
        embed_one(arxiv_id)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--arxiv-id")
    p.add_argument("--subscribe", action="store_true")
    p.add_argument("--backfill", action="store_true")
    args = p.parse_args()

    if args.arxiv_id:
        sys.exit(0 if embed_one(args.arxiv_id) else 1)
    elif args.subscribe:
        subscribe()
    elif args.backfill:
        backfill()
    else:
        p.print_help()
