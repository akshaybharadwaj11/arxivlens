"""OpenTelemetry tracing — exports to Cloud Trace.

Initialize once at startup via setup_tracing(). Get a tracer per module via
get_tracer(__name__) and wrap operations in tracer.start_as_current_span(...).

In production (GCP), traces appear in Cloud Trace within ~30s of the request.
Locally, traces still get exported if ADC is configured for a project — but
you can also set OTEL_TRACES_EXPORTER=none to disable export entirely.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from arxivlens.config import settings
from arxivlens.logging import get_logger

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer

log = get_logger("tracing")

_initialized = False


def setup_tracing(service_name: str = "arxivlens-api") -> None:
    """Initialize OTel exporters. Safe to call multiple times."""
    global _initialized
    if _initialized:
        return

    if os.environ.get("OTEL_DISABLED") == "1":
        log.info("tracing_disabled", reason="OTEL_DISABLED=1")
        _initialized = True
        return

    cfg = settings()
    if not cfg.project_id:
        log.info("tracing_skipped", reason="no_project_id")
        _initialized = True
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": "0.1.0",
                "deployment.environment": cfg.env,
            }
        )
        provider = TracerProvider(resource=resource)
        exporter = CloudTraceSpanExporter(project_id=cfg.project_id)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        log.info("tracing_initialized", service=service_name, project=cfg.project_id)
    except Exception as e:
        log.warning("tracing_init_failed", error=str(e))

    _initialized = True


def get_tracer(name: str) -> "Tracer":
    """Get a tracer for the given module name."""
    from opentelemetry import trace

    return trace.get_tracer(name)


def instrument_fastapi(app) -> None:
    """Auto-instrument a FastAPI app — adds a span per request."""
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
        log.info("fastapi_instrumented")
    except Exception as e:
        log.warning("fastapi_instrument_failed", error=str(e))
