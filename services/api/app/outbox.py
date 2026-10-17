"""Transactional Outbox Relay: sincroniza alterações do PostgreSQL para o Redis.

Garante que toda mutação (criação, edição, exclusão, pausa de QR Code) seja:
1. Gravada no PostgreSQL na mesma transação que o registro de outbox;
2. Processada pelo Relay e gravada no Redis ('qr:{host}:{slug}');
3. Notificada via Redis Pub/Sub ('qr:invalidate') para todos os pods do Redirect limparem seu cache L1 imediatamente.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import asyncpg
import redis.asyncio as aioredis

log = logging.getLogger("api.outbox")

PG_PAYLOAD_SQL = """
SELECT q.id, q.workspace_id, q.status, q.default_url, q.fallback_url, q.expires_at,
       q.utm_params, q.version, d.hostname, q.slug,
       COALESCE(json_agg(json_build_object(
         'priority', r.priority, 'starts_at', r.starts_at, 'ends_at', r.ends_at,
         'os', r.os, 'countries', r.countries, 'target_url', r.target_url,
         'deep_link', r.deep_link) ORDER BY r.priority) FILTER (WHERE r.id IS NOT NULL), '[]')
       AS rules
FROM qrcodes q
JOIN domains d ON d.id = q.domain_id
LEFT JOIN qr_rules r ON r.qrcode_id = q.id
WHERE q.id = $1 AND q.deleted_at IS NULL
GROUP BY q.id, d.hostname
"""


def _cache_key(host: str, slug: str) -> str:
    return f"qr:{host.lower()}:{slug}"


async def write_outbox_event(conn: asyncpg.Connection, event_type: str, payload: dict) -> int:
    """Insere evento na tabela outbox_events dentro da transação atual."""
    return await conn.fetchval(
        """INSERT INTO outbox_events (event_type, payload)
           VALUES ($1, $2::jsonb)
           RETURNING id""",
        event_type,
        json.dumps(payload),
    )


async def sync_qr_to_redis(
    conn: asyncpg.Connection,
    redis: aioredis.Redis,
    qr_id: uuid.UUID,
    invalidate_channel: str = "qr:invalidate",
) -> None:
    """Recarrega os dados do QR code do PostgreSQL e atualiza o Redis com invalidação imediata."""
    row = await conn.fetchrow(PG_PAYLOAD_SQL, qr_id)
    if row is None:
        return
    host = row["hostname"]
    slug = row["slug"]
    key = _cache_key(host, slug)

    expires = row["expires_at"]
    payload = {
        "v": row["version"],
        "qr_id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "status": row["status"],
        "default_url": row["default_url"],
        "fallback_url": row["fallback_url"],
        "expires_at": expires.isoformat() if expires else None,
        "utm": json.loads(row["utm_params"]) if isinstance(row["utm_params"], str) else row["utm_params"],
        "rules": json.loads(row["rules"]) if isinstance(row["rules"], str) else (row["rules"] or []),
    }

    async with redis.pipeline(transaction=False) as pipe:
        pipe.set(key, json.dumps(payload), ex=86400)
        pipe.publish(invalidate_channel, key)
        await pipe.execute()


async def delete_qr_from_redis(
    redis: aioredis.Redis,
    host: str,
    slug: str,
    invalidate_channel: str = "qr:invalidate",
) -> None:
    key = _cache_key(host, slug)
    async with redis.pipeline(transaction=False) as pipe:
        pipe.delete(key)
        pipe.publish(invalidate_channel, key)
        await pipe.execute()


async def process_pending_outbox(
    pool: asyncpg.Pool,
    redis: aioredis.Redis,
    batch_size: int = 50,
    invalidate_channel: str = "qr:invalidate",
) -> int:
    """Processa lote de eventos pendentes na tabela outbox_events."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT id, event_type, payload
               FROM outbox_events
               WHERE processed_at IS NULL
               ORDER BY id ASC
               LIMIT $1
               FOR UPDATE SKIP LOCKED""",
            batch_size,
        )
        if not rows:
            return 0

        for r in rows:
            event_id = r["id"]
            event_type = r["event_type"]
            data = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]

            try:
                if event_type in ("qr.upserted", "qr.updated", "qr.created"):
                    qr_id = uuid.UUID(data["qr_id"])
                    await sync_qr_to_redis(conn, redis, qr_id, invalidate_channel)
                elif event_type == "qr.deleted":
                    await delete_qr_from_redis(redis, data["host"], data["slug"], invalidate_channel)

                await conn.execute(
                    "UPDATE outbox_events SET processed_at = now() WHERE id = $1",
                    event_id,
                )
            except Exception as exc:
                log.error("Erro ao processar outbox event #%s: %r", event_id, exc)
                await conn.execute(
                    "UPDATE outbox_events SET attempts = attempts + 1 WHERE id = $1",
                    event_id,
                )

        return len(rows)


async def outbox_relay_loop(
    pool: asyncpg.Pool,
    redis: aioredis.Redis,
    poll_interval: float = 0.5,
    invalidate_channel: str = "qr:invalidate",
) -> None:
    """Loop contínuo em background executando o outbox relay."""
    log.info("Iniciando outbox relay loop...")
    while True:
        try:
            processed = await process_pending_outbox(pool, redis, invalidate_channel=invalidate_channel)
            if processed == 0:
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            log.warning("Falha temporária no outbox relay loop: %r", exc)
            await asyncio.sleep(poll_interval * 2)
