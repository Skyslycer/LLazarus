from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml


DEFAULT_APP_DATA = "/data"
_MAC_PATTERN = re.compile(r"^[0-9A-Fa-f]{12}$")


class ConfigError(ValueError):
    """Raised when config.yml cannot be created or is invalid."""


@dataclass(frozen=True, slots=True)
class AppPaths:
    directory: Path
    config: Path
    database: Path


@dataclass(frozen=True, slots=True)
class ServerConfig:
    port: int = 4000
    ping_timeout: float = 1.0
    ping_interval: float = 0.5
    wake_timeout: float = 30.0
    service_timeout: float = 60.0
    connect_timeout: float = 2.0
    read_timeout: float = 1800.0
    write_timeout: float = 30.0


@dataclass(frozen=True, slots=True)
class DeviceConfig:
    name: str
    endpoints: tuple[str, ...]
    ping: str | None = None
    mac: str | None = None


@dataclass(frozen=True, slots=True)
class RouterConfig:
    server: ServerConfig
    devices: dict[str, DeviceConfig]


def get_app_paths() -> AppPaths:
    directory = Path(os.environ.get("APP_DATA", DEFAULT_APP_DATA)).resolve()
    return AppPaths(
        directory=directory,
        config=directory / "config.yml",
        database=directory / "router.db",
    )


def load_config(path: Path | None = None) -> RouterConfig:
    config_path = path or get_app_paths().config
    if not config_path.is_file():
        try:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            template_path = Path(__file__).resolve().parent.parent / "config.example.yml"
            config_path.write_text(
                template_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except OSError as exc:
            raise ConfigError(
                f"Configuration file does not exist and could not be created at "
                f"{config_path}: {exc}"
            ) from exc

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Unable to read {config_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config.yml must contain a top-level mapping")

    server = _load_server(raw.get("server", {}))
    devices = _load_devices(raw.get("devices"))
    return RouterConfig(server=server, devices=devices)


def _load_server(raw: Any) -> ServerConfig:
    if not isinstance(raw, dict):
        raise ConfigError("server must be a mapping")

    allowed = set(ServerConfig.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"Unknown server setting(s): {', '.join(sorted(unknown))}")

    defaults = ServerConfig()
    port = _positive_int(raw.get("port", defaults.port), "server.port", maximum=65535)
    return ServerConfig(
        port=port,
        ping_timeout=_positive_float(
            raw.get("ping_timeout", defaults.ping_timeout), "server.ping_timeout"
        ),
        ping_interval=_positive_float(
            raw.get("ping_interval", defaults.ping_interval), "server.ping_interval"
        ),
        wake_timeout=_positive_float(
            raw.get("wake_timeout", defaults.wake_timeout), "server.wake_timeout"
        ),
        service_timeout=_positive_float(
            raw.get("service_timeout", defaults.service_timeout),
            "server.service_timeout",
        ),
        connect_timeout=_positive_float(
            raw.get("connect_timeout", defaults.connect_timeout),
            "server.connect_timeout",
        ),
        read_timeout=_positive_float(
            raw.get("read_timeout", defaults.read_timeout), "server.read_timeout"
        ),
        write_timeout=_positive_float(
            raw.get("write_timeout", defaults.write_timeout), "server.write_timeout"
        ),
    )


def _load_devices(raw: Any) -> dict[str, DeviceConfig]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("devices must be a mapping")

    devices: dict[str, DeviceConfig] = {}
    configured_endpoints: set[str] = set()

    for name, device_raw in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("Every device must have a non-empty string name")
        if not isinstance(device_raw, dict):
            raise ConfigError(f"devices.{name} must be a mapping")

        unknown = set(device_raw) - {"ping", "mac", "endpoints"}
        if unknown:
            raise ConfigError(
                f"Unknown setting(s) for device {name}: {', '.join(sorted(unknown))}"
            )

        endpoints_raw = device_raw.get("endpoints")
        if not isinstance(endpoints_raw, list) or not endpoints_raw:
            raise ConfigError(f"devices.{name}.endpoints must be a non-empty list")

        endpoints: list[str] = []
        for index, endpoint_raw in enumerate(endpoints_raw):
            endpoint = _normalize_endpoint(endpoint_raw, f"devices.{name}.endpoints[{index}]")
            if endpoint in configured_endpoints:
                raise ConfigError(f"Endpoint is configured more than once: {endpoint}")
            configured_endpoints.add(endpoint)
            endpoints.append(endpoint)

        ping = _optional_string(device_raw.get("ping"), f"devices.{name}.ping")
        mac = _normalize_mac(device_raw.get("mac"), f"devices.{name}.mac")
        devices[name] = DeviceConfig(
            name=name,
            endpoints=tuple(endpoints),
            ping=ping,
            mac=mac,
        )

    return devices


def _normalize_endpoint(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty URL")

    endpoint = value.strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{field} must be an absolute http:// or https:// URL")
    if parsed.query or parsed.fragment:
        raise ConfigError(f"{field} must not contain a query string or fragment")
    return endpoint


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} must be a non-empty string when provided")
    return value.strip()


def _normalize_mac(value: Any, field: str) -> str | None:
    mac = _optional_string(value, field)
    if mac is None:
        return None

    compact = mac.replace(":", "").replace("-", "").replace(".", "")
    if not _MAC_PATTERN.fullmatch(compact):
        raise ConfigError(f"{field} must be a valid 48-bit MAC address")
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2)).upper()


def _positive_float(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{field} must be a positive number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field} must be a positive number") from exc
    if parsed <= 0:
        raise ConfigError(f"{field} must be greater than zero")
    return parsed


def _positive_int(value: Any, field: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{field} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{field} must be no greater than {maximum}")
    return value
