from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import aiosqlite

from app.config import DeviceConfig


@dataclass(frozen=True, slots=True)
class ModelRoute:
    model_id: str
    device: str
    endpoint: str


class RouteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connection: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.path)
        await self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS models (
                model_id TEXT PRIMARY KEY,
                device TEXT NOT NULL,
                endpoint TEXT NOT NULL
            )
            """
        )
        await self.connection.commit()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    async def synchronize_configuration(
        self, devices: dict[str, DeviceConfig]
    ) -> int:
        cursor = await self.connection.execute(
            "SELECT model_id, device, endpoint FROM models"
        )
        rows = await cursor.fetchall()
        await cursor.close()

        stale_model_ids = [
            model_id
            for model_id, device_name, endpoint in rows
            if device_name not in devices
            or endpoint not in devices[device_name].endpoints
        ]
        if stale_model_ids:
            await self.connection.executemany(
                "DELETE FROM models WHERE model_id = ?",
                ((model_id,) for model_id in stale_model_ids),
            )
            await self.connection.commit()
        return len(stale_model_ids)

    async def synchronize_endpoint_models(
        self,
        device: str,
        endpoint: str,
        model_ids: Iterable[str],
    ) -> tuple[int, int]:
        unique_model_ids = tuple(dict.fromkeys(model_ids))
        cursor = await self.connection.execute(
            "SELECT model_id FROM models WHERE device = ? AND endpoint = ?",
            (device, endpoint),
        )
        previous_model_ids = {row[0] for row in await cursor.fetchall()}
        await cursor.close()
        current_model_ids = set(unique_model_ids)

        await self.connection.execute("BEGIN")
        try:
            if unique_model_ids:
                placeholders = ", ".join("?" for _ in unique_model_ids)
                await self.connection.execute(
                    f"""
                    DELETE FROM models
                    WHERE device = ? AND endpoint = ?
                      AND model_id NOT IN ({placeholders})
                    """,
                    (device, endpoint, *unique_model_ids),
                )
            else:
                await self.connection.execute(
                    "DELETE FROM models WHERE device = ? AND endpoint = ?",
                    (device, endpoint),
                )

            await self.connection.executemany(
                """
                INSERT INTO models (model_id, device, endpoint)
                VALUES (?, ?, ?)
                ON CONFLICT(model_id) DO UPDATE SET
                    device = excluded.device,
                    endpoint = excluded.endpoint
                """,
                ((model_id, device, endpoint) for model_id in unique_model_ids),
            )
            await self.connection.commit()
        except Exception:
            await self.connection.rollback()
            raise
        return (
            len(current_model_ids - previous_model_ids),
            len(previous_model_ids - current_model_ids),
        )

    async def load_routes(self) -> dict[str, ModelRoute]:
        cursor = await self.connection.execute(
            "SELECT model_id, device, endpoint FROM models ORDER BY model_id"
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {
            model_id: ModelRoute(
                model_id=model_id,
                device=device,
                endpoint=endpoint,
            )
            for model_id, device, endpoint in rows
        }

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Route database is not open")
        return self._connection
