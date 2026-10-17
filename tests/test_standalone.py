import json
import pytest
from fastapi.testclient import TestClient

from run_standalone import app, IN_MEMORY_DB


@pytest.fixture
def client():
    from run_standalone import DELETED_SLUGS
    DELETED_SLUGS.discard("promo")
    DELETED_SLUGS.discard("blackfriday")
    DELETED_SLUGS.discard("insta")
    IN_MEMORY_DB["promo"] = {
        "v": 1,
        "name": "Banner Institucional São Paulo",
        "qr_id": "11111111-1111-1111-1111-111111111111",
        "workspace_id": "00000000-0000-0000-0000-000000000003",
        "status": "active",
        "default_url": "https://minhaloja.com.br/produtos",
        "fallback_url": "https://minhaloja.com.br/encerrado",
        "expires_at": None,
        "utm": {"utm_source": "qrcode", "utm_medium": "banner-sp"},
        "design": {
            "foreground": "#0F172A",
            "background": "#FFFFFF",
            "module_style": "rounded",
            "finder_style": "rounded",
            "frame_text": "ESCANEIE AQUI",
        },
        "rules": [],
        "created_at": "2026-10-16T18:00:00Z",
    }
    IN_MEMORY_DB["blackfriday"] = {
        "v": 2,
        "name": "Campanha Black Friday 2026",
        "qr_id": "22222222-2222-2222-2222-222222222222",
        "workspace_id": "00000000-0000-0000-0000-000000000003",
        "status": "active",
        "default_url": "https://minhaloja.com.br/loja-normal",
        "fallback_url": "https://minhaloja.com.br/encerrado",
        "expires_at": None,
        "utm": {"utm_source": "qrcode", "utm_campaign": "bf2026"},
        "design": {
            "foreground": "#991B1B",
            "background": "#FFFFFF",
            "module_style": "square",
            "finder_style": "square",
            "frame_text": "BLACK FRIDAY 50% OFF",
        },
        "rules": [],
        "created_at": "2026-10-16T18:15:00Z",
    }
    IN_MEMORY_DB["insta"] = {
        "v": 3,
        "name": "Cartão de Visitas — Instagram Direto",
        "qr_id": "33333333-3333-3333-3333-333333333333",
        "workspace_id": "00000000-0000-0000-0000-000000000003",
        "status": "active",
        "default_url": "https://instagram.com/empresa",
        "fallback_url": None,
        "expires_at": None,
        "utm": None,
        "design": {
            "foreground": "#4C1D95",
            "background": "#FFFFFF",
            "module_style": "dots",
            "finder_style": "dots",
            "frame_text": "SIGA NO INSTAGRAM",
        },
        "rules": [],
        "created_at": "2026-10-16T18:30:00Z",
    }
    with TestClient(app) as c:
        yield c


def test_dashboard_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "QR Estático" in resp.text
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
    # Live preview com URL de destino direta
    resp_live = client.get("/api/preview-live?slug=test&url=https%3A%2F%2Fmeusite.com%2Fcardapio&color=%230F172A")
    assert resp_live.status_code == 200
    assert resp_live.headers["content-type"] == "image/svg+xml"
    assert "<svg" in resp_live.text

    # Slug preview
    resp_prev = client.get("/preview/promo.svg")
    assert resp_prev.status_code == 200
    assert resp_prev.headers["content-type"] == "image/svg+xml"

    # Serverless fallback preview (slug que não está no banco em memória não deve retornar 404)
    resp_prev_stateless = client.get(
        "/preview/novo-slug-serverless.svg?color=%230F172A&bg_color=%23FFFFFF&frame=SCAN"
    )
    assert resp_prev_stateless.status_code == 200
    assert resp_prev_stateless.headers["content-type"] == "image/svg+xml"
    assert "<svg" in resp_prev_stateless.text

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
        assert "QR Estático" in resp.text


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


def test_idempotent_delete_and_tombstone(client):
    # 1. Cria um QR code temporário
    test_slug = "slug-teste-idempotente"
    create_dto = {
        "name": "QR Idempotente",
        "slug": test_slug,
        "target_url": "https://meusite.com.br/teste-idempotente",
    }
    resp = client.post("/api/qrcodes", json=create_dto)
    assert resp.status_code == 200

    # 2. Primeira exclusão: retorna 200
    del_1 = client.delete(f"/api/qrcodes/{test_slug}")
    assert del_1.status_code == 200
    assert del_1.json()["status"] == "deleted"

    # 3. Segunda exclusão (idempotente): deve retornar 200 sem falha
    del_2 = client.delete(f"/api/qrcodes/{test_slug}")
    assert del_2.status_code == 200
    assert del_2.json()["status"] == "deleted"

    # 4. Excluir slug que nunca existiu: também retorna 200 (idempotente por especificação HTTP RFC)
    del_unknown = client.delete("/api/qrcodes/slug-totalmente-inexistente-xyz")
    assert del_unknown.status_code == 200
    assert del_unknown.json()["status"] == "deleted"

    # 5. O slug excluído não deve aparecer na listagem
    list_resp = client.get("/api/qrcodes")
    assert list_resp.status_code == 200
    all_slugs = [q["slug"] for q in list_resp.json()]
    assert test_slug not in all_slugs

    # 6. Consultar slug excluído retorna 404
    get_resp = client.get(f"/api/qrcodes/{test_slug}")
    assert get_resp.status_code == 404

    # 7. Redirecionar slug excluído retorna 404
    redir_resp = client.get(f"/{test_slug}", follow_redirects=False)
    assert redir_resp.status_code == 404

    # 8. Recriação do mesmo slug após exclusão deve ser permitida com sucesso
    recreate_resp = client.post("/api/qrcodes", json=create_dto)
    assert recreate_resp.status_code == 200
    assert recreate_resp.json()["status"] == "created"


def test_cors_options_preflight(client):
    test_origin = "https://meu-dominio-preview.vercel.app"
    resp = client.options(
        "/api/qrcodes/promo",
        headers={
            "Origin": test_origin,
            "Access-Control-Request-Method": "DELETE",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") in [test_origin, "*"]


def test_clicks_counting_per_qrcode(client):
    slug = "click-counter-test"
    client.post(
        "/api/qrcodes",
        json={
            "name": "QR Teste Cliques",
            "slug": slug,
            "target_url": "https://meusite.com.br/clique",
            "foreground": "#000000",
            "frame_text": "ACESSE",
            "module_style": "rounded",
            "status": "active",
        },
    )

    resp_init = client.get(f"/api/qrcodes/{slug}")
    assert resp_init.status_code == 200
    initial_clicks = resp_init.json().get("clicks", 0)

    for _ in range(3):
        r = client.get(f"/{slug}", follow_redirects=False)
        assert r.status_code == 302

    resp_after = client.get(f"/api/qrcodes/{slug}")
    assert resp_after.status_code == 200
    assert resp_after.json()["clicks"] == initial_clicks + 3

    resp_list = client.get("/api/qrcodes")
    assert resp_list.status_code == 200
    item = next(q for q in resp_list.json() if q["slug"] == slug)
    assert item["clicks"] == initial_clicks + 3

    resp_scans = client.get("/scans")
    assert resp_scans.status_code == 200
    scans_data = resp_scans.json()
    assert "clicks_by_slug" in scans_data
    assert scans_data["clicks_by_slug"].get(slug) == initial_clicks + 3


def test_qr_and_dashboard_gradient_background(client):
    # 1. Verifica se a interface possui o seletor de fundo
    resp_dash = client.get("/")
    assert resp_dash.status_code == 200
    assert "btn-menu-fundo" in resp_dash.text
    assert "dropdown-fundo" in resp_dash.text
    assert "aplicarFundoDashboard" in resp_dash.text
    assert "Gradiente Aurora" in resp_dash.text

    # 2. Live preview com fundo gradiente
    resp_live = client.get(
        "/api/preview-live?slug=live-grad&color=%23000000&bg_type=gradient&bg_color=%23FFF1EB&bg_color_end=%23ACE0F9"
    )
    assert resp_live.status_code == 200
    assert resp_live.headers["content-type"] == "image/svg+xml"
    assert "<linearGradient id=\"qr-bg-grad\"" in resp_live.text
    assert "url(#qr-bg-grad)" in resp_live.text

    # 3. Criação de QR Code com fundo gradiente
    grad_slug = "qr-com-gradiente"
    resp_create = client.post(
        "/api/qrcodes",
        json={
            "name": "QR Gradiente Sunset",
            "slug": grad_slug,
            "target_url": "https://meusite.com.br/sunset",
            "foreground": "#0F172A",
            "background": "#FFF1EB",
            "background_type": "gradient",
            "background_color_end": "#ACE0F9",
            "frame_text": "ACESSE AGORA",
            "module_style": "rounded",
            "status": "active",
        },
    )
    assert resp_create.status_code == 200
    assert resp_create.json()["status"] == "created"

    # 4. Preview SVG do QR Code criado deve conter o gradiente
    resp_prev = client.get(f"/preview/{grad_slug}.svg")
    assert resp_prev.status_code == 200
    assert resp_prev.headers["content-type"] == "image/svg+xml"
    assert "<linearGradient id=\"qr-bg-grad\"" in resp_prev.text
    assert "url(#qr-bg-grad)" in resp_prev.text

    # 5. Exportação SVG e PNG com fundo gradiente
    resp_exp_svg = client.get(f"/export/{grad_slug}?format=svg")
    assert resp_exp_svg.status_code == 200
    assert "<linearGradient id=\"qr-bg-grad\"" in resp_exp_svg.text

    resp_exp_png = client.get(f"/export/{grad_slug}?format=png&size=256")
    assert resp_exp_png.status_code == 200
    assert resp_exp_png.headers["content-type"] == "image/png"






