"""Enriquecimento puro (sem I/O de rede): GeoIP, User-Agent, bots e pseudonimização."""
from __future__ import annotations

import hashlib
import ipaddress
import uuid
from datetime import datetime
from typing import Any, Protocol

from user_agents import parse as parse_ua

BOT_HINTS = ("bot", "crawler", "spider", "preview", "whatsapp", "facebookexternalhit",
             "slackbot", "telegrambot", "discordbot", "linkedinbot", "headless")

COLUMNS = ["event_id", "ts", "workspace_id", "qr_id", "host", "slug", "destination_url",
           "visitor_hash", "country", "region", "city", "device_type", "os", "os_version",
           "browser", "referer", "language", "is_bot"]


class GeoReader(Protocol):
    def city(self, ip: str) -> Any: ...


def lookup_geo(reader: GeoReader | None, ip: str) -> tuple[str, str, str]:
    if reader is None or not ip:
        return "", "", ""
    try:
        if not ipaddress.ip_address(ip).is_global:
            return "", "", ""
        g = reader.city(ip)
        return (g.country.iso_code or "", g.subdivisions.most_specific.name or "",
                g.city.name or "")
    except Exception:                      # AddressNotFoundError, ValueError...
        return "", "", ""


def visitor_hash(salt: str, workspace_id: str, ip: str, ua: str) -> str:
    """Hash estável por workspace → scans únicos sem armazenar IP (LGPD)."""
    return hashlib.sha256(f"{salt}:{workspace_id}:{ip}:{ua}".encode()).hexdigest()


def enrich(event: dict[str, str], *, salt: str, geo: GeoReader | None = None) -> list:
    ua_raw = event.get("ua", "")
    ua = parse_ua(ua_raw)
    lowered = ua_raw.lower()
    is_bot = bool(ua.is_bot or event.get("prefetch") == "1" or not ua_raw
                  or any(hint in lowered for hint in BOT_HINTS))
    if is_bot:
        device = "bot"
    elif ua.is_tablet:
        device = "tablet"
    elif ua.is_mobile:
        device = "mobile"
    else:
        device = "desktop"

    ip = event.get("ip", "")
    country, region, city = lookup_geo(geo, ip)
    country = country or event.get("geo_country", "")
    lang = event.get("lang", "").split(",")[0].split(";")[0].strip()[:16]

    return [
        uuid.UUID(event["event_id"]),
        datetime.fromisoformat(event["ts"]),
        uuid.UUID(event["workspace_id"]),
        uuid.UUID(event["qr_id"]),
        event.get("host", ""),
        event.get("slug", ""),
        event.get("destination_url", ""),
        visitor_hash(salt, event["workspace_id"], ip, ua_raw),
        country, region, city,
        device,
        ua.os.family, ua.os.version_string, ua.browser.family,
        event.get("referer", ""),
        lang,
        int(is_bot),
    ]
