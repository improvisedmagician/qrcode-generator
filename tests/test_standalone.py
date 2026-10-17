import json
import pytest
from fastapi.testclient import TestClient

from run_standalone import app, IN_MEMORY_DB


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_dashboard_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "QR Dinâmico" in resp.text
    assert "escapeHtml" in resp.text
    assert "escapeJs" in resp.text
    assert "sanitizeUrl" in resp.text


def test_list_and_get_qrcodes(client):
    resp = client.get("/api/qrcodes")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 3

    # Check promo slug exists
    slugs = [q["slug"] for q in data]
    assert "promo" in slugs

    # Single QR detail
    resp_single = client.get("/api/qrcodes/promo")
    assert resp_single.status_code == 200
    single = resp_single.json()
    assert single["slug"] == "promo"
    assert "default_url" in single


def test_crud_lifecycle_and_validation(client):
    new_qr = {
        "name": "QR Campanha Teste",
        "slug": "teste-crud-1",
        "target_url": "https://meusite.com.br/promo",
        "foreground": "#E11D48",
        "frame_text": "ACESSE JA",
        "module_style": "rounded",
        "status": "active",
    }

    # 1. Create
    resp_create = client.post("/api/qrcodes", json=new_qr)
    assert resp_create.status_code == 200
    assert resp_create.json()["status"] == "created"

    # 2. Duplicate slug should return 409
    resp_dup = client.post("/api/qrcodes", json=new_qr)
    assert resp_dup.status_code == 409

    # 3. Security: Forbidden protocol should return 422
    bad_proto = {
        "name": "Evil",
        "slug": "evil-xss",
        "target_url": "javascript:alert(document.cookie)",
    }
    resp_bad = client.post("/api/qrcodes", json=bad_proto)
    assert resp_bad.status_code == 422

    # 4. Read back
    resp_get = client.get("/api/qrcodes/teste-crud-1")
    assert resp_get.status_code == 200
    assert resp_get.json()["name"] == "QR Campanha Teste"

    # 5. Update
    resp_update = client.put(
        "/api/qrcodes/teste-crud-1",
        json={"name": "Nome Atualizado", "status": "paused"},
    )
    assert resp_update.status_code == 200
    assert resp_update.json()["data"]["name"] == "Nome Atualizado"
    assert resp_update.json()["data"]["status"] == "paused"

    # 6. Delete
    resp_del = client.delete("/api/qrcodes/teste-crud-1")
    assert resp_del.status_code == 200
    assert resp_del.json()["status"] == "deleted"

    # 7. Verify deletion
    resp_404 = client.get("/api/qrcodes/teste-crud-1")
    assert resp_404.status_code == 404


def test_redirect_302_and_scan_capture(client):
    # Ensure promo exists
    assert "promo" in IN_MEMORY_DB

    resp = client.get(
        "/promo",
        follow_redirects=False,
        headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)",
            "CF-Connecting-IP": "177.100.20.10",
        },
    )
    assert resp.status_code == 302
    assert "minhaloja.com.br" in resp.headers["location"]

    # Nonexistent slug returns 404
    resp_notfound = client.get("/slug-que-nunca-existiu-xyz", follow_redirects=False)
    assert resp_notfound.status_code == 404


def test_preview_and_export(client):
    # Live preview
    resp_live = client.get("/api/preview-live?slug=test&color=%230F172A")
    assert resp_live.status_code == 200
    assert resp_live.headers["content-type"] == "image/svg+xml"
    assert "<svg" in resp_live.text

    # Slug preview
    resp_prev = client.get("/preview/promo.svg")
    assert resp_prev.status_code == 200
    assert resp_prev.headers["content-type"] == "image/svg+xml"

    # Export SVG
    resp_exp_svg = client.get("/export/promo?format=svg")
    assert resp_exp_svg.status_code == 200
    assert resp_exp_svg.headers["content-type"] == "image/svg+xml"

    # Export PNG
    resp_exp_png = client.get("/export/promo?format=png&size=200")
    assert resp_exp_png.status_code == 200
    assert resp_exp_png.headers["content-type"] == "image/png"

    # Export PDF (vetorial de alta resolução)
    resp_exp_pdf = client.get("/export/promo?format=pdf")
    assert resp_exp_pdf.status_code == 200
    assert resp_exp_pdf.headers["content-type"] == "application/pdf"
    assert resp_exp_pdf.content.startswith(b"%PDF")


def test_self_referencing_loop_prevention(client):
    # Tentativa de criar QR apontando para si mesmo (loop infinito)
    loop_dto = {
        "name": "Loop Test",
        "slug": "meuloop",
        "target_url": "http://localhost:8080/meuloop",
    }
    resp = client.post("/api/qrcodes", json=loop_dto)
    assert resp.status_code == 422
    assert "loop infinito" in resp.json()["detail"].lower()


def test_paused_and_fallback_redirection(client):
    # QR pausado com fallback_url
    IN_MEMORY_DB["paused-with-fallback"] = {
        "v": 1,
        "qr_id": "44444444-4444-4444-4444-444444444444",
        "workspace_id": "00000000-0000-0000-0000-000000000003",
        "status": "paused",
        "default_url": "https://promo-encerrada.com",
        "fallback_url": "https://fallback-institucional.com",
        "expires_at": None,
        "rules": [],
    }

    resp = client.get("/paused-with-fallback", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://fallback-institucional.com"

    # QR pausado sem fallback_url -> 410 HTML
    IN_MEMORY_DB["paused-no-fallback"] = {
        "v": 1,
        "qr_id": "55555555-5555-5555-5555-555555555555",
        "workspace_id": "00000000-0000-0000-0000-000000000003",
        "status": "paused",
        "default_url": "https://loja.com",
        "fallback_url": None,
        "expires_at": None,
        "rules": [],
    }
    resp_gone = client.get("/paused-no-fallback", headers={"Accept": "text/html"})
    assert resp_gone.status_code == 410
    assert "QR Code Temporariamente Indisponível" in resp_gone.text


def test_not_found_html_response(client):
    resp = client.get("/slug-desconhecido-12345", headers={"Accept": "text/html"})
    assert resp.status_code == 404
    assert "QR Code /slug-desconhecido-12345 Não Encontrado" in resp.text


def test_vercel_entrypoint_import():
    from api.index import app as vercel_app
    with TestClient(vercel_app) as vc:
        resp = vc.get("/")
        assert resp.status_code == 200
        assert "QR Dinâmico" in resp.text


def test_serverless_scan_enrichment(monkeypatch, client):
    import run_standalone
    monkeypatch.setattr(run_standalone, "IS_SERVERLESS", True)
    
    initial_count = len(run_standalone.CAPTURED_SCANS)
    resp = client.get(
        "/promo",
        headers={
            "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
            "x-real-ip": "177.18.29.40",
            "x-vercel-ip-country": "BR",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert len(run_standalone.CAPTURED_SCANS) == initial_count + 1
    last_scan = run_standalone.CAPTURED_SCANS[-1]
    assert last_scan["slug"] == "promo"
    assert last_scan["os"] == "iOS"


