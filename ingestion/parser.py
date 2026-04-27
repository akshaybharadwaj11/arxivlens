"""Parse PDFs into structured manifests with text, figures, tables, equations.

Uses Marker (https://github.com/VikParuchuri/marker) for primary parsing.
Designed to run as a Cloud Run job, pulling work from the parse Pub/Sub topic.

For local testing, you can run it directly:
  python -m ingestion.parser --arxiv-id 2403.12345

The Cloud Run entry point is `parse_subscriber()` which loops on Pub/Sub.
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

from google.cloud import pubsub_v1, storage

from arxivlens.config import settings
from arxivlens.logging import get_logger, setup_logging

setup_logging()
log = get_logger("parser")

# Lazy imports — Marker is heavy, only load it inside the worker
def _marker():
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict
    from marker.output import text_from_rendered
    return PdfConverter, create_model_dict, text_from_rendered


_models = None
_converter = None


def get_converter():
    """Lazy-init the Marker converter. Models load once per worker."""
    global _models, _converter
    if _converter is None:
        PdfConverter, create_model_dict, _ = _marker()
        _models = create_model_dict()
        _converter = PdfConverter(artifact_dict=_models)
    return _converter


def _slice_into_sections(markdown: str) -> list[dict]:
    """Split markdown into sections by ## headings."""
    sections: list[dict] = []
    current: dict[str, Any] = {"heading": "Preamble", "text": "", "char_offset": 0}
    offset = 0
    for line in markdown.split("\n"):
        if line.startswith("## "):
            if current["text"].strip():
                sections.append(current)
            current = {"heading": line[3:].strip(), "text": "", "char_offset": offset}
        else:
            current["text"] += line + "\n"
        offset += len(line) + 1
    if current["text"].strip():
        sections.append(current)
    return sections


def _extract_tables(markdown: str) -> list[dict]:
    """Find Markdown tables in the document."""
    tables = []
    table_pattern = re.compile(
        r"(\|[^\n]+\|\n\|[\s\-:|]+\|\n(?:\|[^\n]+\|\n?)+)",
        re.MULTILINE,
    )
    for i, m in enumerate(table_pattern.finditer(markdown)):
        block = m.group(1)
        lines = [l for l in block.strip().split("\n") if l.strip()]
        if len(lines) < 3:
            continue
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        rows = []
        for row_line in lines[2:]:
            row = [c.strip() for c in row_line.strip("|").split("|")]
            rows.append(row)
        # Look for a caption above the table
        before = markdown[:m.start()].rstrip().split("\n")
        caption = before[-1] if before else ""
        tables.append({
            "id": f"tbl_{i+1}",
            "caption": caption[:500],
            "headers": headers,
            "rows": rows[:20],  # cap for safety
            "markdown": block,
        })
    return tables


def _extract_equations(markdown: str) -> list[dict]:
    """Pull display equations ($$...$$) out of the markdown."""
    eqs = []
    for i, m in enumerate(re.finditer(r"\$\$(.+?)\$\$", markdown, re.DOTALL)):
        eqs.append({"id": f"eq_{i+1}", "latex": m.group(1).strip()})
    return eqs


def parse_pdf_bytes(pdf_bytes: bytes, arxiv_id: str) -> dict:
    """Parse a single PDF and return the manifest dict."""
    # Write to temp file (Marker's API takes a path)
    tmp_path = Path("/tmp") / f"{arxiv_id}.pdf"
    tmp_path.write_bytes(pdf_bytes)

    converter = get_converter()
    rendered = converter(str(tmp_path))
    _, _, text_fn = _marker()
    markdown, _, images = text_fn(rendered)

    sections = _slice_into_sections(markdown)
    tables = _extract_tables(markdown)
    equations = _extract_equations(markdown)

    # Marker returns images keyed by filename. We persist them later.
    figures = []
    for idx, (fname, pil_img) in enumerate(images.items()):
        # Heuristic: caption is often the line right after the image marker
        figures.append({
            "id": f"fig_{idx+1}",
            "caption": f"Figure {idx+1}",  # refined post-extraction
            "context_text": "",  # filled by post-processor
            "_pil": pil_img,  # popped before serialization
            "_filename": fname,
        })

    manifest = {
        "arxiv_id": arxiv_id,
        "sections": sections,
        "tables": tables,
        "equations": equations,
        "figures_count": len(figures),
    }

    tmp_path.unlink(missing_ok=True)
    return manifest, figures


def parse_one(arxiv_id: str) -> bool:
    """Read PDF + metadata from Cloud Storage, parse, write parsed bucket."""
    cfg = settings()
    storage_client = storage.Client()
    raw = storage_client.bucket(cfg.raw_bucket)
    parsed = storage_client.bucket(cfg.parsed_bucket)

    # Locate the PDF — we don't know yyyy/mm, so list
    blobs = list(raw.list_blobs(prefix="pdfs/", match_glob=f"**/{arxiv_id}.pdf"))
    if not blobs:
        log.warning("pdf_not_found", arxiv_id=arxiv_id)
        return False
    pdf_blob = blobs[0]

    # Find metadata
    meta_path = pdf_blob.name.replace("pdfs/", "metadata/").replace(".pdf", ".json")
    meta_blob = raw.blob(meta_path)
    paper_meta = json.loads(meta_blob.download_as_text()) if meta_blob.exists() else {}

    pdf_bytes = pdf_blob.download_as_bytes()
    log.info("parsing", arxiv_id=arxiv_id, size_bytes=len(pdf_bytes))

    try:
        manifest, figures = parse_pdf_bytes(pdf_bytes, arxiv_id)
    except Exception as e:
        log.error("parse_failed", arxiv_id=arxiv_id, error=str(e))
        return False

    # Persist figures as PNGs
    fig_metadata = []
    for fig in figures:
        png_buf = io.BytesIO()
        fig["_pil"].save(png_buf, format="PNG")
        fig_path = f"figures/{arxiv_id}/{fig['id']}.png"
        parsed.blob(fig_path).upload_from_string(
            png_buf.getvalue(), content_type="image/png"
        )
        fig_metadata.append({
            "id": fig["id"],
            "caption": fig["caption"],
            "context_text": fig["context_text"],
            "image_uri": f"gs://{cfg.parsed_bucket}/{fig_path}",
        })

    manifest["figures"] = fig_metadata
    manifest.update({k: v for k, v in paper_meta.items() if k != "pdf_url"})

    manifest_path = f"manifest/{arxiv_id}.json"
    parsed.blob(manifest_path).upload_from_string(
        json.dumps(manifest, default=str, indent=2),
        content_type="application/json",
    )

    log.info(
        "parsed",
        arxiv_id=arxiv_id,
        sections=len(manifest["sections"]),
        figures=len(fig_metadata),
        tables=len(manifest["tables"]),
        equations=len(manifest["equations"]),
    )

    # Publish embed event
    publisher = pubsub_v1.PublisherClient()
    topic = publisher.topic_path(cfg.project_id, cfg.embed_topic)
    publisher.publish(topic, json.dumps({"arxiv_id": arxiv_id}).encode())

    return True


def parse_subscriber() -> None:
    """Run as a Cloud Run job: pull from Pub/Sub and process."""
    cfg = settings()
    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(cfg.project_id, f"{cfg.parse_topic}-sub")

    def callback(message: pubsub_v1.subscriber.message.Message) -> None:
        try:
            payload = json.loads(message.data.decode())
            ok = parse_one(payload["arxiv_id"])
            if ok:
                message.ack()
            else:
                message.nack()
        except Exception as e:
            log.error("subscriber_error", error=str(e))
            message.nack()

    streaming = subscriber.subscribe(sub_path, callback=callback)
    log.info("subscriber_started", subscription=sub_path)
    try:
        streaming.result(timeout=None)
    except (KeyboardInterrupt, FuturesTimeoutError):
        streaming.cancel()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--arxiv-id", help="Parse a single paper (testing)")
    p.add_argument("--subscribe", action="store_true", help="Run as Pub/Sub subscriber")
    args = p.parse_args()

    if args.arxiv_id:
        ok = parse_one(args.arxiv_id)
        sys.exit(0 if ok else 1)
    elif args.subscribe:
        parse_subscriber()
    else:
        p.print_help()
        sys.exit(1)
