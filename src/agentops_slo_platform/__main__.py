"""Process entry point: ``python -m agentops_slo_platform``."""

from __future__ import annotations

import os

import uvicorn
from agentic_core.observability import configure_logging, configure_telemetry
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from agentops_slo_platform import __version__
from agentops_slo_platform.api.app import create_app
from agentops_slo_platform.config import Settings
from agentops_slo_platform.runtime.container import build_container


def main() -> None:
    settings = Settings()  # populated from AGENTOPS_* environment variables
    configure_logging(
        service="agentops-slo-platform", environment=settings.environment, level=settings.log_level
    )
    exporter = None
    if settings.otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        exporter = OTLPSpanExporter(endpoint=settings.otlp_endpoint)
    configure_telemetry(
        service_name="agentops-slo-platform",
        service_version=__version__,
        environment=settings.environment,
        span_exporter=exporter,
    )
    app = create_app(lambda: build_container(settings))
    FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics,v1/traces")
    uvicorn.run(
        app,
        host=os.environ.get("AGENTOPS_HOST", "127.0.0.1"),
        port=int(os.environ.get("AGENTOPS_PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("AGENTOPS_FORWARDED_ALLOW_IPS", "127.0.0.1"),
        log_config=None,
        access_log=False,
    )


if __name__ == "__main__":
    main()
