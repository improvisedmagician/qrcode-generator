"""Endpoints de métricas e analytics detalhados com consultas no ClickHouse."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from ..deps import WorkspaceCtx, require

router = APIRouter(prefix="/workspaces/{workspace_id}/qrcodes/{id}/analytics", tags=["analytics"])


def get_ch_client(request: Request):
    return getattr(request.app.state, "clickhouse", None)


@router.get("/summary")
async def get_analytics_summary(
    id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require("analytics:read")),
    days: int = Query(30, ge=1, le=365),
    request: Request = None,
) -> dict[str, Any]:
    ch = get_ch_client(request)
    if ch is None:
        return {
            "total_scans": 0,
            "unique_scans": 0,
            "bot_scans": 0,
            "period_days": days,
        }

    try:
        query = """
        SELECT
            count() AS total_scans,
            uniq(visitor_hash) AS unique_scans,
            countIf(is_bot = 1) AS bot_scans
        FROM analytics.scans
        WHERE workspace_id = %(ws)s
          AND qr_id = %(qr)s
          AND ts >= now() - INTERVAL %(days)s DAY
        """
        result = ch.query(query, parameters={"ws": str(ctx.workspace_id), "qr": str(id), "days": days})
        if result.result_rows:
            r = result.result_rows[0]
            return {
                "total_scans": r[0],
                "unique_scans": r[1],
                "bot_scans": r[2],
                "period_days": days,
            }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao consultar analytics: {exc}") from exc

    return {"total_scans": 0, "unique_scans": 0, "bot_scans": 0, "period_days": days}


@router.get("/timeseries")
async def get_analytics_timeseries(
    id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require("analytics:read")),
    days: int = Query(30, ge=1, le=90),
    request: Request = None,
) -> list[dict[str, Any]]:
    ch = get_ch_client(request)
    if ch is None:
        return []

    try:
        query = """
        SELECT
            toDate(ts) AS day,
            count() AS scans,
            uniq(visitor_hash) AS unique_scans
        FROM analytics.scans
        WHERE workspace_id = %(ws)s
          AND qr_id = %(qr)s
          AND is_bot = 0
          AND ts >= now() - INTERVAL %(days)s DAY
        GROUP BY day
        ORDER BY day ASC
        """
        result = ch.query(query, parameters={"ws": str(ctx.workspace_id), "qr": str(id), "days": days})
        return [
            {"date": row[0].isoformat() if hasattr(row[0], "isoformat") else str(row[0]),
             "scans": row[1],
             "unique_scans": row[2]}
            for row in result.result_rows
        ]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao consultar timeseries: {exc}") from exc


@router.get("/breakdown")
async def get_analytics_breakdown(
    id: uuid.UUID,
    ctx: WorkspaceCtx = Depends(require("analytics:read")),
    dimension: str = Query("country", pattern=r"^(country|device_type|os|browser)$"),
    days: int = Query(30, ge=1, le=90),
    request: Request = None,
) -> list[dict[str, Any]]:
    ch = get_ch_client(request)
    if ch is None:
        return []

    # Whitelist estrita de dimensões contra SQL injection
    col_map = {
        "country": "country",
        "device_type": "device_type",
        "os": "os",
        "browser": "browser",
    }
    col = col_map[dimension]

    try:
        query = f"""
        SELECT
            if({col} = '', 'Desconhecido', {col}) AS item,
            count() AS count
        FROM analytics.scans
        WHERE workspace_id = %(ws)s
          AND qr_id = %(qr)s
          AND is_bot = 0
          AND ts >= now() - INTERVAL %(days)s DAY
        GROUP BY item
        ORDER BY count DESC
        LIMIT 20
        """
        result = ch.query(query, parameters={"ws": str(ctx.workspace_id), "qr": str(id), "days": days})
        return [{"name": row[0], "count": row[1]} for row in result.result_rows]
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao consultar breakdown: {exc}") from exc
