from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import Response

from app.config import get_app_paths, load_config
from app.database import ModelRoute, RouteDatabase
from app.proxy import (
    RouterRuntime,
    discover_models,
    forward_request,
    models_response,
)
from app.wake import WakeCoordinator


logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(application: FastAPI):
    paths = get_app_paths()
    config = load_config(paths.config)
    endpoint_count = sum(len(device.endpoints) for device in config.devices.values())
    logger.info(
        "LLazarus loading %s: %d device(s), %d endpoint(s)",
        paths.config,
        len(config.devices),
        endpoint_count,
    )
    database = RouteDatabase(paths.database)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=config.server.connect_timeout,
            read=config.server.read_timeout,
            write=config.server.write_timeout,
            pool=config.server.connect_timeout,
        ),
        follow_redirects=False,
        trust_env=False,
    )

    try:
        await database.open()
        logger.info("Model cache opened at %s", paths.database)
        removed_routes = await database.synchronize_configuration(config.devices)
        if removed_routes:
            logger.info(
                "Removed %d cached route(s) no longer present in configuration",
                removed_routes,
            )

        logger.info("Discovering models from %d configured endpoint(s)", endpoint_count)
        for discovery in await discover_models(config, client):
            added, removed = await database.synchronize_endpoint_models(
                discovery.device,
                discovery.endpoint,
                discovery.model_ids,
            )
            if added or removed:
                logger.info(
                    "Synchronized %s: %d added, %d removed",
                    discovery.endpoint,
                    added,
                    removed,
                )

        routes = await database.load_routes()
        if routes:
            logger.info("Active model routes: %s", _format_routes(routes))
        wake = WakeCoordinator(config.server, config.devices, client)
        application.state.runtime = RouterRuntime(
            config=config,
            routes=routes,
            client=client,
            wake=wake,
        )
        logger.info(
            "LLazarus ready with %d cached model route(s) from %d device(s)",
            len(routes),
            len(config.devices),
        )
        yield
    finally:
        await client.aclose()
        await database.close()


app = FastAPI(
    title="LLazarus",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)


@app.get("/v1/models")
async def list_models(request: Request) -> Response:
    runtime: RouterRuntime = request.app.state.runtime
    return models_response(runtime.routes)


@app.post("/v1/{path:path}")
async def proxy_openai_request(path: str, request: Request) -> Response:
    runtime: RouterRuntime = request.app.state.runtime
    return await forward_request(request, path, runtime)


def run() -> None:
    config = load_config()
    uvicorn.run(app, host="0.0.0.0", port=config.server.port)


def _format_routes(routes: dict[str, ModelRoute], limit: int = 8) -> str:
    visible = list(routes.values())[:limit]
    formatted = ", ".join(
        f"{route.model_id}->{route.device}@{route.endpoint}" for route in visible
    )
    remaining = len(routes) - len(visible)
    return f"{formatted}, +{remaining} more" if remaining else formatted


if __name__ == "__main__":
    run()
