"""Request authentication and tenant resolution (SPEC-DECISIONS D6).

Tenant is taken from the `custom:tenant_id` JWT claim and is NEVER a request
parameter. A token without that claim is a 401.

  ENVIRONMENT=local       HS256, signed with DEV_JWT_SECRET.
                          Mint one with backend/scripts/mint_dev_token.py.
  ENVIRONMENT=production  RS256 Cognito token, verified against the user pool
                          JWKS. The JWKS client is cached in module scope.

The production path has NOT been exercised against a real Cognito pool in this
environment — see the final report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient

from shared import config
from shared.errors import AuthError
from shared.logging_utils import get_logger

logger = get_logger("geotag.auth")

TENANT_CLAIM = "custom:tenant_id"

_jwks_client: PyJWKClient | None = None


@dataclass(frozen=True)
class AuthContext:
    """Who is calling, and which tenant's data they may touch."""

    tenant_id: str
    subject: str


def _bearer_token(event: dict[str, Any]) -> str:
    headers = event.get("headers") or {}
    # API Gateway header casing is not guaranteed.
    value: str | None = None
    for key, raw in headers.items():
        if isinstance(key, str) and key.lower() == "authorization":
            value = raw
            break
    if not value:
        raise AuthError("missing Authorization header")
    parts = value.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise AuthError("Authorization header must be 'Bearer <jwt>'")
    return parts[1]


def _decode_local(token: str) -> dict[str, Any]:
    secret = config.env("DEV_JWT_SECRET")
    if not secret:
        # Refusing to run without a secret beats silently accepting any token.
        raise AuthError("DEV_JWT_SECRET is not configured")
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid token: {exc}") from exc


def _cognito_issuer() -> str:
    pool_id = config.env("COGNITO_USER_POOL_ID")
    if not pool_id:
        raise AuthError("COGNITO_USER_POOL_ID is not configured")
    return f"https://cognito-idp.{config.aws_region()}.amazonaws.com/{pool_id}"


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(f"{_cognito_issuer()}/.well-known/jwks.json", cache_keys=True)
    return _jwks_client


def _decode_cognito(token: str) -> dict[str, Any]:
    client_id = config.env("COGNITO_CLIENT_ID")
    if not client_id:
        raise AuthError("COGNITO_CLIENT_ID is not configured")
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            issuer=_cognito_issuer(),
            options={"require": ["exp", "iss"], "verify_aud": False},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"invalid token: {exc}") from exc

    # Cognito access tokens carry `client_id`; id tokens carry `aud`.
    audience = claims.get("aud") or claims.get("client_id")
    if audience != client_id:
        raise AuthError("token was not issued for this client")
    return claims


def decode_token(token: str) -> dict[str, Any]:
    """Verify a token's signature and standard claims for the current env."""
    if config.is_local():
        return _decode_local(token)
    return _decode_cognito(token)


def require_auth(event: dict[str, Any]) -> AuthContext:
    """Authenticate an API Gateway proxy event. Raises `AuthError` (-> 401)."""
    claims = decode_token(_bearer_token(event))

    tenant_id = claims.get(TENANT_CLAIM)
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise AuthError(f"token is missing the {TENANT_CLAIM} claim")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise AuthError("token is missing the sub claim")

    context = AuthContext(tenant_id=tenant_id.strip(), subject=subject)
    logger.append_keys(tenant_id=context.tenant_id)
    return context
