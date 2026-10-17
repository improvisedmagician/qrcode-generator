"""Configuração da API do dashboard."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class Settings:
    pg_dsn: str = field(default_factory=lambda: _env("PG_DSN", "postgresql://app:app@localhost:5432/qr"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://localhost:6379/0"))
    ch_host: str = field(default_factory=lambda: _env("CH_HOST", "localhost"))
    ch_port: int = field(default_factory=lambda: int(_env("CH_PORT", "8123")))
    ch_user: str = field(default_factory=lambda: _env("CH_USER", "default"))
    ch_password: str = field(default_factory=lambda: _env("CH_PASSWORD", ""))
    ch_database: str = field(default_factory=lambda: _env("CH_DATABASE", "analytics"))
    jwt_secret: str = field(default_factory=lambda: _env("JWT_SECRET", "dev-only-change-me-dev-only-change-me"))
    jwt_issuer: str = "qr-platform"
    access_ttl_minutes: int = 15
    refresh_ttl_days: int = 30
    default_domain: str = field(default_factory=lambda: _env("DEFAULT_DOMAIN", "localhost"))
    short_url_scheme: str = field(default_factory=lambda: _env("SHORT_URL_SCHEME", "http"))
    short_url_port: str = field(default_factory=lambda: _env("SHORT_URL_PORT", ""))
    platform_hosts: tuple[str, ...] = field(default_factory=lambda: tuple(
        h.strip() for h in _env("PLATFORM_HOSTS", "localhost,qr.meudominio.com").split(",")))
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        _env("CORS_ORIGINS", "http://localhost:3000").split(",")))
    seed_demo: bool = field(default_factory=lambda: _env("SEED_DEMO", "0") == "1")
    custom_domain_cname_target: str = field(default_factory=lambda: _env(
        "CUSTOM_DOMAIN_TARGET", "custom.meudominio.com"))


settings = Settings()
