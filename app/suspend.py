from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.config import DeviceConfig


logger = logging.getLogger("uvicorn.error")


@dataclass(slots=True)
class _DeviceActivity:
    suspend_after: float | None
    idle_since: float
    active_requests: int = 0
    eligibility_logged: bool = False


class ActivityLease:
    def __init__(self, tracker: SuspendTracker, device: str) -> None:
        self._tracker = tracker
        self._device = device
        self._closed = False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.shield(self._tracker.finish_request(self._device))


class SuspendTracker:
    def __init__(
        self,
        devices: dict[str, DeviceConfig],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._lock = asyncio.Lock()
        now = clock()
        self._devices = {
            name: _DeviceActivity(
                suspend_after=device.suspend_after,
                idle_since=now,
            )
            for name, device in devices.items()
        }
        policies = [
            f"{name}={state.suspend_after:.3g}s"
            for name, state in self._devices.items()
            if state.suspend_after is not None
        ]
        logger.info(
            "Suspend policies: %s",
            ", ".join(policies) if policies else "none configured",
        )

    async def start_request(self, device: str) -> ActivityLease:
        async with self._lock:
            state = self._devices[device]
            state.active_requests += 1
            state.eligibility_logged = False
            if state.active_requests == 1:
                logger.info("Device %s marked active", device)
        return ActivityLease(self, device)

    async def finish_request(self, device: str) -> None:
        async with self._lock:
            state = self._devices[device]
            if state.active_requests <= 0:
                raise RuntimeError(f"Device {device} has no active request to finish")
            state.active_requests -= 1
            if state.active_requests == 0:
                state.idle_since = self._clock()
                state.eligibility_logged = False
                if state.suspend_after is None:
                    logger.info("Device %s is idle; automatic suspend is disabled", device)
                else:
                    logger.info(
                        "Device %s is idle; suspend eligible in %.3g second(s)",
                        device,
                        state.suspend_after,
                    )

    async def should_suspend(self, device: str) -> bool:
        async with self._lock:
            state = self._devices[device]
            should_suspend = (
                state.suspend_after is not None
                and state.active_requests == 0
                and self._clock() - state.idle_since >= state.suspend_after
            )
            if should_suspend and not state.eligibility_logged:
                logger.info("Device %s reached its AI-idle suspend timeout", device)
                state.eligibility_logged = True
            return should_suspend
