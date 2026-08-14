from __future__ import annotations

import asyncio
import logging
import math
import socket
from collections.abc import Awaitable, Callable

import httpx

from app.config import DeviceConfig, ServerConfig


logger = logging.getLogger("uvicorn.error")


class DeviceUnavailableError(RuntimeError):
    """Raised when a device cannot be reached or woken."""


class EndpointUnavailableError(RuntimeError):
    """Raised when an awake device's inference endpoint is not ready."""


class WakeCoordinator:
    def __init__(
        self,
        server: ServerConfig,
        devices: dict[str, DeviceConfig],
        client: httpx.AsyncClient,
    ) -> None:
        self.server = server
        self.client = client
        self._locks = {device_name: asyncio.Lock() for device_name in devices}

    async def ensure_ready(self, device: DeviceConfig, endpoint: str) -> None:
        async with self._locks[device.name]:
            if device.ping is None:
                await self._ensure_without_ping(device, endpoint)
                return

            if await self._ping_once(device.ping, self.server.ping_timeout):
                logger.info("Device %s is awake; checking service %s", device.name, endpoint)
                if await self._wait_for_endpoint(endpoint, self.server.service_timeout):
                    logger.info("Service ready for device %s at %s", device.name, endpoint)
                    return
                raise EndpointUnavailableError(
                    f"Device {device.name} is awake, but {endpoint} did not become ready"
                )

            if device.mac is None:
                raise DeviceUnavailableError(
                    f"Device {device.name} is asleep or unreachable and has no MAC configured"
                )

            logger.info(
                "Device %s is offline; sending Wake-on-LAN to %s",
                device.name,
                device.mac,
            )
            await send_magic_packet(device.mac)

            if not await self._wait_for_ping(device.ping, self.server.wake_timeout):
                raise DeviceUnavailableError(
                    f"Device {device.name} did not respond after Wake-on-LAN"
                )

            logger.info("Device %s responded to ping; waiting for %s", device.name, endpoint)
            if not await self._wait_for_endpoint(endpoint, self.server.service_timeout):
                raise EndpointUnavailableError(
                    f"Device {device.name} woke, but {endpoint} did not become ready"
                )
            logger.info("Service ready for device %s at %s", device.name, endpoint)

    async def _ensure_without_ping(
        self, device: DeviceConfig, endpoint: str
    ) -> None:
        if await self._endpoint_ready(endpoint, self.server.connect_timeout):
            logger.info("Service ready for device %s at %s", device.name, endpoint)
            return

        if device.mac is not None:
            logger.info(
                "Service for device %s is unreachable; sending Wake-on-LAN to %s",
                device.name,
                device.mac,
            )
            await send_magic_packet(device.mac)
            if await self._wait_for_endpoint(endpoint, self.server.wake_timeout):
                logger.info("Service ready for device %s at %s", device.name, endpoint)
                return
            raise DeviceUnavailableError(
                f"Device or endpoint {device.name} did not respond after Wake-on-LAN"
            )

        logger.info(
            "Service for device %s is unreachable; waiting for %s",
            device.name,
            endpoint,
        )
        if await self._wait_for_endpoint(endpoint, self.server.service_timeout):
            logger.info("Service ready for device %s at %s", device.name, endpoint)
            return
        raise EndpointUnavailableError(f"Endpoint {endpoint} is unavailable")

    async def _wait_for_ping(self, host: str, timeout: float) -> bool:
        return await self._poll_until(
            lambda remaining: self._ping_once(
                host, min(self.server.ping_timeout, remaining)
            ),
            timeout,
        )

    async def _wait_for_endpoint(self, endpoint: str, timeout: float) -> bool:
        return await self._poll_until(
            lambda remaining: self._endpoint_ready(
                endpoint, min(self.server.connect_timeout, remaining)
            ),
            timeout,
        )

    async def _poll_until(
        self,
        check: Callable[[float], Awaitable[bool]],
        timeout: float,
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if await check(remaining):
                return True

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(self.server.ping_interval, remaining))

    async def _ping_once(self, host: str, timeout: float) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                "ping",
                "-n",
                "-c",
                "1",
                "-W",
                str(max(1, math.ceil(timeout))),
                host,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.error("ping executable is not installed")
            return False

        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            await _stop_process(process)
            return False
        except BaseException:
            await _stop_process(process)
            raise
        return process.returncode == 0

    async def _endpoint_ready(self, endpoint: str, timeout: float) -> bool:
        if timeout <= 0:
            return False
        try:
            async with self.client.stream(
                "GET",
                f"{endpoint}/models",
                timeout=httpx.Timeout(timeout),
            ) as response:
                return 200 <= response.status_code < 300
        except httpx.RequestError:
            return False


async def send_magic_packet(mac: str, packets: int = 3) -> None:
    mac_bytes = bytes.fromhex(mac.replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        for packet_number in range(packets):
            await loop.sock_sendto(sock, packet, ("255.255.255.255", 9))
            if packet_number + 1 < packets:
                await asyncio.sleep(0.1)
    finally:
        sock.close()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()
