"""Configuração via variáveis de ambiente (12-factor)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    pg_dsn: str = field(default_factory=lambda: os.getenv(
        "PG_REPLICA_DSN", "postgresql://app:app@localhost:5432/qr"))
    stream: str = field(default_factory=lambda: os.getenv("SCAN_STREAM", "scans"))
    stream_maxlen: int = field(default_factory=lambda: int(os.getenv("SCAN_STREAM_MAXLEN", "5000000")))
    spool_dir: Path = field(default_factory=lambda: Path(os.getenv("SPOOL_DIR", "/tmp/spool" if os.getenv("VERCEL") else "./spool")))
    trusted_ip_header: str = field(default_factory=lambda: os.getenv(
        "TRUSTED_IP_HEADER", "cf-connecting-ip").lower())
    country_header: str = field(default_factory=lambda: os.getenv(
        "COUNTRY_HEADER", "cf-ipcountry").lower())
    l1_ttl: int = field(default_factory=lambda: int(os.getenv("L1_TTL_SECONDS", "30")))
    redis_timeout: float = field(default_factory=lambda: float(os.getenv("REDIS_TIMEOUT", "0.05")))
    pg_timeout: float = field(default_factory=lambda: float(os.getenv("PG_TIMEOUT", "0.3")))
    queue_size: int = field(default_factory=lambda: int(os.getenv("EVENT_QUEUE_SIZE", "50000")))
    invalidate_channel: str = "qr:invalidate"


def cache_key(host: str, slug: str) -> str:
    return f"qr:{host}:{slug}"
