"""
AG-X Community Edition — OpenTelemetry hooks

Emits one OTel span per @agx.protect invocation with standardised attributes:
  gen_ai.*  — OpenTelemetry GenAI semantic conventions
  agx.*     — AG-X specific attributes

Adapted from TraceGuard Ω traceguard/core/otel_hooks.py.
Changes from original:
  - Removed `from config import settings` → uses agx._config.settings
  - Renamed span attribute prefix tg.* → agx.*
  - Added tg.source = "agx-community" to distinguish community vs cloud spans
  - Graceful fallback: if OTLP endpoint is unreachable, logs warning + continues
"""

from __future__ import annotations

import logging
from typing import Optional

from agx._config import settings
from agx._models import AgxSpan

log = logging.getLogger(__name__)

# Whether setup_otel() has been called successfully
_otel_initialized = False
_tracer = None


def setup_otel(endpoint: Optional[str] = None) -> bool:
    """Configure OTel SDK with OTLP gRPC exporter.

    Args:
        endpoint: OTLP gRPC endpoint. Defaults to AGX_OTEL_ENDPOINT env var
                  (default: http://localhost:4317).

    Returns True if setup succeeded, False if opentelemetry packages are missing
    or the endpoint is unreachable.
    """
    global _otel_initialized, _tracer

    target = endpoint or settings.otel_endpoint

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError:
        log.warning(
            "opentelemetry packages not installed. "
            "Run: pip install agx-community[otel]"
        )
        return False

    try:
        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.version": "0.1.0",
                "tg.source": "agx-community",  # distinguish community vs cloud spans
            }
        )

        provider = TracerProvider(resource=resource)

        # OTLP exporter
        try:
            exporter = OTLPSpanExporter(endpoint=target, insecure=True)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        except Exception as exc:
            log.warning(
                "AGX OTel: failed to connect to %s (%s). "
                "Falling back to console exporter.",
                target,
                exc,
            )
            provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("agx-community", "0.1.0")
        _otel_initialized = True
        settings.otel_enabled = True  # signal guard.py to emit spans

        log.info("AGX OTel configured → %s", target)
        return True

    except Exception as exc:
        log.warning("AGX OTel setup failed: %s", exc)
        return False


def emit_span(span: AgxSpan) -> None:
    """Emit one OTel span for the given AgxSpan. No-op if OTel not initialized."""
    if not _otel_initialized or _tracer is None:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.trace import StatusCode

        with _tracer.start_as_current_span(f"agx.{span.agent_name}") as otel_span:
            # --- gen_ai.* semantic conventions ---
            otel_span.set_attribute("gen_ai.system", "agx-community")
            otel_span.set_attribute("gen_ai.operation.name", "agent_invoke")
            if span.input_prompt:
                otel_span.set_attribute(
                    "gen_ai.prompt",
                    span.input_prompt[:1000],  # truncate large prompts
                )
            if span.output_snapshot:
                otel_span.set_attribute(
                    "gen_ai.completion",
                    span.output_snapshot[:1000],
                )

            # --- agx.* attributes ---
            otel_span.set_attribute("agx.agent_name", span.agent_name)
            otel_span.set_attribute("agx.session_id", span.session_id)
            otel_span.set_attribute("agx.outcome", span.outcome.value)
            otel_span.set_attribute("agx.total_ms", span.total_ms)
            otel_span.set_attribute("agx.run_id", span.id)

            # tg.source lets cloud customers distinguish community vs cloud spans
            otel_span.set_attribute("tg.source", "agx-community")

            if span.cage_result is not None:
                otel_span.set_attribute("agx.cage.passed", span.cage_result.passed)
                otel_span.set_attribute("agx.cage.blocked", span.cage_result.blocked)
                otel_span.set_attribute(
                    "agx.cage.duration_ms", span.cage_result.duration_ms
                )
                failed_assertions = [
                    v.message for v in span.cage_result.verdicts if not v.passed
                ]
                if failed_assertions:
                    otel_span.set_attribute(
                        "agx.cage.failures",
                        "; ".join(failed_assertions)[:500],
                    )

            if span.vaccines_fired:
                otel_span.set_attribute(
                    "agx.vaccines_fired", ",".join(span.vaccines_fired)
                )

            if span.error:
                otel_span.set_attribute("agx.error", span.error[:500])
                otel_span.set_status(StatusCode.ERROR, span.error[:200])
            else:
                otel_span.set_status(StatusCode.OK)

    except Exception as exc:
        # Never let OTel failures crash the agent
        log.debug("AGX OTel emit_span failed: %s", exc)
