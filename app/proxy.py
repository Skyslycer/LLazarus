from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable
from contextlib import suppress
from dataclasses import dataclass

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.responses import Response

from app.config import RouterConfig
from app.database import ModelRoute
from app.suspend import ActivityLease, SuspendTracker
from app.wake import (
    DeviceUnavailableError,
    EndpointUnavailableError,
    WakeCoordinator,
)


logger = logging.getLogger("uvicorn.error")

_HOP_BY_HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}
_REQUEST_ONLY_HEADERS = {b"host", b"content-length"}


@dataclass(frozen=True, slots=True)
class EndpointModels:
    device: str
    endpoint: str
    model_ids: tuple[str, ...]


@dataclass(slots=True)
class RouterRuntime:
    config: RouterConfig
    routes: dict[str, ModelRoute]
    client: httpx.AsyncClient
    wake: WakeCoordinator
    suspend: SuspendTracker


class _ClientDisconnected(RuntimeError):
    pass


async def discover_models(
    config: RouterConfig, client: httpx.AsyncClient
) -> list[EndpointModels]:
    configured_endpoints = [
        (device.name, endpoint)
        for device in config.devices.values()
        for endpoint in device.endpoints
    ]
    discoveries = await asyncio.gather(
        *(
            _discover_endpoint_models(
                device,
                endpoint,
                client,
                config.server.connect_timeout,
            )
            for device, endpoint in configured_endpoints
        )
    )
    return [discovery for discovery in discoveries if discovery is not None]


async def _discover_endpoint_models(
    device: str,
    endpoint: str,
    client: httpx.AsyncClient,
    timeout: float,
) -> EndpointModels | None:
    try:
        response = await client.get(
            f"{endpoint}/models",
            timeout=httpx.Timeout(timeout),
        )
        response.raise_for_status()
        payload = response.json()
        model_ids = _parse_model_list(payload)
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
        logger.info(
            "Discovery unavailable: device=%s endpoint=%s (%s)",
            device,
            endpoint,
            _brief_error(exc),
        )
        return None

    logger.info(
        "Discovered device=%s endpoint=%s: %d model(s) [%s]",
        device,
        endpoint,
        len(model_ids),
        _format_model_ids(model_ids),
    )
    return EndpointModels(device=device, endpoint=endpoint, model_ids=model_ids)


def _parse_model_list(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("response is not an OpenAI-compatible model list")

    model_ids: list[str] = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            raise ValueError("model list contains a non-object item")
        model_id = item.get("id")
        if not isinstance(model_id, str) or not model_id:
            raise ValueError("model list contains an invalid model id")
        model_ids.append(model_id)
    return tuple(dict.fromkeys(model_ids))


def _format_model_ids(model_ids: tuple[str, ...], limit: int = 8) -> str:
    visible = model_ids[:limit]
    suffix = f", +{len(model_ids) - limit} more" if len(model_ids) > limit else ""
    return ", ".join(visible) + suffix if visible else "none"


def _brief_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.RequestError):
        return exc.__class__.__name__
    return str(exc)


def models_response(routes: dict[str, ModelRoute]) -> JSONResponse:
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "owned_by": "ai-router",
                }
                for model_id in sorted(routes)
            ],
        }
    )


async def forward_request(
    request: Request,
    path: str,
    runtime: RouterRuntime,
) -> Response:
    body = await request.body()
    model_id = _extract_model_id(body)
    if isinstance(model_id, JSONResponse):
        return model_id

    route = runtime.routes.get(model_id)
    if route is None:
        logger.info("Rejected request path=/v1/%s: unknown model=%s", path, model_id)
        return openai_error(
            status_code=404,
            message=f"Model '{model_id}' is not available",
            error_type="invalid_request_error",
            code="model_not_found",
            param="model",
        )

    device = runtime.config.devices[route.device]
    activity = await runtime.suspend.start_request(route.device)
    activity_handed_to_response = False
    logger.info(
        "Routing path=/v1/%s model=%s to device=%s endpoint=%s",
        path,
        model_id,
        route.device,
        route.endpoint,
    )
    try:
        try:
            upstream_response = await _open_upstream_or_disconnect(
                request,
                _open_upstream(request, path, body, runtime, route),
            )
        except _ClientDisconnected:
            logger.info(
                "Client disconnected before backend stream started for model=%s",
                model_id,
            )
            return Response(status_code=499)
        except (DeviceUnavailableError, EndpointUnavailableError) as exc:
            logger.warning("Route unavailable for model=%s: %s", model_id, exc)
            return openai_error(
                status_code=503,
                message=str(exc),
                error_type="service_unavailable_error",
                code="service_unavailable",
            )
        except httpx.RequestError as exc:
            logger.warning("Backend request to %s failed: %s", route.endpoint, exc)
            return openai_error(
                status_code=503,
                message=f"Backend endpoint {route.endpoint} became unavailable",
                error_type="service_unavailable_error",
                code="service_unavailable",
            )

        logger.info(
            "Backend responded model=%s status=%d content-type=%s",
            model_id,
            upstream_response.status_code,
            upstream_response.headers.get("content-type", "unknown"),
        )

        async def stream_upstream():
            try:
                async for chunk in upstream_response.aiter_raw():
                    yield chunk
            except httpx.RequestError as exc:
                logger.warning("Backend stream interrupted for model=%s: %s", model_id, exc)
                raise
            finally:
                await _finish_upstream(upstream_response, activity)

        response = StreamingResponse(
            stream_upstream(),
            status_code=upstream_response.status_code,
            background=BackgroundTask(_finish_upstream, upstream_response, activity),
        )
        response.raw_headers = _filtered_response_headers(upstream_response.headers.raw)
        activity_handed_to_response = True
        return response
    finally:
        if not activity_handed_to_response:
            await activity.close()


async def _open_upstream(
    request: Request,
    path: str,
    body: bytes,
    runtime: RouterRuntime,
    route: ModelRoute,
) -> httpx.Response:
    device = runtime.config.devices[route.device]
    await runtime.wake.ensure_ready(device, route.endpoint)

    target_url = f"{route.endpoint}/{path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"
    upstream_request = runtime.client.build_request(
        method=request.method,
        url=target_url,
        headers=_filtered_request_headers(request.headers.raw),
        content=body,
    )
    return await runtime.client.send(upstream_request, stream=True)


async def _open_upstream_or_disconnect(
    request: Request,
    operation: Awaitable[httpx.Response],
) -> httpx.Response:
    operation_task = asyncio.ensure_future(operation)
    disconnect_task = asyncio.create_task(_wait_for_disconnect(request))
    try:
        done, _ = await asyncio.wait(
            {operation_task, disconnect_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disconnect_task in done:
            if operation_task.done() and not operation_task.cancelled():
                with suppress(Exception):
                    response = operation_task.result()
                    await response.aclose()
            else:
                operation_task.cancel()
            await asyncio.gather(operation_task, return_exceptions=True)
            raise _ClientDisconnected
        return await operation_task
    finally:
        disconnect_task.cancel()
        if not operation_task.done():
            operation_task.cancel()
        await asyncio.gather(operation_task, disconnect_task, return_exceptions=True)


async def _wait_for_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _finish_upstream(
    upstream_response: httpx.Response,
    activity: ActivityLease,
) -> None:
    try:
        await upstream_response.aclose()
    finally:
        await activity.close()


def _extract_model_id(body: bytes) -> str | JSONResponse:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return openai_error(
            status_code=400,
            message="Request body must be valid JSON",
            error_type="invalid_request_error",
            code="invalid_json",
        )

    if not isinstance(payload, dict):
        return openai_error(
            status_code=400,
            message="Request body must be a JSON object",
            error_type="invalid_request_error",
            code="invalid_request",
        )

    model_id = payload.get("model")
    if not isinstance(model_id, str) or not model_id:
        return openai_error(
            status_code=400,
            message="A non-empty 'model' field is required",
            error_type="invalid_request_error",
            code="model_required",
            param="model",
        )
    return model_id


def _filtered_request_headers(
    raw_headers: list[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    excluded = _connection_header_names(raw_headers)
    excluded.update(_HOP_BY_HOP_HEADERS)
    excluded.update(_REQUEST_ONLY_HEADERS)
    return [(name, value) for name, value in raw_headers if name.lower() not in excluded]


def _filtered_response_headers(
    raw_headers: list[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    excluded = _connection_header_names(raw_headers)
    excluded.update(_HOP_BY_HOP_HEADERS)
    return [(name, value) for name, value in raw_headers if name.lower() not in excluded]


def _connection_header_names(
    raw_headers: list[tuple[bytes, bytes]],
) -> set[bytes]:
    names: set[bytes] = set()
    for name, value in raw_headers:
        if name.lower() == b"connection":
            names.update(part.strip().lower() for part in value.split(b",") if part.strip())
    return names


def openai_error(
    status_code: int,
    message: str,
    error_type: str,
    code: str,
    param: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        },
    )
