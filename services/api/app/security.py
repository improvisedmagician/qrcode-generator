"""Senhas (argon2id) e tokens JWT de curta duração.

SSO: o login por IdP (SAML/OIDC via WorkOS/Keycloak) só precisa resolver o usuário e chamar
`issue_tokens()` — o restante da API não muda.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from .config import settings

_hasher = PasswordHasher()
# Hash fixo para comparação quando o usuário não existe (evita timing attack / enumeração)
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except VerifyMismatchError:
        return False
    except Exception:
        return False


@dataclass(frozen=True)
class TokenUser:
    id: uuid.UUID
    org_id: uuid.UUID
    email: str


def _encode(user: TokenUser, kind: str, ttl: timedelta) -> str:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(user.id), "org": str(user.org_id), "email": user.email, "typ": kind,
        "iss": settings.jwt_issuer, "iat": now, "exp": now + ttl, "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def issue_tokens(user: TokenUser) -> dict:
    return {
        "access_token": _encode(user, "access", timedelta(minutes=settings.access_ttl_minutes)),
        "refresh_token": _encode(user, "refresh", timedelta(days=settings.refresh_ttl_days)),
        "token_type": "bearer",
        "expires_in": settings.access_ttl_minutes * 60,
    }


def decode_token(token: str, kind: str = "access") -> TokenUser:
    claims = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"],
                        issuer=settings.jwt_issuer, options={"require": ["exp", "sub", "typ"]})
    if claims["typ"] != kind:
        raise jwt.InvalidTokenError("wrong token type")
    return TokenUser(uuid.UUID(claims["sub"]), uuid.UUID(claims["org"]), claims["email"])
