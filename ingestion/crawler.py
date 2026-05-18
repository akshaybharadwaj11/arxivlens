"""Pull paper metadata + PDFs from ArXiv and stage them in Cloud Storage.

Usage:
  python -m ingestion.crawler --max-papers 100 --categories cs.CL,cs.LG

For a smoke test, run with --max-papers 100 (~5 min).
For the real ingest, scale to 5000 papers (~2 hours).
"""

from __future__ import annotations

import argparse
import json
import time

import feedparser
import httpx
from google.cloud import pubsub_v1, storage

from arxivlens.config import settings
from arxivlens.logging import get_logger, setup_logging

setup_logging()
log = get_logger("crawler")

ARXIV_API = "https://export.arxiv.org/api/query"


def fetch_arxiv_batch(
    categories: list[str],
    start: int,
    batch_size: int = 100,
) -> list[dict]:
    """Query the ArXiv Atom API for a batch of papers."""
    cat_query = " OR ".join(f"cat:{c}" for c in categories)
    params = {
        "search_query": cat_query,
        "start": start,
        "max_results": batch_size,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    r = httpx.get(ARXIV_API, params=params, timeout=30.0, follow_redirects=True)
    r.raise_for_status()
    feed = feedparser.parse(r.text)
    papers = []
    for entry in feed.entries:
        # ArXiv ID is the last segment of the URL: http://arxiv.org/abs/2403.12345v1
        arxiv_id = entry.id.split("/abs/")[-1].split("v")[0]
        pdf_url = next(
            (link.href for link in entry.links if link.get("type") == "application/pdf"),
            None,
        )
        if not pdf_url:
            continue
        papers.append(
            {
                "arxiv_id": arxiv_id,
                "title": entry.title.strip().replace("\n", " "),
                "authors": [a.name for a in entry.authors],
                "primary_category": entry.tags[0]["term"] if entry.tags else "unknown",
                "published": entry.published[:10],
                "abstract": entry.summary.strip().replace("\n", " "),
                "pdf_url": pdf_url,
            }
        )
    return papers


def download_pdf(pdf_url: str) -> bytes:
    """Download PDF content. ArXiv asks for a 3-second delay between requests."""
    r = httpx.get(pdf_url, timeout=60.0, follow_redirects=True)
    r.raise_for_status()
    return r.content


def upload_paper(
    storage_client: storage.Client,
    paper: dict,
    pdf_bytes: bytes,
) -> tuple[str, str]:
    bucket = storage_client.bucket(settings().raw_bucket)

    pub = paper["published"]  # YYYY-MM-DD
    yyyy, mm = pub[:4], pub[5:7]

    pdf_path = f"pdfs/{yyyy}/{mm}/{paper['arxiv_id']}.pdf"
    meta_path = f"metadata/{yyyy}/{mm}/{paper['arxiv_id']}.json"

    bucket.blob(pdf_path).upload_from_string(pdf_bytes, content_type="application/pdf")
    bucket.blob(meta_path).upload_from_string(
        json.dumps(paper, indent=2),
        content_type="application/json",
    )
    return pdf_path, meta_path


def publish_parse_event(publisher: pubsub_v1.PublisherClient, arxiv_id: str) -> None:
    topic = publisher.topic_path(settings().project_id, settings().parse_topic)
    publisher.publish(topic, json.dumps({"arxiv_id": arxiv_id}).encode())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--max-papers", type=int, default=100)
    p.add_argument("--categories", default="cs.CL,cs.LG,cs.CV")
    p.add_argument("--batch-size", type=int, default=100)
    p.add_argument("--start", type=int, default=0)
    args = p.parse_args()

    cats = args.categories.split(",")
    storage_client = storage.Client()
    publisher = pubsub_v1.PublisherClient()

    fetched = 0
    cursor = args.start
    while fetched < args.max_papers:
        batch_size = min(args.batch_size, args.max_papers - fetched)
        log.info("fetching_batch", cursor=cursor, batch_size=batch_size)
        papers = fetch_arxiv_batch(cats, cursor, batch_size)
        if not papers:
            log.info("no_more_papers")
            break

        for paper in papers:
            try:
                pdf = download_pdf(paper["pdf_url"])
                upload_paper(storage_client, paper, pdf)
                publish_parse_event(publisher, paper["arxiv_id"])
                fetched += 1
                log.info("uploaded", arxiv_id=paper["arxiv_id"], total=fetched)
                time.sleep(3.0)  # ArXiv rate-limit etiquette
            except Exception as e:
                log.warning("paper_failed", arxiv_id=paper.get("arxiv_id"), error=str(e))

        cursor += batch_size

    log.info("crawl_complete", total=fetched)


if __name__ == "__main__":
    main()
