"""Citation verifier — for each cited claim, checks that the cited chunk entails it.

Uses an NLI cross-encoder. Model loads lazily.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

from sentence_transformers import CrossEncoder

from arxivlens.config import settings
from arxivlens.logging import get_logger
from retrieval.hybrid import RetrievedChunk

log = get_logger("verifier")

CITATION_RE = re.compile(r"\[([^,\]]+),\s*([^\]]+)\]")

_model: CrossEncoder | None = None


def get_nli() -> CrossEncoder:
    global _model
    if _model is None:
        cfg = settings()
        # nli-deberta-v3-base outputs (contradiction, entailment, neutral) logits
        _model = CrossEncoder(cfg.nli_model, max_length=512)
    return _model


@dataclass
class VerifiedSentence:
    sentence: str
    citation_chunk_ids: list[str]
    entailment_score: float
    supported: bool


def split_sentences(text: str) -> list[str]:
    # Lightweight splitter — good enough for English answers
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p]


def verify_answer(
    answer: str,
    chunks: Sequence[RetrievedChunk],
    threshold: float = 0.5,
) -> list[VerifiedSentence]:
    """For each sentence with a citation, check NLI entailment from the cited chunk."""
    chunk_lookup = {c.chunk_id: c for c in chunks}
    results: list[VerifiedSentence] = []
    nli = get_nli()

    for sent in split_sentences(answer):
        matches = CITATION_RE.findall(sent)
        cited_ids = [m[1].strip() for m in matches]
        if not cited_ids:
            results.append(VerifiedSentence(
                sentence=sent, citation_chunk_ids=[],
                entailment_score=0.0, supported=False,
            ))
            continue

        # Score against the union of cited chunks
        evidence = "\n".join(
            chunk_lookup[cid].content[:1500]
            for cid in cited_ids if cid in chunk_lookup
        )
        if not evidence:
            results.append(VerifiedSentence(
                sentence=sent, citation_chunk_ids=cited_ids,
                entailment_score=0.0, supported=False,
            ))
            continue

        # Cross-encoder NLI: returns logits for [contradiction, entailment, neutral]
        logits = nli.predict([(evidence, sent)])
        # Softmax-y normalization → entailment probability
        import numpy as np
        scores = np.exp(logits[0]) / np.exp(logits[0]).sum()
        entail = float(scores[1])

        results.append(VerifiedSentence(
            sentence=sent,
            citation_chunk_ids=cited_ids,
            entailment_score=entail,
            supported=entail >= threshold,
        ))

    log.info(
        "verified",
        n_sentences=len(results),
        n_supported=sum(1 for r in results if r.supported),
    )
    return results


def faithfulness_score(verifications: list[VerifiedSentence]) -> float:
    cited = [v for v in verifications if v.citation_chunk_ids]
    if not cited:
        return 0.0
    return sum(v.entailment_score for v in cited) / len(cited)
