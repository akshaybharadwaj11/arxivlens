"""Memory-safe Marker parser for ArXivLens.

Design choices to keep memory bounded:
1. CPU-only (TORCH_DEVICE=cpu set in Dockerfile)
2. One paper per Cloud Run execution — no long-lived subscriber state
3. Explicit gc.collect() and model unload after each parse
4. Cap PDFs at 50 pages (research papers >50 pages are usually surveys w/o new figs)
5. Lazy import of heavy modules — fail fast if Marker is the issue

Run modes:
  python -m ingestion.parser --arxiv-id 2410.12345     # parse one
  python -m ingestion.parser --pull --max-papers 5     # pull N from Pub/Sub then exit
  python -m ingestion.parser --subscribe               # legacy streaming (don't use)
"""

from __future__ import annotations

import argparse
import gc
import io
import json
import os
import re
import sys
from typing import Any

from google.cloud import pubsub_v1, storage

from arxivlens.config import settings
from arxivlens.logging import get_logger, setup_logging

setup_logging()
log = get_logger("parser")

MAX_PAGES = int(os.environ.get("MAX_PDF_PAGES", "50"))


# -----------------------------------------------------------------------------
# Marker — lazy-loaded, explicitly torn down
# -----------------------------------------------------------------------------
def _load_marker_converter():
    """Build a fresh PdfConverter. Caller is responsible for releasing it."""
    from marker.converters.pdf import PdfConverter
    from marker.models import create_model_dict

    log.info("loading_marker_models")
    models = create_model_dict()
    converter = PdfConverter(artifact_dict=models)
    log.info("marker_loaded")
    return converter, models


def _release(converter, models) -> None:
    """Aggressively free Marker's memory."""
    try:
        del converter
        if models:
            for k in list(models.keys()):
                del models[k]
            del models
    except Exception:
        pass
    gc.collect()
    try:
        import torch

        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


# -----------------------------------------------------------------------------
# PDF preprocessing — truncate huge PDFs
# -----------------------------------------------------------------------------
def _truncate_pdf(pdf_bytes: bytes, max_pages: int = MAX_PAGES) -> bytes:
    """Return PDF bytes truncated to first `max_pages` pages."""
    try:
        import pypdf  # tiny dep; if not present, skip truncation
    except ImportError:
        return pdf_bytes

    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    if len(reader.pages) <= max_pages:
        return pdf_bytes

    log.info("truncating_pdf", original_pages=len(reader.pages), keeping=max_pages)

    writer = pypdf.PdfWriter()
    for page in reader.pages[:max_pages]:
        writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# -----------------------------------------------------------------------------
# Marker output post-processing
# -----------------------------------------------------------------------------
def _slice_into_sections(markdown: str) -> list[dict]:
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
    tables = []
    table_pattern = re.compile(
        r"(\|[^\n]+\|\n\|[\s\-:|]+\|\n(?:\|[^\n]+\|\n?)+)",
        re.MULTILINE,
    )
    for i, m in enumerate(table_pattern.finditer(markdown)):
        block = m.group(1)
        lines = [line for line in block.strip().split("\n") if line.strip()]
        if len(lines) < 3:
            continue
        headers = [h.strip() for h in lines[0].strip("|").split("|")]
        rows = []
        for row_line in lines[2:]:
            row = [c.strip() for c in row_line.strip("|").split("|")]
            rows.append(row)
        before = markdown[: m.start()].rstrip().split("\n")
        caption = before[-1] if before else ""
        tables.append(
            {
                "id": f"tbl_{i + 1}",
                "caption": caption[:500],
                "headers": headers,
                "rows": rows[:20],
                "markdown": block,
            }
        )
    return tables


def _extract_equations(markdown: str) -> list[dict]:
    eqs = []
    for i, m in enumerate(re.finditer(r"\$\$(.+?)\$\$", markdown, re.DOTALL)):
        eqs.append({"id": f"eq_{i + 1}", "latex": m.group(1).strip()})
    return eqs


# -----------------------------------------------------------------------------
# Main per-paper parsing
# -----------------------------------------------------------------------------
def _parse_pdf_with_marker(pdf_bytes: bytes, arxiv_id: str, parsed_bucket) -> dict:
    """Parse one PDF, write figures to GCS, return manifest dict (without metadata)."""
    from marker.output import text_from_rendered

    converter, models = _load_marker_converter()
    try:
        # Marker reads from a path; write to /tmp
        tmp_path = f"/tmp/{arxiv_id}.pdf"
        with open(tmp_path, "wb") as f:
            f.write(pdf_bytes)

        rendered = converter(tmp_path)
        markdown, _, images = text_from_rendered(rendered)

        sections = _slice_into_sections(markdown)
        tables = _extract_tables(markdown)
        equations = _extract_equations(markdown)

        # Persist figures
        figures = []
        for idx, (_fname, pil_img) in enumerate(images.items(), start=1):
            png_buf = io.BytesIO()
            pil_img.save(png_buf, format="PNG", optimize=True)
            fig_path = f"figures/{arxiv_id}/fig_{idx}.png"
            parsed_bucket.blob(fig_path).upload_from_string(
                png_buf.getvalue(), content_type="image/png"
            )
            figures.append(
                {
                    "id": f"fig_{idx}",
                    "caption": f"Figure {idx}",
                    "context_text": "",
                    "image_uri": f"gs://{parsed_bucket.name}/{fig_path}",
                }
            )
            png_buf.close()
            pil_img.close()

        # Cleanup tmp
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

        return {
            "sections": sections,
            "tables": tables,
            "equations": equations,
            "figures": figures,
            "figures_count": len(figures),
        }
    finally:
        _release(converter, models)


def parse_one(arxiv_id: str) -> bool:
    cfg = settings()
    storage_client = storage.Client()
    raw = storage_client.bucket(cfg.raw_bucket)
    parsed = storage_client.bucket(cfg.parsed_bucket)

    blobs = list(raw.list_blobs(prefix="pdfs/", match_glob=f"**/{arxiv_id}.pdf"))
    if not blobs:
        log.warning("pdf_not_found", arxiv_id=arxiv_id)
        return False
    pdf_blob = blobs[0]

    meta_path = pdf_blob.name.replace("pdfs/", "metadata/").replace(".pdf", ".json")
    meta_blob = raw.blob(meta_path)
    paper_meta = json.loads(meta_blob.download_as_text()) if meta_blob.exists() else {}

    pdf_bytes = pdf_blob.download_as_bytes()
    log.info("parsing", arxiv_id=arxiv_id, size_bytes=len(pdf_bytes))

    try:
        pdf_bytes = _truncate_pdf(pdf_bytes)
        manifest_core = _parse_pdf_with_marker(pdf_bytes, arxiv_id, parsed)
    except Exception as e:
        log.error("parse_failed", arxiv_id=arxiv_id, error=str(e), error_type=type(e).__name__)
        return False
    finally:
        # Release the PDF bytes promptly
        del pdf_bytes
        gc.collect()

    manifest = {
        "arxiv_id": arxiv_id,
        **manifest_core,
        **{k: v for k, v in paper_meta.items() if k != "pdf_url"},
    }

    parsed.blob(f"manifest/{arxiv_id}.json").upload_from_string(
        json.dumps(manifest, default=str, indent=2),
        content_type="application/json",
    )

    log.info(
        "parsed",
        arxiv_id=arxiv_id,
        sections=len(manifest_core["sections"]),
        figures=len(manifest_core["figures"]),
        tables=len(manifest_core["tables"]),
        equations=len(manifest_core["equations"]),
    )

    publisher = pubsub_v1.PublisherClient()
    topic = publisher.topic_path(cfg.project_id, cfg.embed_topic)
    publisher.publish(topic, json.dumps({"arxiv_id": arxiv_id}).encode())

    return True


# -----------------------------------------------------------------------------
# Pull-mode runner — fetch N messages, process, exit
# -----------------------------------------------------------------------------
def pull_and_process(max_papers: int = 5, subscription: str = "arxivlens-dev-parse-sub") -> int:
    """Synchronous pull. Process up to `max_papers`, then exit cleanly."""
    cfg = settings()
    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(cfg.project_id, subscription)

    processed = 0
    failed = 0

    while processed + failed < max_papers:
        log.info("pulling", remaining=max_papers - processed - failed)
        response = subscriber.pull(
            request={"subscription": sub_path, "max_messages": 1},
            timeout=30.0,
        )
        if not response.received_messages:
            log.info("no_messages_available")
            break

        msg = response.received_messages[0]
        try:
            payload = json.loads(msg.message.data.decode())
            arxiv_id = payload["arxiv_id"]
        except Exception as e:
            log.error("bad_message", error=str(e))
            subscriber.acknowledge(request={"subscription": sub_path, "ack_ids": [msg.ack_id]})
            failed += 1
            continue

        ok = parse_one(arxiv_id)
        if ok:
            subscriber.acknowledge(request={"subscription": sub_path, "ack_ids": [msg.ack_id]})
            processed += 1
        else:
            # Don't ack — let Pub/Sub redeliver
            failed += 1

        # Aggressive cleanup between papers
        gc.collect()

    subscriber.close()
    log.info("pull_complete", processed=processed, failed=failed)
    return processed


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--arxiv-id")
    p.add_argument(
        "--pull",
        action="store_true",
        help="Pull N messages and exit (recommended for Cloud Run jobs)",
    )
    p.add_argument("--max-papers", type=int, default=5, help="Max papers to process in --pull mode")
    p.add_argument(
        "--subscribe", action="store_true", help="Legacy streaming mode (don't use on Cloud Run)"
    )
    args = p.parse_args()

    if args.arxiv_id:
        sys.exit(0 if parse_one(args.arxiv_id) else 1)
    elif args.pull:
        n = pull_and_process(max_papers=args.max_papers)
        sys.exit(0 if n > 0 else 1)
    elif args.subscribe:
        log.warning("streaming_mode_deprecated_use_pull")
        # legacy code path retained for reference; not exposed by Dockerfile CMD
        sys.exit(2)
    else:
        p.print_help()
        sys.exit(1)
