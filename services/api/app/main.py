"""API do Dashboard Corporativo — autenticação, RBAC, CRUD de QR Codes com outbox e exportação."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import Settings, settings as default_settings
from .outbox import outbox_relay_loop
from .routers import analytics, auth, qrcodes, workspaces
from .seed import seed_demo_data

log = logging.getLogger("api")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def create_app(
    settings: Settings | None = None,
    *,
    pool: asyncpg.Pool | None = None,
    redis: Any = None,
    clickhouse: Any = None,
    connect: bool = True,
    run_relay: bool = True,
) -> FastAPI:
    cfg = settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal pool, redis, clickhouse
        relay_task = None

        if connect:
            try:
                pool = await asyncpg.create_pool(cfg.pg_dsn, min_size=2, max_size=20)
                log.info("Conectado ao PostgreSQL com sucesso.")
            except Exception as exc:
                log.error("Não foi possível conectar ao PostgreSQL: %r", exc)

            try:
                redis = aioredis.from_url(cfg.redis_url, decode_responses=True)
                log.info("Conectado ao Redis com sucesso.")
            except Exception as exc:
                log.error("Não foi possível conectar ao Redis: %r", exc)

            try:
                import clickhouse_connect
                clickhouse = clickhouse_connect.get_client(
                    host=cfg.ch_host,
                    port=cfg.ch_port,
                    username=cfg.ch_user,
                    password=cfg.ch_password,
                    database=cfg.ch_database,
                )
                log.info("Conectado ao ClickHouse com sucesso.")
            except Exception as exc:
                log.warning("ClickHouse indisponível (%r) — analytics retornará vazio", exc)

        app.state.pool = pool
        app.state.redis = redis
        app.state.clickhouse = clickhouse

        if pool is not None and cfg.seed_demo:
            try:
                await seed_demo_data(pool)
            except Exception as exc:
                log.warning("Erro no seed demo: %r", exc)

        if run_relay and pool is not None and redis is not None:
            relay_task = asyncio.create_task(outbox_relay_loop(pool, redis))

        try:
            yield
        finally:
            if relay_task:
                relay_task.cancel()
                await asyncio.gather(relay_task, return_exceptions=True)
            if pool:
                await pool.close()
            if redis and hasattr(redis, "aclose"):
                await redis.aclose()

    app = FastAPI(
        title="Plataforma SaaS de QR Codes Dinâmicos",
        version="1.0.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(cfg.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(auth.router)
    app.include_router(qrcodes.router)
    app.include_router(analytics.router)
    app.include_router(workspaces.router)

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "postgres": pool is not None,
            "redis": redis is not None,
            "clickhouse": clickhouse is not None,
        }

    return app


app = create_app()
