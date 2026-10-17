import asyncio
import json
from datetime import datetime, timezone

import fakeredis
import pytest

from app.resolver import NOT_FOUND, Resolver

ROW = {
    "id": "11111111-1111-1111-1111-111111111111",
    "workspace_id": "22222222-2222-2222-2222-222222222222",
    "status": "active", "default_url": "https://destino.com", "fallback_url": None,
    "expires_at": datetime(2030, 1, 1, tzinfo=timezone.utc), "utm_params": None,
    "version": 3, "rules": "[]",
}


class BrokenRedis:
    """Simula Redis fora do ar."""
    async def get(self, *_a, **_k):
        raise ConnectionError("redis down")

    async def set(self, *_a, **_k):
        raise ConnectionError("redis down")


class PgStub:
    def __init__(self, row=ROW, delay=0.0, fail=False):
        self.row, self.delay, self.fail, self.calls = row, delay, fail, 0

    async def __call__(self, host, slug):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("pg down")
        return self.row


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


async def test_redis_hit_skips_postgres(redis):
    await redis.set("qr:h:abcd", json.dumps({"qr_id": "x", "default_url": "https://a"}))
    pg = PgStub()
    r = Resolver(redis, pg)
    assert (await r.resolve("h", "abcd"))["qr_id"] == "x"
    assert pg.calls == 0


async def test_redis_miss_falls_back_to_pg_and_writes_through(redis):
    pg = PgStub()
    r = Resolver(redis, pg)
    data = await r.resolve("h", "abcd")
    assert data["default_url"] == "https://destino.com"
    assert data["expires_at"].startswith("2030-01-01")
    await asyncio.sleep(0.01)  # write-through é em background
    assert json.loads(await redis.get("qr:h:abcd"))["v"] == 3


async def test_singleflight_prevents_stampede(redis):
    pg = PgStub(delay=0.05)
    r = Resolver(redis, pg)
    results = await asyncio.gather(*[r.resolve("h", "abcd") for _ in range(100)])
    assert pg.calls == 1
    assert all(res["v"] == 3 for res in results)


async def test_not_found_is_negatively_cached(redis):
    pg = PgStub(row=None)
    r = Resolver(redis, pg)
    assert await r.resolve("h", "nope") is None
    assert await r.resolve("h", "nope") is None
    assert pg.calls == 1
    await asyncio.sleep(0.01)
    assert await redis.get("qr:h:nope") == NOT_FOUND


async def test_serves_stale_when_redis_and_pg_are_down():
    pg = PgStub()
    r = Resolver(BrokenRedis(), pg)
    assert (await r.resolve("h", "abcd"))["v"] == 3      # aquece via PG
    r.l1.clear()                                          # TTL do L1 expirou
    pg.fail = True                                        # agora o PG também caiu
    data = await r.resolve("h", "abcd")
    assert data is not None and data["v"] == 3            # servido da cópia STALE


async def test_redis_breaker_opens_and_stops_calling_redis():
    pg = PgStub()
    r = Resolver(BrokenRedis(), pg)
    for i in range(6):
        await r.resolve("h", f"slug{i}")
    assert r.redis_breaker.is_open


async def test_redis_timeout_is_bounded():
    class SlowRedis:
        async def get(self, *_):
            await asyncio.sleep(5)

        async def set(self, *_a, **_k):
            pass

    r = Resolver(SlowRedis(), PgStub(), redis_timeout=0.05)
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await r.resolve("h", "abcd") is not None
    assert loop.time() - start < 0.5


async def test_invalidate_drops_l1(redis):
    pg = PgStub()
    r = Resolver(redis, pg)
    await r.resolve("h", "abcd")
    r.invalidate("qr:h:abcd")
    assert "qr:h:abcd" not in r.l1
