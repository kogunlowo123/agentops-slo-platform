"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from agentic_core.observability import bind_correlation_id, get_logger
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from agentops_slo_platform import __version__
from agentops_slo_platform.api.routes import router
from agentops_slo_platform.domain.errors import PlatformError
from agentops_slo_platform.runtime.container import Container

ContainerFactory = Callable[[], Awaitable[Container]]

log = get_logger("agentops_slo_platform.api")


async def run_periodically(
    name: str,
    container: Container,
    job: Callable[[], Awaitable[Any]],
    interval_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run ``job`` forever; a failing iteration is logged and counted, never fatal."""
    while True:
        try:
            await job()
        except Exception:
            container.metrics.loop_failures.labels(name).inc()
            log.exception("background_job_failed", job=name)
        await sleep(interval_s)


class McpEndpoint:
    """ASGI endpoint handing /mcp to the streamable-HTTP session manager of the container."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        container: Container = scope["app"].state.container
        await container.mcp.session_manager.handle_request(scope, receive, send)


def _error(status: int, code: str, description: str, **extra: Any) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
    if status == 503:
        headers = {"Retry-After": "5"}
    return JSONResponse(
        status_code=status,
        content={"error": code, "error_description": description, **extra},
        headers=headers,
    )


def create_app(factory: ContainerFactory, *, background: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = await factory()
        app.state.container = container
        settings = container.settings
        tasks: list[asyncio.Task[None]] = []
        if background:
            jobs: list[tuple[str, Callable[[], Awaitable[Any]], float]] = [
                (
                    "finalize_workflows",
                    container.analytics.finalize_due,
                    settings.finalize_interval_s,
                ),
                ("evaluate_slos", container.slos.evaluate_all, settings.evaluation_interval_s),
                ("purge_retention", container.purge_expired, settings.purge_interval_s),
            ]
            tasks = [
                asyncio.create_task(run_periodically(name, container, job, interval))
                for name, job, interval in jobs
            ]
        log.info("startup", version=__version__)
        try:
            async with container.mcp.session_manager.run():
                yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await container.aclose()

    app = FastAPI(
        title="AgentOps SLO Platform",
        version=__version__,
        description=(
            "OTLP ingestion of agent traces (GenAI semantic conventions), agent/tool/memory/"
            "workflow metrics, token and cost attribution, multi-window multi-burn-rate SLO "
            "alerting, workflow failure classification and policy-gated recovery actions."
        ),
        lifespan=lifespan,
    )
    app.include_router(router)
    app.router.routes.append(
        Route("/mcp", endpoint=McpEndpoint(), methods=["GET", "POST", "DELETE"])
    )

    @app.middleware("http")
    async def correlation_and_metrics(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-correlation-id")
        cid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else None
        started = time.perf_counter()
        with bind_correlation_id(cid) as bound:
            response = await call_next(request)
        response.headers["X-Correlation-ID"] = bound
        response.headers["X-Content-Type-Options"] = "nosniff"
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        container: Container | None = getattr(request.app.state, "container", None)
        if container is not None:
            container.metrics.requests.labels(
                request.method, path, str(response.status_code)
            ).observe(time.perf_counter() - started)
        return response

    @app.exception_handler(PlatformError)
    async def platform_error(_: Request, exc: PlatformError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(
            422,
            "invalid_request",
            "request failed validation",
            details=[
                {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))} for e in exc.errors()
            ],
        )

    return app
