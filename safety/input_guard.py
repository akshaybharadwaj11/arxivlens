"""Input-side guardrails: length check, simple jailbreak heuristics.

For v1, this is a tiny module. v2: replace with a fine-tuned distilbert classifier.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from arxivlens.config import settings

JAILBREAK_PATTERNS = [
    r"ignore (all |previous |prior )?instructions",
    r"you are now",
    r"pretend (to be|you are)",
    r"system prompt",
    r"reveal your instructions",
    r"<\|.*?\|>",  # special-token spoofing
]
_compiled = [re.compile(p, re.IGNORECASE) for p in JAILBREAK_PATTERNS]


@dataclass
class GuardCheck:
    ok: bool
    reason: str | None = None


def check_input(query: str) -> GuardCheck:
    cfg = settings()
    if not query or not query.strip():
        return GuardCheck(False, "Empty query")
    if len(query) > cfg.max_query_length:
        return GuardCheck(False, f"Query exceeds {cfg.max_query_length} chars")
    for pat in _compiled:
        if pat.search(query):
            return GuardCheck(False, "Query matched a jailbreak pattern")
    return GuardCheck(True)
