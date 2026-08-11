from httpx import AsyncClient

from tests.fakes import FakeRedis


async def test_health_is_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0"


async def test_readiness_reports_database_and_queue(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"database": "ok", "redis": "ok"},
        "queue": {"in_flight": 0, "retained": 0, "dead_lettered": 0},
    }


async def test_readiness_stays_ready_when_redis_is_down(
    client: AsyncClient, fake_redis: FakeRedis
) -> None:
    """Redis being down is degraded, not unavailable: the webhook falls back to
    in-process handling, so pulling the instance out of the load balancer would
    make an outage worse rather than better."""
    fake_redis.fail_with = ConnectionError("redis is gone")

    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"database": "ok", "redis": "degraded"}
    assert body["queue"] is None


async def test_openapi_schema_builds(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/health" in response.json()["paths"]
