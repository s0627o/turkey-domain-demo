from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


class AuthError(RuntimeError):
    pass


class OidcProvider(Protocol):
    def authorization_url(self, state: str, nonce: str) -> str: ...

    def exchange_and_verify(
        self,
        code: str,
        nonce: str,
        issuer: str,
        audience: str,
        now: datetime,
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class LoginRequest:
    state: str
    nonce: str
    authorization_url: str


@dataclass(frozen=True)
class CreatedSession:
    email: str
    cookie_token: str
    csrf_token: str


@dataclass(frozen=True)
class WebSession:
    email: str
    token_hash: str
    csrf_hash: str


class SessionStore:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        idle_ttl: timedelta,
        absolute_ttl: timedelta,
    ) -> None:
        self.connection = connection
        self.idle_ttl = idle_ttl
        self.absolute_ttl = absolute_ttl

    def create(self, email: str, now: datetime) -> CreatedSession:
        cookie_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        self.connection.execute(
            """
            INSERT INTO web_sessions(
                token_hash, email, csrf_hash, created_at, last_seen_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _hash(cookie_token),
                email.strip().lower(),
                _hash(csrf_token),
                _iso(now),
                _iso(now),
                _iso(now + self.absolute_ttl),
            ),
        )
        return CreatedSession(email.strip().lower(), cookie_token, csrf_token)

    def authenticate(self, cookie_token: str | None, now: datetime) -> WebSession | None:
        if not cookie_token:
            return None
        token_hash = _hash(cookie_token)
        row = self.connection.execute(
            "SELECT * FROM web_sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        if now >= _datetime(row["expires_at"]):
            return None
        if now - _datetime(row["last_seen_at"]) >= self.idle_ttl:
            return None
        self.connection.execute(
            "UPDATE web_sessions SET last_seen_at = ? WHERE token_hash = ?",
            (_iso(now), token_hash),
        )
        return WebSession(row["email"], token_hash, row["csrf_hash"])

    def validate_csrf(self, session: WebSession, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(session.csrf_hash, _hash(candidate))

    def revoke(self, session: WebSession, now: datetime) -> None:
        self.connection.execute(
            "UPDATE web_sessions SET revoked_at = ? WHERE token_hash = ?",
            (_iso(now), session.token_hash),
        )


class AuthService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        provider: OidcProvider,
        sessions: SessionStore,
        *,
        allowed_emails: frozenset[str],
        issuer: str,
        audience: str,
        transaction_ttl: timedelta,
    ) -> None:
        self.connection = connection
        self.provider = provider
        self.sessions = sessions
        self.allowed_emails = frozenset(email.strip().lower() for email in allowed_emails)
        self.issuer = issuer
        self.audience = audience
        self.transaction_ttl = transaction_ttl

    def with_connection(self, connection: sqlite3.Connection) -> AuthService:
        sessions = SessionStore(
            connection,
            idle_ttl=self.sessions.idle_ttl,
            absolute_ttl=self.sessions.absolute_ttl,
        )
        return AuthService(
            connection,
            self.provider,
            sessions,
            allowed_emails=self.allowed_emails,
            issuer=self.issuer,
            audience=self.audience,
            transaction_ttl=self.transaction_ttl,
        )

    def begin_login(self, now: datetime) -> LoginRequest:
        state_secret = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        state = f"{state_secret}.{nonce}"
        self.connection.execute(
            """
            INSERT INTO oauth_transactions(state_hash, nonce_hash, created_at, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (_hash(state), _hash(nonce), _iso(now), _iso(now + self.transaction_ttl)),
        )
        return LoginRequest(state, nonce, self.provider.authorization_url(state, nonce))

    def finish_login(self, state: str, code: str, now: datetime) -> CreatedSession:
        nonce = state.rpartition(".")[2]
        if not nonce:
            raise AuthError("OAuth state nonce is invalid")
        claimed = self.connection.execute(
            """
            UPDATE oauth_transactions
            SET consumed_at = ?
            WHERE state_hash = ?
              AND nonce_hash = ?
              AND consumed_at IS NULL
              AND expires_at > ?
            """,
            (_iso(now), _hash(state), _hash(nonce), _iso(now)),
        )
        if claimed.rowcount != 1:
            raise AuthError("OAuth state is invalid or expired")
        claims = self.provider.exchange_and_verify(code, nonce, self.issuer, self.audience, now)
        email = str(claims.get("email", "")).strip().lower()
        if claims.get("email_verified") is not True or email not in self.allowed_emails:
            raise AuthError("Google account is not authorized")
        return self.sessions.create(email, now)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.isoformat()


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)
