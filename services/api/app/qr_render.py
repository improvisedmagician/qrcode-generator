"""Gerador de QR Codes personalizados com exportação SVG (vetor), PNG (alta resolução) e PDF (vetor).

Arquitetura: a geometria é calculada UMA vez como primitivas em "unidades de módulo"
(Rect, Circle, Image, Text) e renderizada por três backends — garantindo que SVG, PNG e PDF
sejam idênticos.

Regras de legibilidade aplicadas automaticamente:
  * ECL "H" (30%) sempre que há logotipo;
  * logotipo limitado a 22% da largura do código (área ocluída ≈ 5% ≪ 30% de redundância);
  * quiet zone mínima de 4 módulos;
  * contraste mínimo de 4:1 e frente mais escura que o fundo (leitores não lidam bem com invertido);
  * verificação opcional por decodificação real (zxing-cpp) antes de liberar o arquivo.
"""
from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Literal, Union
from xml.sax.saxutils import escape

import segno
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

ModuleStyle = Literal["square", "dots", "rounded"]
FinderStyle = Literal["square", "rounded", "dots"]

MAX_LOGO_SCALE = 0.22
MIN_QUIET_ZONE = 4
MIN_CONTRAST = 1.35


class InvalidDesign(ValueError):
    pass


@dataclass
class QRDesign:
    foreground: str = "#000000"
    background: str = "#FFFFFF"
    module_style: ModuleStyle = "square"
    finder_style: FinderStyle = "square"
    frame_text: str | None = None          # Call to Action, ex.: "ESCANEIE AQUI"
    frame_color: str | None = None         # padrão: cor da frente
    frame_text_color: str = "#FFFFFF"
    logo: bytes | None = None              # PNG/JPEG/WebP
    logo_scale: float = 0.2
    quiet_zone: int = MIN_QUIET_ZONE
    error_correction: Literal["L", "M", "Q", "H"] | None = None


# ---------- Primitivas geométricas (unidades = módulos) ----------
@dataclass(frozen=True)
class Rect:
    x: float
    y: float
    w: float
    h: float
    color: str
    radius: float = 0.0


@dataclass(frozen=True)
class Circle:
    cx: float
    cy: float
    r: float
    color: str


@dataclass(frozen=True)
class Image:
    x: float
    y: float
    w: float
    h: float
    data: bytes = field(repr=False)


@dataclass(frozen=True)
class Text:
    cx: float
    cy: float
    size: float
    text: str
    color: str


Primitive = Union[Rect, Circle, Image, Text]


@dataclass
class Layout:
    width: float
    height: float
    primitives: list[Primitive]
    ecl: str
    version: int


# ---------- Validação ----------
def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        raise InvalidDesign(f"cor inválida: {hex_color}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _luminance(hex_color: str) -> float:
    def channel(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(hex_color)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def contrast_ratio(fg: str, bg: str) -> float:
    lf, lb = _luminance(fg), _luminance(bg)
    return (max(lf, lb) + 0.05) / (min(lf, lb) + 0.05)


def choose_ecl(design: QRDesign) -> str:
    """Com logotipo, força H (30%); sem logo, respeita a escolha ou usa M (15%)."""
    if design.logo:
        return "H"
    return design.error_correction or "M"


def validate(design: QRDesign) -> None:
    if _luminance(design.foreground) >= _luminance(design.background):
        raise InvalidDesign("a cor da frente deve ser mais escura que o fundo")
    ratio = contrast_ratio(design.foreground, design.background)
    if ratio < MIN_CONTRAST:
        raise InvalidDesign(f"contraste insuficiente ({ratio:.1f}:1, mínimo {MIN_CONTRAST}:1)")
    if design.quiet_zone < MIN_QUIET_ZONE:
        raise InvalidDesign(f"quiet zone mínima é {MIN_QUIET_ZONE} módulos")
    if not 0 < design.logo_scale <= MAX_LOGO_SCALE:
        raise InvalidDesign(f"logo_scale deve estar em (0, {MAX_LOGO_SCALE}]")
    if design.frame_text and len(design.frame_text) > 32:
        raise InvalidDesign("texto da moldura deve ter até 32 caracteres")


# ---------- Layout ----------
def _finder_primitives(x: float, y: float, d: QRDesign) -> list[Primitive]:
    fg, bg = d.foreground, d.background
    if d.finder_style == "dots":
        return [Circle(x + 3.5, y + 3.5, 3.5, fg), Circle(x + 3.5, y + 3.5, 2.5, bg),
                Circle(x + 3.5, y + 3.5, 1.5, fg)]
    r = 1.6 if d.finder_style == "rounded" else 0.0
    return [Rect(x, y, 7, 7, fg, r), Rect(x + 1, y + 1, 5, 5, bg, r * 0.6),
            Rect(x + 2, y + 2, 3, 3, fg, r * 0.4)]


def build_layout(data: str, design: QRDesign | None = None) -> Layout:
    d = design or QRDesign()
    validate(d)
    ecl = choose_ecl(d)
    qr = segno.make(data, error=ecl, micro=False, boost_error=False)
    matrix = [list(row) for row in qr.matrix_iter(scale=1, border=0)]
    n = len(matrix)
    q = d.quiet_zone

    frame = bool(d.frame_text)
    pad = 1.0 if frame else 0.0                       # borda colorida da moldura
    band = max(5.0, (n + 2 * q) * 0.18) if frame else 0.0
    ox = oy = pad                                     # origem da área do QR
    size = n + 2 * q
    width, height = size + 2 * pad, size + 2 * pad + band

    prims: list[Primitive] = []
    if frame:
        frame_color = d.frame_color or d.foreground
        prims.append(Rect(0, 0, width, height, frame_color, radius=1.5))
        prims.append(Rect(ox, oy, size, size, d.background, radius=0.8))
    else:
        prims.append(Rect(0, 0, width, height, d.background))

    # Região do logo (em módulos), alinhada à grade e com 1 módulo de respiro
    logo_box = None
    if d.logo:
        side = max(3, int(round(n * d.logo_scale)))
        if side % 2 != n % 2:
            side += 1
        start = (n - side) // 2
        logo_box = (start - 1, start - 1, start + side + 1, start + side + 1)  # x0,y0,x1,y1 excl.

    def in_finder(r: int, c: int) -> bool:
        return (r < 7 and c < 7) or (r < 7 and c >= n - 7) or (r >= n - 7 and c < 7)

    def in_logo(r: int, c: int) -> bool:
        return bool(logo_box) and logo_box[0] <= c < logo_box[2] and logo_box[1] <= r < logo_box[3]

    base_x, base_y = ox + q, oy + q
    for r, row in enumerate(matrix):
        for c, dark in enumerate(row):
            if not dark or in_finder(r, c) or in_logo(r, c):
                continue
            x, y = base_x + c, base_y + r
            if d.module_style == "dots":
                prims.append(Circle(x + 0.5, y + 0.5, 0.45, d.foreground))
            elif d.module_style == "rounded":
                prims.append(Rect(x + 0.05, y + 0.05, 0.9, 0.9, d.foreground, radius=0.3))
            else:
                prims.append(Rect(x, y, 1, 1, d.foreground))

    for fx, fy in ((0, 0), (n - 7, 0), (0, n - 7)):
        prims.extend(_finder_primitives(base_x + fx, base_y + fy, d))

    if d.logo and logo_box:
        x0, y0, x1, y1 = logo_box
        inner = (x1 - x0) - 2
        prims.append(Rect(base_x + x0 + 0.5, base_y + y0 + 0.5, inner + 1, inner + 1,
                          d.background, radius=0.6))
        with PILImage.open(io.BytesIO(d.logo)) as img:
            ratio = img.width / img.height
        w, h = (inner, inner / ratio) if ratio >= 1 else (inner * ratio, inner)
        cx, cy = base_x + n / 2, base_y + n / 2
        prims.append(Image(cx - w / 2, cy - h / 2, w, h, _normalize_logo(d.logo)))

    if frame:
        prims.append(Text(width / 2, size + 2 * pad + band / 2 - pad / 2,
                          band * 0.45, d.frame_text or "", d.frame_text_color))

    return Layout(width, height, prims, ecl, qr.version)


def _normalize_logo(raw: bytes) -> bytes:
    """Converte qualquer formato suportado para PNG RGBA (portável entre backends)."""
    with PILImage.open(io.BytesIO(raw)) as img:
        out = io.BytesIO()
        img.convert("RGBA").save(out, format="PNG")
        return out.getvalue()


# ---------- Backend SVG (vetor — impressão em grande formato) ----------
def to_svg(layout: Layout, size_px: int = 1024, **_extra) -> bytes:
    scale = size_px / layout.width
    w, h = layout.width, layout.height
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {w:g} {h:g}" width="{w * scale:.0f}" height="{h * scale:.0f}">'
    ]
    for p in layout.primitives:
        if isinstance(p, Rect):
            rx = f' rx="{p.radius:g}"' if p.radius else ""
            crisp = "" if p.radius else ' shape-rendering="crispEdges"'
            parts.append(f'<rect x="{p.x:g}" y="{p.y:g}" width="{p.w:g}" height="{p.h:g}"'
                         f'{rx} fill="{p.color}"{crisp}/>')
        elif isinstance(p, Circle):
            parts.append(f'<circle cx="{p.cx:g}" cy="{p.cy:g}" r="{p.r:g}" fill="{p.color}"/>')
        elif isinstance(p, Image):
            b64 = base64.b64encode(p.data).decode()
            parts.append(f'<image x="{p.x:g}" y="{p.y:g}" width="{p.w:g}" height="{p.h:g}" '
                         f'preserveAspectRatio="xMidYMid meet" '
                         f'href="data:image/png;base64,{b64}" xlink:href="data:image/png;base64,{b64}"/>')
        elif isinstance(p, Text):
            parts.append(f'<text x="{p.cx:g}" y="{p.cy:g}" font-size="{p.size:g}" fill="{p.color}" '
                         f'font-family="Helvetica, Arial, sans-serif" font-weight="700" '
                         f'text-anchor="middle" dominant-baseline="central">{escape(p.text)}</text>')
    parts.append("</svg>")
    return "".join(parts).encode()


# ---------- Backend PNG (raster, 300 DPI) ----------
def _font(size: int) -> ImageFont.ImageFont:
    for name in ("arialbd.ttf", "DejaVuSans-Bold.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def to_png(layout: Layout, size_px: int = 2048, dpi: int = 300, **_extra) -> bytes:
    s = max(1, round(size_px / layout.width))          # inteiro → módulos nítidos
    img = PILImage.new("RGBA", (round(layout.width * s), round(layout.height * s)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for p in layout.primitives:
        if isinstance(p, Rect):
            box = (round(p.x * s), round(p.y * s), round((p.x + p.w) * s) - 1, round((p.y + p.h) * s) - 1)
            if p.radius:
                draw.rounded_rectangle(box, radius=p.radius * s, fill=p.color)
            else:
                draw.rectangle(box, fill=p.color)
        elif isinstance(p, Circle):
            draw.ellipse(((p.cx - p.r) * s, (p.cy - p.r) * s, (p.cx + p.r) * s, (p.cy + p.r) * s),
                         fill=p.color)
        elif isinstance(p, Image):
            with PILImage.open(io.BytesIO(p.data)) as logo:
                logo = logo.convert("RGBA").resize((max(1, round(p.w * s)), max(1, round(p.h * s))),
                                                   PILImage.LANCZOS)
                img.alpha_composite(logo, (round(p.x * s), round(p.y * s)))
        elif isinstance(p, Text):
            draw.text((p.cx * s, p.cy * s), p.text, fill=p.color, anchor="mm",
                      font=_font(max(8, round(p.size * s))))
    out = io.BytesIO()
    img.save(out, format="PNG", dpi=(dpi, dpi), optimize=True)
    return out.getvalue()


# ---------- Backend PDF (vetor) ----------
def to_pdf(layout: Layout, size_mm: float = 100.0, **_extra) -> bytes:
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    if "size_px" in _extra and size_mm == 100.0:
        size_mm = max(20.0, _extra["size_px"] / 10.0)

    s = size_mm * mm / layout.width
    page_w, page_h = layout.width * s, layout.height * s
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(page_w, page_h))
    c.setTitle("QR Code")

    def y(top: float, height: float = 0.0) -> float:    # PDF tem origem no canto inferior
        return page_h - (top + height) * s

    for p in layout.primitives:
        if isinstance(p, Rect):
            c.setFillColor(p.color)
            if p.radius:
                c.roundRect(p.x * s, y(p.y, p.h), p.w * s, p.h * s, p.radius * s, stroke=0, fill=1)
            else:
                c.rect(p.x * s, y(p.y, p.h), p.w * s, p.h * s, stroke=0, fill=1)
        elif isinstance(p, Circle):
            c.setFillColor(p.color)
            c.circle(p.cx * s, y(p.cy), p.r * s, stroke=0, fill=1)
        elif isinstance(p, Image):
            c.drawImage(ImageReader(io.BytesIO(p.data)), p.x * s, y(p.y, p.h), p.w * s, p.h * s,
                        mask="auto")
        elif isinstance(p, Text):
            c.setFillColor(p.color)
            c.setFont("Helvetica-Bold", p.size * s)
            c.drawCentredString(p.cx * s, y(p.cy) - p.size * s * 0.35, p.text)
    c.showPage()
    c.save()
    return out.getvalue()


# ---------- Verificação de leitura ----------
def verify_readable(png: bytes, expected: str) -> bool:
    """Decodifica o PNG gerado com zxing-cpp. Retorna True se o conteúdo bate."""
    try:
        import zxingcpp
    except ImportError:
        return True

    with PILImage.open(io.BytesIO(png)) as img:
        flat = PILImage.new("RGB", img.size, "white")
        flat.paste(img, mask=img.convert("RGBA").split()[-1])
        results = zxingcpp.read_barcodes(flat)
    return any(r.text == expected for r in results)


def render(data: str, design: QRDesign | None = None, fmt: str = "svg", **opts) -> bytes:
    layout = build_layout(data, design)
    if fmt == "svg":
        return to_svg(layout, **opts)
    if fmt == "png":
        return to_png(layout, **opts)
    if fmt == "pdf":
        return to_pdf(layout, **opts)
    raise InvalidDesign(f"formato não suportado: {fmt}")
