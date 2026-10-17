"""Redirect Service — hot path. Stateless, somente leitura e tolerante a falhas."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .capture import EventPipeline, build_event
from .config import Settings
from .resolver import PG_SQL, PgFetch, Resolver
from .rules import decide

log = logging.getLogger("redirect")
SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
NO_STORE = {"Cache-Control": "no-store, private"}  # o edge NÃO pode cachear (perderia scans)

DEEPLINK_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Abrindo…</title></head>
<body style="font-family:sans-serif;text-align:center;padding-top:30vh">
<a id="go" href="{web_attr}">Continuar</a>
<script>var w={web_js};location.href={app_js};setTimeout(function(){{location.replace(w)}},1200);</script>
</body></html>"""


async def _invalidation_listener(redis: Any, resolver: Resolver, channel: str) -> None:
    """A API publica a chave alterada; todos os pods limpam o L1 imediatamente."""
    while True:
        try:
            pubsub = redis.pubsub()
            await pubsub.subscribe(channel)
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    data = message["data"]
                    resolver.invalidate(data.decode() if isinstance(data, bytes) else data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("invalidation listener error: %r", exc)
            await asyncio.sleep(1)


def create_app(
    settings: Settings | None = None,
    *,
    redis: Any = None,
    pg_fetch: PgFetch | None = None,
    connect: bool = True,
) -> FastAPI:
    """`connect=False` + dependências injetadas permite testar sem infraestrutura."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal redis, pg_fetch
        pool = None
        if connect:
            import asyncpg
            import redis.asyncio as aioredis

            redis = aioredis.from_url(settings.redis_url, decode_responses=True,
                                      socket_connect_timeout=0.2, socket_timeout=0.5,
                                      health_check_interval=10)
            try:   # o serviço SOBE mesmo sem Postgres (cache/STALE seguram o tráfego)
                pool = await asyncpg.create_pool(settings.pg_dsn, min_size=1, max_size=10, timeout=2)

                async def pg_fetch(host: str, slug: str):  # noqa: F811
                    return await pool.fetchrow(PG_SQL, host, slug)
            except Exception as exc:
                log.error("postgres unavailable at startup: %r", exc)

        app.state.resolver = Resolver(redis, pg_fetch, l1_ttl=settings.l1_ttl,
                                      redis_timeout=settings.redis_timeout,
                                      pg_timeout=settings.pg_timeout)
        app.state.pipeline = EventPipeline(redis, stream=settings.stream,
                                           maxlen=settings.stream_maxlen,
                                           spool_dir=settings.spool_dir,
                                           queue_size=settings.queue_size)
        app.state.pipeline.start()
        listener = None
        if redis is not None and hasattr(redis, "pubsub"):
            listener = asyncio.create_task(
                _invalidation_listener(redis, app.state.resolver, settings.invalidate_channel))
        try:
            yield
        finally:
            if listener:
                listener.cancel()
                await asyncio.gather(listener, return_exceptions=True)
            await app.state.pipeline.stop()
            if pool is not None:
                await pool.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz(request: Request) -> dict:
        resolver: Resolver = request.app.state.resolver
        pipeline: EventPipeline = request.app.state.pipeline
        return {
            "ok": True,   # liveness: o serviço responde mesmo com dependências degradadas
            "redis_breaker_open": resolver.redis_breaker.is_open,
            "pg_breaker_open": resolver.pg_breaker.is_open,
            "l1_size": len(resolver.l1),
            "event_queue": pipeline.queue.qsize(),
            "spooled_events": pipeline.spooled,
        }

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/{slug}")
    async def redirect(slug: str, request: Request) -> Response:
        if not SLUG_RE.fullmatch(slug):
            return PlainTextResponse("Not found", status_code=404)

        host = request.headers.get("host", "").split(":")[0].lower()
        payload = await request.app.state.resolver.resolve(host, slug)
        if payload is None:
            return PlainTextResponse("QR Code não encontrado", status_code=404, headers=NO_STORE)

        headers = request.headers
        decision = decide(payload, headers.get("user-agent", ""),
                          headers.get(settings.country_header), datetime.now(timezone.utc))
        if decision.kind == "gone":
            return PlainTextResponse("QR Code indisponível", status_code=410, headers=NO_STORE)

        request.app.state.pipeline.capture(build_event(
            headers, request.client.host if request.client else None, payload,
            host=host, slug=slug, destination=decision.url or "",
            trusted_ip_header=settings.trusted_ip_header,
            country_header=settings.country_header,
        ))

        if decision.kind == "deeplink":
            body = DEEPLINK_HTML.format(web_attr=html.escape(decision.url or "", quote=True),
                                        web_js=json.dumps(decision.url),
                                        app_js=json.dumps(decision.app_url))
            return HTMLResponse(body, headers=NO_STORE)

        return RedirectResponse(decision.url or "/", status_code=302, headers={
            **NO_STORE, "Referrer-Policy": "no-referrer-when-downgrade"})

    return app


app = create_app()
