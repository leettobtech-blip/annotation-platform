import hmac
import os
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

bearer = HTTPBearer(auto_error=False)
demo_token = os.getenv("DEMO_TOKEN")
issuer = os.getenv("OIDC_ISSUER")
audience = os.getenv("OIDC_AUDIENCE")
jwks_url = os.getenv("OIDC_JWKS_URL")
jwks = jwt.PyJWKClient(jwks_url, timeout=3) if jwks_url else None


class Principal(BaseModel):
    tenant: UUID
    subject: str
    scopes: set[str]


def check_config():
    if demo_token:
        if any((issuer, audience, jwks_url)):
            raise RuntimeError("Configure exactly one authentication mode")
    elif not (issuer and audience and jwks_url and jwks_url.startswith("https://")):
        raise RuntimeError("Configure OIDC_ISSUER, OIDC_AUDIENCE and HTTPS OIDC_JWKS_URL")


def authenticate(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials and len(credentials.credentials) <= 8192:
        token = credentials.credentials
        if demo_token and hmac.compare_digest(token.encode(), demo_token.encode()):
            return Principal(tenant=UUID(int=1), subject="local-demo", scopes={"annotation:read", "annotation:write"})
        if jwks and not demo_token:
            try:
                key = jwks.get_signing_key_from_jwt(token).key
                claims = jwt.decode(token, key, algorithms=["RS256"], issuer=issuer, audience=audience,
                                    options={"require": ["exp", "sub", "tenant_id"]})
                return Principal(tenant=claims["tenant_id"], subject=claims["sub"],
                                 scopes=set(claims.get("scope", "").split()))
            except (jwt.PyJWTError, ValueError, AttributeError):
                pass
    raise HTTPException(401, "Invalid bearer token", headers={"WWW-Authenticate": "Bearer"})


def permission(scope):
    def require(principal=Depends(authenticate)):
        if scope not in principal.scopes:
            raise HTTPException(403, "Missing permission")
        return principal
    return require
