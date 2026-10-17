"""Schemas Pydantic (contratos da API) e validação de URLs."""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from .config import settings

OsName = Literal["ios", "android", "windows", "macos", "linux", "other"]
Status = Literal["active", "paused", "expired", "archived"]


def validate_destination(url: str) -> str:
    """Somente http(s), host obrigatório e proibido apontar para a própria plataforma (loop)."""
    if re.search(r"[\r\n\t]", url):
        raise ValueError("URL não pode conter caracteres de controle (CRLF)")
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("URL deve ser http(s) com host válido")
    if parts.hostname.lower() in settings.platform_hosts:
        raise ValueError("destino não pode apontar para o domínio de redirecionamento")
    if len(url) > 2048:
        raise ValueError("URL muito longa")
    return url.strip()


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class Rule(BaseModel):
    priority: int = Field(100, ge=0, le=10_000)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    os: list[OsName] | None = None
    countries: list[str] | None = None
    target_url: str
    deep_link: dict[Literal["ios", "android"], str] | None = None

    @field_validator("target_url")
    @classmethod
    def _validate_target_url(cls, v: str) -> str:
        return validate_destination(v)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _tz(cls, v):
        return _aware(v)

    @field_validator("countries")
    @classmethod
    def _countries(cls, v):
        if v is None:
            return v
        if any(len(c) != 2 or not c.isalpha() for c in v):
            raise ValueError("use códigos ISO-3166 alfa-2 (ex.: BR)")
        return [c.upper() for c in v]

    @model_validator(mode="after")
    def _window(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at deve ser posterior a starts_at")
        return self


class Design(BaseModel):
    foreground: str = "#000000"
    background: str = "#FFFFFF"
    module_style: Literal["square", "dots", "rounded"] = "square"
    finder_style: Literal["square", "rounded", "dots"] = "square"
    frame_text: str | None = Field(None, max_length=32)
    frame_color: str | None = None
    frame_text_color: str = "#FFFFFF"
    logo_base64: str | None = Field(None, max_length=400_000)   # PoC; produção: chave no S3
    logo_scale: float = Field(0.2, gt=0, le=0.22)
    error_correction: Literal["L", "M", "Q", "H"] | None = None


class QRCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    default_url: str
    fallback_url: str | None = None
    hostname: str | None = None                  # domínio personalizado; padrão = plataforma
    slug: str | None = Field(None, pattern=r"^[A-Za-z0-9_-]{4,64}$")  # slug "vanity"
    folder_id: uuid.UUID | None = None
    tags: list[str] = Field(default_factory=list, max_length=20)
    utm: dict[str, str] | None = None
    expires_at: datetime | None = None
    design: Design = Field(default_factory=Design)
    rules: list[Rule] = Field(default_factory=list, max_length=50)

    @field_validator("default_url")
    @classmethod
    def _validate_default(cls, v: str) -> str:
        return validate_destination(v)

    @field_validator("fallback_url")
    @classmethod
    def _validate_fallback(cls, v: str | None) -> str | None:
        return validate_destination(v) if v else v

    @field_validator("expires_at")
    @classmethod
    def _validate_expires(cls, v: datetime | None) -> datetime | None:
        return _aware(v)


class QRUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    default_url: str | None = None
    fallback_url: str | None = None
    status: Status | None = None
    folder_id: uuid.UUID | None = None
    tags: list[str] | None = None
    utm: dict[str, str] | None = None
    expires_at: datetime | None = None
    design: Design | None = None
    rules: list[Rule] | None = None              # se enviado, substitui todas as regras

    @field_validator("default_url")
    @classmethod
    def _validate_default(cls, v: str | None) -> str | None:
        return validate_destination(v) if v else v

    @field_validator("fallback_url")
    @classmethod
    def _validate_fallback(cls, v: str | None) -> str | None:
        return validate_destination(v) if v else v

    @field_validator("expires_at")
    @classmethod
    def _validate_expires(cls, v: datetime | None) -> datetime | None:
        return _aware(v)


class QROut(BaseModel):
    id: uuid.UUID
    name: str
    short_url: str
    slug: str
    hostname: str
    default_url: str
    fallback_url: str | None
    status: Status
    folder_id: uuid.UUID | None
    tags: list[str]
    utm: dict[str, str] | None
    expires_at: datetime | None
    design: dict
    rules: list[dict]
    version: int
    created_at: datetime
    updated_at: datetime


class QRPage(BaseModel):
    items: list[QROut]
    next_cursor: str | None


class LoginIn(BaseModel):
    email: str
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class DomainIn(BaseModel):
    hostname: str = Field(
        min_length=4,
        max_length=253,
        pattern=r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
    )
