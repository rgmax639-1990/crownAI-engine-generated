from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from db import get_conn, get_user
from security import InvalidTokenError, decode_access_token

bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _user_from_token(token: str) -> dict:
    try:
        payload = decode_access_token(token)
    except InvalidTokenError:
        raise _unauthorized("Invalid or expired access token.")
    with get_conn() as conn:
        user = get_user(conn, payload.get("sub", ""))
        if not user:
            raise _unauthorized("User associated with this token no longer exists.")
        return dict(user)


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> dict:
    if credentials is None or not credentials.credentials:
        raise _unauthorized("Missing bearer token. Sign in with Google or Microsoft first.")
    return _user_from_token(credentials.credentials)


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[dict]:
    """For endpoints that also serve anonymous visitors: no token -> None, but
    a token that is present and invalid is still rejected with 401 (never
    silently treated as anonymous)."""
    if credentials is None or not credentials.credentials:
        return None
    return _user_from_token(credentials.credentials)
