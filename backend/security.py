"""JWT access-token issuing/verification for backend-managed sessions.

Users authenticate via Google/Microsoft OAuth (see oauth_providers.py); once
their identity is verified we mint our own short-lived bearer token so the
frontend never has to hold a Google/Microsoft token.
"""
import os
import time

import jwt

JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY") or "dev-only-insecure-secret-change-me"
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_SECONDS = 7 * 24 * 60 * 60  # 7 days


def create_access_token(user_id: str, email: str) -> str:
    payload = {
        "sub": user_id,
        "email": email,
        "iat": int(time.time()),
        "exp": int(time.time()) + JWT_EXPIRY_SECONDS,
    }
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


class InvalidTokenError(Exception):
    pass


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(str(exc)) from exc
