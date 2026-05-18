"""Citation verifier — for each cited claim, checks that the cited chunk entails it.

Uses an NLI cross-encoder. Model loads lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sentence_transformers import CrossEncoder

import re
from collections.abc import Sequence
from dataclasses import dataclass

from arxivlens.config import settings
from arxivlens.logging import get_logger
from retrieval.hybrid import RetrievedChunk

log = get_logger("verifier")

# Two citation formats Gemini produces
CITATION_RE_COMMA = re.compile(r"\[([^,\]]+),\s*([^\]]+)\]")
CITATION_RE_BARE = re.compile(r"\d{4}\.\d{4,5}(?::[^,\]\s]+)?")

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


def _strip_math(s: str) -> str:
    """Remove LaTeX/code so NLI model is not confused by formulas."""
    # Remove citation tags like [2604.22709, 8:1] or [2604.22709:8:1]
    s = re.sub(r"\[\d{4}\.\d{4,5}[^\]]*\]", "", s)
    s = re.sub(r"\$[^$]+\$", "FORMULA", s)
    s = re.sub(r"\$\$[^$]+\$\$", "FORMULA", s)
    s = re.sub(r"\\begin\{equation\}.+?\\end\{equation\}", "FORMULA", s, flags=re.DOTALL)
    s = re.sub(r"\\[a-zA-Z]+\{[^}]*\}", "TERM", s)
    s = re.sub(r"\\[a-zA-Z]+", "TERM", s)
    s = re.sub(r"`[^`]+`", "VAR", s)
    return s.strip()


def _focused_evidence(claim: str, cited_ids: list[str], chunk_lookup: dict) -> str:
    """Return the most relevant 800 chars from each cited chunk."""
    import re

    # Word-overlap ranking — cheap and effective for sentence selection
    claim_terms = set(re.findall(r"\w{4,}", claim.lower()))

    bits: list[str] = []
    for cid in cited_ids:
        chunk = chunk_lookup.get(cid)
        if not chunk:
            continue
        content = chunk.content
        # Split into sentences-ish
        parts = re.split(r"(?<=[.!?])\s+|\n\n", content)
        scored = [
            (len(claim_terms & set(re.findall(r"\w{4,}", p.lower()))), p)
            for p in parts
            if len(p.strip()) > 20
        ]
        scored.sort(reverse=True)
        # Take top-3 best-matching parts, max 800 chars total
        top = " ".join(p for _, p in scored[:3])[:800]
        if top:
            bits.append(top)
    return (
        "\n".join(bits)
        if bits
        else "\n".join(chunk_lookup[c].content[:600] for c in cited_ids if c in chunk_lookup)
    )


def verify_answer(
    answer: str,
    chunks: Sequence[RetrievedChunk],
    threshold: float = 0.35,
) -> list[VerifiedSentence]:
    """For each sentence with a citation, check NLI entailment from the cited chunk."""
    chunk_lookup: dict[str, RetrievedChunk] = {}
    for c in chunks:
        chunk_lookup[c.chunk_id] = c
        parts = c.chunk_id.split(":", 1)
        if len(parts) == 2:
            chunk_lookup[parts[1]] = c
        chunk_lookup.setdefault(c.arxiv_id, c)
    results: list[VerifiedSentence] = []
    nli = get_nli()

    for sent in split_sentences(answer):
        cited_ids: list[str] = []
        for m in CITATION_RE_COMMA.findall(sent):
            cited_ids.append(m[1].strip())
        for bracket in re.findall(r"\[([^\]]+)\]", sent):
            for m in CITATION_RE_BARE.findall(bracket):
                if m not in cited_ids:
                    cited_ids.append(m)
        if not cited_ids:
            results.append(
                VerifiedSentence(
                    sentence=sent,
                    citation_chunk_ids=[],
                    entailment_score=0.0,
                    supported=False,
                )
            )
            continue

        # Score against the union of cited chunks
        evidence = _focused_evidence(sent, cited_ids, chunk_lookup)
        if not evidence:
            results.append(
                VerifiedSentence(
                    sentence=sent,
                    citation_chunk_ids=cited_ids,
                    entailment_score=0.0,
                    supported=False,
                )
            )
            continue

        # Cross-encoder NLI: returns logits for [contradiction, entailment, neutral]
        # Strip citations from the claim only — math notation stays.
        # Earlier experiments showed _strip_math destroyed lexical signal
        # (numbers in the claim, FORMULA token in evidence).
        import re as _re

        sent_clean = _re.sub(r"\[\d{4}\.\d{4,5}[^\]]*\]", "", sent).strip()
        logits = nli.predict([(evidence, sent_clean)])
        # Softmax-y normalization → entailment probability
        import numpy as np

        scores = np.exp(logits[0]) / np.exp(logits[0]).sum()
        entail = float(scores[1])

        results.append(
            VerifiedSentence(
                sentence=sent,
                citation_chunk_ids=cited_ids,
                entailment_score=entail,
                supported=entail >= threshold,
            )
        )

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
    return sum(1.0 if v.supported else 0.0 for v in cited) / len(cited)
