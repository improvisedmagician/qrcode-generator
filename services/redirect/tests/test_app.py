import json
from pathlib import Path

import fakeredis
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

PAYLOAD = {
    "v": 1, "qr_id": "11111111-1111-1111-1111-111111111111",
    "workspace_id": "22222222-2222-2222-2222-222222222222",
    "status": "active", "default_url": "https://destino.com/pagina", "fallback_url": None,
    "expires_at": None, "utm": {"utm_source": "qrcode"}, "rules": [],
}
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"


class BrokenRedis:
    async def get(self, *_a, **_k):
        raise ConnectionError("down")

    async def set(self, *_a, **_k):
        raise ConnectionError("down")

    def pipeline(self, *_a, **_k):
        raise ConnectionError("down")


def test_redirect_302_and_event_published(tmp_path: Path):
    server = fakeredis.FakeServer()
    sync = fakeredis.FakeRedis(server=server, decode_responses=True)
    sync.set("qr:testserver:promo1", json.dumps(PAYLOAD))
    redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)

    app = create_app(Settings(spool_dir=tmp_path), redis=redis, connect=False)
    with TestClient(app) as client:
        resp = client.get("/promo1", follow_redirects=False,
                          headers={"User-Agent": IPHONE, "CF-Connecting-IP": "200.1.2.3",
                                   "CF-IPCountry": "BR"})
        assert resp.status_code == 302
        assert resp.headers["location"] == "https://destino.com/pagina?utm_source=qrcode"
        assert "no-store" in resp.headers["cache-control"]

    entries = sync.xrange("scans")                      # shutdown faz flush da fila
    assert len(entries) == 1
    event = entries[0][1]
    assert event["ip"] == "200.1.2.3" and event["geo_country"] == "BR"
    assert event["slug"] == "promo1" and event["ua"] == IPHONE


def test_unknown_and_invalid_slugs(tmp_path: Path):
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    app = create_app(Settings(spool_dir=tmp_path), redis=redis, connect=False)
    with TestClient(app) as client:
        assert client.get("/naoexiste", follow_redirects=False).status_code == 404
        assert client.get("/a.b", follow_redirects=False).status_code == 404
        assert client.get("/healthz").json()["ok"] is True


def test_redirect_survives_redis_outage_and_spools_events(tmp_path: Path):
    row = {
        "id": PAYLOAD["qr_id"], "workspace_id": PAYLOAD["workspace_id"], "status": "active",
        "default_url": "https://destino.com", "fallback_url": None, "expires_at": None,
        "utm_params": None, "version": 1, "rules": "[]",
    }

    async def pg_fetch(host, slug):
        return row

    app = create_app(Settings(spool_dir=tmp_path), redis=BrokenRedis(), pg_fetch=pg_fetch,
                     connect=False)
    with TestClient(app) as client:
        for _ in range(3):
            resp = client.get("/promo1", follow_redirects=False)
            assert resp.status_code == 302

    lines = [ln for f in tmp_path.glob("spool-*.jsonl") for ln in f.read_text().splitlines()]
    assert len(lines) == 3                               # nenhuma métrica perdida
    assert json.loads(lines[0])["slug"] == "promo1"


def test_deeplink_interstitial(tmp_path: Path):
    payload = {**PAYLOAD, "utm": None, "rules": [{
        "os": ["ios"], "target_url": "https://instagram.com/marca",
        "deep_link": {"ios": "instagram://user?username=marca"}}]}
    server = fakeredis.FakeServer()
    fakeredis.FakeRedis(server=server).set("qr:testserver:insta1", json.dumps(payload))
    app = create_app(Settings(spool_dir=tmp_path),
                     redis=fakeredis.FakeAsyncRedis(server=server, decode_responses=True),
                     connect=False)
    with TestClient(app) as client:
        resp = client.get("/insta1", headers={"User-Agent": IPHONE})
        assert resp.status_code == 200
        assert "instagram://user?username=marca" in resp.text
        assert "https://instagram.com/marca" in resp.text
