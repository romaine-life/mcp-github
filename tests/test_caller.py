"""Tank attestation caller authentication."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.caller import (  # noqa: E402
    CallerAuthError,
    CallerIdentity,
    TankJWTAuthenticator,
)


class _SigningKey:
    def __init__(self, key) -> None:
        self.key = key


class _JWKSClient:
    def __init__(self, public_key) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, _token: str) -> _SigningKey:
        return _SigningKey(self._public_key)


@pytest.fixture
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def authenticator(private_key) -> TankJWTAuthenticator:
    return TankJWTAuthenticator(
        jwks_client=_JWKSClient(private_key.public_key()),
        issuer="tank-operator",
        audience="mcp-github-tank",
    )


def _token(private_key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "tank-operator",
        "aud": "mcp-github-tank",
        "sub": "tank-session:default:12",
        "iat": now,
        "nbf": now - 1,
        "exp": now + 300,
        "owner_email": "Alice@Example.test",
        "github_installation_id": 123,
        "is_host": False,
        "is_super_admin": False,
        "session_scope": "default",
        "session_id": "12",
        "pod_name": "session-12",
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


def test_authenticator_returns_identity(authenticator, private_key) -> None:
    caller = authenticator.authenticate(f"Bearer {_token(private_key)}")

    assert caller == CallerIdentity(
        email="alice@example.test",
        installation_id=123,
        is_host=False,
        is_super_admin=False,
        session_scope="default",
        session_id="12",
        pod_name="session-12",
    )


def test_host_attestation_may_omit_installation(authenticator, private_key) -> None:
    token = _token(
        private_key,
        owner_email="host@example.test",
        is_host=True,
        github_installation_id=None,
    )

    caller = authenticator.authenticate(f"Bearer {token}")

    assert caller.email == "host@example.test"
    assert caller.installation_id is None
    assert caller.is_host is True


def test_missing_bearer_rejected(authenticator) -> None:
    with pytest.raises(CallerAuthError, match="missing Authorization"):
        authenticator.authenticate(None)


def test_wrong_audience_rejected(authenticator, private_key) -> None:
    token = _token(private_key, aud="other-service")

    with pytest.raises(CallerAuthError, match="invalid Tank session attestation"):
        authenticator.authenticate(f"Bearer {token}")


def test_wrong_issuer_rejected(authenticator, private_key) -> None:
    token = _token(private_key, iss="other-issuer")

    with pytest.raises(CallerAuthError, match="invalid Tank session attestation"):
        authenticator.authenticate(f"Bearer {token}")


def test_expired_token_rejected(authenticator, private_key) -> None:
    token = _token(private_key, exp=int(time.time()) - 60)

    with pytest.raises(CallerAuthError, match="invalid Tank session attestation"):
        authenticator.authenticate(f"Bearer {token}")


def test_non_host_requires_installation(authenticator, private_key) -> None:
    token = _token(private_key, github_installation_id=None)

    with pytest.raises(CallerAuthError, match="missing GitHub installation"):
        authenticator.authenticate(f"Bearer {token}")


def test_session_claims_are_required(authenticator, private_key) -> None:
    token = _token(private_key, session_id="")

    with pytest.raises(CallerAuthError, match="missing session_id"):
        authenticator.authenticate(f"Bearer {token}")
