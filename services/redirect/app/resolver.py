"""Resolução slug → payload com cache multicamada, singleflight e circuit breakers.

Ordem: L1 (memória, TTL) → Redis → PostgreSQL (réplica) → STALE (última versão conhecida).
Nenhuma falha de dependência propaga exceção para o handler HTTP.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Mapping

from cachetools import LRUCache, TTLCache

from .config import cache_key

log = logging.getLogger("redirect.resolver")

NOT_FOUND = "__404__"
PgFetch = Callable[[str, str], Awaitable[Mapping[str, Any] | None]]

PG_SQL = """
SELECT q.id, q.workspace_id, q.status, q.default_url, q.fallback_url, q.expires_at,
       q.utm_params, q.version,
       COALESCE(json_agg(json_build_object(
         'priority', r.priority, 'starts_at', r.starts_at, 'ends_at', r.ends_at,
         'os', r.os, 'countries', r.countries, 'target_url', r.target_url,
         'deep_link', r.deep_link) ORDER BY r.priority) FILTER (WHERE r.id IS NOT NULL), '[]')
       AS rules
FROM qrcodes q
JOIN domains d ON d.id = q.domain_id AND d.hostname = $1 AND d.status = 'active'
LEFT JOIN qr_rules r ON r.qrcode_id = q.id
WHERE q.slug = $2 AND q.deleted_at IS NULL
GROUP BY q.id
"""


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def row_to_payload(row: Mapping[str, Any]) -> dict:
    expires = row["expires_at"]
    return {
        "v": row["version"],
        "qr_id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "status": row["status"],
        "default_url": row["default_url"],
        "fallback_url": row["fallback_url"],
        "expires_at": expires.isoformat() if expires else None,
        "utm": _json(row["utm_params"]),
        "rules": _json(row["rules"]) or [],
    }


class Breaker:
    """Circuit breaker mínimo: abre após N falhas consecutivas, meia-abertura após cooldown."""

    def __init__(self, threshold: int = 5, cooldown: float = 5.0) -> None:
        self.threshold, self.cooldown = threshold, cooldown
        self.fails, self.opened_at = 0, 0.0

    @property
    def is_open(self) -> bool:
        if self.fails < self.threshold:
            return False
        return (time.monotonic() - self.opened_at) < self.cooldown

    def success(self) -> None:
        self.fails = 0

    def failure(self) -> None:
        self.fails += 1
        if self.fails >= self.threshold:
            self.opened_at = time.monotonic()


class Resolver:
    def __init__(
        self,
        redis: Any,
        pg_fetch: PgFetch | None,
        *,
        l1_ttl: int = 30,
        redis_timeout: float = 0.05,
        pg_timeout: float = 0.3,
    ) -> None:
        self.redis, self.pg_fetch = redis, pg_fetch
        self.redis_timeout, self.pg_timeout = redis_timeout, pg_timeout
        self.l1: TTLCache = TTLCache(maxsize=200_000, ttl=l1_ttl)
        self.stale: LRUCache = LRUCache(maxsize=500_000)
        self.negative: TTLCache = TTLCache(maxsize=100_000, ttl=15)
        self.redis_breaker, self.pg_breaker = Breaker(), Breaker()
        self._inflight: dict[str, asyncio.Future] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---------- API pública ----------
    async def resolve(self, host: str, slug: str) -> dict | None:
        key = cache_key(host, slug)
        if (hit := self.l1.get(key)) is not None:
            return hit
        if key in self.negative:
            return None
        if (pending := self._inflight.get(key)) is not None:   # singleflight
            return await asyncio.shield(pending)

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = future
        try:
            data = await self._load(key, host, slug)
            future.set_result(data)
            return data
        except BaseException:          # _load não lança erros de I/O; só cancelamento chega aqui
            future.cancel()
            raise
        finally:
            self._inflight.pop(key, None)

    def invalidate(self, key: str) -> None:
        self.l1.pop(key, None)
        self.negative.pop(key, None)
        self.stale.pop(key, None)

    # ---------- internos ----------
    async def _load(self, key: str, host: str, slug: str) -> dict | None:
        data = await self._from_redis(key)
        if data is None:
            data = await self._from_postgres(key, host, slug)
        if data is None:                       # todas as fontes indisponíveis
            data = self.stale.get(key)
            if data is not None:
                log.warning("serving STALE payload for %s", key)
        if data is None or data == NOT_FOUND:
            self.negative[key] = True
            return None
        self.l1[key] = self.stale[key] = data
        return data

    async def _from_redis(self, key: str) -> dict | str | None:
        if self.redis is None or self.redis_breaker.is_open:
            return None
        try:
            raw = await asyncio.wait_for(self.redis.get(key), timeout=self.redis_timeout)
            self.redis_breaker.success()
        except Exception as exc:
            self.redis_breaker.failure()
            log.warning("redis GET failed (%s): %r", key, exc)
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return NOT_FOUND if raw == NOT_FOUND else json.loads(raw)

    async def _from_postgres(self, key: str, host: str, slug: str) -> dict | str | None:
        if self.pg_fetch is None or self.pg_breaker.is_open:
            return None
        try:
            row = await asyncio.wait_for(self.pg_fetch(host, slug), timeout=self.pg_timeout)
            self.pg_breaker.success()
        except Exception as exc:
            self.pg_breaker.failure()
            log.error("postgres fallback failed (%s): %r", key, exc)
            return None
        if row is None:
            self._background(self._redis_set(key, NOT_FOUND, 60))
            return NOT_FOUND
        payload = row_to_payload(row)
        self._background(self._redis_set(key, json.dumps(payload), 86_400))
        return payload

    async def _redis_set(self, key: str, value: str, ttl: int) -> None:
        if self.redis is None or self.redis_breaker.is_open:
            return
        try:
            await asyncio.wait_for(self.redis.set(key, value, ex=ttl), timeout=0.1)
        except Exception:
            pass                                    # best-effort

    def _background(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
