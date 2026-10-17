import io
import pytest
from PIL import Image

from app.qr_render import (
    QRDesign,
    build_layout,
    choose_ecl,
    contrast_ratio,
    render,
    validate,
    verify_readable,
    InvalidDesign,
)


def test_contrast_ratio_and_luminance_validation():
    # Preto sobre branco tem contraste alto (> 15:1)
    ratio = contrast_ratio("#000000", "#FFFFFF")
    assert ratio > 15.0

    # Fundo escuro com frente clara viola a regra de contraste de leitores comuns
    with pytest.raises(InvalidDesign, match="mais escura que o fundo"):
        validate(QRDesign(foreground="#FFFFFF", background="#000000"))

    # Cores com pouco contraste (cinza escuro sobre cinza médio)
    with pytest.raises(InvalidDesign, match="contraste insuficiente"):
        validate(QRDesign(foreground="#555555", background="#666666"))


def test_quiet_zone_enforcement():
    with pytest.raises(InvalidDesign, match="quiet zone"):
        validate(QRDesign(quiet_zone=2))


def test_automatic_ecl_upgrade_when_logo_present():
    # Sem logo, usa o default M ou o configurado
    d1 = QRDesign(error_correction="L")
    assert choose_ecl(d1) == "L"

    # Cria imagem dummy 50x50 para simular logotipo
    img = Image.new("RGBA", (50, 50), (255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    logo_bytes = buf.getvalue()

    # Com logo, OBRIGATORIAMENTE sobe para nível H (30% de redundância)
    d2 = QRDesign(logo=logo_bytes, error_correction="L")
    assert choose_ecl(d2) == "H"


def test_render_svg_png_pdf():
    url = "https://qr.empresa.com.br/promo-2026"

    # SVG (vetor)
    svg_bytes = render(url, QRDesign(foreground="#003366", background="#FFFFFF"), fmt="svg")
    assert svg_bytes.startswith(b"<svg")
    assert b"</svg>" in svg_bytes

    # PNG (raster)
    png_bytes = render(url, QRDesign(foreground="#003366", background="#FFFFFF"), fmt="png", size_px=512)
    assert png_bytes.startswith(b"\x89PNG")

    # PDF (vetor)
    pdf_bytes = render(url, QRDesign(foreground="#003366", background="#FFFFFF"), fmt="pdf")
    assert pdf_bytes.startswith(b"%PDF")


def test_readability_with_real_zxing_decoder():
    url = "https://empresa.com.br/lp/black-friday"
    design = QRDesign(
        foreground="#111827",
        background="#FFFFFF",
        module_style="rounded",
        finder_style="rounded",
        frame_text="ESCANEIE AQUI",
    )
    png_bytes = render(url, design=design, fmt="png", size_px=800)

    # Verifica se a imagem gerada é decodificada com 100% de precisão pelo zxing-cpp
    readable = verify_readable(png_bytes, url)
    assert readable is True
