import json
import logging
import time
from contextlib import contextmanager

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode, get_current_span
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from ai_lab.settings import Settings

PROPAGATOR = TraceContextTextMapPropagator()  # No baggage or user-defined headers.
ATTRIBUTES = frozenset(
    {
        "http.request.method",
        "http.route",
        "http.response.status_code",
        "lab.operation",
        "lab.outcome",
        "lab.model.mode",
        "lab.model.name",
        "lab.steps",
        "lab.tool.calls",
        "lab.retry",
        "lab.retrieval.count",
        "lab.error.type",
        "lab.schema.version",
    }
)
LOGGER = logging.getLogger("ai_lab.operational")


class Telemetry:
    """Explicit metadata-only instrumentation. No global auto-instrumentation."""

    def __init__(self, service: str, settings: Settings, exporter=None, metric_reader=None):
        self.service = service
        self.resource = Resource(
            {
                "service.name": service,
                "service.namespace": "personal-ai-lab",
                "service.version": settings.version,
                "deployment.environment.name": "local",
                "lab.schema.version": "1.0.0",
            }
        )
        self.provider = TracerProvider(resource=self.resource)
        if exporter is not None:
            self.provider.add_span_processor(SimpleSpanProcessor(exporter))
        elif settings.otlp_endpoint:
            self.provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=settings.otlp_endpoint.rstrip("/") + "/v1/traces", timeout=2
                    ),
                    max_queue_size=512,
                    max_export_batch_size=128,
                    schedule_delay_millis=200,
                )
            )
        self.tracer = self.provider.get_tracer("ai_lab.manual", "0.1.0")
        readers = [metric_reader] if metric_reader else []
        if settings.otlp_endpoint and not readers:
            readers = [
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(
                        endpoint=settings.otlp_endpoint.rstrip("/") + "/v1/metrics", timeout=2
                    ),
                    export_interval_millis=1000,
                    export_timeout_millis=3000,
                )
            ]
        self.meter_provider = MeterProvider(resource=self.resource, metric_readers=readers)
        meter = self.meter_provider.get_meter("ai_lab.manual", "0.1.0")
        self.duration = meter.create_histogram("lab.operation.duration", unit="s")
        self.calls = meter.create_counter("lab.operation.calls", unit="{call}")

    @contextmanager
    def span(self, name, *, kind=SpanKind.INTERNAL, context=None, attributes=None):
        attrs = attributes or {}
        if set(attrs) - ATTRIBUTES:
            raise ValueError("Telemetry attribute not allowlisted")
        with self.tracer.start_as_current_span(
            name,
            kind=kind,
            context=context,
            attributes=attrs,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            yield span

    def record(self, operation: str, outcome: str, started: float):
        labels = {"lab.operation": operation, "lab.outcome": outcome}
        self.duration.record(time.monotonic() - started, labels)
        self.calls.add(1, labels)
        ctx = get_current_span().get_span_context()
        LOGGER.info(
            json.dumps(
                {
                    "event": "operation.completed",
                    "service": self.service,
                    "operation": operation,
                    "outcome": outcome,
                    "trace_id": f"{ctx.trace_id:032x}",
                    "span_id": f"{ctx.span_id:016x}",
                },
                sort_keys=True,
            )
        )

    def flush(self):
        self.provider.force_flush(timeout_millis=5000)
        self.meter_provider.force_flush(timeout_millis=5000)

    def shutdown(self):
        self.provider.shutdown()
        self.meter_provider.shutdown()


def mark_error(span, error_type: str):
    span.set_attribute("lab.error.type", error_type)
    span.set_status(StatusCode.ERROR)  # Never put exception text in the description.
