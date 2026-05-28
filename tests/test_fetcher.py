"""Tests for IcsHttpSource caching behaviour using httpx MockTransport."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from webcal_mcp.config import CalendarConfig
from webcal_mcp.fetcher import IcsHttpSource


def _make_source(
    handler: httpx.MockTransport, *, ttl_seconds: int = 900
) -> tuple[IcsHttpSource, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler)
    cfg = CalendarConfig(name="c", url="https://example.com/c.ics", ttl_seconds=ttl_seconds)
    return IcsHttpSource(cfg, client), client


@pytest.mark.asyncio
async def test_cached_within_ttl(simple_ics: bytes) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=simple_ics, headers={"etag": "v1"})

    source, client = _make_source(httpx.MockTransport(handler))
    try:
        window = (
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        await source.events(*window)
        await source.events(*window)
    finally:
        await client.aclose()
    assert len(calls) == 1  # second call served from cache


@pytest.mark.asyncio
async def test_etag_revalidation(simple_ics: bytes) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "If-None-Match" in request.headers:
            return httpx.Response(304)
        return httpx.Response(200, content=simple_ics, headers={"etag": "v1"})

    source, client = _make_source(httpx.MockTransport(handler), ttl_seconds=0)
    try:
        window = (
            datetime(2026, 6, 1, tzinfo=timezone.utc),
            datetime(2026, 6, 10, tzinfo=timezone.utc),
        )
        first = await source.events(*window)
        second = await source.events(*window)
    finally:
        await client.aclose()

    assert len(calls) == 2
    assert calls[1].headers.get("If-None-Match") == "v1"
    assert [e.uid for e in first] == [e.uid for e in second]


@pytest.mark.asyncio
async def test_get_event_uses_cache(simple_ics: bytes) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=simple_ics)

    source, client = _make_source(httpx.MockTransport(handler))
    try:
        event = await source.get_event("evt-2@test")
    finally:
        await client.aclose()

    assert event is not None
    assert event.summary == "Lunch with Alice"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_http_error_propagates() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    source, client = _make_source(httpx.MockTransport(handler))
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await source.events(
                datetime(2026, 6, 1, tzinfo=timezone.utc),
                datetime(2026, 6, 2, tzinfo=timezone.utc),
            )
    finally:
        await client.aclose()
