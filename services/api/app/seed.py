"""Seeder inicial para desenvolvimento e testes locais."""
from __future__ import annotations

import logging
import uuid
import asyncpg
from .security import hash_password

log = logging.getLogger("api.seed")

DEMO_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEMO_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
DEMO_WS_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


async def seed_demo_data(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        # Cria organização
        await conn.execute(
            """INSERT INTO organizations (id, name, plan)
               VALUES ($1, 'Empresa Demo Corp', 'enterprise')
               ON CONFLICT (id) DO NOTHING""",
            DEMO_ORG_ID,
        )

        # Cria usuário demo (admin@empresa.com / senha123)
        pwd = hash_password("senha123")
        await conn.execute(
            """INSERT INTO users (id, organization_id, email, name, password_hash)
               VALUES ($1, $2, 'admin@empresa.com', 'Admin Demo', $3)
               ON CONFLICT (email) DO NOTHING""",
            DEMO_USER_ID,
            DEMO_ORG_ID,
            pwd,
        )

        # Cria workspace demo
        await conn.execute(
            """INSERT INTO workspaces (id, organization_id, name, slug)
               VALUES ($1, $2, 'Marketing & Vendas', 'marketing')
               ON CONFLICT (id) DO NOTHING""",
            DEMO_WS_ID,
            DEMO_ORG_ID,
        )

        # Obtém role de 'owner'
        owner_role_id = await conn.fetchval(
            "SELECT id FROM roles WHERE organization_id IS NULL AND name = 'owner'",
        )

        if owner_role_id:
            await conn.execute(
                """INSERT INTO workspace_members (workspace_id, user_id, role_id)
                   VALUES ($1, $2, $3)
                   ON CONFLICT DO NOTHING""",
                DEMO_WS_ID,
                DEMO_USER_ID,
                owner_role_id,
            )

        log.info("Seed concluído: admin@empresa.com / senha123 [Workspace: %s]", DEMO_WS_ID)
