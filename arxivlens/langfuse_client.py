"""Langfuse Cloud client for LLM observability (v4 SDK).

Each /chat request becomes a Langfuse trace with nested observations for
retrieval, generation, and verification. Output: a clickable UI showing
prompt → retrieved chunks → generated answer → faithfulness scores.

Disabled gracefully if LANGFUSE_PUBLIC_KEY is unset.

The v4 SDK uses start_as_current_observation(as_type=...) and exposes
scoring on the active span. We wrap that in a stable internal API so
generation/app.py doesn't change when Langfuse rev's again.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from arxivlens.config import settings
from arxivlens.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger("langfuse_client")

_initialized = False
_disabled = False


def _ensure_initialized() -> bool:
    """Initialize the global Langfuse client from env. Returns True if usable."""
    global _initialized, _disabled

    if _initialized:
        return not _disabled

    if os.environ.get("LANGFUSE_DISABLED") == "1":
        _disabled = True
        _initialized = True
        log.info("langfuse_disabled", reason="LANGFUSE_DISABLED=1")
        return False

    cfg = settings()
    if not cfg.langfuse_public_key or not cfg.langfuse_secret_key:
        _disabled = True
        _initialized = True
        log.info("langfuse_skipped", reason="keys_not_set")
        return False

    # v4: keys are picked up from env by get_client(). Set them now.
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", cfg.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", cfg.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", cfg.langfuse_host)

    try:
        from langfuse import get_client

        # First call constructs and caches the singleton
        get_client()
        log.info("langfuse_initialized", host=cfg.langfuse_host)
        _initialized = True
        return True
    except Exception as e:
        _disabled = True
        _initialized = True
        log.warning("langfuse_init_failed", error=str(e))
        return False


def flush() -> None:
    """Flush queued events. Call before process exit for short-lived jobs."""
    if not _ensure_initialized():
        return
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception as e:
        log.warning("langfuse_flush_failed", error=str(e))


@contextmanager
def trace(name: str, **kwargs: Any) -> Iterator[_LangfuseRequest]:
    """Top-level context manager for a single request.

    Usage:
        with lf_trace("chat.request", query=q, top_k=3) as lf:
            lf.span("retrieve", input=..., output=...)
            lf.generation("gemini.generate", model="...", input=..., output=...)
            lf.score("faithfulness", value=0.8)
    """
    if not _ensure_initialized():
        yield _NoopRequest()
        return

    try:
        from langfuse import get_client

        client = get_client()
        with client.start_as_current_observation(as_type="span", name=name) as root_span:
            # Stamp the trace itself with input/metadata (v4 pattern)
            try:
                root_span.update(
                    input=kwargs.get("query"),
                    metadata={k: v for k, v in kwargs.items() if k != "query"},
                )
            except Exception:  # v4 update calls, best-effort
                pass

            try:
                yield _LangfuseRequest(client, root_span)
            finally:
                try:
                    client.flush()
                except Exception as e:
                    log.warning("langfuse_flush_failed", error=str(e))
    except Exception as e:
        # If anything in the trace setup explodes, fall through to a noop
        # so the actual request still completes.
        log.warning("langfuse_trace_failed", error=str(e))
        yield _NoopRequest()


class _LangfuseRequest:
    """Stable internal API over Langfuse v4 observations."""

    def __init__(self, client: Any, root_span: Any) -> None:
        self._client = client
        self._root = root_span

    def span(
        self,
        name: str,
        input: Any = None,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            with self._client.start_as_current_observation(as_type="span", name=name) as s:
                s.update(input=input, output=output, metadata=metadata or {})
        except Exception as e:
            log.warning("langfuse_span_failed", name=name, error=str(e))

    def generation(
        self,
        name: str,
        model: str,
        input: Any = None,
        output: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        try:
            with self._client.start_as_current_observation(
                as_type="generation", name=name, model=model
            ) as g:
                g.update(input=input, output=output, metadata=metadata or {})
        except Exception as e:
            log.warning("langfuse_generation_failed", name=name, error=str(e))

    def score(self, name: str, value: float, comment: str | None = None) -> None:
        try:
            # In v4, scoring is a method on the active span/trace
            self._root.score_trace(name=name, value=value, comment=comment or "")
        except Exception as e:
            log.warning("langfuse_score_failed", name=name, error=str(e))

    def update(self, **kwargs: Any) -> None:
        try:
            self._root.update(**kwargs)
        except Exception as e:
            log.warning("langfuse_update_failed", error=str(e))


class _NoopRequest:
    """Drop-in when Langfuse is disabled. Same API, all no-ops."""

    def span(self, *args: Any, **kwargs: Any) -> None:
        pass

    def generation(self, *args: Any, **kwargs: Any) -> None:
        pass

    def score(self, *args: Any, **kwargs: Any) -> None:
        pass

    def update(self, *args: Any, **kwargs: Any) -> None:
        pass
