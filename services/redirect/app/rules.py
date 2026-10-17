"""Motor de regras puro (sem I/O): agendamento, SO, país, expiração, UTM e deep link."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_IOS = re.compile(r"iPhone|iPad|iPod")


def detect_os(user_agent: str) -> str:
    """Detecção leve para o hot path (o parsing completo acontece no worker)."""
    if _IOS.search(user_agent):
        return "ios"
    if "Android" in user_agent:
        return "android"
    if "Windows" in user_agent:
        return "windows"
    if "Mac OS X" in user_agent:
        return "macos"
    if "Linux" in user_agent:
        return "linux"
    return "other"


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rule_matches(rule: dict, os_name: str, country: str | None, now: datetime) -> bool:
    start, end = parse_ts(rule.get("starts_at")), parse_ts(rule.get("ends_at"))
    if start and now < start:
        return False
    if end and now >= end:
        return False
    if rule.get("os") and os_name not in rule["os"]:
        return False
    if rule.get("countries") and (country or "").upper() not in rule["countries"]:
        return False
    return True


def pick_rule(payload: dict, os_name: str, country: str | None, now: datetime) -> dict | None:
    rules = sorted(payload.get("rules") or [], key=lambda r: r.get("priority", 100))
    return next((r for r in rules if _rule_matches(r, os_name, country, now)), None)


def with_utm(url: str, utm: dict | None) -> str:
    """Acrescenta UTMs sem sobrescrever parâmetros já presentes no destino."""
    if not utm:
        return url
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in utm.items():
        query.setdefault(key, str(value))
    return urlunsplit(parts._replace(query=urlencode(query)))


@dataclass(frozen=True)
class Decision:
    kind: str                 # "redirect" | "deeplink" | "gone"
    url: str | None = None
    app_url: str | None = None


def decide(payload: dict, user_agent: str, country: str | None, now: datetime) -> Decision:
    expires_at = parse_ts(payload.get("expires_at"))
    if payload.get("status") != "active" or (expires_at and now >= expires_at):
        fallback = payload.get("fallback_url")
        return Decision("redirect", fallback) if fallback else Decision("gone")

    os_name = detect_os(user_agent)
    rule = pick_rule(payload, os_name, country, now)
    target = with_utm(rule["target_url"] if rule else payload["default_url"], payload.get("utm"))

    deep_link = (rule or {}).get("deep_link") or {}
    if os_name in deep_link:
        return Decision("deeplink", target, deep_link[os_name])
    return Decision("redirect", target)
