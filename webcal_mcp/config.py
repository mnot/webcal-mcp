"""Configuration loading."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

DEFAULT_CONFIG_PATHS = [
    Path.home() / ".config" / "webcal-mcp" / "config.toml",
    Path.home() / ".webcal-mcp.toml",
]

DEFAULT_TTL_SECONDS = 900


@dataclass(frozen=True)
class CalendarConfig:
    name: str
    source: str = "ics"
    url: str = ""
    identifier: str = ""
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    description: str = ""

    @property
    def http_url(self) -> str:
        """Normalize webcal:// to https://."""
        if self.url.startswith("webcal://"):
            return "https://" + self.url[len("webcal://") :]
        if self.url.startswith("webcals://"):
            return "https://" + self.url[len("webcals://") :]
        return self.url


@dataclass(frozen=True)
class Config:
    calendars: dict[str, CalendarConfig] = field(default_factory=dict)
    default_ttl_seconds: int = DEFAULT_TTL_SECONDS
    http_timeout_seconds: float = 30.0
    user_agent: str = "webcal-mcp/0.1"

    @property
    def default_calendar(self) -> str | None:
        """The unambiguous default calendar name, or None if there's a choice to make."""
        if len(self.calendars) == 1:
            return next(iter(self.calendars))
        return None


def resolve_config_path() -> Path:
    """Locate the config file: WEBCAL_MCP_CONFIG, else the first default path."""
    if env := os.environ.get("WEBCAL_MCP_CONFIG"):
        return Path(env).expanduser()
    for path in DEFAULT_CONFIG_PATHS:
        if path.exists():
            return path
    raise FileNotFoundError(
        "No webcal-mcp config found. Set WEBCAL_MCP_CONFIG or create " f"{DEFAULT_CONFIG_PATHS[0]}."
    )


def load_config(path: Path | None = None) -> Config:
    cfg_path = path if path is not None else resolve_config_path()
    with cfg_path.open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)

    default_ttl = int(raw.get("default_ttl_seconds", DEFAULT_TTL_SECONDS))
    timeout = float(raw.get("http_timeout_seconds", 30.0))
    user_agent = str(raw.get("user_agent", "webcal-mcp/0.1"))

    calendars: dict[str, CalendarConfig] = {}
    raw_cals = raw.get("calendars", {})
    if not isinstance(raw_cals, dict):
        raise ValueError("[calendars] must be a table")
    for name, entry in raw_cals.items():
        if not isinstance(entry, dict):
            raise ValueError(f"calendar {name!r} must be a table")
        source = str(entry.get("source", "ics"))
        if source == "ics":
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError(f"calendar {name!r} missing 'url'")
            identifier = ""
        elif source == "eventkit":
            url = ""
            identifier = str(entry.get("identifier") or name)
        else:
            raise ValueError(
                f"calendar {name!r} has unknown source {source!r} " "(expected 'ics' or 'eventkit')"
            )
        calendars[name] = CalendarConfig(
            name=name,
            source=source,
            url=url,
            identifier=identifier,
            ttl_seconds=int(entry.get("ttl_seconds", default_ttl)),
            description=str(entry.get("description", "")),
        )

    if not calendars:
        raise ValueError(f"No [calendars.*] entries found in {cfg_path}")

    return Config(
        calendars=calendars,
        default_ttl_seconds=default_ttl,
        http_timeout_seconds=timeout,
        user_agent=user_agent,
    )
