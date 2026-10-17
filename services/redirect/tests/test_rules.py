from datetime import datetime, timezone

from app.rules import decide, detect_os, pick_rule, with_utm

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"
ANDROID = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/120 Mobile"
WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120"

BLACK_FRIDAY = {
    "priority": 10,
    "starts_at": "2026-11-27T03:00:00Z",
    "ends_at": "2026-12-01T03:00:00Z",
    "target_url": "https://loja.com/black-friday",
}
IOS_APP = {"priority": 20, "os": ["ios"], "target_url": "https://apps.apple.com/app/id1"}


def payload(**overrides):
    base = {
        "qr_id": "q", "workspace_id": "w", "status": "active",
        "default_url": "https://loja.com", "fallback_url": None,
        "expires_at": None, "utm": None, "rules": [IOS_APP, BLACK_FRIDAY],
    }
    return {**base, **overrides}


def at(iso: str) -> datetime:
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc)


def test_detect_os():
    assert detect_os(IPHONE) == "ios"
    assert detect_os(ANDROID) == "android"
    assert detect_os(WINDOWS) == "windows"
    assert detect_os("") == "other"


def test_schedule_window_boundaries():
    p = payload()
    assert pick_rule(p, "windows", None, at("2026-11-27T02:59:59")) is None
    assert pick_rule(p, "windows", None, at("2026-11-27T03:00:00")) == BLACK_FRIDAY
    assert pick_rule(p, "windows", None, at("2026-12-01T03:00:00")) is None  # fim exclusivo


def test_priority_order_independent_of_list_order():
    # Durante a Black Friday, a regra de prioridade 10 vence a do iOS (20)
    assert pick_rule(payload(), "ios", None, at("2026-11-28T12:00:00")) == BLACK_FRIDAY
    assert pick_rule(payload(), "ios", None, at("2026-10-01T12:00:00")) == IOS_APP


def test_country_rule():
    p = payload(rules=[{"countries": ["PT"], "target_url": "https://loja.pt"}])
    assert pick_rule(p, "ios", "pt", at("2026-10-01T00:00:00"))["target_url"] == "https://loja.pt"
    assert pick_rule(p, "ios", "BR", at("2026-10-01T00:00:00")) is None


def test_utm_does_not_override_existing_params():
    url = with_utm("https://a.com/p?utm_source=mail&x=1", {"utm_source": "qr", "utm_medium": "print"})
    assert "utm_source=mail" in url and "utm_medium=print" in url and "x=1" in url


def test_decide_default_and_expired_and_paused():
    now = at("2026-10-01T00:00:00")
    assert decide(payload(), WINDOWS, None, now).url == "https://loja.com"

    expired = payload(expires_at="2026-09-01T00:00:00+00:00", fallback_url="https://loja.com/fim")
    assert decide(expired, WINDOWS, None, now).url == "https://loja.com/fim"

    paused = payload(status="paused")
    assert decide(paused, WINDOWS, None, now).kind == "gone"


def test_decide_deeplink():
    rule = {"os": ["ios", "android"], "target_url": "https://instagram.com/marca",
            "deep_link": {"ios": "instagram://user?username=marca"}}
    p = payload(rules=[rule])
    now = at("2026-10-01T00:00:00")
    d = decide(p, IPHONE, None, now)
    assert d.kind == "deeplink" and d.app_url.startswith("instagram://")
    assert decide(p, ANDROID, None, now).kind == "redirect"
