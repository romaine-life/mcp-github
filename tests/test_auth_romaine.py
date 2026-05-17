"""auth.romaine.life inbound JWT authenticator + installation resolver."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.auth_romaine import (  # noqa: E402
    AuthRomaineLifeAuthenticator,
)
from mcp_github.caller import CallerAuthError  # noqa: E402


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


def _service_jwt(private_key, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://auth.romaine.life",
        "aud": "https://auth.romaine.life",
        "sub": "svc:tank:session-37",
        "email": "pod-session-37@service.tank.romaine.life",
        "name": "Service: tank pod-session-37",
        "role": "service",
        "actor_email": "Owner@example.com",
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test"})


def _stub_installation_endpoint(
    *, installation_id: int | None = None, is_host: bool = False, is_super_admin: bool = False
) -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps({
                "email": "owner@example.com",
                "installation_id": installation_id,
                "is_host": is_host,
                "is_super_admin": is_super_admin,
            }),
            headers={"content-type": "application/json"},
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def _authenticator(private_key, http_client) -> AuthRomaineLifeAuthenticator:
    return AuthRomaineLifeAuthenticator(
        issuer="https://auth.romaine.life",
        jwks_url="https://auth.romaine.life/api/auth/jwks",
        installation_url="http://tank-operator.svc/api/internal/github/installation",
        jwks_client=_JWKSClient(private_key.public_key()),
        http_client=http_client,
    )


def test_authenticate_resolves_installation_for_user(private_key):
    http = _stub_installation_endpoint(installation_id=424242)
    a = _authenticator(private_key, http)
    caller = a.authenticate(f"Bearer {_service_jwt(private_key)}")

    assert caller.email == "owner@example.com"  # lowercased
    assert caller.installation_id == 424242
    assert caller.is_host is False
    assert caller.is_super_admin is False
    assert caller.session_id == "session-37"
    assert caller.session_scope == "tank"


def test_authenticate_handles_host_with_no_installation(private_key):
    http = _stub_installation_endpoint(installation_id=None, is_host=True, is_super_admin=True)
    a = _authenticator(private_key, http)
    caller = a.authenticate(f"Bearer {_service_jwt(private_key, actor_email='host@romaine.life')}")

    assert caller.is_host is True
    assert caller.is_super_admin is True
    assert caller.installation_id is None
    assert caller.email == "host@romaine.life"


def test_authenticate_rejects_non_host_with_no_installation(private_key):
    http = _stub_installation_endpoint(installation_id=None, is_host=False)
    a = _authenticator(private_key, http)
    with pytest.raises(CallerAuthError, match="no GitHub App installation"):
        a.authenticate(f"Bearer {_service_jwt(private_key)}")


def test_authenticate_rejects_non_service_role(private_key):
    http = _stub_installation_endpoint(installation_id=1)
    a = _authenticator(private_key, http)
    token = _service_jwt(private_key, role="admin")
    with pytest.raises(CallerAuthError, match="requires role=service"):
        a.authenticate(f"Bearer {token}")


def test_authenticate_rejects_service_without_actor_email(private_key):
    http = _stub_installation_endpoint(installation_id=1)
    a = _authenticator(private_key, http)
    token = _service_jwt(private_key, actor_email="")
    with pytest.raises(CallerAuthError, match="actor_email"):
        a.authenticate(f"Bearer {token}")


def test_authenticate_rejects_wrong_issuer(private_key):
    http = _stub_installation_endpoint(installation_id=1)
    a = _authenticator(private_key, http)
    token = _service_jwt(private_key, iss="https://impostor.example")
    with pytest.raises(CallerAuthError, match="invalid auth.romaine.life JWT"):
        a.authenticate(f"Bearer {token}")


def test_authenticate_rejects_expired(private_key):
    http = _stub_installation_endpoint(installation_id=1)
    a = _authenticator(private_key, http)
    token = _service_jwt(private_key, exp=int(time.time()) - 120)
    with pytest.raises(CallerAuthError):
        a.authenticate(f"Bearer {token}")


def test_authenticate_rejects_wrong_signature(private_key):
    http = _stub_installation_endpoint(installation_id=1)
    a = _authenticator(private_key, http)
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "iss": "https://auth.romaine.life",
            "sub": "svc:tank:x",
            "role": "service",
            "actor_email": "owner@example.com",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
        attacker,
        algorithm="RS256",
        headers={"kid": "test"},
    )
    with pytest.raises(CallerAuthError):
        a.authenticate(f"Bearer {token}")


def test_authenticate_surfaces_installation_endpoint_error(private_key):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    a = _authenticator(private_key, http)
    with pytest.raises(CallerAuthError, match="installation lookup returned 500"):
        a.authenticate(f"Bearer {_service_jwt(private_key)}")


def test_installation_lookup_caches_per_email(private_key):
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            content=json.dumps({
                "email": "owner@example.com",
                "installation_id": 9,
                "is_host": False,
                "is_super_admin": False,
            }),
            headers={"content-type": "application/json"},
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    a = _authenticator(private_key, http)
    a.authenticate(f"Bearer {_service_jwt(private_key)}")
    a.authenticate(f"Bearer {_service_jwt(private_key)}")
    a.authenticate(f"Bearer {_service_jwt(private_key)}")
    assert calls["n"] == 1


