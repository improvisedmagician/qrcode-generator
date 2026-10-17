"""Captura assíncrona de scans: fila em memória → Redis Streams (lotes) → spool em disco (fallback).

O handler HTTP só chama `EventPipeline.capture()`, que é O(1) e nunca bloqueia nem lança.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("redirect.capture")

MAX_BATCH = 500
FLUSH_INTERVAL = 0.05


def client_ip(headers: Mapping[str, str], peer: str | None, trusted_header: str) -> str:
    """Confia apenas no header injetado pelo edge; nunca em X-Forwarded-For arbitrário."""
    return headers.get(trusted_header) or peer or ""


def build_event(
    headers: Mapping[str, str],
    peer: str | None,
    payload: dict,
    *,
    host: str,
    slug: str,
    destination: str,
    trusted_ip_header: str,
    country_header: str,
) -> dict[str, str]:
    """Extrai os dados do request. Todos os valores são str (exigência do Redis Streams)."""
    return {
        "event_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "qr_id": payload["qr_id"],
        "workspace_id": payload["workspace_id"],
        "host": host,
        "slug": slug,
        "destination_url": destination[:2048],
        "ip": client_ip(headers, peer, trusted_ip_header),  # só p/ GeoIP no worker; descartado lá
        "ua": headers.get("user-agent", "")[:512],
        "referer": headers.get("referer", "")[:512],
        "lang": headers.get("accept-language", "")[:64],
        "prefetch": "1" if (headers.get("sec-purpose") or headers.get("purpose")) else "0",
        "geo_country": headers.get(country_header, "")[:2],
    }


class EventPipeline:
    def __init__(self, redis: Any, *, stream: str, maxlen: int, spool_dir: Path, queue_size: int):
        self.redis, self.stream, self.maxlen = redis, stream, maxlen
        self.spool_dir = spool_dir
        self.queue: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=queue_size)
        self.spooled = 0
        self._pending: list[dict[str, str]] = []   # lote retirado da fila e ainda não confirmado
        self._tasks: list[asyncio.Task] = []

    # ---------- hot path ----------
    def capture(self, event: dict[str, str]) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            self._spool([event])

    # ---------- ciclo de vida ----------
    def start(self) -> None:
        self._tasks = [asyncio.create_task(self._flusher()), asyncio.create_task(self._replayer())]

    async def stop(self) -> None:
        """Graceful shutdown: envia o lote em andamento + fila; o que falhar vai para o disco."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        remaining, self._pending = self._pending, []
        while not self.queue.empty():
            remaining.append(self.queue.get_nowait())
        if remaining and not await self._send(remaining):
            self._spool(remaining)

    # ---------- envio ----------
    async def _send(self, batch: list[dict[str, str]]) -> bool:
        if self.redis is None:
            return False
        try:
            async with self.redis.pipeline(transaction=False) as pipe:
                for event in batch:
                    pipe.xadd(self.stream, event, maxlen=self.maxlen, approximate=True)
                await asyncio.wait_for(pipe.execute(), timeout=1.0)
            return True
        except Exception as exc:
            log.warning("stream unavailable (%d events): %r", len(batch), exc)
            return False

    async def _fill_pending(self) -> None:
        """Bloqueia até o 1º evento, espera a janela de lote e drena sem risco de perda."""
        self._pending.append(await self.queue.get())
        await asyncio.sleep(FLUSH_INTERVAL)
        while len(self._pending) < MAX_BATCH and not self.queue.empty():
            self._pending.append(self.queue.get_nowait())

    async def _flusher(self) -> None:
        while True:
            try:
                await self._fill_pending()
                batch = self._pending
                if not await self._send(batch):
                    self._spool(batch)
                self._pending = []
            except asyncio.CancelledError:
                break

    # ---------- spool em disco ----------
    def _spool_file(self) -> Path:
        return self.spool_dir / f"spool-{os.getpid()}.jsonl"

    def _spool(self, batch: list[dict[str, str]]) -> None:
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            with open(self._spool_file(), "a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(e) + "\n" for e in batch)
            self.spooled += len(batch)
        except OSError as exc:                      # último recurso: log estruturado
            log.error("spool failed, events lost=%d: %r", len(batch), exc)

    async def replay_spool(self) -> int:
        """Reenvia arquivos do spool. Retorna quantos eventos foram reenviados."""
        sent = 0
        for file in sorted(self.spool_dir.glob("spool-*.jsonl")):
            processing = file.with_name(f"{file.stem}.{uuid.uuid4().hex}.replaying")
            try:
                file.rename(processing)
            except OSError:
                continue
            lines = processing.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines if line.strip()]
            for i in range(0, len(events), MAX_BATCH):
                chunk = events[i:i + MAX_BATCH]
                if not await self._send(chunk):
                    self._spool(events[i:])        # devolve o que faltou ao spool
                    processing.unlink(missing_ok=True)
                    return sent
                sent += len(chunk)
            processing.unlink(missing_ok=True)
        return sent

    async def _replayer(self) -> None:
        while True:
            try:
                await asyncio.sleep(10)
                if self.spool_dir.exists():
                    await self.replay_spool()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("spool replay error: %r", exc)
