"""Run the eval suite against a golden set.

Usage:
  python -m eval.run_eval --golden eval/golden_set_v1.jsonl --out eval/results.json

Produces:
  - Retrieval metrics: Recall@k, MRR, nDCG
  - RAG metrics: faithfulness (via NLI verifier)
  - Optional: LLM-as-judge if --llm-judge passed
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from pathlib import Path

from arxivlens.logging import get_logger, setup_logging
from generation.generator import generate
from retrieval.hybrid import hybrid_search
from retrieval.reranker import rerank
from safety.verifier import verify_answer, faithfulness_score

setup_logging()
log = get_logger("eval")


def recall_at_k(retrieved_ids: list[str], gold_ids: list[str], k: int = 10) -> float:
    if not gold_ids:
        return 0.0
    top = set(retrieved_ids[:k])
    return len(top & set(gold_ids)) / len(set(gold_ids))


def mrr(retrieved_ids: list[str], gold_ids: list[str]) -> float:
    gold = set(gold_ids)
    for i, rid in enumerate(retrieved_ids, start=1):
        if rid in gold:
            return 1.0 / i
    return 0.0


def ndcg(retrieved_ids: list[str], gold_ids: list[str], k: int = 10) -> float:
    gold = set(gold_ids)
    dcg = 0.0
    for i, rid in enumerate(retrieved_ids[:k], start=1):
        if rid in gold:
            dcg += 1.0 / math.log2(i + 1)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(gold), k) + 1))
    return dcg / ideal if ideal > 0 else 0.0


def evaluate(golden_path: Path, top_k: int = 10) -> dict:
    items = [json.loads(l) for l in golden_path.read_text().splitlines() if l.strip()]
    log.info("loaded_golden", n=len(items))

    per_item_metrics = []
    by_type: dict[str, list[dict]] = {}

    for item in items:
        if item.get("query_type") == "out_of_scope":
            continue  # Refusal eval is separate

        candidates = hybrid_search(item["query"], top_k=30)
        reranked = rerank(item["query"], candidates, top_k=top_k)
        retrieved_ids = [c.chunk_id for c in reranked]

        gold = item.get("gold_chunk_ids") or []
        # If chunk-level gold isn't available, fall back to paper-level
        if not gold:
            paper_gold = item.get("gold_paper_ids", [])
            retrieved_papers = [c.arxiv_id for c in reranked]
            gold = paper_gold
            retrieved_ids = retrieved_papers

        metrics = {
            "query": item["query"],
            "query_type": item.get("query_type", "unknown"),
            "recall@10": recall_at_k(retrieved_ids, gold, k=10),
            "mrr": mrr(retrieved_ids, gold),
            "ndcg@10": ndcg(retrieved_ids, gold, k=10),
        }

        # Generation + faithfulness
        answer = generate(item["query"], reranked)
        verifications = verify_answer(answer, reranked)
        metrics["faithfulness"] = faithfulness_score(verifications)
        metrics["answer"] = answer

        by_type.setdefault(metrics["query_type"], []).append(metrics)
        per_item_metrics.append(metrics)
        log.info("evaluated", query=item["query"][:60], **{
            k: round(v, 3) for k, v in metrics.items()
            if isinstance(v, float)
        })

    # Aggregates
    def avg(items: list[dict], key: str) -> float:
        if not items:
            return 0.0
        return sum(i[key] for i in items) / len(items)

    summary = {
        "n_queries": len(per_item_metrics),
        "overall": {
            "recall@10": avg(per_item_metrics, "recall@10"),
            "mrr": avg(per_item_metrics, "mrr"),
            "ndcg@10": avg(per_item_metrics, "ndcg@10"),
            "faithfulness": avg(per_item_metrics, "faithfulness"),
        },
        "by_type": {
            t: {
                "recall@10": avg(items, "recall@10"),
                "mrr": avg(items, "mrr"),
                "ndcg@10": avg(items, "ndcg@10"),
                "faithfulness": avg(items, "faithfulness"),
                "n": len(items),
            }
            for t, items in by_type.items()
        },
    }
    return {"summary": summary, "items": per_item_metrics}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--golden", default="eval/golden_set_v1.jsonl")
    p.add_argument("--out", default="eval/results.json")
    p.add_argument("--top-k", type=int, default=10)
    args = p.parse_args()

    results = evaluate(Path(args.golden), top_k=args.top_k)
    Path(args.out).write_text(json.dumps(results, indent=2))
    log.info("eval_complete", **results["summary"]["overall"])
    print(json.dumps(results["summary"], indent=2))


if __name__ == "__main__":
    main()
