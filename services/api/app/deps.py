"""Dependências FastAPI: autenticação, RBAC por workspace e contexto transacional com RLS."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Callable

import asyncpg
import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .security import TokenUser, decode_token

bearer = HTTPBearer(auto_error=False)


def has_permission(granted: list[str] | None, needed: str) -> bool:
    """'*' concede tudo; 'qr:*' concede todas as ações do recurso 'qr'."""
    if not granted:
        return False
    resource = needed.split(":", 1)[0]
    return "*" in granted or needed in granted or f"{resource}:*" in granted


def get_pool(request: Request) -> asyncpg.Pool:
    return request.app.state.pool


async def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> TokenUser:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing token",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        return decode_token(creds.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token",
                            headers={"WWW-Authenticate": "Bearer"}) from None


@dataclass
class WorkspaceCtx:
    conn: asyncpg.Connection
    user: TokenUser
    workspace_id: uuid.UUID
    permissions: list[str]


MEMBERSHIP_SQL = """
SELECT r.permissions
FROM workspace_members m
JOIN roles r ON r.id = m.role_id
JOIN workspaces w ON w.id = m.workspace_id
WHERE m.workspace_id = $1 AND m.user_id = $2 AND w.organization_id = $3
"""


def require(permission: str) -> Callable[..., AsyncIterator[WorkspaceCtx]]:
    """Abre UMA transação por request, valida a permissão e ativa o RLS do workspace."""

    async def dependency(
        workspace_id: uuid.UUID,
        user: TokenUser = Depends(current_user),
        pool: asyncpg.Pool = Depends(get_pool),
    ) -> AsyncIterator[WorkspaceCtx]:
        async with pool.acquire() as conn:
            async with conn.transaction():
                perms = await conn.fetchval(MEMBERSHIP_SQL, workspace_id, user.id, user.org_id)
                if perms is None:   # 404 em vez de 403: não revela a existência do workspace
                    raise HTTPException(status.HTTP_404_NOT_FOUND, "workspace not found")
                if not has_permission(perms, permission):
                    raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission {permission}")
                await conn.execute("SELECT set_config('app.workspace_id', $1, true)", str(workspace_id))
                yield WorkspaceCtx(conn, user, workspace_id, list(perms))

    return dependency


async def audit(ctx: WorkspaceCtx, action: str, entity_type: str, entity_id: uuid.UUID | None,
                diff: str | None = None, ip: str | None = None) -> None:
    await ctx.conn.execute(
        """INSERT INTO audit_logs (organization_id, actor_id, action, entity_type, entity_id, diff, ip)
           VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::inet)""",
        ctx.user.org_id, ctx.user.id, action, entity_type, entity_id, diff, ip)
