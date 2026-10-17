"""Consome Redis Streams, enriquece e grava em lote no ClickHouse (at-least-once + dedupe)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
from pathlib import Path

import redis.asyncio as aioredis

from .enrich import COLUMNS, enrich

log = logging.getLogger("analytics-worker")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

STREAM = os.getenv("SCAN_STREAM", "scans")
DLQ = f"{STREAM}:dlq"
GROUP = os.getenv("CONSUMER_GROUP", "analytics")
CONSUMER = os.getenv("CONSUMER_NAME", socket.gethostname())
BATCH = int(os.getenv("BATCH_SIZE", "2000"))


def open_geo():
    path = Path(os.getenv("GEOIP_DB", "/data/GeoLite2-City.mmdb"))
    if not path.exists():
        log.warning("GeoIP database not found at %s — cidade/estado ficarão vazios", path)
        return None
    import geoip2.database
    return geoip2.database.Reader(str(path))


def open_clickhouse():
    import clickhouse_connect
    return clickhouse_connect.get_client(
        host=os.getenv("CH_HOST", "localhost"), port=int(os.getenv("CH_PORT", "8123")),
        username=os.getenv("CH_USER", "default"), password=os.getenv("CH_PASSWORD", ""),
        database=os.getenv("CH_DATABASE", "analytics"))


async def process(rds, ch, msgs, *, salt: str, geo) -> None:
    rows, dead = [], []
    for msg_id, fields in msgs:
        try:
            rows.append(enrich(fields, salt=salt, geo=geo))
        except Exception as exc:                  # evento malformado → DLQ (não trava o stream)
            dead.append((msg_id, fields, repr(exc)))
    if rows:
        await asyncio.to_thread(ch.insert, "scans", rows, column_names=COLUMNS)
    for msg_id, fields, err in dead:
        await rds.xadd(DLQ, {"id": msg_id, "error": err, "event": json.dumps(fields)},
                       maxlen=100_000, approximate=True)
    # ACK só depois de persistir: se cair antes, a mensagem é reprocessada (dedupe por event_id)
    await rds.xack(STREAM, GROUP, *[m for m, _ in msgs])


async def run() -> None:
    salt = os.environ["VISITOR_SALT"]
    rds = aioredis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True)
    ch, geo = open_clickhouse(), open_geo()
    try:
        await rds.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except aioredis.ResponseError:
        pass                                      # grupo já existe

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:               # Windows
            pass

    log.info("consumer %s listening on %s/%s", CONSUMER, STREAM, GROUP)
    while not stop.is_set():
        try:
            # 1) recupera mensagens órfãs de consumidores que morreram (> 60 s pendentes)
            _, claimed, *_ = await rds.xautoclaim(STREAM, GROUP, CONSUMER,
                                                  min_idle_time=60_000, count=BATCH)
            if claimed:
                await process(rds, ch, [m for m in claimed if m and m[1]], salt=salt, geo=geo)
            # 2) mensagens novas
            resp = await rds.xreadgroup(GROUP, CONSUMER, {STREAM: ">"}, count=BATCH, block=1000)
            msgs = [m for _, batch in (resp or []) for m in batch]
            if msgs:
                await process(rds, ch, msgs, salt=salt, geo=geo)
                log.info("persisted %d scans", len(msgs))
        except Exception as exc:                  # ClickHouse/Redis fora: não ACK, tenta de novo
            log.error("batch failed, retrying: %r", exc)
            await asyncio.sleep(2)


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
