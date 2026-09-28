"""Tests for the health check endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator

from httpx import AsyncClient

from app.db import get_db
from app.main import app


async def test_liveness_probe(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_probe_reports_database_and_configuration(
    client: AsyncClient,
) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200

    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["configuration"]["ok"] is True
    assert body["checks"]["configuration"]["problems"] == []


async def test_readiness_reports_database_failure(client: AsyncClient) -> None:
    class BrokenSession:
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("database is down")

    async def broken_get_db() -> AsyncIterator[BrokenSession]:
        yield BrokenSession()

    previous = app.dependency_overrides[get_db]
    app.dependency_overrides[get_db] = broken_get_db
    try:
        response = await client.get("/readyz")
    finally:
        app.dependency_overrides[get_db] = previous

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"]["ok"] is False
    assert body["checks"]["database"]["error"] == "RuntimeError"

