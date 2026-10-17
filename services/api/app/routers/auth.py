"""Endpoints de autenticação e perfil do usuário."""
from __future__ import annotations

import json
from typing import Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import current_user, get_pool
from ..schemas import LoginIn, RefreshIn
from ..security import TokenUser, decode_token, issue_tokens, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

USER_LOOKUP_SQL = """
SELECT u.id, u.organization_id, u.email, u.name, u.password_hash, u.is_active
FROM users u
WHERE u.email = $1
"""

WORKSPACES_SQL = """
SELECT w.id, w.name, w.slug, r.name AS role_name, r.permissions
FROM workspace_members wm
JOIN workspaces w ON w.id = wm.workspace_id
JOIN roles r ON r.id = wm.role_id
WHERE wm.user_id = $1
ORDER BY w.name
"""


@router.post("/login")
async def login(body: LoginIn, pool: asyncpg.Pool = Depends(get_pool)) -> dict[str, Any]:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(USER_LOOKUP_SQL, body.email.strip().lower())
        if row is None or not row["is_active"]:
            verify_password(body.password, None)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credenciais inválidas",
            )

        if not verify_password(body.password, row["password_hash"]):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Credenciais inválidas",
            )

        await conn.execute("UPDATE users SET last_login_at = now() WHERE id = $1", row["id"])

        user = TokenUser(id=row["id"], org_id=row["organization_id"], email=row["email"])
        tokens = issue_tokens(user)
        return {
            **tokens,
            "user": {
                "id": str(row["id"]),
                "name": row["name"],
                "email": row["email"],
                "organization_id": str(row["organization_id"]),
            },
        }


@router.post("/refresh")
async def refresh(body: RefreshIn, pool: asyncpg.Pool = Depends(get_pool)) -> dict[str, Any]:
    try:
        user = decode_token(body.refresh_token, kind="refresh")
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token inválido ou expirado",
        ) from None

    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id, is_active FROM users WHERE id = $1", user.id)
        if row is None or not row["is_active"]:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário inativo")

    return issue_tokens(user)


@router.get("/me")
async def me(
    user: TokenUser = Depends(current_user),
    pool: asyncpg.Pool = Depends(get_pool),
) -> dict[str, Any]:
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            """SELECT u.id, u.email, u.name, o.name AS organization_name, o.plan
               FROM users u
               JOIN organizations o ON o.id = u.organization_id
               WHERE u.id = $1""",
            user.id,
        )
        if user_row is None:
            raise HTTPException(status_code=404, detail="Usuário não encontrado")

        ws_rows = await conn.fetch(WORKSPACES_SQL, user.id)
        workspaces = [
            {
                "id": str(w["id"]),
                "name": w["name"],
                "slug": w["slug"],
                "role": w["role_name"],
                "permissions": list(w["permissions"]),
            }
            for w in ws_rows
        ]

        return {
            "id": str(user_row["id"]),
            "email": user_row["email"],
            "name": user_row["name"],
            "organization": {
                "id": str(user.org_id),
                "name": user_row["organization_name"],
                "plan": user_row["plan"],
            },
            "workspaces": workspaces,
        }
