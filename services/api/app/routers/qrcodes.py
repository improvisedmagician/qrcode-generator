"""CRUD completo de QR Codes, gerenciamento de regras, tags, histórico e exportação (SVG/PNG/PDF)."""
from __future__ import annotations

import base64
import json
import secrets
import string
import uuid
from typing import Any, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from ..config import settings
from ..deps import WorkspaceCtx, audit, require
from ..qr_render import QRDesign, render, verify_readable
from ..schemas import QRCreate, QRPage, QRUpdate

router = APIRouter(prefix="/workspaces/{workspace_id}/qrcodes", tags=["qrcodes"])

ALPHABET = string.ascii_letters + string.digits


def generate_slug(length: int = 7) -> str:
    """Gera slug aleatório Base62 com alta entropia (62^7 ≈ 3,5 trilhões de combinações)."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def format_short_url(hostname: str, slug: str) -> str:
    if settings.redirect_service_url:
        base = settings.redirect_service_url.rstrip("/")
        return f"{base}/{slug}"
    port = settings.short_url_port if settings.short_url_port and hostname == "localhost" else ""
    return f"{settings.short_url_scheme}://{hostname}{port}/{slug}"


QR_DETAIL_SQL = """
SELECT q.id, q.name, q.slug, d.hostname, q.default_url, q.fallback_url, q.status,
       q.folder_id, q.utm_params, q.expires_at, q.design, q.version,
       q.created_at, q.updated_at,
       COALESCE(array_agg(DISTINCT t.name) FILTER (WHERE t.name IS NOT NULL), '{}') AS tags,
       COALESCE(json_agg(json_build_object(
         'priority', r.priority, 'starts_at', r.starts_at, 'ends_at', r.ends_at,
         'os', r.os, 'countries', r.countries, 'target_url', r.target_url,
         'deep_link', r.deep_link) ORDER BY r.priority) FILTER (WHERE r.id IS NOT NULL), '[]') AS rules
FROM qrcodes q
JOIN domains d ON d.id = q.domain_id
LEFT JOIN qrcode_tags qt ON qt.qrcode_id = q.id
LEFT JOIN tags t ON t.id = qt.tag_id
LEFT JOIN qr_rules r ON r.qrcode_id = q.id
WHERE q.id = $1 AND q.workspace_id = $2 AND q.deleted_at IS NULL
GROUP BY q.id, d.hostname
"""


def _format_qr_row(row: asyncpg.Record) -> dict[str, Any]:
    design = json.loads(row["design"]) if isinstance(row["design"], str) else (row["design"] or {})
    rules = json.loads(row["rules"]) if isinstance(row["rules"], str) else (row["rules"] or [])
    utm = json.loads(row["utm_params"]) if isinstance(row["utm_params"], str) else row["utm_params"]

    return {
        "id": str(row["id"]),
        "name": row["name"],
        "short_url": format_short_url(row["hostname"], row["slug"]),
        "slug": row["slug"],
        "hostname": row["hostname"],
        "default_url": row["default_url"],
        "fallback_url": row["fallback_url"],
        "status": row["status"],
        "folder_id": str(row["folder_id"]) if row["folder_id"] else None,
        "tags": list(row["tags"] or []),
        "utm": utm,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "design": design,
        "rules": rules,
        "version": row["version"],
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


@router.get("", response_model=QRPage)
async def list_qrcodes(
    ctx: WorkspaceCtx = Depends(require("qr:read")),
    search: Optional[str] = Query(None, description="Busca por nome"),
    folder_id: Optional[uuid.UUID] = Query(None, description="Filtrar por pasta"),
    tag: Optional[str] = Query(None, description="Filtrar por tag"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> QRPage:
    where_clauses = ["q.workspace_id = $1", "q.deleted_at IS NULL"]
    params: list[Any] = [ctx.workspace_id]

    if search:
        params.append(f"%{search.strip()}%")
        where_clauses.append(f"q.name ILIKE ${len(params)}")

    if folder_id:
        params.append(folder_id)
        where_clauses.append(f"q.folder_id = ${len(params)}")

    if tag:
        params.append(tag.strip())
        where_clauses.append(f"EXISTS (SELECT 1 FROM qrcode_tags qt2 JOIN tags t2 ON t2.id = qt2.tag_id WHERE qt2.qrcode_id = q.id AND t2.name = ${len(params)})")

    where_sql = " AND ".join(where_clauses)
    params.extend([limit, offset])

    query = f"""
    SELECT q.id, q.name, q.slug, d.hostname, q.default_url, q.fallback_url, q.status,
           q.folder_id, q.utm_params, q.expires_at, q.design, q.version,
           q.created_at, q.updated_at,
           COALESCE(array_agg(DISTINCT t.name) FILTER (WHERE t.name IS NOT NULL), '{{}}') AS tags,
           COALESCE(json_agg(json_build_object(
             'priority', r.priority, 'starts_at', r.starts_at, 'ends_at', r.ends_at,
             'os', r.os, 'countries', r.countries, 'target_url', r.target_url,
             'deep_link', r.deep_link) ORDER BY r.priority) FILTER (WHERE r.id IS NOT NULL), '[]') AS rules
    FROM qrcodes q
    JOIN domains d ON d.id = q.domain_id
    LEFT JOIN qrcode_tags qt ON qt.qrcode_id = q.id
    LEFT JOIN tags t ON t.id = qt.tag_id
    LEFT JOIN qr_rules r ON r.qrcode_id = q.id
    WHERE {where_sql}
    GROUP BY q.id, d.hostname
    ORDER BY q.created_at DESC
    LIMIT ${len(params) - 1} OFFSET ${len(params)}
    """

    rows = await ctx.conn.fetch(query, *params)
    items = [_format_qr_row(r) for r in rows]
    next_cursor = str(offset + limit) if len(items) == limit else None
    return QRPage(items=items, next_cursor=next_cursor)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_qrcode(
    body: QRCreate,
    ctx: WorkspaceCtx = Depends(require("qr:create")),
) -> dict[str, Any]:
    hostname = (body.hostname or settings.default_domain).strip().lower()

    # Busca domínio ativo permitido (da plataforma ou da organização)
    domain_row = await ctx.conn.fetchrow(
        """SELECT id, hostname FROM domains
           WHERE hostname = $1 AND (organization_id IS NULL OR organization_id = $2) AND status = 'active'""",
        hostname,
        ctx.user.org_id,
    )
    if domain_row is None:
        raise HTTPException(
            status_code=400,
            detail=f"Domínio '{hostname}' não está ativo ou não pertence à sua organização",
        )
    domain_id = domain_row["id"]

    # Determina ou gera o slug
    slug = body.slug.strip() if body.slug else None
    if slug:
        conflict = await ctx.conn.fetchval(
            "SELECT 1 FROM qrcodes WHERE domain_id = $1 AND slug = $2",
            domain_id,
            slug,
        )
        if conflict:
            raise HTTPException(status_code=409, detail="Este slug já está em uso para este domínio")
    else:
        for _ in range(5):
            candidate = generate_slug()
            conflict = await ctx.conn.fetchval(
                "SELECT 1 FROM qrcodes WHERE domain_id = $1 AND slug = $2",
                domain_id,
                candidate,
            )
            if not conflict:
                slug = candidate
                break
        if not slug:
            raise HTTPException(status_code=500, detail="Não foi possível gerar um slug único. Tente novamente.")

    qr_id = uuid.uuid4()
    utm_json = json.dumps(body.utm) if body.utm else None
    design_dict = body.design.model_dump()
    design_json = json.dumps(design_dict)

    await ctx.conn.execute(
        """INSERT INTO qrcodes (id, workspace_id, domain_id, slug, name, folder_id,
                               default_url, fallback_url, expires_at, utm_params, design, created_by)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11::jsonb, $12)""",
        qr_id,
        ctx.workspace_id,
        domain_id,
        slug,
        body.name,
        body.folder_id,
        body.default_url,
        body.fallback_url,
        body.expires_at,
        utm_json,
        design_json,
        ctx.user.id,
    )

    # Inserção de regras de redirecionamento dinâmico
    for rule in body.rules:
        deep_json = json.dumps(rule.deep_link) if rule.deep_link else None
        await ctx.conn.execute(
            """INSERT INTO qr_rules (qrcode_id, priority, starts_at, ends_at, os, countries, target_url, deep_link)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)""",
            qr_id,
            rule.priority,
            rule.starts_at,
            rule.ends_at,
            rule.os,
            rule.countries,
            rule.target_url,
            deep_json,
        )

    # Inserção e associação de tags
    for tag_name in set(t.strip() for t in body.tags if t.strip()):
        tag_id = await ctx.conn.fetchval(
            """INSERT INTO tags (workspace_id, name)
               VALUES ($1, $2)
               ON CONFLICT (workspace_id, name) DO UPDATE SET name = EXCLUDED.name
               RETURNING id""",
            ctx.workspace_id,
            tag_name,
        )
        await ctx.conn.execute(
            "INSERT INTO qrcode_tags (qrcode_id, tag_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            qr_id,
            tag_id,
        )

    # Outbox transacional para sincronização instantânea com o Redis
    await ctx.conn.execute(
        """INSERT INTO outbox_events (event_type, payload)
           VALUES ('qr.created', $1::jsonb)""",
        json.dumps({"qr_id": str(qr_id), "host": hostname, "slug": slug}),
    )

    await audit(ctx, "qr.create", "qrcode", qr_id, json.dumps({"slug": slug, "target": body.default_url}))

    row = await ctx.conn.fetchrow(QR_DETAIL_SQL, qr_id, ctx.workspace_id)
    return _format_qr_row(row)


@router.get("/{id}")
async def get_qrcode(
    id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require("qr:read")),
) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(QR_DETAIL_SQL, id, ctx.workspace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="QR Code não encontrado")
    return _format_qr_row(row)


@router.patch("/{id}")
async def update_qrcode(
    id: uuid.UUID,
    body: QRUpdate,
    ctx: WorkspaceCtx = Depends(require("qr:update")),
) -> dict[str, Any]:
    row = await ctx.conn.fetchrow(
        """SELECT q.id, q.default_url, d.hostname, q.slug, q.version
           FROM qrcodes q
           JOIN domains d ON d.id = q.domain_id
           WHERE q.id = $1 AND q.workspace_id = $2 AND q.deleted_at IS NULL""",
        id,
        ctx.workspace_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="QR Code não encontrado")

    old_url = row["default_url"]
    hostname = row["hostname"]
    slug = row["slug"]

    updates = []
    params: list[Any] = [id, ctx.workspace_id]

    if body.name is not None:
        params.append(body.name)
        updates.append(f"name = ${len(params)}")

    if body.default_url is not None:
        params.append(body.default_url)
        updates.append(f"default_url = ${len(params)}")
        if body.default_url != old_url:
            await ctx.conn.execute(
                """INSERT INTO qr_destination_history (qrcode_id, old_url, new_url, changed_by)
                   VALUES ($1, $2, $3, $4)""",
                id,
                old_url,
                body.default_url,
                ctx.user.id,
            )

    if body.fallback_url is not None:
        params.append(body.fallback_url)
        updates.append(f"fallback_url = ${len(params)}")

    if body.status is not None:
        params.append(body.status)
        updates.append(f"status = ${len(params)}")

    if body.folder_id is not None:
        params.append(body.folder_id)
        updates.append(f"folder_id = ${len(params)}")

    if body.utm is not None:
        params.append(json.dumps(body.utm))
        updates.append(f"utm_params = ${len(params)}::jsonb")

    if body.expires_at is not None:
        params.append(body.expires_at)
        updates.append(f"expires_at = ${len(params)}")

    if body.design is not None:
        params.append(json.dumps(body.design.model_dump()))
        updates.append(f"design = ${len(params)}::jsonb")

    if updates:
        updates.append("version = version + 1")
        updates.append("updated_at = now()")
        set_sql = ", ".join(updates)
        await ctx.conn.execute(
            f"UPDATE qrcodes SET {set_sql} WHERE id = $1 AND workspace_id = $2",
            *params,
        )

    # Substituição de regras se fornecidas
    if body.rules is not None:
        await ctx.conn.execute("DELETE FROM qr_rules WHERE qrcode_id = $1", id)
        for rule in body.rules:
            deep_json = json.dumps(rule.deep_link) if rule.deep_link else None
            await ctx.conn.execute(
                """INSERT INTO qr_rules (qrcode_id, priority, starts_at, ends_at, os, countries, target_url, deep_link)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)""",
                id,
                rule.priority,
                rule.starts_at,
                rule.ends_at,
                rule.os,
                rule.countries,
                rule.target_url,
                deep_json,
            )

    # Atualização de tags se fornecidas
    if body.tags is not None:
        await ctx.conn.execute("DELETE FROM qrcode_tags WHERE qrcode_id = $1", id)
        for tag_name in set(t.strip() for t in body.tags if t.strip()):
            tag_id = await ctx.conn.fetchval(
                """INSERT INTO tags (workspace_id, name)
                   VALUES ($1, $2)
                   ON CONFLICT (workspace_id, name) DO UPDATE SET name = EXCLUDED.name
                   RETURNING id""",
                ctx.workspace_id,
                tag_name,
            )
            await ctx.conn.execute(
                "INSERT INTO qrcode_tags (qrcode_id, tag_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                id,
                tag_id,
            )

    # Outbox para propagar atualização de cache e invalidar L1
    await ctx.conn.execute(
        """INSERT INTO outbox_events (event_type, payload)
           VALUES ('qr.updated', $1::jsonb)""",
        json.dumps({"qr_id": str(id), "host": hostname, "slug": slug}),
    )

    await audit(ctx, "qr.update", "qrcode", id, json.dumps({"diff": body.model_dump(exclude_unset=True)}))

    updated_row = await ctx.conn.fetchrow(QR_DETAIL_SQL, id, ctx.workspace_id)
    return _format_qr_row(updated_row)


@router.delete("/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_qrcode(
    id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require("qr:delete")),
) -> Response:
    row = await ctx.conn.fetchrow(
        """SELECT q.id, d.hostname, q.slug
           FROM qrcodes q
           JOIN domains d ON d.id = q.domain_id
           WHERE q.id = $1 AND q.workspace_id = $2 AND q.deleted_at IS NULL""",
        id,
        ctx.workspace_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="QR Code não encontrado")

    await ctx.conn.execute(
        "UPDATE qrcodes SET deleted_at = now() WHERE id = $1 AND workspace_id = $2",
        id,
        ctx.workspace_id,
    )

    await ctx.conn.execute(
        """INSERT INTO outbox_events (event_type, payload)
           VALUES ('qr.deleted', $1::jsonb)""",
        json.dumps({"qr_id": str(id), "host": row["hostname"], "slug": row["slug"]}),
    )

    await audit(ctx, "qr.delete", "qrcode", id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{id}/export")
async def export_qrcode(
    id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require("qr:read")),
    format: str = Query("svg", pattern=r"^(svg|png|pdf)$"),
    size: int = Query(1024, ge=128, le=4096),
    verify: bool = Query(False, description="Verificar leitura com decodificador real"),
) -> Response:
    row = await ctx.conn.fetchrow(
        """SELECT q.id, q.slug, d.hostname, q.design
           FROM qrcodes q
           JOIN domains d ON d.id = q.domain_id
           WHERE q.id = $1 AND q.workspace_id = $2 AND q.deleted_at IS NULL""",
        id,
        ctx.workspace_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="QR Code não encontrado")

    short_url = format_short_url(row["hostname"], row["slug"])
    design_data = json.loads(row["design"]) if isinstance(row["design"], str) else (row["design"] or {})

    logo_bytes = None
    if design_data.get("logo_base64"):
        try:
            b64 = design_data["logo_base64"].split(",")[-1]
            logo_bytes = base64.b64decode(b64)
        except Exception:
            pass

    design = QRDesign(
        foreground=design_data.get("foreground", "#000000"),
        background=design_data.get("background", "#FFFFFF"),
        module_style=design_data.get("module_style", "square"),
        finder_style=design_data.get("finder_style", "square"),
        frame_text=design_data.get("frame_text"),
        frame_color=design_data.get("frame_color"),
        frame_text_color=design_data.get("frame_text_color", "#FFFFFF"),
        logo=logo_bytes,
        logo_scale=float(design_data.get("logo_scale", 0.2)),
        error_correction=design_data.get("error_correction"),
    )

    try:
        content = render(short_url, design=design, fmt=format, size_px=size)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Erro ao renderizar QR Code: {exc}") from exc

    if verify and format == "png":
        if not verify_readable(content, short_url):
            raise HTTPException(
                status_code=422,
                detail="QR Code falhou na verificação de legibilidade (contraste insuficiente ou logo excessivo)",
            )

    media_types = {
        "svg": "image/svg+xml",
        "png": "image/png",
        "pdf": "application/pdf",
    }
    filename = f"qr-{row['slug']}.{format}"
    return Response(
        content=content,
        media_type=media_types[format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
