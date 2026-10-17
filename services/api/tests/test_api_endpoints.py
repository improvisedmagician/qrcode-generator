import uuid
from datetime import datetime, timezone, timedelta
import pytest
from pydantic import ValidationError

from app.schemas import (
    validate_destination,
    Design,
    QRCreate,
    QRUpdate,
    DomainIn,
    LoginIn,
    RefreshIn,
    Rule,
)
from app.routers.qrcodes import generate_slug, format_short_url, _format_qr_row
from app.qr_render import QRDesign, render, validate, verify_readable, InvalidDesign


def test_validate_destination_crlf_injection():
    # CRLF injection attempts must be blocked
    with pytest.raises(ValueError, match="CRLF"):
        validate_destination("https://exemplo.com/path\r\nInjected-Header: bad")

    with pytest.raises(ValueError, match="CRLF"):
        validate_destination("https://exemplo.com/path\nInjected-Header: bad")

    with pytest.raises(ValueError, match="CRLF"):
        validate_destination("https://exemplo.com/path\tSet-Cookie: evil")


def test_validate_destination_limits():
    # URL too long
    long_url = "https://exemplo.com/" + ("a" * 2050)
    with pytest.raises(ValueError, match="URL muito longa"):
        validate_destination(long_url)

    # Missing hostname
    with pytest.raises(ValueError, match="host válido"):
        validate_destination("https://")


def test_domain_input_validation():
    # Valid domains
    assert DomainIn(hostname="qr.meusite.com").hostname == "qr.meusite.com"
    assert DomainIn(hostname="links.brand.com.br").hostname == "links.brand.com.br"

    # Invalid domains
    with pytest.raises(ValidationError):
        DomainIn(hostname="invalid_domain")

    with pytest.raises(ValidationError):
        DomainIn(hostname="http://already-has-scheme.com")


def test_auth_schemas():
    login = LoginIn(email="admin@corp.com", password="password123")
    assert login.email == "admin@corp.com"
    assert login.password == "password123"

    ref = RefreshIn(refresh_token="my-jwt-token")
    assert ref.refresh_token == "my-jwt-token"


def test_qr_create_and_update_schemas():
    now = datetime.now(timezone.utc)
    create_dto = QRCreate(
        name="Campanha Verão",
        default_url="https://site.com/verao",
        slug="verao2026",
        design=Design(foreground="#2563EB", frame_text="PROMO"),
        expires_at=now + timedelta(days=30),
    )
    assert create_dto.name == "Campanha Verão"
    assert create_dto.slug == "verao2026"
    assert create_dto.design.foreground == "#2563EB"

    update_dto = QRUpdate(
        name="Novo Nome",
        status="paused",
        default_url="https://site.com/novo",
    )
    assert update_dto.status == "paused"
    assert update_dto.name == "Novo Nome"


def test_generate_slug_and_short_url():
    slug1 = generate_slug(7)
    slug2 = generate_slug(7)
    assert len(slug1) == 7
    assert len(slug2) == 7
    assert slug1 != slug2

    short_url = format_short_url("qr.meudominio.com", "abc1234")
    assert "qr.meudominio.com" in short_url
    assert short_url.endswith("/abc1234")


def test_format_qr_row():
    mock_row = {
        "id": uuid.uuid4(),
        "name": "QR Teste",
        "hostname": "qr.com",
        "slug": "teste1",
        "default_url": "https://teste.com",
        "fallback_url": None,
        "status": "active",
        "folder_id": None,
        "tags": ["marketing", "sp"],
        "utm_params": '{"utm_source": "qr"}',
        "expires_at": datetime(2026, 12, 31, tzinfo=timezone.utc),
        "design": '{"foreground": "#000000"}',
        "rules": '[]',
        "version": 1,
        "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }
    formatted = _format_qr_row(mock_row)
    assert formatted["slug"] == "teste1"
    assert formatted["tags"] == ["marketing", "sp"]
    assert formatted["utm"] == {"utm_source": "qr"}
    assert formatted["design"] == {"foreground": "#000000"}


def test_qr_render_all_formats():
    design = QRDesign(
        foreground="#1E293B",
        background="#FFFFFF",
        module_style="rounded",
        finder_style="rounded",
        frame_text="ESCANEIE",
    )
    url = "https://exemplo.com/promo"

    # SVG rendering
    svg = render(url, design=design, fmt="svg", size_px=200)
    assert svg.startswith(b"<svg")
    assert b"ESCANEIE" in svg

    # PNG rendering
    png = render(url, design=design, fmt="png", size_px=200)
    assert png.startswith(b"\x89PNG")

    # PDF rendering
    pdf = render(url, design=design, fmt="pdf")
    assert pdf.startswith(b"%PDF")


def test_verify_readable_and_validate():
    # Valid design contrast passes
    valid = QRDesign(foreground="#000000", background="#FFFFFF")
    validate(valid)

    # Invisible design (white on white) fails validate
    invisible = QRDesign(foreground="#FFFFFF", background="#FFFFFF")
    with pytest.raises(InvalidDesign):
        validate(invisible)

    # verify_readable barcode scanner test
    target = "https://empresa.com/leitura"
    png_bytes = render(target, design=valid, fmt="png", size_px=400)
    assert verify_readable(png_bytes, target) is True
