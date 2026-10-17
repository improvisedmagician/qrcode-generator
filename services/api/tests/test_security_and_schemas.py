import uuid
from datetime import datetime, timezone, timedelta
import pytest
from pydantic import ValidationError

from app.security import (
    TokenUser,
    hash_password,
    verify_password,
    issue_tokens,
    decode_token,
)
from app.schemas import validate_destination, Rule, QRCreate


def test_password_hashing():
    pwd = "minha-senha-segura-123"
    hashed = hash_password(pwd)
    assert hashed.startswith("$argon2id$")
    assert verify_password(pwd, hashed) is True
    assert verify_password("senha-errada", hashed) is False
    assert verify_password(pwd, None) is False


def test_jwt_issuance_and_decoding():
    uid = uuid.uuid4()
    org_id = uuid.uuid4()
    user = TokenUser(id=uid, org_id=org_id, email="dev@empresa.com")

    tokens = issue_tokens(user)
    assert "access_token" in tokens
    assert "refresh_token" in tokens

    decoded = decode_token(tokens["access_token"], kind="access")
    assert decoded.id == uid
    assert decoded.org_id == org_id
    assert decoded.email == "dev@empresa.com"

    # Não permite usar refresh_token como access_token
    with pytest.raises(Exception):
        decode_token(tokens["refresh_token"], kind="access")


def test_validate_destination():
    # Válidos
    assert validate_destination("https://site.com/promo") == "https://site.com/promo"
    assert validate_destination("http://intranet.local/x") == "http://intranet.local/x"

    # Inválidos: schemes proibidos (javascript, file, etc)
    with pytest.raises(ValueError):
        validate_destination("javascript:alert(1)")

    # Anti-looping: não permite apontar para a própria plataforma
    with pytest.raises(ValueError, match="não pode apontar para o domínio de redirecionamento"):
        validate_destination("https://qr.meudominio.com/infinito")


def test_schedule_window_validation():
    now = datetime.now(timezone.utc)
    # Válido: fim posterior ao início
    r = Rule(
        target_url="https://site.com",
        starts_at=now,
        ends_at=now + timedelta(days=2),
    )
    assert r.target_url == "https://site.com"

    # Inválido: fim anterior ou igual ao início
    with pytest.raises(ValidationError):
        Rule(
            target_url="https://site.com",
            starts_at=now + timedelta(days=5),
            ends_at=now,
        )
