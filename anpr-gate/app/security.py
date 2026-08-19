from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any

import redis
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import HTTPException, Request, status


password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)


def validate_password(password: str) -> None:
    if len(password) < 14:
        raise ValueError("Password must be at least 14 characters")


def hash_password(password: str) -> str:
    validate_password(password)
    return password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


class SessionManager:
    def __init__(
        self,
        client: redis.Redis,
        cookie_name: str,
        ttl_seconds: int,
        secure_cookie: bool,
    ) -> None:
        self.redis = client
        self.cookie_name = cookie_name
        self.ttl_seconds = ttl_seconds
        self.secure_cookie = secure_cookie

    def create(self, user: dict[str, Any]) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        payload = json.dumps(
            {
                "user_id": user["id"],
                "username": user["username"],
                "csrf": csrf,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        self.redis.setex(f"session:{token}", self.ttl_seconds, payload)
        return token, csrf

    def get(self, request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(self.cookie_name)
        if not token:
            return None
        raw = self.redis.get(f"session:{token}")
        if not raw:
            return None
        self.redis.expire(f"session:{token}", self.ttl_seconds)
        return json.loads(raw)

    def destroy(self, request: Request) -> None:
        token = request.cookies.get(self.cookie_name)
        if token:
            self.redis.delete(f"session:{token}")

    def set_cookie(self, response, token: str) -> None:
        response.set_cookie(
            self.cookie_name,
            token,
            max_age=self.ttl_seconds,
            httponly=True,
            secure=self.secure_cookie,
            samesite="strict",
            path="/",
        )

    def clear_cookie(self, response) -> None:
        response.delete_cookie(self.cookie_name, path="/")

    def require(self, request: Request) -> dict[str, Any]:
        session = self.get(request)
        if session is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return session

    @staticmethod
    def require_csrf(session: dict[str, Any], supplied: str) -> None:
        if not supplied or not secrets.compare_digest(session["csrf"], supplied):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

    @staticmethod
    def _rate_key(username: str, client_ip: str) -> str:
        digest = hashlib.sha256(f"{username.lower()}|{client_ip}".encode()).hexdigest()
        return f"login-rate:{digest}"

    def login_rate_limited(self, username: str, client_ip: str) -> bool:
        value = self.redis.get(self._rate_key(username, client_ip))
        return bool(value and int(value) >= 5)

    def login_failed(self, username: str, client_ip: str) -> None:
        key = self._rate_key(username, client_ip)
        count = self.redis.incr(key)
        if count == 1:
            self.redis.expire(key, 900)

    def login_succeeded(self, username: str, client_ip: str) -> None:
        self.redis.delete(self._rate_key(username, client_ip))


def client_ip(request: Request) -> str:
    # The production service is reachable only through the colocated cloudflared
    # container. Direct host ports are not published.
    return request.headers.get("CF-Connecting-IP") or (
        request.client.host if request.client else "0.0.0.0"
    )
