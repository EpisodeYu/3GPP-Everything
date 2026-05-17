"""core/auth.py 单测：密码哈希、JWT 签发/解码、token 类型校验。

不连 DB / Redis。
"""

from __future__ import annotations

import time
import uuid

import pytest
from jose import JWTError, jwt

from app.core.auth import (
    JWT_ALGORITHM,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    hash_refresh_token,
    require_token_type,
    verify_password,
)
from app.core.config import Settings
from app.core.errors import UnauthorizedError


def _settings() -> Settings:
    return Settings(APP_SECRET_KEY="x" * 32, ACCESS_TOKEN_EXPIRE_MINUTES=15)


def test_hash_and_verify_password_roundtrip() -> None:
    h = hash_password("hunter2hunter2")
    assert h != "hunter2hunter2"
    assert verify_password("hunter2hunter2", h)
    assert not verify_password("wrong", h)


def test_verify_password_on_corrupt_hash_is_false() -> None:
    # 不抛异常
    assert verify_password("anything", "not-a-bcrypt-hash") is False


def test_create_access_token_payload() -> None:
    s = _settings()
    uid = uuid.uuid4()
    tok = create_access_token(user_id=uid, role="admin", settings=s)
    payload = jwt.decode(tok, s.APP_SECRET_KEY.get_secret_value(), algorithms=[JWT_ALGORITHM])
    assert payload["sub"] == str(uid)
    assert payload["role"] == "admin"
    assert payload["type"] == "access"
    assert payload["exp"] - payload["iat"] == 15 * 60


def test_create_refresh_token_returns_hash_and_exp() -> None:
    s = _settings()
    uid = uuid.uuid4()
    tok, tok_hash, exp = create_refresh_token(user_id=uid, role="user", settings=s)
    assert hash_refresh_token(tok) == tok_hash
    assert exp.timestamp() > time.time()
    # 解码内容
    payload = jwt.decode(tok, s.APP_SECRET_KEY.get_secret_value(), algorithms=[JWT_ALGORITHM])
    assert payload["type"] == "refresh"
    assert payload["sub"] == str(uid)


def test_decode_token_invalid_raises_unauthorized() -> None:
    s = _settings()
    with pytest.raises(UnauthorizedError):
        decode_token("not.a.jwt", settings=s)


def test_decode_token_wrong_secret_raises() -> None:
    s1 = Settings(APP_SECRET_KEY="k1" * 16)
    s2 = Settings(APP_SECRET_KEY="k2" * 16)
    tok = create_access_token(user_id=uuid.uuid4(), role="user", settings=s1)
    with pytest.raises(UnauthorizedError):
        decode_token(tok, settings=s2)


def test_require_token_type_mismatch_raises() -> None:
    with pytest.raises(UnauthorizedError):
        require_token_type({"type": "refresh"}, "access")
    # 正确通过
    require_token_type({"type": "access"}, "access")


def test_missing_secret_raises_500() -> None:
    s = Settings(APP_SECRET_KEY="")
    with pytest.raises(UnauthorizedError) as ei:
        create_access_token(user_id=uuid.uuid4(), role="user", settings=s)
    assert ei.value.status_code == 500


def test_jwt_round_trip_via_decode_token() -> None:
    s = _settings()
    uid = uuid.uuid4()
    tok = create_access_token(user_id=uid, role="user", settings=s)
    payload = decode_token(tok, settings=s)
    assert payload["sub"] == str(uid)


def test_expired_token_raises() -> None:
    s = _settings()
    # 构造一个 exp 在过去的 token
    payload = {
        "sub": str(uuid.uuid4()),
        "type": "access",
        "role": "user",
        "iat": int(time.time()) - 1000,
        "exp": int(time.time()) - 10,
    }
    tok = jwt.encode(payload, s.APP_SECRET_KEY.get_secret_value(), algorithm=JWT_ALGORITHM)
    with pytest.raises((UnauthorizedError, JWTError)):
        decode_token(tok, settings=s)
