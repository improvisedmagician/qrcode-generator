import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from worker.enrich import COLUMNS, enrich, lookup_geo, visitor_hash

IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
IPAD = ("Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")
WIN = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def event(**kw):
    base = {
        "event_id": str(uuid.uuid4()), "ts": datetime.now(timezone.utc).isoformat(),
        "qr_id": str(uuid.uuid4()), "workspace_id": str(uuid.uuid4()), "host": "localhost",
        "slug": "abcd", "destination_url": "https://x", "ip": "200.1.2.3", "ua": IPHONE,
        "referer": "", "lang": "pt-BR,pt;q=0.9", "prefetch": "0", "geo_country": "BR",
    }
    return {**base, **kw}


class FakeGeo:
    def city(self, ip):
        return SimpleNamespace(country=SimpleNamespace(iso_code="BR"),
                               subdivisions=SimpleNamespace(most_specific=SimpleNamespace(name="São Paulo")),
                               city=SimpleNamespace(name="Campinas"))


def row(ev, geo=None):
    return dict(zip(COLUMNS, enrich(ev, salt="s", geo=geo)))


def test_device_and_os_classification():
    assert row(event(ua=IPHONE))["device_type"] == "mobile"
    assert row(event(ua=IPHONE))["os"] == "iOS"
    assert row(event(ua=IPAD))["device_type"] == "tablet"
    r = row(event(ua=WIN))
    assert r["device_type"] == "desktop" and r["os"] == "Windows"


def test_bots_and_prefetch_are_flagged():
    assert row(event(ua="WhatsApp/2.23"))["is_bot"] == 1
    assert row(event(ua="Googlebot/2.1"))["device_type"] == "bot"
    assert row(event(prefetch="1"))["is_bot"] == 1
    assert row(event())["is_bot"] == 0


def test_geo_enrichment_and_fallback_to_edge_country():
    r = row(event(), geo=FakeGeo())
    assert (r["country"], r["region"], r["city"]) == ("BR", "São Paulo", "Campinas")
    assert row(event(geo_country="PT"))["country"] == "PT"          # sem base GeoIP
    assert lookup_geo(FakeGeo(), "10.0.0.1") == ("", "", "")        # IP privado ignorado


def test_ip_is_never_persisted_and_hash_is_stable():
    ev = event()
    r = row(ev)
    assert "200.1.2.3" not in [str(v) for v in r.values()]
    assert r["visitor_hash"] == visitor_hash("s", ev["workspace_id"], "200.1.2.3", IPHONE)
    assert r["language"] == "pt-BR"
