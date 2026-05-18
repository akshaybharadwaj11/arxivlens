"""Custom metrics exported to Cloud Monitoring.

Each /chat request records:
  - chat.requests (counter): total requests, labeled by status
  - chat.faithfulness (histogram): per-request faithfulness score
  - chat.latency_ms (histogram): per-request total latency
  - chat.cited_sentences (histogram): how many sentences cited evidence
  - chat.supported_sentences (histogram): how many were verified supported

These show up in Cloud Monitoring under
'workload.googleapis.com/<metric>' and can be queried via MQL or chart
in the dashboard.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from arxivlens.config import settings
from arxivlens.logging import get_logger

if TYPE_CHECKING:
    from opentelemetry.metrics import Counter, Histogram, Meter

log = get_logger("metrics")

_meter: Meter | None = None
_initialized = False

# Module-level so tests/CI can import without setup
_request_counter: Counter | None = None
_faithfulness_hist: Histogram | None = None
_latency_hist: Histogram | None = None
_cited_hist: Histogram | None = None
_supported_hist: Histogram | None = None


def setup_metrics(service_name: str = "arxivlens-api") -> None:
    """Initialize the Cloud Monitoring metric exporter. Idempotent."""
    global _meter, _initialized
    global _request_counter, _faithfulness_hist, _latency_hist
    global _cited_hist, _supported_hist

    if _initialized:
        return

    if os.environ.get("OTEL_DISABLED") == "1":
        _initialized = True
        log.info("metrics_disabled", reason="OTEL_DISABLED=1")
        return

    cfg = settings()
    if not cfg.project_id:
        _initialized = True
        log.info("metrics_skipped", reason="no_project_id")
        return

    try:
        from opentelemetry import metrics
        from opentelemetry.exporter.cloud_monitoring import (
            CloudMonitoringMetricsExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.metrics.view import DropAggregation, View
        from opentelemetry.sdk.resources import Resource

        resource = Resource.create(
            {
                "service.name": service_name,
                "service.version": "0.1.0",
                "deployment.environment": cfg.env,
            }
        )
        reader = PeriodicExportingMetricReader(
            CloudMonitoringMetricsExporter(project_id=cfg.project_id),
            export_interval_millis=60_000,  # 1 min export interval
        )
        # Filter out SDK self-instrumentation that fires faster than
        # Cloud Monitoring's 5-second minimum sampling rate.
        views = [
            # Drop all OTel SDK self-telemetry — these fire faster than
            # Cloud Monitoring's 5s minimum sampling rate and fail the batch.
            View(instrument_name="otel.sdk.*", aggregation=DropAggregation()),
            View(instrument_name="otel.*", aggregation=DropAggregation()),
        ]
        provider = MeterProvider(resource=resource, metric_readers=[reader], views=views)
        metrics.set_meter_provider(provider)
        _meter = metrics.get_meter("arxivlens")

        _request_counter = _meter.create_counter(
            "chat.requests",
            description="Total /chat requests",
            unit="1",
        )
        _faithfulness_hist = _meter.create_histogram(
            "chat.faithfulness",
            description="Faithfulness score per request (0.0–1.0)",
            unit="1",
        )
        _latency_hist = _meter.create_histogram(
            "chat.latency_ms",
            description="End-to-end /chat latency",
            unit="ms",
        )
        _cited_hist = _meter.create_histogram(
            "chat.cited_sentences",
            description="Sentences containing citations",
            unit="1",
        )
        _supported_hist = _meter.create_histogram(
            "chat.supported_sentences",
            description="Sentences verified as supported by NLI",
            unit="1",
        )

        log.info("metrics_initialized", service=service_name)
    except Exception as e:
        log.warning("metrics_init_failed", error=str(e))

    _initialized = True


def record_chat(
    faithfulness: float,
    latency_ms: float,
    n_cited: int,
    n_supported: int,
    status: str = "ok",
) -> None:
    """Record metrics for a single /chat request. Safe to call when disabled."""
    if _request_counter is None:
        return
    attrs = {"status": status}
    try:
        _request_counter.add(1, attrs)
        _faithfulness_hist.record(faithfulness, attrs)
        _latency_hist.record(latency_ms, attrs)
        _cited_hist.record(n_cited, attrs)
        _supported_hist.record(n_supported, attrs)
    except Exception as e:
        log.warning("metric_record_failed", error=str(e))
